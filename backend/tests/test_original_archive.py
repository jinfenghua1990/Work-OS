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

    def test_invoice_registration_links_original(self):
        data=b"%PDF-1.4 test invoice"
        with server.db() as conn:
            evidence=original_archive.ingest(conn,io.BytesIO(data),len(data),"202611.pdf","input_invoice","2026-11")
        handler=server.Api.__new__(server.Api)
        handler.reply=lambda code,payload:(code,payload)
        payload={
            "source_key":"invoice-no-0001", "invoice_number":"0001",
            "direction":"purchase","counterparty":"测试供货商",
            "issued_on":"2026-11-20","total_amount":"113.00",
            "tax_amount":"13.00","currency":"CNY","exchange_rate":"1",
            "archive_file_id":evidence["id"]
        }
        code,receipt=handler.create_invoice(payload)
        self.assertEqual(code,201,receipt)
        code,duplicate=handler.create_invoice(payload)
        self.assertEqual(code,200,duplicate)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],1)
            linked=conn.execute("SELECT file_id,source_system,source_document_id FROM archive_links").fetchone()
            self.assertEqual(linked["file_id"],evidence["id"])
            self.assertEqual(linked["source_system"],"finance.invoice")
            self.assertEqual(linked["source_document_id"],receipt["id"])

    def test_bank_import_links_document_without_posting_expense(self):
        data=b"date,account,reference,amount"
        with server.db() as conn:
            evidence=original_archive.ingest(conn,io.BytesIO(data),len(data),"bank.csv","bank","2026-11")
        handler=server.Api.__new__(server.Api)
        handler.reply=lambda code,payload:(code,payload)
        row={"source_key":"bank:account:2026-11-20:ref1",
             "transaction_on":"2026-11-20","account_name":"测试银行",
             "bank_reference":"ref1","description":"收款",
             "direction":"in","amount":"100.00","currency":"CNY"}
        code,result=handler.import_bank({"archive_file_id":evidence["id"],"rows":[row]})
        self.assertEqual(code,200,result)
        self.assertEqual(len(result["created"]),1)
        code,again=handler.import_bank({"archive_file_id":evidence["id"],"rows":[row]})
        self.assertEqual(code,200,again)
        self.assertEqual(len(again["duplicates"]),1)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_legacy_database_migration_is_repeatable(self):
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],server.LATEST_SCHEMA_VERSION)
        server.init_db()
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],server.LATEST_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
