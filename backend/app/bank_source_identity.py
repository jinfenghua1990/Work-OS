"""Stable identity for bank statements; uploaded file names are not transaction IDs.

The automatic-import path only accepts bank-issued transaction references,
scoped to the actual account and transaction date. Without one, the importer
must stop for review rather than inventing an unsafe 'file:row' identity.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date


def bank_reference_identity(row: dict) -> str:
    account = str(row.get("account_name") or "").strip()
    reference = str(row.get("bank_reference") or "").strip()
    day = str(row.get("transaction_on") or "").strip()
    if not account or account == "未指定账户":
        raise ValueError("bank account name is required for cross-file idempotency")
    if not reference:
        raise ValueError("bank-issued transaction reference is required; review and supply a stable identifier before importing")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError as exc:
        raise ValueError("bank transaction date must use YYYY-MM-DD") from exc
    # Scope reference to both account and calendar date. Changes in amount,
    # currency, direction or memo must be compared as CONFLICTS, not new rows.
    identity = {
        "account_name": account.casefold(),
        "transaction_on": day,
        "bank_reference": reference,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "bank:v3:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
