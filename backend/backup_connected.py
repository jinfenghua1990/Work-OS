#!/usr/bin/env python3
"""Read-only Neon PostgreSQL dump and verified NAS original-file mirror.

No overwrite, no remote data writes and no automatic pruning. A dump is
marked verified only after pg_restore can list its contents and every archive
file on NAS matches immutable SHA-256 metadata.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
from uuid import uuid4


def pg_environment(url:str,source:dict|None=None)->dict:
    if not url:
        raise ValueError("WORKOS_DATABASE_URL must be configured")
    parsed=urlsplit(url)
    if parsed.scheme not in ("postgres","postgresql"):
        raise ValueError("shared backup requires PostgreSQL")
    if not all((parsed.hostname,parsed.path,parsed.username,parsed.password)):
        raise ValueError("PostgreSQL host, database, user and password are required")
    host=parsed.hostname
    if "-pooler." in host:
        raise ValueError("pg_dump needs the direct Neon database endpoint, not a pooler")
    opts=parse_qs(parsed.query)
    sslmode=opts.get("sslmode",["require"])[0].lower()
    if sslmode not in ("require","verify-ca","verify-full"):
        raise ValueError("backup connection must require TLS")
    env=dict(source if source is not None else os.environ)
    env.update({
        "PGHOST":host,
        "PGPORT":str(parsed.port or 5432),
        "PGUSER":unquote(parsed.username),
        "PGDATABASE":unquote(parsed.path.lstrip("/")),
        "PGPASSWORD":unquote(parsed.password),
        "PGSSLMODE":sslmode,
    })
    if "channel_binding" in opts:
        env["PGCHANNELBINDING"]=opts["channel_binding"][0]
    return env


def check_path(path:str,name:str)->Path:
    if not path:
        raise ValueError(name+" is not configured")
    root=Path(path).expanduser()
    if not root.is_absolute():
        raise ValueError(name+" must be an absolute NAS/local path")
    return root.resolve()


def sha256_file(path:Path)->str:
    hash_sum=hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda:source.read(1024*1024),b""):
            hash_sum.update(block)
    return hash_sum.hexdigest()


def validate_nas_mirror(root:Path,rows:list[dict])->dict:
    if not root.is_dir():
        raise ValueError("original-file NAS mirror is not accessible")
    verified=0
    bytes_total=0
    for item in rows:
        sha=str(item["sha256"]).lower()
        file_id=str(item["id"])
        size=int(item["size_bytes"])
        key=str(item["storage_key"])
        if not re.fullmatch("[0-9a-f]{64}",sha) or file_id!=sha:
            raise ValueError("archive metadata has an invalid SHA-256 identity")
        if key!=sha[:2]+"/"+sha[2:4]+"/"+sha:
            raise ValueError("archive metadata has a non-canonical storage key")
        original=root/key
        if not original.is_file():
            raise ValueError("missing NAS original: "+sha)
        if original.stat().st_size!=size:
            raise ValueError("NAS original size mismatch: "+sha)
        if sha256_file(original)!=sha:
            raise ValueError("NAS original SHA-256 mismatch: "+sha)
        verified+=1;bytes_total+=size
    return {"verified_original_files":verified,"verified_original_bytes":bytes_total}


def source_inventory(url:str)->dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ValueError("psycopg is required for connected backup") from exc
    tls_mode=parse_qs(urlsplit(url).query).get("sslmode",["require"])[0]
    if tls_mode not in ("require","verify-ca","verify-full"):
        raise ValueError("source inventory requires TLS")
    with psycopg.connect(url,sslmode=tls_mode,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT id,sha256,size_bytes,storage_key FROM public.archive_files ORDER BY id"
            )
            files=[dict(item) for item in cur.fetchall()]
            cur.execute("SELECT MAX(version) AS version FROM public.schema_migrations")
            version=int(cur.fetchone()["version"] or 0)
            cur.execute("SELECT COUNT(*) AS count FROM public.journal_entries")
            journal_count=int(cur.fetchone()["count"])
    return {"schema_version":version,"journal_count":journal_count,"files":files}


def _run_checked(args:list[str],env:dict,timeout:int)->None:
    try:
        result=subprocess.run(args,env=env,text=True,capture_output=True,
                              timeout=timeout,check=False)
    except (FileNotFoundError,subprocess.TimeoutExpired) as exc:
        raise RuntimeError(args[0]+" unavailable or timed out") from exc
    if result.returncode!=0:
        # postgres client output may contain host/user info: no passwords or
        # raw stderr are written into a report or returned to the UI.
        raise RuntimeError(args[0]+" exited with an error (details hidden)")


def backup()->dict:
    if os.environ.get("WORKOS_DB_BACKEND","postgres").strip().lower()!="postgres":
        raise ValueError("connected NAS backup must never use SQLite")
    url=os.environ.get("WORKOS_DATABASE_URL","").strip()
    env=pg_environment(url)
    original_root=check_path(os.environ.get("WORKOS_ARCHIVE_ROOT",""),"WORKOS_ARCHIVE_ROOT")
    backup_root=check_path(os.environ.get("WORKOS_BACKUP_ROOT",""),"WORKOS_BACKUP_ROOT")
    if original_root==backup_root:
        raise ValueError("backup directory must be separate from original-file mirror")
    before=source_inventory(url)
    nas=validate_nas_mirror(original_root,before["files"])
    backup_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    folder="workos-"+stamp+"-"+uuid4().hex[:8]
    temp=Path(tempfile.mkdtemp(prefix=".pending-",dir=backup_root))
    try:
        dump=temp/"database.dump"
        _run_checked(["pg_dump","--format=custom","--no-owner",
                      "--no-privileges","--file",str(dump)],env,900)
        if not dump.is_file() or dump.stat().st_size==0:
            raise ValueError("pg_dump produced an empty or missing archive")
        dump.chmod(0o600)
        _run_checked(["pg_restore","--list",str(dump)],env,120)
        after=source_inventory(url)
        if before!=after:
            raise RuntimeError("source archive index or journal state changed during backup; retry")
        result={"status":"database_dump_and_nas_mirror_verified",
                "created_at":datetime.now(timezone.utc).isoformat(),
                "schema_version":before["schema_version"],
                "journal_count":before["journal_count"],
                "database_dump_sha256":sha256_file(dump),
                "database_dump_bytes":dump.stat().st_size,
                **nas,
                "originals_root":str(original_root),
                "original_files":before["files"],
                "recovery_restore_tested":False,
                "r2_remote_verified":False}
        (temp/"manifest.json").write_text(
            json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2),
            encoding="utf-8",
        )
        (temp/"manifest.json").chmod(0o600)
        target=backup_root/folder
        if target.exists():
            raise RuntimeError("backup target unexpectedly exists")
        temp.rename(target)
        return {k:v for k,v in result.items() if k not in ("original_files","originals_root")} | {
            "backup_folder":str(target)
        }
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def verify_saved_backup(folder:Path)->dict:
    manifest_path=folder/"manifest.json"
    dump=folder/"database.dump"
    if not manifest_path.is_file() or not dump.is_file():
        raise ValueError("missing manifest or database.dump")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(dump)!=manifest.get("database_dump_sha256"):
        raise ValueError("database dump has changed or was corrupted")
    root=check_path(manifest.get("originals_root",""),"originals_root")
    nas=validate_nas_mirror(root,manifest.get("original_files",[]))
    if nas["verified_original_files"]!=manifest.get("verified_original_files"):
        raise ValueError("original-file count changed since backup")
    return {"status":"backup_checksums_verified",
            "backup_folder":str(folder),
            "database_dump_bytes":dump.stat().st_size,
            **nas,"recovery_restore_tested":False}


def load_private_env():
    """Load local, untracked .env only when running as a command."""
    if os.environ.get("WORKOS_LOAD_ENV_FILE","1").lower() in ("0","false","no"):
        return
    env_file=Path(__file__).resolve().parents[1]/".env"
    if not env_file.is_file():return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:continue
        name,value=line.split("=",1)
        name=name.strip()
        if name and re.fullmatch("[A-Za-z_][A-Za-z_0-9]*",name):
            os.environ.setdefault(name,value.strip().strip("\"'"))


def main():
    load_private_env()
    parser=argparse.ArgumentParser(description="Work OS connected Neon + NAS backup")
    parser.add_argument("--verify",metavar="BACKUP_FOLDER")
    args=parser.parse_args()
    result=verify_saved_backup(Path(args.verify).resolve()) if args.verify else backup()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
