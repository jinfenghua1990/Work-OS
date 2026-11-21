"""ALSVID finance overview and exceptions — read-only, without a second business ledger.

ALSVID orders, inventory, VIN and sales remain authoritative in ALSVID Admin.
Work OS is the only finance journal and source archive. Until an authenticated
ALSVID integration is installed, no live ALSVID order or inventory figure is
claimed. Existing Work OS bike records are historical references only.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

CENT=Decimal("0.01")
ZERO=Decimal("0")


def _money(value:object)->Decimal:
    try:
        out=Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid financial amount") from exc
    if not out.is_finite():
        raise ValueError("non-finite amount")
    return out


def _format(amount:Decimal)->str:
    return str(amount.quantize(CENT))


def _period(value:str)->str:
    try:
        if len(value)!=7 or datetime.strptime(value,"%Y-%m").strftime("%Y-%m")!=value:
            raise ValueError
    except (ValueError,TypeError) as exc:
        raise ValueError("period must use YYYY-MM") from exc
    return value


def _bike_item(
    source_type:str, source_id:str|None, personal_bike:set[str],
    bike_expenses:set[str]|None=None,
)->bool:
    """Attribute a financial item by its origin ID, never by payee or amount."""
    return (
        source_type in ("bike_milestone","bike_landed_cost","bike_sale","bike_receipt")
        or source_type.startswith("alsvid_")
        or (source_type=="personal_advance" and source_id in personal_bike)
        or (source_type=="operating_expense" and source_id in (bike_expenses or set()))
    )


def overview(conn,period:str)->dict:
    period=_period(period)
    personal=[dict(x) for x in conn.execute(
        "SELECT p.id,p.source_key,p.claimant,p.paid_on,p.amount,p.currency,"
        "p.status,p.employee_payable_id,p.business_line,p.payment_reference,"
        "r.settled_amount AS reimbursed "
        "FROM personal_advances p "
        "LEFT JOIN receivables_payables r ON r.id=p.employee_payable_id "
        "WHERE p.business_line='bike' ORDER BY p.paid_on DESC,p.id DESC"
    ).fetchall()]
    personal_ids={x["id"] for x in personal}
    # Financial expenses made in Work OS can also belong to the bike business.
    # In particular, personal-paid merchant invoices link to their
    # operating_expense supplier payable, not to the employee payable.
    bike_expense_ids={row["id"] for row in conn.execute(
        "SELECT id FROM expenses WHERE business_line='bike'"
    ).fetchall()}
    def belongs(source_type:str,source_id:str|None)->bool:
        return _bike_item(source_type,source_id,personal_ids,bike_expense_ids)
    issues=[]
    def flag(key,title,detail="",reference="",priority="attention",scope="alsvid"):
        issues.append({"code":key,"title":title,"detail":detail,
                       "reference":str(reference),"priority":priority,"scope":scope})
    flag("integration_pending","ALSVID 业务接口尚未接通",
         "本页只展示 Work OS 已归集财务数据；ALSVID 实时采购、库存、销售和利润均未核验。",
         priority="blocking")

    personal_open=ZERO
    personal_pending_count=0
    draft_count=0
    personal_rows=[]
    for x in personal:
        amt=_money(x["amount"])
        paid_back=_money(x["reimbursed"])
        owing=amt-paid_back
        if owing<0:
            flag("reimbursement_overpaid","个人报销超额","应复核已付款与个人垫付原额",x["source_key"],"blocking")
        if x["status"]=="draft":
            draft_count+=1
            flag("personal_pending_approval","个人垫付待审核",
                 f'{x["claimant"]} · CNY {_format(amt)}',x["source_key"])
        elif owing>0:
            personal_open+=owing
            personal_pending_count+=1
            flag("personal_pending_reimbursement","个人垫付待报销",
                 f'{x["claimant"]} · CNY {_format(owing)}',x["source_key"])
        if len(personal_rows)<50:
            personal_rows.append({"source_key":x["source_key"],"claimant":x["claimant"],
                "amount":_format(amt),"outstanding":_format(max(owing,ZERO)),
                "paid_on":x["paid_on"],"status":"draft" if x["status"]=="draft" else
                "reimbursed" if owing<=0 else "partially_reimbursed" if paid_back>0 else "pending_reimbursement"})
    # Only recognized/approved liabilities count as employee payable totals;
    # unapproved drafts are review items, not accounting liabilities.
    open_items=[]
    for row in conn.execute(
        "SELECT id,direction,source_type,source_id,counterparty,original_amount,"
        "settled_amount,currency,due_on FROM receivables_payables "
        "WHERE direction IN ('payable','receivable') ORDER BY created_at DESC,id DESC"
    ).fetchall():
        x=dict(row)
        if belongs(x["source_type"],x["source_id"]):
            open_items.append(x)

    payables=defaultdict(lambda:ZERO)
    receivables=defaultdict(lambda:ZERO)
    paid=defaultdict(lambda:ZERO)
    received=defaultdict(lambda:ZERO)
    linked_invoice_by_item=set()
    missing_originals=0
    allocation_count=0
    checked_bank_files=set()
    for row in conn.execute(
        "SELECT a.bank_transaction_id,a.target_id,a.amount,"
        "b.transaction_on,b.direction,b.currency,b.bank_reference,"
        "r.source_type,r.source_id "
        "FROM bank_allocations a "
        "JOIN bank_transactions b ON b.id=a.bank_transaction_id "
        "JOIN receivables_payables r ON r.id=a.target_id "
        "WHERE b.transaction_on LIKE ? AND a.target_type IN ('payable','receivable')",
        (period+"-%",),
    ).fetchall():
        x=dict(row)
        if not belongs(x["source_type"],x["source_id"]):
            continue
        allocation_count+=1
        (received if x["direction"]=="in" else paid)[x["currency"]]+=_money(x["amount"])
        # One bank transaction can cover multiple ALSVID invoice/order
        # obligations; audit its evidence once, not once per allocation.
        if x["bank_transaction_id"] not in checked_bank_files:
            checked_bank_files.add(x["bank_transaction_id"])
            has_original=conn.execute(
                "SELECT 1 FROM archive_links "
                "WHERE source_system='finance.bank_transaction' "
                "AND source_document_id=? LIMIT 1",(x["bank_transaction_id"],),
            ).fetchone()
            if not has_original:
                missing_originals+=1
                flag("bank_original_missing","已归属 ALSVID 的银行流水缺原件关联",
                     x["bank_reference"],x["bank_transaction_id"],"attention")

    for x in open_items:
        amount=_money(x["original_amount"])
        settled=_money(x["settled_amount"])
        due=amount-settled
        if due<0:
            flag("open_item_overpaid","应收/应付账款核销超额",x["counterparty"],x["id"],"blocking")
        if due>0:
            (receivables if x["direction"]=="receivable" else payables)[x["currency"]]+=due
            if x["due_on"] and x["due_on"]<date.today().isoformat():
                flag("overdue_business_item","ALSVID 应收/应付逾期",
                    f'{x["counterparty"]} · {x["currency"]} {_format(due)}',
                    x["id"],"attention")

    invoice_ids=set()
    invoice_counts={"input":0,"output":0,"tax_pending":0,
                    "without_business_link":0,"missing_original":0}
    linked_amounts=defaultdict(lambda:ZERO)
    invoice_rows=[]
    for invoice in conn.execute(
        "SELECT id,invoice_number,direction,counterparty,currency,total_amount,"
        "tax_status,source_system,source_document_id "
        "FROM invoices WHERE issued_on LIKE ? ORDER BY issued_on DESC,id DESC",
        (period+"-%",),
    ).fetchall():
        x=dict(invoice)
        links=[dict(r) for r in conn.execute(
            "SELECT l.target_id,l.linked_amount,r.source_type,r.source_id "
            "FROM invoice_links l "
            "LEFT JOIN receivables_payables r ON r.id=l.target_id "
            "WHERE l.invoice_id=?",(x["id"],),
        ).fetchall()]
        matched_links=[z for z in links if z["source_type"] and
                       belongs(z["source_type"],z["source_id"])]
        direct=str(x["source_system"] or "").casefold() in ("alsvid","alsvid_admin","alsvid-admin")
        if not direct and not matched_links:
            continue
        if x["id"] in invoice_ids:
            continue
        invoice_ids.add(x["id"])
        invoice_counts["input" if x["direction"]=="purchase" else "output"]+=1
        if x["tax_status"]=="pending":
            invoice_counts["tax_pending"]+=1
        if not matched_links:
            invoice_counts["without_business_link"]+=1
            flag("invoice_unlinked","ALSVID 发票尚未关联 ALSVID 业务单据",
                 x["invoice_number"],x["id"])
        if direct and not x["source_document_id"]:
            flag("invoice_source_identity_missing","ALSVID 发票缺少外部唯一业务 ID",
                 x["invoice_number"],x["id"],"attention")
        if len(matched_links)<len(links):
            flag("invoice_cross_project","发票关联了不属于 ALSVID 的往来",
                 x["invoice_number"],x["id"],"blocking")
        # Partial cross-project invoices must not have their *whole face value*
        # attributed to ALSVID. Count only bike-linked amounts. Direct ALSVID
        # invoices without links count as an unallocated document, not profit.
        for z in matched_links:
            linked_amounts[x["currency"]]+=_money(z["linked_amount"])
            linked_invoice_by_item.add(z["target_id"])
        original=conn.execute(
            "SELECT 1 FROM archive_links "
            "WHERE source_system='finance.invoice' AND source_document_id=? LIMIT 1",
            (x["id"],),
        ).fetchone()
        if not original:
            invoice_counts["missing_original"]+=1
            flag("invoice_original_missing","发票未关联原始凭证",
                 x["invoice_number"],x["id"])
        if len(invoice_rows)<40:
            invoice_rows.append({
                "id":x["id"],"number":x["invoice_number"],
                "direction":x["direction"],"currency":x["currency"],
                "tax_status":x["tax_status"],"source_system":x["source_system"],
                "has_original":bool(original),"linked_to_alsvid":bool(matched_links),
            })

    # A missing invoice relationship is an attention item, not evidence the
    # supplier failed to issue an invoice.
    for x in open_items:
        if (x["source_type"]!="personal_advance" and x["id"] not in linked_invoice_by_item
                and x["direction"]=="payable" and len(issues)<200):
            flag("payable_invoice_unlinked","ALSVID 应付尚未找到关联发票",
                 x["counterparty"],x["id"],"attention")

    # Only the money already booked in Work OS can form a historical
    # finance reference; it is not a live ALSVID sales/profit figure.
    ledger={"sales_revenue_cny":ZERO,"cost_of_sales_cny":ZERO,
            "operating_expenses_cny":ZERO}
    for row in conn.execute(
        "SELECT l.account,l.debit,l.credit "
        "FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id "
        "WHERE e.posted_on LIKE ? AND COALESCE(l.business_line,e.business_line)='bike'",
        (period+"-%",),
    ).fetchall():
        account=row["account"]
        debit=_money(row["debit"]);credit=_money(row["credit"])
        if account=="sales_revenue":
            ledger["sales_revenue_cny"]+=credit-debit
        elif account=="cost_of_goods_sold":
            ledger["cost_of_sales_cny"]+=debit-credit
        elif account.startswith("operating_expense:"):
            ledger["operating_expenses_cny"]+=debit-credit
    ledger["reference_margin_cny"]=(
        ledger["sales_revenue_cny"]-ledger["cost_of_sales_cny"]-
        ledger["operating_expenses_cny"]
    )
    legacy_orders=conn.execute("SELECT COUNT(*) FROM bike_orders").fetchone()[0]
    legacy_sales=conn.execute("SELECT COUNT(*) FROM bike_sales").fetchone()[0]
    if legacy_orders or legacy_sales:
        flag("legacy_business_records","Work OS 仍保留历史自行车业务记录",
             f"{legacy_orders} 条采购引用、{legacy_sales} 条销售记录；"
             "仅作为历史对照，未与 ALSVID 官方业务数据完成合并。",priority="attention")

    def balances(values):
        return [{"currency":currency,"amount":_format(value)}
                for currency,value in sorted(values.items())]
    priority={"blocking":0,"attention":1,"info":2}
    issues.sort(key=lambda x:(priority.get(x["priority"],3),x["code"],x["reference"]))
    return {
        "period":period,
        "period_scope":{
            "cash":"selected_month","invoice":"selected_month",
            "payables":"current_outstanding_not_historical_month_end",
            "personal_reimbursements":"current_outstanding_not_historical_month_end",
            "note":"银行收支、发票和分录按选择月份；应收应付及个人报销余额为当前未结（不是该月月底历史快照）。"
        },
        "integration":{
            "connected":False,"status":"not_connected",
            "last_verified_sync":None,"contract_version":"alsvid-finance-v1",
            "source_of_truth":"ALSVID Admin",
            "note":"尚无通过验收的 ALSVID API 接入，不提供实时业务金额或同步成功记录。"
        },
        "business":{
            "purchase_order_count":None,"sales_order_count":None,
            "inventory_count":None,"business_metrics_verified":False,
            "legacy_local_purchase_refs":int(legacy_orders),
            "legacy_local_sales_rows":int(legacy_sales),
        },
        "financial":{
            "cash_paid":balances(paid),"cash_received":balances(received),
            "accounts_payable":balances(payables),"accounts_receivable":balances(receivables),
            "bank_allocation_count":allocation_count,
            "invoice_linked":balances(linked_amounts),"invoices":invoice_counts,
            "personal_pending_approval":draft_count,
            "personal_pending_reimbursement":personal_pending_count,
            "personal_outstanding_cny":_format(personal_open),
            "ledger_reference_cny":{key:_format(amount) for key,amount in ledger.items()},
            "ledger_reference_complete":False,
            "scope":"仅 Work OS 已确认属于 ALSVID 的财务记录；银行收支、发票、分录按所选月份，应收应付及报销为当前余额；不含 ALSVID 尚未同步业务。"
        },
        "exceptions":{"total":len(issues),"blocking":sum(1 for x in issues if x["priority"]=="blocking"),
                      "items":issues[:100],"truncated":len(issues)>100},
        "personal_reimbursements":personal_rows,
        "sync_events":[],"sync_events_available":False,
        "read_only":True
    }
