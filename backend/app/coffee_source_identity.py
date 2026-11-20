"""Stable identity for fulfilled coffee sales, independent of uploaded file names.

This module deliberately does not handle refunds, cancellations or stock returns:
those are distinct business events with separate accounting workflows.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_sale_source(data: dict[str, Any]) -> dict[str, str | bool]:
    """Return a canonical posting identity while preserving legacy API compatibility.

    New imports supply channel, store_name, external_order_no and external_line_id.
    Existing integrations may still use source_row_key, but their legacy keys
    cannot guarantee cross-file deduplication and must be migrated separately.
    """
    event = str(data.get("event_type") or "sale").strip().lower()
    if event != "sale":
        raise ValueError(
            "Only fulfilled sale lines can post through this endpoint; "
            "refunds, cancellations and physical returns require separate workflows"
        )
    channel = str(data.get("channel") or "").strip().casefold()
    store = str(data.get("store_name") or "").strip().casefold()
    order = str(data.get("external_order_no") or "").strip()
    line = str(data.get("external_line_id") or "").strip()
    if any((store, order, line)):
        if not all((channel, store, order, line)):
            raise ValueError(
                "Cross-file sale identity requires channel, store_name, "
                "external_order_no and external_line_id"
            )
        natural = {
            "channel": channel,
            "store_name": store,
            "external_order_no": order,
            "external_line_id": line,
            "event_type": "sale",
        }
        encoded = json.dumps(natural, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return {
            **natural,
            "source_row_key": "coffee:sale:v2:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "source": f"channel:{channel}",
            "structured": True,
        }
    legacy_key = str(data.get("source_row_key") or "").strip()
    if not legacy_key:
        raise ValueError("Provide a stable sale identity or legacy source_row_key")
    return {
        "channel": channel,
        "store_name": "",
        "external_order_no": "",
        "external_line_id": "",
        "event_type": "sale",
        "source_row_key": legacy_key,
        "source": str(data.get("source") or "file_import"),
        "structured": False,
    }
