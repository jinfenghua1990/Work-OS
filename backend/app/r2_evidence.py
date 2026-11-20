"""Cloudflare R2 immutable primary archive for original Work OS documents.

PostgreSQL preserves immutable SHA-256 metadata and source-row snapshots.
R2 holds exact bytes. A separate NAS filesystem remains the backup.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from datetime import datetime, timezone
import evidence_db

def mode(conn) -> str:
    setting=os.environ.get("WORKOS_EVIDENCE_STORAGE","").strip().lower()
    if not setting: setting="r2" if getattr(conn,"is_postgres",False) else "database"
    if setting not in ("r2","database"):
        raise ValueError("WORKOS_EVIDENCE_STORAGE must be r2 or database")
    if setting=="r2" and not getattr(conn,"is_postgres",False):
        raise ValueError("R2 primary storage requires the PostgreSQL backend")
    return setting

def _table(conn) -> str:
    return "evidence.r2_locations" if getattr(conn,"is_postgres",False) else "evidence_r2_locations"

def ensure_tables(conn) -> None:
    if getattr(conn,"is_postgres",False):
        conn.execute("ALTER TABLE evidence.original_documents ALTER COLUMN exact_bytes DROP NOT NULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS evidence.r2_locations (
            sha256 TEXT PRIMARY KEY REFERENCES evidence.original_documents(sha256),
            bucket TEXT NOT NULL,
            object_key TEXT NOT NULL UNIQUE,
            size_bytes BIGINT NOT NULL CHECK(size_bytes>0),
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE OR REPLACE TRIGGER imm_r2_locations
            BEFORE UPDATE OR DELETE ON evidence.r2_locations FOR EACH ROW
            EXECUTE FUNCTION evidence.forbid_original_mutation()""")
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS evidence_r2_locations (
            sha256 TEXT PRIMARY KEY REFERENCES evidence_original_documents(sha256),
            bucket TEXT NOT NULL,
            object_key TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL CHECK(size_bytes>0),
            created_at TEXT NOT NULL
        )""")
        for action in ("UPDATE","DELETE"):
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS imm_evidence_r2_locations_"+action.lower()+
                " BEFORE "+action+" ON evidence_r2_locations BEGIN "
                "SELECT RAISE(ABORT, 'original evidence is immutable'); END"
            )

def _config():
    account=os.environ.get("WORKOS_R2_ACCOUNT_ID","").strip()
    bucket=os.environ.get("WORKOS_R2_BUCKET","").strip()
    key_id=os.environ.get("WORKOS_R2_ACCESS_KEY_ID","").strip()
    secret=os.environ.get("WORKOS_R2_SECRET_ACCESS_KEY","").strip()
    if not all((account,bucket,key_id,secret)):
        raise ValueError("R2 credentials/bucket not configured; file was not posted")
    if not re.fullmatch(r"[0-9a-f]{32}",account,re.I):
        raise ValueError("invalid R2 account ID")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,62}",bucket):
        raise ValueError("invalid R2 bucket name")
    return account,bucket,key_id,secret

def _client(account,key_id,secret):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ValueError("R2 storage needs boto3 installed") from exc
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4",
                      s3={"addressing_style":"path"},
                      connect_timeout=10,read_timeout=60,retries={"max_attempts":2}),
    )

def _key(sha256):
    if not re.fullmatch(r"[0-9a-f]{64}",sha256):
        raise ValueError("invalid source document SHA-256")
    return f"work-os/originals/{sha256[:2]}/{sha256[2:4]}/{sha256}"

def _read_checked(client,bucket,key,sha256,size):
    response=client.get_object(Bucket=bucket,Key=key)
    stream=response["Body"]
    try:
        chunks=[]
        total=0
        while True:
            part=stream.read(min(1024*1024,size+1-total))
            if not part: break
            total+=len(part)
            if total>size: raise ValueError("R2 original exceeded expected size")
            chunks.append(part)
        payload=b"".join(chunks)
    finally:
        stream.close()
    if len(payload)!=size or hashlib.sha256(payload).hexdigest()!=sha256:
        raise ValueError("R2 original SHA-256 / size verification failed")
    return payload

def _store_object(sha256,payload):
    account,bucket,key_id,secret=_config()
    client=_client(account,key_id,secret)
    key=_key(sha256)
    try:
        client.put_object(
            Bucket=bucket,Key=key,Body=payload,IfNoneMatch="*",
            ContentType="application/octet-stream",Metadata={"sha256":sha256},
        )
    except Exception as exc:
        code=getattr(exc,"response",{}).get("Error",{}).get("Code","")
        if code not in ("PreconditionFailed","412"):
            raise ValueError("R2 immutable upload failed; no financial posting") from exc
    try:
        _read_checked(client,bucket,key,sha256,len(payload))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("R2 read-back verification failed; no financial posting") from exc
    return bucket,key

def location(conn,file_id):
    return conn.execute(
        f"SELECT sha256,bucket,object_key,size_bytes FROM {_table(conn)} WHERE sha256=?",
        (file_id,),
    ).fetchone()

def store(conn,file_id,payload,upload_id,filename,category,period,source_system=""):
    if mode(conn)!="r2":
        raise ValueError("R2 evidence store requires R2 mode")
    if not payload or hashlib.sha256(payload).hexdigest()!=file_id:
        raise ValueError("source original SHA-256 mismatch")
    # Verify remote write/read before the SQL business transaction can commit.
    bucket,key=_store_object(file_id,payload)
    documents=evidence_db._table(conn,"original_documents")
    snapshots=evidence_db._table(conn,"row_snapshots")
    batches=evidence_db._table(conn,"import_batches")
    stamp=datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"INSERT INTO {documents} (sha256,size_bytes,exact_bytes,created_at) "
        "VALUES(?,?,NULL,?) ON CONFLICT(sha256) DO NOTHING",
        (file_id,len(payload),stamp),
    )
    original=conn.execute(
        f"SELECT size_bytes FROM {documents} WHERE sha256=?",(file_id,)
    ).fetchone()
    if original is None or int(original["size_bytes"])!=len(payload):
        raise ValueError("SQL original index does not match R2 original")
    conn.execute(
        f"INSERT INTO {_table(conn)} (sha256,bucket,object_key,size_bytes,created_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
        (file_id,bucket,key,len(payload),stamp),
    )
    stored=location(conn,file_id)
    if not stored or (stored["bucket"],stored["object_key"],int(stored["size_bytes"]))!=(bucket,key,len(payload)):
        raise ValueError("immutable R2 object locator mismatch")
    existing=conn.execute(
        f"SELECT parse_status,row_count FROM {snapshots} WHERE file_id=? AND parser_version=?",
        (file_id,evidence_db.PARSER_VERSION),
    ).fetchone()
    if existing:
        parse_status,row_count=existing["parse_status"],int(existing["row_count"])
    else:
        parse_status,rows,error=evidence_db._raw_cells(payload,filename)
        row_count=len(rows)
        conn.execute(
            f"INSERT INTO {snapshots} "
            "(file_id,parser_version,raw_rows_json,row_count,parse_status,parse_error,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (file_id,evidence_db.PARSER_VERSION,
             json.dumps(rows,ensure_ascii=False,separators=(",",":")),
             row_count,parse_status,error,stamp),
        )
    conn.execute(
        f"INSERT INTO {batches} "
        "(upload_id,file_id,original_name,category,period,source_system,created_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(upload_id) DO NOTHING",
        (upload_id,file_id,filename,category,period,source_system,stamp),
    )
    return {"stored_in_r2":True,"stored_in_database":False,
            "raw_rows_status":parse_status,"raw_rows_count":row_count,
            "database_content_sha256":file_id}

def fetch(conn,file_id):
    item=location(conn,file_id)
    if item is None: return None
    account,configured_bucket,key_id,secret=_config()
    if item["bucket"]!=configured_bucket or item["object_key"]!=_key(file_id):
        raise ValueError("R2 locator does not match configured bucket or canonical key")
    try:
        return _read_checked(
            _client(account,key_id,secret),item["bucket"],item["object_key"],
            file_id,int(item["size_bytes"]),
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("R2 original unavailable; do not substitute unverified data") from exc

def archived_status(conn,file_ids):
    found=set()
    ids=list(dict.fromkeys(file_ids))
    for n in range(0,len(ids),200):
        chunk=ids[n:n+200]
        placeholders=",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT sha256 FROM {_table(conn)} WHERE sha256 IN ({placeholders})",
            tuple(chunk),
        ):
            found.add(row["sha256"])
    return found
