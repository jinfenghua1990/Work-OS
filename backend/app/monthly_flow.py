"""Compact monthly cash/invoice flow report.

A bank receipt is a cash fact, not automatically sales revenue. An invoice
amount is a tax document fact, not automatically deductible or settled.
Amounts retain their own currencies and are added with Decimal only.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

CENT=Decimal("0.01")


def _month(period:str)->str:
    try:
        if len(period)!=7 or datetime.strptime(period,"%Y-%m").strftime("%Y-%m")!=period:
            raise ValueError
    except ValueError as exc:
        raise ValueError("period must use YYYY-MM") from exc
    return period


def _format(amount:Decimal)->str:
    return str(amount.quantize(CENT,rounding=ROUND_HALF_UP))


def report(conn, period:str)->dict:
    period=_month(period)
    bank=defaultdict(lambda:{"inflow":Decimal(0),"outflow":Decimal(0),
                              "transfer_in":Decimal(0),"transfer_out":Decimal(0),
                              "unallocated":Decimal(0),"transactions":0,"unmatched":0})
    rows=conn.execute(
        "SELECT b.id,b.currency,b.direction,b.amount,b.cash_flow_category,"
        "COALESCE((SELECT SUM(CAST(a.amount AS NUMERIC)) FROM bank_allocations a "
        "WHERE a.bank_transaction_id=b.id),0) AS allocated "
        "FROM bank_transactions b WHERE substr(b.transaction_on,1,7)=? "
        "ORDER BY b.transaction_on,b.id",(period,),
    ).fetchall()
    for row in rows:
        item=bank[row["currency"]]
        total=Decimal(str(row["amount"]))
        used=Decimal(str(row["allocated"]))
        remaining=total-used
        if remaining<0:raise ValueError("bank allocation exceeds original amount; manual audit required")
        if row["direction"] not in ("in","out"):raise ValueError("invalid bank direction")
        direction="inflow" if row["direction"]=="in" else "outflow"
        item[direction]+=total
        if row["cash_flow_category"]=="transfer":
            item["transfer_in" if direction=="inflow" else "transfer_out"]+=total
        item["transactions"]+=1
        item["unallocated"]+=remaining
        if remaining>0:item["unmatched"]+=1
    bank_result=[]
    for currency,data in sorted(bank.items()):
        bank_result.append({
            "currency":currency,
            "inflow":_format(data["inflow"]),
            "outflow":_format(data["outflow"]),
            "net":_format(data["inflow"]-data["outflow"]),
            "transfer_in":_format(data["transfer_in"]),
            "transfer_out":_format(data["transfer_out"]),
            "unallocated":_format(data["unallocated"]),
            "transactions":data["transactions"],
            "unmatched":data["unmatched"],
        })

    invoice_data=defaultdict(lambda:{"input_count":0,"output_count":0,
                                     "input_total":Decimal(0),"output_total":Decimal(0),
                                     "pending_tax":0})
    invoices=conn.execute(
        "SELECT direction,currency,total_amount,tax_status FROM invoices "
        "WHERE substr(issued_on,1,7)=?",(period,)
    ).fetchall()
    for row in invoices:
        item=invoice_data[row["currency"]]
        if row["direction"]=="purchase":
            item["input_count"]+=1
            item["input_total"]+=Decimal(str(row["total_amount"]))
        elif row["direction"]=="sales":
            item["output_count"]+=1
            item["output_total"]+=Decimal(str(row["total_amount"]))
        else:
            raise ValueError("invalid invoice direction")
        if row["tax_status"]=="pending":item["pending_tax"]+=1
    invoice_result=[{"currency":currency,
                     "input_count":data["input_count"],"output_count":data["output_count"],
                     "input_total":_format(data["input_total"]),
                     "output_total":_format(data["output_total"]),
                     "pending_tax":data["pending_tax"]}
                    for currency,data in sorted(invoice_data.items())]
    count=conn.execute(
        "SELECT COUNT(*) FROM archive_uploads WHERE period=?",(period,)
    ).fetchone()[0]
    closed=conn.execute(
        "SELECT 1 FROM period_locks WHERE period=?",(period,)
    ).fetchone() is not None
    return {"period":period,"closed":closed,
            "bank":{"transaction_count":len(rows),"currencies":bank_result},
            "invoices":{"document_count":len(invoices),"currencies":invoice_result},
            "original_uploads":int(count),
            "scope":"银行收支和发票分别统计；流水不能直接代表收入或利润；不同币种不合并。"}
