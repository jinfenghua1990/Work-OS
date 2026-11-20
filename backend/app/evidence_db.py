"""Immutable original evidence stored *inside* the Work OS database.

PostgreSQL uses a dedicated evidence schema, SQLite has isolated evidence_*
tables. Both persist the exact uploaded binary bytes plus a distinct, versioned
raw-rows snapshot (if the file can be parsed without business transformations).
Financial postings never UPDATE these records.

The disk/NAS content-addressed archive remains a secondary, separately
backed-up copy. These guards are NOT regulatory WORM: a privileged database
administrator can bypass triggers, and a local SQLite owner can edit the file.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import xlsx_reader

PARSER_VERSION = "source-rows-v1"
# Keep the free cloud tier from silently filling up. Increase only after
# checking quota and making a verified offline copy.
DEFAULT_PG_BUDGET = 256 * 1024 * 1024
DEFAULT_SQLITE_BUDGET = 8 * 1024 * 1024 * 1024


def _is_pg(conn) -> bool:
    return bool(getattr(conn, "is_postgres", False))


def _table(conn, name: str) -> str:
    return f"evidence.{name}" if _is_pg(conn) else f"evidence_{name}"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn) -> None:
    """Add the independent original-document area without touching finance rows."""
    if _is_pg(conn):
        conn.execute("CREATE SCHEMA IF NOT EXISTS evidence")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence.original_documents (
                sha256 TEXT PRIMARY KEY REFERENCES public.archive_files(id),
                size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
                exact_bytes BYTEA NOT NULL,
                created_at TEXT NOT NULL,
                CHECK(octet_length(exact_bytes) = size_bytes)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence.row_snapshots (
                file_id TEXT NOT NULL REFERENCES evidence.original_documents(sha256),
                parser_version TEXT NOT NULL,
                raw_rows_json TEXT NOT NULL,
                row_count INTEGER NOT NULL CHECK(row_count >= 0),
                parse_status TEXT NOT NULL,
                parse_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(file_id, parser_version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence.import_batches (
                upload_id TEXT PRIMARY KEY REFERENCES public.archive_uploads(id),
                file_id TEXT NOT NULL REFERENCES evidence.original_documents(sha256),
                original_name TEXT NOT NULL, category TEXT NOT NULL,
                period TEXT NOT NULL, source_system TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE OR REPLACE FUNCTION evidence.forbid_original_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'original evidence is immutable';
            END;
            $$ LANGUAGE plpgsql
        """)
        for name in ("original_documents", "row_snapshots", "import_batches"):
            conn.execute(
                f"CREATE OR REPLACE TRIGGER imm_{name} "
                f"BEFORE UPDATE OR DELETE ON evidence.{name} FOR EACH ROW "
                f"EXECUTE FUNCTION evidence.forbid_original_mutation()"
            )
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_original_documents (
                sha256 TEXT PRIMARY KEY REFERENCES archive_files(id),
                size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                exact_bytes BLOB NOT NULL,
                created_at TEXT NOT NULL,
                CHECK(length(exact_bytes) = size_bytes)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_row_snapshots (
                file_id TEXT NOT NULL REFERENCES evidence_original_documents(sha256),
                parser_version TEXT NOT NULL,
                raw_rows_json TEXT NOT NULL,
                row_count INTEGER NOT NULL CHECK(row_count >= 0),
                parse_status TEXT NOT NULL,
                parse_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(file_id, parser_version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_import_batches (
                upload_id TEXT PRIMARY KEY REFERENCES archive_uploads(id),
                file_id TEXT NOT NULL REFERENCES evidence_original_documents(sha256),
                original_name TEXT NOT NULL, category TEXT NOT NULL,
                period TEXT NOT NULL, source_system TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        for table in ("evidence_original_documents", "evidence_row_snapshots", "evidence_import_batches"):
            for action in ("UPDATE", "DELETE"):
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS imm_{table}_{action.lower()} "
                    f"BEFORE {action} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'original evidence is immutable'); END"
                )


def _raw_cells(binary: bytes, filename: str) -> tuple[str, list[list[str]], str]:
    extension = Path(filename).suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        return "binary_only", [], ""
    try:
        if extension == ".xlsx":
            rows = xlsx_reader.first_sheet_rows(binary)
        else:
            decoded = binary.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(decoded, newline=""), strict=True))
            if len(rows) > 10000:
                raise ValueError("CSV raw row preview exceeds 10,000 rows")
            if any(len(row) > 160 for row in rows):
                raise ValueError("CSV raw row preview exceeds 160 columns")
        return "parsed", rows, ""
    except (UnicodeError, ValueError, csv.Error) as exc:
        # The original bytes stay unchanged; parser improvements must generate
        # a new version, never overwrite the first parsing attempt.
        return "unparsed", [], str(exc)[:500]


def _budget(conn) -> int:
    configured = os.environ.get("WORKOS_EVIDENCE_DB_MAX_BYTES")
    limit = int(configured) if configured else (
        DEFAULT_PG_BUDGET if _is_pg(conn) else DEFAULT_SQLITE_BUDGET
    )
    if limit <= 0:
        raise ValueError("WORKOS_EVIDENCE_DB_MAX_BYTES must be a positive byte count")
    return limit


def store(
    conn, file_id: str, exact_bytes: bytes, upload_id: str,
    filename: str, category: str, period: str, source_system: str = "",
) -> dict:
    """Append a complete original and its separate, unmodified source rows.

    Must be called inside the same transaction that appends archive_uploads.
    The function refuses to store a partial original when the budget is hit.
    """
    if not exact_bytes or hashlib.sha256(exact_bytes).hexdigest() != file_id:
        raise ValueError("original evidence byte checksum mismatch")
    documents = _table(conn, "original_documents")
    snapshots = _table(conn, "row_snapshots")
    batches = _table(conn, "import_batches")
    existing = conn.execute(
        f"SELECT size_bytes,exact_bytes FROM {documents} WHERE sha256=?",
        (file_id,),
    ).fetchone()
    if existing:
        if existing["exact_bytes"] is None:
            raise ValueError("original already stored in R2; database bytes not available")
        old_bytes = bytes(existing["exact_bytes"])
        if len(old_bytes) != len(exact_bytes) or old_bytes != exact_bytes:
            raise ValueError("original evidence identity collision or stored bytes changed")
    else:
        used = conn.execute(
            f"SELECT COALESCE(SUM(size_bytes),0) AS used FROM {documents}"
        ).fetchone()["used"]
        if int(used) + len(exact_bytes) > _budget(conn):
            raise ValueError(
                "original evidence database capacity guard reached; "
                "retain the local/NAS original and configure a larger verified archive "
                "before importing financial records"
            )
        conn.execute(
            f"INSERT INTO {documents} (sha256,size_bytes,exact_bytes,created_at) "
            "VALUES(?,?,?,?)",
            (file_id, len(exact_bytes), exact_bytes, _stamp()),
        )
    previous = conn.execute(
        f"SELECT parser_version,parse_status,row_count FROM {snapshots} "
        "WHERE file_id=? AND parser_version=?",
        (file_id, PARSER_VERSION),
    ).fetchone()
    if previous:
        parse_status = previous["parse_status"]
        row_count = int(previous["row_count"])
    else:
        parse_status, raw_rows, parse_error = _raw_cells(exact_bytes, filename)
        row_count = len(raw_rows)
        # Keep *raw* source values; do not normalize dates, account numbers,
        # prices, tax, currency or order identifiers into finance fields here.
        snapshot_json = json.dumps(raw_rows, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            f"INSERT INTO {snapshots} "
            "(file_id,parser_version,raw_rows_json,row_count,parse_status,parse_error,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (file_id, PARSER_VERSION, snapshot_json, row_count, parse_status, parse_error, _stamp()),
        )
    conn.execute(
        f"INSERT INTO {batches} "
        "(upload_id,file_id,original_name,category,period,source_system,created_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(upload_id) DO NOTHING",
        (upload_id, file_id, filename, category, period, source_system, _stamp()),
    )
    return {
        "stored_in_database": True,
        "raw_rows_status": parse_status,
        "raw_rows_count": row_count,
        "database_content_sha256": file_id,
    }


def fetch(conn, file_id: str) -> bytes | None:
    row = conn.execute(
        f"SELECT exact_bytes,size_bytes FROM {_table(conn,'original_documents')} "
        "WHERE sha256=?", (file_id,),
    ).fetchone()
    if not row:
        return None
    if row["exact_bytes"] is None:
        return None  # R2-backed V42 records contain metadata, not BYTEA originals.
    data = bytes(row["exact_bytes"])
    if len(data) != int(row["size_bytes"]) or hashlib.sha256(data).hexdigest() != file_id:
        raise ValueError("database original evidence failed SHA-256 verification")
    return data


def source_rows(conn, file_id: str, offset: int = 0, limit: int = 100) -> dict | None:
    if offset < 0 or limit < 1 or limit > 200:
        raise ValueError("raw-row preview requires offset>=0 and limit between 1 and 200")
    row = conn.execute(
        f"SELECT raw_rows_json,row_count,parse_status,parse_error,parser_version "
        f"FROM {_table(conn,'row_snapshots')} "
        "WHERE file_id=? AND parser_version=?",
        (file_id, PARSER_VERSION),
    ).fetchone()
    if not row:
        return None
    values = json.loads(row["raw_rows_json"])
    return {
        "file_id": file_id, "parser_version": row["parser_version"],
        "parse_status": row["parse_status"], "parse_error": row["parse_error"],
        "total_rows": row["row_count"], "offset": offset,
        "rows": values[offset:offset + limit],
        "is_business_posted": False,
    }


def archived_status(conn, file_ids: list[str]) -> set[str]:
    """Fetch DB-backed originals in batches, avoiding one remote query per file."""
    if not file_ids:
        return set()
    result = set()
    table = _table(conn, "original_documents")
    unique = list(dict.fromkeys(file_ids))
    for start in range(0, len(unique), 200):
        chunk = unique[start:start+200]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT sha256 FROM {table} WHERE exact_bytes IS NOT NULL AND sha256 IN ({placeholders})",
            tuple(chunk),
        ):
            result.add(row["sha256"])
    return result
