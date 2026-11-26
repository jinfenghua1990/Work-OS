#!/usr/bin/env python3
"""Backfill earlier original-document archive files into the isolated SQL area.

Default is a read-only inventory. The --apply mode is explicit and does not
alter financial transactions, invoice values, bank matching or posted journals.

Examples:
  python backend/backfill_evidence_db.py
  WORKOS_EVIDENCE_BACKFILL_ALLOW=1 python backend/backfill_evidence_db.py --apply

Requires the app schema to already be at server.LATEST_SCHEMA_VERSION (currently
46); this script never migrates. Original file bytes are never guessed from
parsed sales, invoices or database ledger amounts.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import evidence_db
import r2_evidence
import original_archive
import server


def migrate(apply: bool = False) -> dict:
    if apply and os.environ.get("WORKOS_EVIDENCE_BACKFILL_ALLOW") != "1":
        raise ValueError("backfill --apply requires WORKOS_EVIDENCE_BACKFILL_ALLOW=1")
    with server.db() as conn:
        version=conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM schema_migrations").fetchone()["v"]
        if int(version) != server.LATEST_SCHEMA_VERSION:
            raise RuntimeError(
                "database schema version is not current; migration must be reviewed separately"
            )
        records=conn.execute(
            "SELECT f.id,f.storage_key,f.size_bytes,u.id AS upload_id,u.original_name,"
            "u.category,u.period,u.source_system FROM archive_files f "
            "JOIN archive_uploads u ON u.file_id=f.id ORDER BY f.id,u.uploaded_at,u.id"
        ).fetchall()
        missing=[]
        corrupt=[]
        already=0
        candidates=[]
        for row in records:
            file_id=row["id"]
            in_r2 = r2_evidence.location(conn,file_id) if r2_evidence.mode(conn)=="r2" else None
            existing=conn.execute(
                f"SELECT sha256 FROM {evidence_db._table(conn,'original_documents')} WHERE sha256=?",
                (file_id,),
            ).fetchone()
            if in_r2 or (existing and r2_evidence.mode(conn)!="r2"):
                already+=1
            path=original_archive.archive_root() / row["storage_key"]
            if not path.is_file():
                # If the database already has the exact original, recover
                # from it instead of guessing the bytes from business tables.
                binary=evidence_db.fetch(conn,file_id) if existing else None
                if binary is None and in_r2:
                    binary=r2_evidence.fetch(conn,file_id)
                if binary is None:
                    missing.append(file_id)
                    continue
            else:
                binary=path.read_bytes()
            if len(binary) != int(row["size_bytes"]) or hashlib.sha256(binary).hexdigest() != file_id:
                corrupt.append(file_id)
                continue
            candidates.append((row,binary))
        if apply and (missing or corrupt):
            raise RuntimeError(
                f"refusing partial archive backfill: {len(missing)} missing and "
                f"{len(corrupt)} hash-invalid originals"
            )
        completed=0
        if apply:
            # One atomic database transaction for all metadata changes; the
            # existing original files are never modified or deleted.
            conn.execute("BEGIN IMMEDIATE")
            for row,binary in candidates:
                persist=r2_evidence.store if r2_evidence.mode(conn)=="r2" else evidence_db.store
                persist(
                    conn,row["id"],binary,row["upload_id"],
                    row["original_name"],row["category"],row["period"],row["source_system"]
                )
                completed+=1
    return {
        "mode":"applied" if apply else "read_only",
        "existing_upload_records":len(records),
        "already_copied_file_uploads":already,
        "verifiable_original_uploads":len(candidates),
        "copied_uploads":completed,
        "missing_originals":len(missing),
        "hash_invalid_originals":len(corrupt),
        "errors_do_not_reconstruct_missing_bytes":True,
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true",help="explicitly copy verified originals into SQL")
    args=parser.parse_args()
    print(migrate(apply=args.apply))


if __name__=="__main__":
    main()
