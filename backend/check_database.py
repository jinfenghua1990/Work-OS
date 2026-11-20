#!/usr/bin/env python3
"""Read-only local preflight for the Work OS PostgreSQL/Neon schema.

No DDL, no data mutation, no migrations, no credentials printed.

    WORKOS_DB_BACKEND=postgres WORKOS_DATABASE_URL=... python backend/check_database.py

Alternatively put the two values in the ignored local .env file.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import server


def main() -> int:
    if server.DB_BACKEND != "postgres":
        print("当前配置为本地 SQLite。检查 Neon 前请配置 WORKOS_DB_BACKEND=postgres。")
        return 2
    try:
        result = server.postgres_schema_status()
    except Exception as exc:
        # Avoid printing raw psycopg exceptions: they can disclose connection
        # details and other sensitive database metadata.
        print(f"数据库连接或只读检查失败（{type(exc).__name__}）。请检查本地 .env 配置和网络连接。")
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["ready"]:
        print("当前数据库结构与 Work OS 代码匹配。未修改任何数据。")
        return 0
    print("结构未就绪：禁止将此数据库用于正式记账。请先备份，并在测试分支验收迁移。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
