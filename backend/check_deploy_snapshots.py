#!/usr/bin/env python3
"""Read-only guard for the committed deploy snapshots.

Both `deploy/seed/workos.sqlite3` and `deploy/runtime-db/workos.sqlite3` are
shipped to other machines, so they must match the schema the code requires and
must not carry real business data. This check exists because a release once
shipped a schema-45 snapshot while the code required 46, which only surfaced
after a deploy.

Every database is opened with `mode=ro`, so no `-wal`/`-shm` sidecar files are
created in the working tree.

    python backend/check_deploy_snapshots.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "app"))
import server  # noqa: E402

SNAPSHOTS = (
    ROOT / "deploy" / "seed" / "workos.sqlite3",
    ROOT / "deploy" / "runtime-db" / "workos.sqlite3",
)

# Tables that would indicate real business data leaked into a public snapshot.
BUSINESS_TABLES = (
    "purchases", "sales_imports", "bank_transactions", "invoices",
    "invoice_credit_notes", "journal_entries", "journal_lines",
    "archive_files", "evidence_legacy_source_records",
)


def check(path: Path) -> list[str]:
    problems: list[str] = []
    label = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    if not path.is_file():
        return [f"{label}: missing"]
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            problems.append(f"{label}: stray {sidecar.name} (checkpoint and use journal_mode=DELETE)")
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"{label}: integrity_check returned {integrity!r}")
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        if journal != "delete":
            problems.append(f"{label}: journal_mode is {journal!r}, expected 'delete'")
        has_migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not has_migrations:
            problems.append(f"{label}: schema_migrations table missing")
            return problems
        version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        if int(version) != server.LATEST_SCHEMA_VERSION:
            problems.append(
                f"{label}: schema {version}, code requires {server.LATEST_SCHEMA_VERSION}"
            )
        for table in BUSINESS_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                problems.append(f"{label}: table {table} missing")
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                problems.append(f"{label}: {table} contains {count} rows (must ship empty)")
        accounts = conn.execute("SELECT COUNT(*) FROM chart_of_accounts").fetchone()[0]
        if accounts == 0:
            problems.append(f"{label}: chart_of_accounts is empty, default chart of accounts missing")
        print(f"  {label}: schema {version}, journal_mode={journal}, accounts={accounts}")
    return problems


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(argument).resolve() for argument in arguments] or list(SNAPSHOTS)
    print(f"Checking deploy snapshots against schema {server.LATEST_SCHEMA_VERSION}")
    problems: list[str] = []
    for path in paths:
        problems.extend(check(path))
    if problems:
        print("FAIL: deploy snapshots are not shippable", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("PASS: deploy snapshots match the code schema and contain no business data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
