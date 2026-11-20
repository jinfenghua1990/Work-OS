"""Private, content-addressed archive for original source documents.

Local/NAS filesystem backend. The database stores metadata, not file contents.
This is an offline-development implementation, NOT a compliant WORM archive
or a remotely accessible service: the API must remain bound to loopback until
authentication and authorization are implemented.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

ALLOWED_EXTENSIONS = {".csv", ".xls", ".xlsx", ".pdf", ".ofd", ".xml", ".txt", ".zip"}
CATEGORIES = {"bank", "input_invoice", "output_invoice", "sales", "purchase", "expense", "contract", "other"}
DEFAULT_LIMIT = 50 * 1024 * 1024


def archive_root() -> Path:
    configured = os.environ.get("WORKOS_ARCHIVE_ROOT")
    return Path(configured) if configured else Path(__file__).resolve().parents[1] / "data" / "archive"


def safe_filename(filename: str) -> str:
    # A file name is presentation metadata only: never allow it into a path.
    filename = str(filename).replace("\\", "/").split("/")[-1].strip()
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    if not filename or filename.startswith(".") or len(filename) > 200:
        raise ValueError("invalid original filename")
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("unsupported original document extension")
    return filename


def validate_meta(filename: str, category: str, period: str) -> str:
    filename = safe_filename(filename)
    if category not in CATEGORIES:
        raise ValueError("invalid document category")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("period must use YYYY-MM")
    return filename


def ensure_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS archive_files (
            id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
            storage_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_uploads (
            id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES archive_files(id),
            original_name TEXT NOT NULL, category TEXT NOT NULL, period TEXT NOT NULL,
            source_system TEXT NOT NULL DEFAULT '', uploaded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_archive_uploads_period ON archive_uploads(period,category);
        CREATE TABLE IF NOT EXISTS archive_links (
            id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES archive_files(id),
            source_system TEXT NOT NULL, source_document_id TEXT NOT NULL,
            linked_at TEXT NOT NULL, UNIQUE(file_id,source_system,source_document_id)
        );
        CREATE INDEX IF NOT EXISTS ix_archive_links_source ON archive_links(source_system,source_document_id);
    """)


def ingest(conn, stream, size: int, filename: str, category: str, period: str, source_system: str = "") -> dict:
    filename = validate_meta(filename, category, period)
    limit = int(os.environ.get("WORKOS_ARCHIVE_MAX_BYTES", str(DEFAULT_LIMIT)))
    if size <= 0 or size > limit or limit <= 0:
        raise ValueError("document is empty or exceeds the configured upload limit")
    if len(source_system) > 100:
        raise ValueError("source system is too long")
    root = archive_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Temp file and hash must both be computed on the exact uploaded bytes.
    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=root)
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    remaining = size
    try:
        with os.fdopen(fd, "wb") as fh:
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("unexpected end of uploaded document")
                digest.update(block)
                fh.write(block)
                remaining -= len(block)
            fh.flush()
            os.fsync(fh.fileno())
        hex_digest = digest.hexdigest()
        key = f"{hex_digest[:2]}/{hex_digest[2:4]}/{hex_digest}"
        dest = root / key
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Hard-link without overwrite: duplicate content reuses the same object.
        # Never use a client-controlled filename as the destination path.
        try:
            os.link(tmp, dest)
            dest.chmod(0o600)
        except FileExistsError:
            if dest.stat().st_size != size:
                raise RuntimeError("archived file hash collision or corrupted archive")
        stamp = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO archive_files (id,sha256,size_bytes,storage_key,created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (hex_digest, hex_digest, size, key, stamp),
        )
        upload_id = str(uuid4())
        conn.execute(
            "INSERT INTO archive_uploads (id,file_id,original_name,category,period,source_system,uploaded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (upload_id, hex_digest, filename, category, period, source_system, stamp),
        )
        return {"id":hex_digest,"upload_id":upload_id,"sha256":hex_digest,
                "size_bytes":size,"original_name":filename,"category":category,
                "period":period,"duplicate_content":dest.stat().st_size == size and
                conn.execute("SELECT COUNT(*) FROM archive_uploads WHERE file_id=?", (hex_digest,)).fetchone()[0] > 1}
    finally:
        tmp.unlink(missing_ok=True)


def link_document(conn, file_id: str, source_system: str, source_document_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", file_id or ""):
        raise ValueError("invalid archive file ID")
    if not source_system or not source_document_id or len(source_system) > 100 or len(source_document_id) > 250:
        raise ValueError("source system and source document ID are required")
    if not conn.execute("SELECT 1 FROM archive_files WHERE id=?", (file_id,)).fetchone():
        raise ValueError("original archived document was not found")
    found = conn.execute(
        "SELECT id FROM archive_links WHERE file_id=? AND source_system=? AND source_document_id=?",
        (file_id, source_system, source_document_id),
    ).fetchone()
    if found:
        return {"id": found["id"], "file_id": file_id, "duplicate": True}
    link_id = str(uuid4())
    conn.execute(
        "INSERT INTO archive_links (id,file_id,source_system,source_document_id,linked_at) VALUES (?,?,?,?,?)",
        (link_id, file_id, source_system, source_document_id, datetime.now(timezone.utc).isoformat()),
    )
    return {"id":link_id,"file_id":file_id,"duplicate":False}


def fetch_file_metadata(conn, file_id: str):
    return conn.execute(
        "SELECT f.*,u.original_name FROM archive_files f JOIN archive_uploads u ON u.file_id=f.id "
        "WHERE f.id=? ORDER BY u.uploaded_at,u.id LIMIT 1", (file_id,)
    ).fetchone()


def open_verified_file(file_id: str, storage_key: str):
    if not re.fullmatch(r"[0-9a-f]{64}", file_id or ""):
        raise ValueError("invalid archive file ID")
    expected = f"{file_id[:2]}/{file_id[2:4]}/{file_id}"
    if storage_key != expected:
        raise ValueError("archive storage key does not match document digest")
    path = archive_root() / expected
    # Re-hash before serving to avoid returning silently corrupted evidence.
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    if h.hexdigest() != file_id:
        raise ValueError("archived original file failed SHA-256 verification")
    return path


def download_disposition(filename: str) -> str:
    return "attachment; filename*=UTF-8''" + quote(safe_filename(filename))
