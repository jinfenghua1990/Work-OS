"""Archive regression tests use a temporary SQLite database and private directory.

Run without Neon: python -m unittest discover -s backend/tests -v
"""
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server
import original_archive


class OriginalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = server.DB_PATH
        server.DB_PATH = Path(self.tmp.name) / "workos.sqlite3"
        self.env = patch.dict(os.environ, {"WORKOS_ARCHIVE_ROOT":str(Path(self.tmp.name) / "archive")})
        self.env.start()
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self.previous_path
        self.env.stop()
        self.tmp.cleanup()

    def test_duplicate_content_is_one_object_but_two_uploads(self):
        data = b"date,order,amount\n2026-11-20,abc,100\n"
        with server.db() as conn:
            a = original_archive.ingest(conn,io.BytesIO(data),len(data),"sales-a.csv","sales","2026-11")
            b = original_archive.ingest(conn,io.BytesIO(data),len(data),"renamed.csv","sales","2026-11")
            self.assertEqual(a["id"],hashlib.sha256(data).hexdigest())
            self.assertEqual(a["id"],b["id"])
            self.assertNotEqual(a["upload_id"],b["upload_id"])
            self.assertFalse(a["duplicate_content"])
            self.assertTrue(b["duplicate_content"])
            count = conn.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0]
            upload_count = conn.execute("SELECT COUNT(*) FROM archive_uploads").fetchone()[0]
            self.assertEqual((count,upload_count),(1,2))
            meta = original_archive.fetch_file_metadata(conn,a["id"])
        path = original_archive.open_verified_file(a["id"],meta["storage_key"])
        self.assertEqual(path.read_bytes(),data)

    def test_links_are_idempotent_and_retain_source_trace(self):
        data = b"invoice,amount\nA,100"
        with server.db() as conn:
            meta = original_archive.ingest(conn,io.BytesIO(data),len(data),"input.pdf","input_invoice","2026-11")
            one=original_archive.link_document(conn,meta["id"],"finance.invoice","INV-001")
            two=original_archive.link_document(conn,meta["id"],"finance.invoice","INV-001")
            self.assertFalse(one["duplicate"])
            self.assertTrue(two["duplicate"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links").fetchone()[0],1)

    def test_reject_invalid_extension_size_and_traversal(self):
        with server.db() as conn:
            with self.assertRaisesRegex(ValueError,"extension"):
                original_archive.ingest(conn,io.BytesIO(b"test"),4,"script.exe","other","2026-11")
            with self.assertRaisesRegex(ValueError,"empty"):
                original_archive.ingest(conn,io.BytesIO(b""),0,"report.csv","sales","2026-11")
            self.assertEqual(original_archive.safe_filename("../../bank.csv"),"bank.csv")
            with self.assertRaisesRegex(ValueError,"period"):
                original_archive.ingest(conn,io.BytesIO(b"a"),1,"a.csv","sales","2026-13")

    def test_immutable_original_detects_corruption(self):
        data=b"original"
        with server.db() as conn:
            meta=original_archive.ingest(conn,io.BytesIO(data),len(data),"report.csv","sales","2026-11")
            row=original_archive.fetch_file_metadata(conn,meta["id"])
        path=original_archive.open_verified_file(meta["id"],row["storage_key"])
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError,"SHA-256"):
            original_archive.open_verified_file(meta["id"],row["storage_key"])

    def test_legacy_database_migration_is_repeatable(self):
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],37)
        server.init_db()
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],37)


if __name__ == "__main__":
    unittest.main()
