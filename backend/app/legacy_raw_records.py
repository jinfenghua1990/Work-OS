"""Append-only legacy PostgreSQL source-record snapshots.

These are copies of rows *as stored in the old CoffeeBear database* and are
NOT substitutions for signed PDF/OFD/Excel/CSV originals. Original file bytes
must be acquired separately and verified against their declared SHA-256.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

ALLOWED_SOURCE_TABLES = (
    "sales_orders", "sales_order_items",
    "bank_transactions", "bank_import_batches",
    "tax_invoices", "tax_invoice_imports", "tax_invoice_import_records",
    "archive_files", "external_purchase_orders",
    "alibaba1688_file_imports", "jackyun_file_imports",
    "jackyun_file_import_records", "consumable_purchases",
    "finance_entries",
)


def table_name(conn) -> str:
    return "evidence.legacy_source_records" if getattr(conn,"is_postgres",False) else "evidence_legacy_source_records"


def ensure_tables(conn) -> None:
    if getattr(conn,"is_postgres",False):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence.legacy_source_records (
                source_system TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_pk TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                raw_json_text TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (source_system,source_table,source_pk,snapshot_sha256),
                CHECK(length(snapshot_sha256)=64)
            )
        """)
        conn.execute("""
            CREATE OR REPLACE TRIGGER imm_legacy_source_records
            BEFORE UPDATE OR DELETE ON evidence.legacy_source_records
            FOR EACH ROW EXECUTE FUNCTION evidence.forbid_original_mutation()
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_legacy_source_records (
                source_system TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_pk TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                raw_json_text TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (source_system,source_table,source_pk,snapshot_sha256),
                CHECK(length(snapshot_sha256)=64)
            )
        """)
        for action in ("UPDATE","DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS imm_evidence_legacy_records_{action.lower()} "
                f"BEFORE {action} ON evidence_legacy_source_records BEGIN "
                "SELECT RAISE(ABORT, 'original evidence is immutable'); END"
            )


def store_record(
    conn, source_system:str, source_table:str, source_pk:str, raw_json_text:str,
) -> dict:
    if source_system != "coffeebear-prod" or source_table not in ALLOWED_SOURCE_TABLES:
        raise ValueError("unsupported source system or source table")
    if not source_pk or len(source_pk)>160:
        raise ValueError("invalid source record identifier")
    if not isinstance(raw_json_text,str) or not raw_json_text.strip():
        raise ValueError("original source row JSON is required")
    # Validate JSON without reserializing: preserve the exact text received from
    # PostgreSQL row_to_json as the snapshot, including every original column.
    parsed=json.loads(raw_json_text)
    if not isinstance(parsed,dict):
        raise ValueError("source record must be an object")
    digest=hashlib.sha256(raw_json_text.encode("utf-8")).hexdigest()
    existing=conn.execute(
        f"SELECT 1 FROM {table_name(conn)} "
        "WHERE source_system=? AND source_table=? AND source_pk=? AND snapshot_sha256=?",
        (source_system,source_table,source_pk,digest)
    ).fetchone()
    if existing:
        return {"duplicate":True,"snapshot_sha256":digest}
    conn.execute(
        f"INSERT INTO {table_name(conn)} "
        "(source_system,source_table,source_pk,snapshot_sha256,raw_json_text,captured_at) "
        "VALUES(?,?,?,?,?,?)",
        (source_system,source_table,source_pk,digest,raw_json_text,
         datetime.now(timezone.utc).isoformat())
    )
    return {"duplicate":False,"snapshot_sha256":digest}


def counts(conn) -> list[dict]:
    rows=conn.execute(
        f"SELECT source_table,COUNT(*) AS snapshot_count,COUNT(DISTINCT source_pk) AS source_records "
        f"FROM {table_name(conn)} WHERE source_system=? GROUP BY source_table ORDER BY source_table",
        ("coffeebear-prod",)
    ).fetchall()
    return [dict(row) for row in rows]
