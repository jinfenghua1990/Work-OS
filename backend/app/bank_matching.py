"""Read-only bank-to-receivable/payable matching proposals.

No journals, allocations or balances are written. A human must explicitly
confirm a suggestion via the existing, idempotent /api/bank/allocate endpoint.
"""
from __future__ import annotations

import re
from collections import Counter
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
    """Only offer fully unambiguous, same-currency, exactly equal matches."""
    proposals=[]
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
            if due==available and due>0:
                candidates.append(item)
        if len(candidates)==1:
            item=candidates[0]
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
                "reason":"收付方向、币种、未结金额完全一致，银行摘要包含往来单位名称",
            })
    # A payable should not be recommended against multiple bank rows.
    matches_per_item=Counter(result["target_id"] for result in proposals)
    return [row for row in proposals if matches_per_item[row["target_id"]]==1]


def from_database(conn,limit:int=200) -> dict:
    if not 1<=limit<=500:
        raise ValueError("match limit must be between 1 and 500")
    banks=[dict(row) for row in conn.execute(
        "SELECT b.id,b.bank_reference,b.transaction_on,b.description,b.direction,"
        "b.amount,b.currency,b.base_amount_cny,"
        "COALESCE((SELECT SUM(CAST(a.amount AS NUMERIC)) FROM bank_allocations a "
        "WHERE a.bank_transaction_id=b.id),0) AS allocated_amount "
        "FROM bank_transactions b ORDER BY b.transaction_on DESC,b.id DESC LIMIT ?",
        (limit,),
    ).fetchall()]
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
