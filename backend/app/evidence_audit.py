"""Read-only verification of original evidence and its accounting references.

This checks the bytes currently stored on the local/NAS archive filesystem.
It does NOT establish off-site backup, WORM compliance, or authenticity of
the issuing institution's bank statement or tax invoice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import original_archive
import evidence_db
import r2_evidence

BUSINESS_SOURCES = {
    "coffee.sale": ("sale_lines", "source_row_key"),
    "coffee.refund": ("sales_refunds", "id"),
    "coffee.purchase": ("purchases", "id"),
    "bike.sale": ("bike_sales", "id"),
    "finance.invoice": ("invoices", "id"),
    "finance.bank_transaction": ("bank_transactions", "id"),
}

MAX_FILES = 2000
MAX_LINKS = 10000
MAX_ISSUES = 30


def _count(conn, table: str) -> int:
    # All table names come from constants; no user input reaches SQL identifiers.
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def audit_evidence(conn) -> dict:
    file_total = _count(conn, "archive_files")
    upload_total = _count(conn, "archive_uploads")
    link_total = _count(conn, "archive_links")
    files = conn.execute(
        "SELECT id,storage_key,size_bytes FROM archive_files ORDER BY id LIMIT ?",
        (MAX_FILES,)
    ).fetchall()
    links = conn.execute(
        "SELECT file_id,source_system,source_document_id FROM archive_links "
        "ORDER BY file_id,source_system,source_document_id LIMIT ?",
        (MAX_LINKS,)
    ).fetchall()

    missing = 0
    corrupted = 0
    missing_database_originals = 0
    corrupted_database_originals = 0
    missing_r2_originals = 0
    corrupted_r2_originals = 0
    broken_links = 0
    issues: list[dict] = []
    for item in files:
        # Verify the R2 original by re-reading all bytes and SHA-256.
        # Legacy V41 originals are verified from BYTEA instead.
        try:
            if r2_evidence.location(conn,item["id"]):
                r2_evidence.fetch(conn,item["id"])
            else:
                database_original=evidence_db.fetch(conn,item["id"])
                if database_original is None:
                    missing_r2_originals += 1
                    missing_database_originals += 1
                    if len(issues)<MAX_ISSUES:
                        issues.append({"type":"original_not_backfilled_to_r2","file_id":item["id"]})
                else:
                    # Legacy originals remain readable, but need an R2 copy.
                    if r2_evidence.mode(conn)=="r2":
                        missing_r2_originals += 1
                        if len(issues)<MAX_ISSUES:
                            issues.append({"type":"legacy_original_needs_r2_copy","file_id":item["id"]})
        except (ValueError,OSError):
            if r2_evidence.location(conn,item["id"]):
                corrupted_r2_originals += 1
                if len(issues)<MAX_ISSUES:
                    issues.append({"type":"r2_original_missing_or_corrupted","file_id":item["id"]})
            else:
                corrupted_database_originals += 1
                if len(issues)<MAX_ISSUES:
                    issues.append({"type":"invalid_database_original","file_id":item["id"]})
        try:
            path = original_archive.open_verified_file(item["id"],item["storage_key"])
            if path.stat().st_size != item["size_bytes"]:
                raise ValueError("saved size differs from archive index")
        except FileNotFoundError:
            missing += 1
            if len(issues)<MAX_ISSUES:
                issues.append({"type":"missing_file","file_id":item["id"]})
        except (ValueError, OSError):
            corrupted += 1
            if len(issues)<MAX_ISSUES:
                issues.append({"type":"invalid_or_corrupted_file","file_id":item["id"]})
    checked_links = 0
    for link in links:
        source = link["source_system"]
        if source not in BUSINESS_SOURCES:
            continue
        checked_links += 1
        table, column = BUSINESS_SOURCES[source]
        present = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1",
            (link["source_document_id"],)
        ).fetchone()
        if not present:
            broken_links += 1
            if len(issues)<MAX_ISSUES:
                issues.append({"type":"missing_business_record",
                               "file_id":link["file_id"],
                               "source_system":source,
                               "source_document_id":link["source_document_id"]})
    without_link = int(conn.execute(
        "SELECT COUNT(*) FROM archive_files f "
        "WHERE NOT EXISTS (SELECT 1 FROM archive_links l WHERE l.file_id=f.id)"
    ).fetchone()[0])
    # Historic financial transactions may predate the archive subsystem.
    # Report them as unlinked records, never as evidence of lost originals.
    unlinked_business: dict[str,int] = {}
    for source,(table,column) in BUSINESS_SOURCES.items():
        missing_count=int(conn.execute(
            f"SELECT COUNT(*) FROM {table} b WHERE NOT EXISTS ("
            "SELECT 1 FROM archive_links l "
            f"WHERE l.source_system=? AND l.source_document_id=b.{column})",
            (source,)
        ).fetchone()[0])
        unlinked_business[source]=missing_count
    unlinked_total=sum(unlinked_business.values())
    complete = file_total<=MAX_FILES and link_total<=MAX_LINKS
    status = ("partial" if not complete else
              "needs_attention" if missing or corrupted or missing_database_originals or corrupted_database_originals or missing_r2_originals or corrupted_r2_originals or broken_links or unlinked_total else "passed")
    return {
        "checked_at":datetime.now(timezone.utc).isoformat(),
        "status":status,
        "complete":complete,
        "file_total":file_total,
        "checked_files":len(files),
        "upload_total":upload_total,
        "link_total":link_total,
        "checked_known_business_links":checked_links,
        "missing_files":missing,
        "corrupted_files":corrupted,
        "missing_database_originals":missing_database_originals,
        "corrupted_database_originals":corrupted_database_originals,
        "missing_r2_originals":missing_r2_originals,
        "corrupted_r2_originals":corrupted_r2_originals,
        "broken_business_links":broken_links,
        "files_without_business_links":without_link,
        "business_records_without_evidence":unlinked_total,
        "business_without_evidence_by_type":unlinked_business,
        "issues":issues,
        "note":"R2 完整原件下载后校验 SHA-256，V41 老原件可从数据库读取；"
               "NAS 镜像独立核验。真实 R2 可用性及电子档案合规需单独验收；"
               "缺失的历史原文件不能用账务数据重建。",

    }
