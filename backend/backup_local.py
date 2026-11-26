#!/usr/bin/env python3
"""Work OS offline SQLite + original evidence backup.

Run from project root:
    python backend/backup_local.py --target /mnt/nas/work-os-backups
    python backend/backup_local.py --verify /mnt/nas/work-os-backups/backup-YYYY...
Does not connect to Neon or overwrite primary files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parent
DEFAULT_DB=ROOT/"data"/"workos.sqlite3"
DEFAULT_ARCHIVE=ROOT/"data"/"archive"


def digest_file(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb") as f:
        while block:=f.read(1024*1024):
            digest.update(block)
    return digest.hexdigest()


def checked_backup(db_source:Path,archive_root:Path,target_root:Path)->Path:
    db_source=db_source.resolve()
    archive_root=archive_root.resolve()
    target_root=target_root.resolve()
    if not db_source.is_file():
        raise FileNotFoundError("SQLite database does not exist")
    if target_root==archive_root or archive_root in target_root.parents:
        raise ValueError("backup target cannot be inside the primary archive")
    target_root.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name=f"backup-{stamp}-{uuid4().hex[:8]}"
    staging=target_root/(".staging-"+backup_name)
    staging.mkdir(mode=0o700)
    try:
        db_snapshot=staging/"workos.sqlite3"
        with sqlite3.connect(f"file:{db_source}?mode=ro",uri=True) as source,sqlite3.connect(db_snapshot) as dest:
            source.backup(dest)
        with sqlite3.connect(db_snapshot) as copied:
            check=copied.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0]!="ok":
                raise RuntimeError("backed up database integrity check failed")
            has_archive=copied.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archive_files'").fetchone()
            records=copied.execute("SELECT id,sha256,size_bytes,storage_key FROM archive_files").fetchall() if has_archive else []
            has_migrations=copied.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            version_row=copied.execute("SELECT MAX(version) FROM schema_migrations").fetchone() if has_migrations else None
            schema_version=int(version_row[0]) if version_row and version_row[0] is not None else None
        manifest={"created_at":datetime.now(timezone.utc).isoformat(),
                  "schema_version":schema_version,
                  "database_sha256":digest_file(db_snapshot),"documents":[]}
        for file_id,sha256,size,key in records:
            expected=f"{sha256[:2]}/{sha256[2:4]}/{sha256}"
            if key!=expected or file_id!=sha256:
                raise ValueError("archive metadata contains an invalid storage path")
            original=archive_root/expected
            if not original.is_file() or original.stat().st_size!=size or digest_file(original)!=sha256:
                raise RuntimeError(f"original document missing or corrupted: {sha256}")
            destination=staging/"archive"/expected
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(original,destination)
            if digest_file(destination)!=sha256:
                raise RuntimeError("backup copy failed content verification")
            manifest["documents"].append({"sha256":sha256,"size_bytes":size,"key":expected})
        (staging/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
        final=target_root/backup_name
        staging.rename(final)
        return final
    except Exception:
        shutil.rmtree(staging,ignore_errors=True)
        raise


def verify_backup(folder:Path)->dict:
    folder=folder.resolve()
    manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    database=folder/"workos.sqlite3"
    if digest_file(database)!=manifest["database_sha256"]:
        raise ValueError("backup SQLite checksum mismatch")
    with sqlite3.connect(f"file:{database}?mode=ro",uri=True) as copied:
        if copied.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise ValueError("backup SQLite integrity check failed")
    for document in manifest["documents"]:
        sha=document["sha256"]
        expected=f"{sha[:2]}/{sha[2:4]}/{sha}"
        if document["key"]!=expected:
            raise ValueError("invalid manifest file reference")
        path=folder/"archive"/expected
        if path.stat().st_size!=document["size_bytes"] or digest_file(path)!=sha:
            raise ValueError("backed up original evidence failed SHA-256 check")
    return {"files_verified":len(manifest["documents"]),"database_verified":True,
            "schema_version":manifest.get("schema_version")}


def main():
    p=argparse.ArgumentParser(description="Backup local Work OS SQLite and original evidence")
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument("--target",type=Path)
    group.add_argument("--verify",type=Path)
    p.add_argument("--db",type=Path,default=DEFAULT_DB)
    p.add_argument("--archive",type=Path,default=Path(os.environ.get("WORKOS_ARCHIVE_ROOT",DEFAULT_ARCHIVE)))
    args=p.parse_args()
    if args.verify:
        print(json.dumps(verify_backup(args.verify),ensure_ascii=False))
    else:
        created=checked_backup(args.db,args.archive,args.target)
        print(json.dumps({"backup":str(created),"verification":verify_backup(created)},ensure_ascii=False))


if __name__=="__main__":
    main()
