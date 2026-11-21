"""Start the local Work OS UI API against the one shared Neon PostgreSQL store.

Unlike run_offline.py this command refuses silent SQLite fallback. It performs
no schema migrations, backups, or other destructive administration actions.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

os.environ.setdefault("WORKOS_DB_BACKEND","postgres")
os.environ.setdefault("WORKOS_EVIDENCE_STORAGE","r2")
os.environ.setdefault("WORKOS_HOST","127.0.0.1")
os.environ.setdefault("WORKOS_PORT","8106")
sys.path.insert(0,str(Path(__file__).resolve().parent/"app"))
import server

def check_configuration():
    if server.DB_BACKEND!="postgres":
        raise RuntimeError("共享经营账套禁止回退到 SQLite；请检查 WORKOS_DB_BACKEND")
    if not server.DATABASE_URL or not server.DATABASE_URL.startswith(("postgresql://","postgres://")):
        raise RuntimeError("请在私有 .env 配置 WORKOS_DATABASE_URL")
    if os.environ.get("WORKOS_EVIDENCE_STORAGE","r2").strip().lower()!="r2":
        raise RuntimeError("共享模式原始文件必须存储在 Cloudflare R2")
    if not server.local_auth.enabled() or not server.local_auth.configured_hash_valid():
        raise RuntimeError("共享模式必须设置有效的 WORKOS_AUTH_PASSWORD_HASH")
    required=("WORKOS_R2_ACCOUNT_ID","WORKOS_R2_BUCKET",
              "WORKOS_R2_ACCESS_KEY_ID","WORKOS_R2_SECRET_ACCESS_KEY",
              "WORKOS_ARCHIVE_ROOT")
    missing=[name for name in required if not os.environ.get(name,"").strip()]
    if missing:
        raise RuntimeError("缺少 R2/NAS 私有配置："+", ".join(missing))
    archive=Path(os.environ["WORKOS_ARCHIVE_ROOT"])
    if not archive.is_absolute():
        raise RuntimeError("WORKOS_ARCHIVE_ROOT 必须是本地/NAS 的绝对路径")
    if os.environ.get("WORKOS_HOST","127.0.0.1") not in ("127.0.0.1","localhost","::1"):
        raise RuntimeError("共享账套 API 必须只绑定本机回环地址")

if __name__=="__main__":
    check_configuration()
    server.main()
