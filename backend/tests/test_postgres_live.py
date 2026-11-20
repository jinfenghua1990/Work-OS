"""Disposable PostgreSQL smoke tests: no Neon connection is permitted.

Run locally with an isolated, empty PostgreSQL database named workos_ci on
localhost, set WORKOS_TEST_POSTGRES_URL, then:
    python -m unittest backend/tests/test_postgres_live.py -v

CI starts a fresh PostgreSQL service container for these tests. They do NOT
run against user data, remote addresses, or production databases.
"""
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server


class PostgresLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get("WORKOS_TEST_POSTGRES_URL", "").strip()
        if not url:
            raise unittest.SkipTest("No disposable PostgreSQL CI database configured")
        parsed = urlparse(url)
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.path != "/workos_ci":
            raise RuntimeError("Refusing tests: database must be workos_ci on localhost")
        if server._psycopg is None:
            raise unittest.SkipTest("psycopg driver not installed")
        cls.old_backend, cls.old_url = server.DB_BACKEND, server.DATABASE_URL
        server.DB_BACKEND, server.DATABASE_URL = "postgres", url
        try:
            server.init_db()
        except Exception:
            server.DB_BACKEND, server.DATABASE_URL = cls.old_backend, cls.old_url
            raise

    @classmethod
    def tearDownClass(cls):
        server.DB_BACKEND, server.DATABASE_URL = cls.old_backend, cls.old_url

    def test_new_postgres_schema_is_v39(self):
        with server.db() as conn:
            result = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            self.assertEqual(result["version"], server.LATEST_SCHEMA_VERSION)
            self.assertEqual(server.LATEST_SCHEMA_VERSION, 39)
            self.assertIsNotNone(
                conn.execute("SELECT to_regclass('public.archive_files') AS name").fetchone()["name"]
            )
            self.assertIsNotNone(
                conn.execute("SELECT to_regclass('public.sample_dispositions') AS name").fetchone()["name"]
            )

    def test_purchase_and_receipt_are_persistent_across_connections(self):
        api = server.Api.__new__(server.Api)
        api.reply = lambda status, payload: (status, payload)
        marker = uuid4().hex[:10]
        po = f"PG-PO-{marker}"
        status, created = api.create_purchase({
            "number": po, "supplier": "本地测试供货商", "sku": "PG-CAPSULES",
            "item_type": "finished_goods", "quantity": 5,
            "unit_cost": "8", "ordered_on": "2026-11-20",
            "currency": "CNY", "entity_code": "main",
        })
        self.assertEqual(status, 201, created)
        status, receipt = api.receive(created["id"], {
            "source_key": f"PG-RECEIVE-{marker}", "quantity": 5,
            "received_on": "2026-11-20",
        })
        self.assertEqual(status, 201, receipt)
        with server.db() as conn:
            row = conn.execute(
                "SELECT ordered_qty,received_qty FROM purchases WHERE number=?",
                (po,),
            ).fetchone()
            self.assertIsNotNone(row, "COMMIT did not persist purchase in PostgreSQL")
            self.assertEqual((row["ordered_qty"], row["received_qty"]), (5, 5))
            movements = conn.execute(
                "SELECT COUNT(*) AS count FROM purchase_receipts WHERE purchase_id=?",
                (created["id"],),
            ).fetchone()["count"]
            self.assertEqual(movements, 1)

    def test_financial_error_rolls_back_every_statement(self):
        code = f"PG-ROLLBACK-{uuid4().hex[:10]}"
        with self.assertRaisesRegex(RuntimeError, "intentional"):
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO projects (id,code,name,entity_code,starts_on,ends_on,currency,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid4()), code, "测试项目", "main", "2026-11-20",
                     "2026-11-20", "CNY", server.now()),
                )
                raise RuntimeError("intentional transaction rollback")
        with server.db() as conn:
            present = conn.execute(
                "SELECT 1 AS present FROM projects WHERE code=?", (code,)
            ).fetchone()
            self.assertIsNone(present)

    def test_v35_migration_against_disposable_pg_database(self):
        # Regression check: the connected Neon dev branch was at schema 35.
        # The DELETE is allowed only in the disposable localhost workos_ci DB.
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM schema_migrations WHERE version>35")
        server.init_db()
        server.init_db()
        with server.db() as conn:
            version = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()["version"]
            self.assertEqual(version, 39)


if __name__ == "__main__":
    unittest.main()
