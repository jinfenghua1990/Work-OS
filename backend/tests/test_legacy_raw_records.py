"""Append-only legacy CoffeeBear source-row snapshot tests use a temporary SQLite database.

Run without Neon: python -m unittest backend.tests.test_legacy_raw_records -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import legacy_raw_records
import server


class LegacyRawRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = server.DB_PATH
        server.DB_PATH = Path(self.tmp.name) / "workos.sqlite3"
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self.previous_path
        self.tmp.cleanup()

    def test_store_is_append_only_and_idempotent(self):
        raw = '{"id":1,"order_no":"SO-1","amount":"10.00"}'
        with server.db() as conn:
            first = legacy_raw_records.store_record(
                conn, "coffeebear-prod", "sales_orders", "SO-1", raw
            )
            second = legacy_raw_records.store_record(
                conn, "coffeebear-prod", "sales_orders", "SO-1", raw
            )
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
            counts = legacy_raw_records.counts(conn)
            self.assertEqual(counts[0]["source_table"], "sales_orders")
            self.assertEqual(counts[0]["snapshot_count"], 1)
            self.assertEqual(counts[0]["source_records"], 1)

    def test_same_record_with_changed_bytes_is_a_new_snapshot(self):
        with server.db() as conn:
            legacy_raw_records.store_record(
                conn, "coffeebear-prod", "sales_orders", "SO-1", '{"amount":"10.00"}'
            )
            changed = legacy_raw_records.store_record(
                conn, "coffeebear-prod", "sales_orders", "SO-1", '{"amount":"11.00"}'
            )
            self.assertFalse(changed["duplicate"])
            self.assertEqual(legacy_raw_records.counts(conn)[0]["snapshot_count"], 2)

    def test_update_and_delete_are_rejected(self):
        with server.db() as conn:
            legacy_raw_records.store_record(
                conn, "coffeebear-prod", "sales_orders", "SO-1", '{"id":1}'
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE evidence_legacy_source_records SET raw_json_text='tampered'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM evidence_legacy_source_records")

    def test_rejects_unknown_source_table(self):
        with server.db() as conn:
            with self.assertRaisesRegex(ValueError, "unsupported source"):
                legacy_raw_records.store_record(
                    conn, "coffeebear-prod", "unknown_table", "X-1", '{"id":1}'
                )

    def test_schema_45_database_reaches_46_before_serving(self):
        with server.db() as conn:
            conn.execute("DROP TABLE evidence_legacy_source_records")
            conn.execute("DELETE FROM schema_migrations WHERE version=46")
        server.require_database_ready()
        with server.db() as conn:
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 46
            )
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_legacy_source_records'"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
