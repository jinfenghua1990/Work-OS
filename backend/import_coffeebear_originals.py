#!/usr/bin/env python3
"""Read-only-first CoffeeBear historical originals importer for Work OS R2.

Requires access to the original Mac/NAS files. Never reconstruct originals
from ledger rows. No invoice/bank/accounting postings are created by this tool.

Dry run (default):
  COFFEEBEAR_DATABASE_URL=... python backend/import_coffeebear_originals.py \
    --map-path '/Users/gino/ecommerce-workspace-data/production=/Volumes/NAS/production'
Apply after every original validates and R2 credentials have been configured:
  WORKOS_COFFEEBEAR_IMPORT_ALLOW=1 python backend/import_coffeebear_originals.py --apply ...
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent/"app"))
import server
import original_archive
import r2_evidence

_ALLOWED_CATEGORIES=original_archive.CATEGORIES

def legacy_manifest(url: str) -> list[dict]:
    if not url:
        raise ValueError("COFFEEBEAR_DATABASE_URL or --manifest is required")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install backend/requirements.txt before reading the old database") from exc
    tables={
        "archive_files":(
            "SELECT id::text AS source_id,original_name,stored_path,size AS size_bytes,"
            "sha256,category,period_year,period_month,created_at FROM archive_files"
        ),
        "tax_invoice_imports":(
            "SELECT id::text AS source_id,original_name,stored_path,size AS size_bytes,"
            "sha256,'other'::text AS category,period_year,period_month,created_at "
            "FROM tax_invoice_imports"
        ),
        "jackyun_file_imports":(
            "SELECT id::text AS source_id,original_name,stored_path,size AS size_bytes,"
            "sha256,'purchase'::text AS category,NULL::integer AS period_year,"
            "NULL::integer AS period_month,created_at FROM jackyun_file_imports"
        ),
        "alibaba1688_file_imports":(
            "SELECT id::text AS source_id,original_name,stored_path,size AS size_bytes,"
            "sha256,'purchase'::text AS category,NULL::integer AS period_year,"
            "NULL::integer AS period_month,created_at FROM alibaba1688_file_imports"
        ),
    }
    records=[]
    with psycopg.connect(url,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            for name,query in tables.items():
                cur.execute(query)
                for row in cur.fetchall():
                    row=dict(row)
                    row["source_table"]=name
                    # Snapshot to portable text metadata without changing file bytes.
                    row["created_at"]=row["created_at"].isoformat() if row["created_at"] else None
                    records.append(row)
    return records

def relocated(path: str, mappings: list[str]) -> Path:
    path=str(path or "")
    for item in mappings:
        if "=" not in item: raise ValueError("--map-path must be old-prefix=new-prefix")
        old,new=item.split("=",1)
        if not old or not new: raise ValueError("path mappings cannot be empty")
        if path==old or path.startswith(old.rstrip("/")+"/"):
            path=new.rstrip("/")+path[len(old.rstrip("/")):]
            break
    return Path(path)

def period_of(record: dict) -> str:
    try:
        year=int(record.get("period_year") or 0)
        month=int(record.get("period_month") or 0)
    except (ValueError,TypeError):
        year=month=0
    if 2000<=year<=2100 and 1<=month<=12:
        return f"{year:04d}-{month:02d}"
    date=str(record.get("created_at") or "")
    try:
        datetime.fromisoformat(date.replace("Z","+00:00"))
        return date[:7]
    except ValueError as exc:
        raise ValueError("missing valid source period; do not invent dates") from exc

def inspect(records: list[dict], mappings: list[str]) -> dict:
    plans=[]
    issues=[]
    for r in records:
        ref=f'{r.get("source_table","unknown")}:{r.get("source_id","?")}'
        try:
            if r.get("source_table") not in (
                "archive_files","tax_invoice_imports","jackyun_file_imports","alibaba1688_file_imports"
            ):
                raise ValueError("unknown source table")
            path=relocated(r["stored_path"],mappings)
            if not path.is_file(): raise ValueError("source file not accessible on this machine")
            size=path.stat().st_size
            if not size: raise ValueError("original file empty")
            expected=str(r.get("sha256") or "").lower()
            h=hashlib.sha256()
            with path.open("rb") as stream:
                for part in iter(lambda:stream.read(1024*1024),b""):
                    h.update(part)
            if h.hexdigest()!=expected: raise ValueError("original SHA-256 mismatch")
            recorded_size=int(r.get("size_bytes") or 0)
            if recorded_size and recorded_size!=size:
                raise ValueError("original file size mismatch")
            filename=original_archive.safe_filename(r["original_name"])
            period=period_of(r)
            cat=str(r.get("category") or "other")
            if cat not in _ALLOWED_CATEGORIES: cat="other"
            plans.append({"source":ref,"path":str(path),"sha256":expected,"bytes":size,
                          "original_name":filename,"period":period,"category":cat,
                          "size_unrecorded":not bool(recorded_size)})
        except (KeyError,TypeError,ValueError,OSError) as exc:
            issues.append({"source":ref,"error":str(exc)})
    return {"records":len(records),"verifiable":len(plans),
            "unique_sha256":len({p["sha256"] for p in plans}),
            "unknown_historical_sizes":sum(p["size_unrecorded"] for p in plans),
            "issues":issues,"plans":plans}

def run(records, mappings, apply=False) -> dict:
    report=inspect(records,mappings)
    if not apply:
        return {k:v for k,v in report.items() if k!="plans"} | {"mode":"dry_run"}
    if os.environ.get("WORKOS_COFFEEBEAR_IMPORT_ALLOW")!="1":
        raise ValueError("apply requires WORKOS_COFFEEBEAR_IMPORT_ALLOW=1")
    if report["issues"] or report["verifiable"]!=report["records"]:
        raise RuntimeError("refusing partial import: verify every original first")
    if server.DB_BACKEND!="postgres":
        raise ValueError("CoffeeBear migration target must be PostgreSQL Work OS")
    copied=0
    previously=0
    with server.db() as conn:
        version=conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
        if int(version)!=server.LATEST_SCHEMA_VERSION:
            raise RuntimeError("target Work OS schema must be current")
        if r2_evidence.mode(conn)!="r2":
            raise RuntimeError("R2 storage mode is required")
    for item in report["plans"]:
        source_system="coffeebear."+item["source"].split(":",1)[0]
        source_id=item["source"].split(":",1)[1]
        with server.db() as conn:
            if conn.execute(
                "SELECT 1 FROM archive_links WHERE source_system=? AND source_document_id=? LIMIT 1",
                (source_system,source_id),
            ).fetchone():
                previously+=1
                continue
            conn.execute("BEGIN IMMEDIATE")
            with Path(item["path"]).open("rb") as stream:
                result=original_archive.ingest(
                    conn,stream,item["bytes"],item["original_name"],item["category"],
                    item["period"],source_system,
                )
            original_archive.link_document(conn,result["id"],source_system,source_id)
        copied+=1
    return {k:v for k,v in report.items() if k!="plans"} | {
        "mode":"applied","copied_upload_batches":copied,
        "previously_linked":previously,
        "accounting_rows_created":0,
    }

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--manifest",help="offline JSON list exported from legacy read-only database")
    parser.add_argument("--map-path",action="append",default=[],help="old absolute prefix=new accessible NAS prefix")
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    if args.manifest:
        records=json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if not isinstance(records,list):raise ValueError("manifest must be a JSON list")
    else:
        records=legacy_manifest(os.environ.get("COFFEEBEAR_DATABASE_URL",""))
    report=run(records,args.map_path,apply=args.apply)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if report.get("issues"):
        raise SystemExit(2)

if __name__=="__main__":
    main()
