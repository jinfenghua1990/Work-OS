"""Exercise EXACT reviewed schema-44/45 SQL on a disposable PostgreSQL database.

This is only a CI/local test, never a Neon migration command. Refuses any
address other than localhost and a source database other than workos_ci.
The temporary database is created and dropped by the local CI database owner.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))
import server


def require_ci_url() -> str:
    url = os.environ.get("WORKOS_TEST_POSTGRES_URL", "").strip()
    parsed = urlparse(url)
    if (not url or parsed.scheme not in {"postgresql", "postgres"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != "/workos_ci"
            or not parsed.username):
        raise RuntimeError(
            "Refusing migration rehearsal: only localhost/workos_ci with "
            "WORKOS_TEST_POSTGRES_URL is allowed"
        )
    if server._psycopg is None:
        raise RuntimeError("psycopg is required for disposable PostgreSQL test")
    return url


def exact_migration_sql(version: int) -> str:
    paths = {
        44: ROOT / "migrations" / "44_invoice_red_and_tax_rates.sql",
        45: ROOT / "migrations" / "45_named_finance_users.sql",
    }
    return paths[version].read_text(encoding="utf-8")


def version(conn) -> int:
    return int(conn.execute(
        "SELECT MAX(version) FROM public.schema_migrations"
    ).fetchone()[0])


def expect_rejected(conn, script: str, expected: str):
    try:
        conn.execute(script)
    except Exception as exc:
        if expected not in str(exc):
            raise AssertionError(
                f"Expected migration guard {expected!r}, got {exc!s}"
            ) from exc
        # A rejected SQL migration may leave a failed transaction open.
        conn.execute("ROLLBACK")
    else:
        raise AssertionError(f"Migration unexpectedly succeeded: {expected}")


def main() -> None:
    source_url = require_ci_url()
    parsed = urlparse(source_url)
    db_name = "workos_migration_ci_" + uuid4().hex[:12]
    test_url = urlunparse(parsed._replace(path="/" + db_name))
    ident = server._psycopg.sql.Identifier(db_name)
    admin = server._psycopg.connect(source_url, autocommit=True)
    original_backend, original_url = server.DB_BACKEND, server.DATABASE_URL
    created = False
    try:
        admin.execute(server._psycopg.sql.SQL("CREATE DATABASE {}").format(ident))
        created = True
        server.DB_BACKEND, server.DATABASE_URL = "postgres", test_url

        # Populate only an empty local CI DB. New-schema bootstrap is used
        # to construct the known v43 base by removing ONLY v44/v45 objects.
        server.init_db()
        with server._psycopg.connect(test_url, autocommit=True) as pg:
            if version(pg) != 45:
                raise AssertionError("CI bootstrap expected schema 45")
            pg.execute("DROP TABLE public.finance_personal_advance_reviews")
            pg.execute("DROP TABLE public.finance_users")
            pg.execute("DROP TABLE public.tax_rate_rules")
            pg.execute("DROP TABLE public.invoice_credit_notes")
            pg.execute("DELETE FROM public.schema_migrations WHERE version > 43")
            if version(pg) != 43:
                raise AssertionError("failed to establish isolated schema 43")

        # A source blue invoice is retained byte-for-byte through both DDL
        # upgrades; creating a red table must not mutate old accounting data.
        api = server.Api.__new__(server.Api)
        api.reply = lambda code, data: (code, data)
        status, source = api.create_invoice({
            "source_key": "ci-blue-unchanged", "invoice_number": "00000101",
            "direction": "purchase", "counterparty": "CI Source Vendor",
            "issued_on": "2026-11-20", "total_amount": "113.00",
            "tax_amount": "13.00", "currency": "CNY"
        })
        if status != 201:
            raise AssertionError(f"cannot create pre-migration invoice: {source}")
        with server._psycopg.connect(test_url, autocommit=True) as pg:
            old = pg.execute(
                "SELECT * FROM public.invoices WHERE id=%s", (source["id"],)
            ).fetchone()
            old_journals = pg.execute(
                "SELECT COUNT(*) FROM public.journal_entries"
            ).fetchone()[0]

            if server.postgres_schema_status()["ready"]:
                raise AssertionError("schema 43 must be rejected by v45 runtime")
            try:
                server.require_database_ready()
            except RuntimeError:
                pass
            else:
                raise AssertionError("v45 runtime accepted schema 43")

            sql44 = exact_migration_sql(44)
            sql45 = exact_migration_sql(45)
            expect_rejected(pg, sql45, "schema 44 required")
            if version(pg) != 43:
                raise AssertionError("failed v45 script wrote to v43 schema")

            pg.execute(sql44)
            if version(pg) != 44:
                raise AssertionError("v44 upgrade did not record version 44")
            if pg.execute("SELECT to_regclass('public.invoice_credit_notes')").fetchone()[0] is None:
                raise AssertionError("v44 credit-note table missing")
            if pg.execute("SELECT to_regclass('public.tax_rate_rules')").fetchone()[0] is None:
                raise AssertionError("v44 rate table missing")
            expect_rejected(pg, sql44, "schema 43 required")

            pg.execute(sql45)
            if version(pg) != 45:
                raise AssertionError("v45 upgrade did not record version 45")
            if pg.execute("SELECT to_regclass('public.finance_users')").fetchone()[0] is None:
                raise AssertionError("v45 users table missing")
            if pg.execute("SELECT to_regclass('public.finance_personal_advance_reviews')").fetchone()[0] is None:
                raise AssertionError("v45 review table missing")
            if pg.execute("SELECT COUNT(*) FROM public.finance_users").fetchone()[0] != 0:
                raise AssertionError("migration unexpectedly created named credentials")
            expect_rejected(pg, sql45, "schema 44 required")

            new = pg.execute(
                "SELECT * FROM public.invoices WHERE id=%s", (source["id"],)
            ).fetchone()
            new_journals = pg.execute(
                "SELECT COUNT(*) FROM public.journal_entries"
            ).fetchone()[0]
            if old != new or old_journals != new_journals:
                raise AssertionError("schema upgrades changed historical invoice or journal")
        server.require_database_ready()
        print("PASS: exact 43→44→45 migration scripts, guards, original invoice,")
        print("      no default users, journals unchanged and v45 runtime readiness")
    finally:
        server.DB_BACKEND, server.DATABASE_URL = original_backend, original_url
        try:
            if created:
                admin.execute(server._psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(ident))
        finally:
            admin.close()


if __name__ == "__main__":
    main()
