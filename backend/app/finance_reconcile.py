"""Monthly three-way reconciliation: bank statement, allocations/journals and invoices.

Read-only. Never books income from bank deposits or assumes an invoice is a
payment. Every amount is calculated as Decimal in the source currency.
"""
from __future__ import annotations

from collections import defaultdict
import invoice_red
from datetime import datetime
from decimal import Decimal

ZERO=Decimal("0")
CENT=Decimal("0.01")


def _amount(value:object)->Decimal:
    out=Decimal(str(value if value is not None else "0"))
    if not out.is_finite():
        raise ValueError("non-finite financial amount")
    return out


def _money(value:Decimal)->str:
    return str(value.quantize(CENT))


def _period(period:str)->str:
    try:
        if len(period)!=7 or datetime.strptime(period,"%Y-%m").strftime("%Y-%m")!=period:
            raise ValueError
    except ValueError as exc:
        raise ValueError("period must use YYYY-MM") from exc
    return period


def monthly(conn,period:str)->dict:
    period=_period(period)
    accounts=[dict(x) for x in conn.execute(
        "SELECT id,account_name,currency FROM bank_accounts WHERE active=1 ORDER BY account_name"
    ).fetchall()]
    bank_rows=[dict(x) for x in conn.execute(
        "SELECT id,transaction_on,account_name,bank_reference,description,direction,"
        "amount,currency,base_amount_cny FROM bank_transactions "
        "WHERE transaction_on LIKE ? ORDER BY transaction_on,id",
        (period+"-%",),
    ).fetchall()]

    allocations=[dict(x) for x in conn.execute(
        "SELECT a.bank_transaction_id,a.target_type,a.target_id,a.amount,"
        "f.bank_amount_cny FROM bank_allocations a "
        "JOIN bank_transactions b ON b.id=a.bank_transaction_id "
        "LEFT JOIN bank_allocation_fx f ON f.allocation_id=a.id "
        "WHERE b.transaction_on LIKE ?",(period+"-%",),
    ).fetchall()]
    by_bank=defaultdict(list)
    for allocation in allocations:
        by_bank[allocation["bank_transaction_id"]].append(allocation)

    # Bank-side GL movements from each allocation must equal the allocation's
    # verified CNY values; foreign-currency bank facts stay separately tracked.
    bank_gl=defaultdict(lambda:ZERO)
    for entry in conn.execute(
        "SELECT e.source_id,l.debit,l.credit FROM journal_entries e "
        "JOIN journal_lines l ON l.entry_id=e.id "
        "WHERE e.source_type='bank_allocation' AND e.posted_on LIKE ? "
        "AND l.account='bank'",(period+"-%",),
    ).fetchall():
        bank_gl[entry["source_id"]]+=_amount(entry["debit"])-_amount(entry["credit"])

    groups={}
    unallocated_lines=[]
    missing_evidence=[]
    posting_errors=[]
    provisional_allocations=0
    for bank in bank_rows:
        name=bank["account_name"]
        currency=bank["currency"]
        key=(name,currency)
        if key not in groups:
            groups[key]={"inflow":ZERO,"outflow":ZERO,"allocated":ZERO,
                         "remaining":ZERO,"transaction_count":0,
                         "unallocated_count":0,"unclassified_count":0,
                         "missing_evidence_count":0,"posting_gap_count":0,
                         "posting_gap_cny":ZERO}
        data=groups[key]
        value=_amount(bank["amount"])
        direction=bank["direction"]
        if direction not in ("in","out") or value<=0:
            raise ValueError("invalid imported bank amount or direction")
        data["inflow" if direction=="in" else "outflow"]+=value
        data["transaction_count"]+=1
        linked=by_bank[bank["id"]]
        used=sum((_amount(a["amount"]) for a in linked),ZERO)
        remaining=value-used
        if remaining<0:
            raise ValueError("bank transaction allocated beyond its original amount")
        data["allocated"]+=used
        data["remaining"]+=remaining
        if remaining>0:
            data["unallocated_count"]+=1
            if len(unallocated_lines)<30:
                unallocated_lines.append({
                    "transaction_id":bank["id"],"date":bank["transaction_on"],
                    "account":name,"currency":currency,"reference":bank["bank_reference"],
                    "description":bank["description"],"remaining":_money(remaining),
                })
        data["unclassified_count"]+=sum(1 for a in linked if a["target_type"]=="unclassified")

        evidence=conn.execute(
            "SELECT 1 FROM archive_links WHERE source_system='finance.bank_transaction' "
            "AND source_document_id=? LIMIT 1",(bank["id"],),
        ).fetchone()
        if not evidence:
            data["missing_evidence_count"]+=1
            if len(missing_evidence)<30:
                missing_evidence.append({"type":"bank","id":bank["id"],
                                         "reference":bank["bank_reference"]})

        if linked:
            expected=sum((_amount(a["bank_amount_cny"]) for a in linked if
                          a["bank_amount_cny"] is not None),ZERO)
            missing_fx=any(a["bank_amount_cny"] is None for a in linked)
            actual=bank_gl[bank["id"]]
            signed_expected=expected if direction=="in" else -expected
            gap=signed_expected-actual
            if missing_fx or gap!=0:
                data["posting_gap_count"]+=1
                data["posting_gap_cny"]+=gap
                if len(posting_errors)<30:
                    posting_errors.append({"type":"bank_gl","id":bank["id"],
                        "currency":currency,"missing_fx":missing_fx,
                        "difference_cny":_money(gap)})
        elif bank_gl[bank["id"]]!=0:
            data["posting_gap_count"]+=1
            data["posting_gap_cny"]-=bank_gl[bank["id"]]
            posting_errors.append({"type":"bank_gl_without_allocation","id":bank["id"]})

    statement_rows=[dict(r) for r in conn.execute(
        "SELECT s.bank_account_id,s.opening_balance,s.closing_balance,"
        "s.statement_reference FROM bank_statement_reconciliations s "
        "WHERE s.period=?",(period,),
    ).fetchall()]
    statements={x["bank_account_id"]:x for x in statement_rows}
    account_result=[]
    missing_statements=0
    mismatched_balances=0
    missing_journals=sum(x["posting_gap_count"] for x in groups.values())

    for account in accounts:
        key=(account["account_name"],account["currency"])
        info=groups.get(key,{"inflow":ZERO,"outflow":ZERO,"allocated":ZERO,
            "remaining":ZERO,"transaction_count":0,"unallocated_count":0,
            "unclassified_count":0,"missing_evidence_count":0,
            "posting_gap_count":0,"posting_gap_cny":ZERO})
        statement=statements.get(account["id"])
        difference=None
        calculated=None
        status="statement_missing"
        if statement:
            opening=_amount(statement["opening_balance"])
            closing=_amount(statement["closing_balance"])
            calculated=opening+info["inflow"]-info["outflow"]
            difference=calculated-closing
            status="matched" if difference==0 else "balance_difference"
            if difference:mismatched_balances+=1
        else:
            missing_statements+=1
        account_result.append({
            "account_id":account["id"],"account":account["account_name"],
            "currency":account["currency"],"status":status,
            "statement_reference":statement["statement_reference"] if statement else None,
            "opening_balance":statement["opening_balance"] if statement else None,
            "closing_balance":statement["closing_balance"] if statement else None,
            "calculated_closing":_money(calculated) if calculated is not None else None,
            "difference":_money(difference) if difference is not None else None,
            **{field:_money(info[field]) for field in
                ("inflow","outflow","allocated","remaining","posting_gap_cny")},
            **{field:info[field] for field in
                ("transaction_count","unallocated_count","unclassified_count",
                 "missing_evidence_count","posting_gap_count")},
        })
    unknown_accounts=[{"account":name,"currency":currency} for name,currency in groups
                      if (name,currency) not in {(a["account_name"],a["currency"]) for a in accounts}]

    invoices=[dict(row) for row in conn.execute(
        "SELECT i.id,i.invoice_number,i.direction,i.counterparty,i.currency,"
        "i.total_amount,i.tax_status "
        "FROM invoices i WHERE i.issued_on LIKE ?",(period+"-%",),
    ).fetchall()]
    invoice_result={"total":len(invoices),"unlinked":0,"partially_linked":0,
                    "overlinked":0,"red_issued_in_period":0,
                    "tax_pending":0,"missing_evidence":0,"details":[]}
    # Exact linked totals are calculated from the original strings, not SQL
    # numeric casts which might round via floating point under SQLite.
    for item in invoices:
        paid=sum((_amount(r[0]) for r in conn.execute(
            "SELECT linked_amount FROM invoice_links WHERE invoice_id=?",(item["id"],),
        ).fetchall()),ZERO)
        gross=_amount(item["total_amount"])
        net_info=invoice_red.net_for(conn,item["id"],as_of=period+"-31")
        net=_amount(net_info["net_amount"])
        if paid>net:
            invoice_result["overlinked"]+=1
        elif not paid and net>ZERO:
            invoice_result["unlinked"]+=1
        elif paid<net:
            invoice_result["partially_linked"]+=1
        if item["tax_status"]=="pending":
            invoice_result["tax_pending"]+=1
        evidence=conn.execute(
            "SELECT 1 FROM archive_links WHERE source_system='finance.invoice' "
            "AND source_document_id=? LIMIT 1",(item["id"],),
        ).fetchone()
        if not evidence:
            invoice_result["missing_evidence"]+=1
        if (paid!=net or not evidence) and len(invoice_result["details"])<20:
            invoice_result["details"].append({
                "id":item["id"],"number":item["invoice_number"],
                "counterparty":item["counterparty"],"direction":item["direction"],
                "currency":item["currency"],"face_value":_money(gross),
                "net_amount":_money(net),"credited_amount":net_info["credited_amount"],
                "linked_amount":_money(paid),"remaining":_money(net-paid),
                "overlinked":paid>net,
                "has_original":bool(evidence),
            })

    red_rows=conn.execute(
        "SELECT blue_invoice_id,invoice_number FROM invoice_credit_notes WHERE issued_on LIKE ?",
        (period+"-%",)
    ).fetchall()
    invoice_result["red_issued_in_period"]=len(red_rows)
    # Historical blue invoices can be overlinked by red notes in this month.
    for red in red_rows:
        blue_id=red["blue_invoice_id"]
        if any(item["id"]==blue_id for item in invoices):
            continue
        linked=sum((_amount(r[0]) for r in conn.execute(
            "SELECT linked_amount FROM invoice_links WHERE invoice_id=?",(blue_id,)
        ).fetchall()),ZERO)
        net=_amount(invoice_red.net_for(conn,blue_id,as_of=period+"-31")["net_amount"])
        if linked>net:
            invoice_result["overlinked"]+=1
            if len(invoice_result["details"])<20:
                invoice_result["details"].append({
                    "id":blue_id,"red_invoice_number":red["invoice_number"],
                    "net_amount":_money(net),"linked_amount":_money(linked),
                    "overlinked":True,"remaining":_money(net-linked)
                })
    coverage=conn.execute(
        "SELECT account_count FROM bank_account_coverage WHERE period=?",(period,),
    ).fetchone()
    coverage_valid=bool(coverage and int(coverage["account_count"])==len(accounts))
    total_unallocated=sum(x["unallocated_count"] for x in groups.values())
    total_unclassified=sum(x["unclassified_count"] for x in groups.values())
    source_missing=sum(x["missing_evidence_count"] for x in groups.values())
    has_issues=bool(
        missing_statements or mismatched_balances or missing_journals or
        total_unallocated or source_missing or not coverage_valid or
        unknown_accounts or invoice_result["missing_evidence"] or
        invoice_result["overlinked"]
    )
    status=("no_accounts" if not accounts else
            "review_required" if has_issues else
            "balanced_with_pending_items" if total_unclassified or
            invoice_result["unlinked"] or invoice_result["tax_pending"] else
            "balanced")
    return {
        "period":period,"status":status,
        "accounts":account_result,"unknown_accounts":unknown_accounts,
        "bank":{"transactions":len(bank_rows),"unallocated_count":total_unallocated,
                "unclassified_allocations":total_unclassified,
                "missing_originals":source_missing,
                "posting_errors":posting_errors,"posting_error_count":missing_journals,
                "unallocated_rows":unallocated_lines,"missing_evidence":missing_evidence},
        "invoice":invoice_result,
        "account_inventory_confirmed":coverage_valid,
        "scope":"核对银行对账单余额、银行资金核销与总账银行科目、发票关联；未开票销售仍应按履约确认收入成本，发票不等于付款；数据与证据缺失时不标为已完成。",
    }
