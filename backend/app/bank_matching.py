"""Read-only bank-to-receivable/payable matching proposals.

No journals, allocations or balances are written. A human must explicitly
confirm a suggestion via the existing, idempotent /api/bank/allocate endpoint.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


def _money(value: object) -> Decimal | None:
    try:
        number=Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _identity(value: object) -> str:
    return "".join(re.findall(r"[0-9a-z\u4e00-\u9fff]+",str(value or "").casefold()))


def propose(bank_rows: list[dict], open_items: list[dict]) -> list[dict]:
    """Offer single-target partial payments only when the whole batch is safe.

    Multiple bank instalments may match one receivable/payable, but the total
    proposed amount must not exceed its current unpaid original-currency
    amount. Multiple candidate business items always require manual review.
    """
    proposals=[]
    outstanding_by_target={}
    for bank in bank_rows:
        total=_money(bank.get("amount"))
        used=_money(bank.get("allocated_amount","0"))
        if total is None or used is None:
            continue
        available=total-used
        if available<=0 or not _money(bank.get("base_amount_cny")):
            continue
        note=_identity(bank.get("description"))
        if not note or not bank.get("bank_reference"):
            continue
        direction="receivable" if bank.get("direction")=="in" else "payable" if bank.get("direction")=="out" else ""
        candidates=[]
        for item in open_items:
            name=_identity(item.get("counterparty"))
            if len(name)<3 or name not in note:
                continue
            if item.get("direction")!=direction or item.get("currency")!=bank.get("currency"):
                continue
            original=_money(item.get("original_amount"))
            settled=_money(item.get("settled_amount"))
            carrying=_money(item.get("original_amount_cny"))
            if original is None or settled is None or carrying is None:
                continue
            due=original-settled
            if 0<available<=due:
                candidates.append(item)
                outstanding_by_target[item["id"]]=due
        if len(candidates)==1:
            item=candidates[0]
            due=outstanding_by_target[item["id"]]
            proposals.append({
                "bank_transaction_id":bank["id"],
                "bank_reference":bank["bank_reference"],
                "transaction_on":bank["transaction_on"],
                "description":bank["description"],
                "target_id":item["id"],
                "target_type":direction,
                "counterparty":item["counterparty"],
                "currency":bank["currency"],
                "amount":str(available),
                "confidence":"high",
                "requires_confirmation":True,
                "is_partial":available<due,
                "reason":"往来单位、收付方向、币种一致；金额不超过未结余额，需人工确认",
            })
    # Do not recommend a batch if two apparently valid bank rows could cause
    # overpayment to the same receivable/payable. Never silently choose one.
    sums={}
    for row in proposals:
        sums[row["target_id"]]=sums.get(row["target_id"],Decimal(0))+Decimal(row["amount"])
    return [row for row in proposals if sums[row["target_id"]]<=outstanding_by_target[row["target_id"]]]


def from_database(conn,limit:int=200) -> dict:
    if not 1<=limit<=500:
        raise ValueError("match limit must be between 1 and 500")
    banks=[dict(row) for row in conn.execute(
        "SELECT b.id,b.bank_reference,b.transaction_on,b.description,b.direction,"
        "b.amount,b.currency,b.base_amount_cny "
        "FROM bank_transactions b LEFT JOIN period_locks p "
        "ON p.period=substr(b.transaction_on,1,7) "
        "WHERE p.period IS NULL "
        "ORDER BY b.transaction_on DESC,b.id DESC LIMIT ?",
        (limit,),
    ).fetchall()]
    # Do not sum currency amounts in a SQL floating-point accumulator.
    if banks:
        ids=[bank["id"] for bank in banks]
        placeholders=",".join("?" for _ in ids)
        totals={}
        for allocation in conn.execute(
            f"SELECT bank_transaction_id,amount FROM bank_allocations "
            f"WHERE bank_transaction_id IN ({placeholders})",ids
        ).fetchall():
            key=allocation["bank_transaction_id"]
            totals[key]=totals.get(key,Decimal(0))+Decimal(str(allocation["amount"]))
        for bank in banks:
            bank["allocated_amount"]=str(totals.get(bank["id"],Decimal(0)))
    items=[dict(row) for row in conn.execute(
        "SELECT r.id,r.counterparty,r.direction,r.original_amount,r.settled_amount,"
        "r.currency,f.original_amount_cny "
        "FROM receivables_payables r LEFT JOIN open_item_fx f ON f.item_id=r.id "
        "ORDER BY r.created_at DESC LIMIT 2000"
    ).fetchall()]
    results=propose(banks,items)
    return {"scanned_bank_rows":len(banks),"review_required":True,
            "suggestions":results,"suggestion_count":len(results),
            "note":"仅生成确定性匹配建议；未经确认不核销、不记账"}
