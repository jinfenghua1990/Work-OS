"""Raw source evidence tests: exact bytes, parsed raw rows, immutability and copy.

Everything runs against a disposable local SQLite database and temporary files.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND/"app"))

import backfill_evidence_db
import evidence_db
import original_archive
import server


class ImmutableRawEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.old_db=server.DB_PATH
        server.DB_PATH=Path(self.folder.name)/"workos.sqlite3"
        self.path=Path(self.folder.name)/"raw"
        self.env=patch.dict(os.environ,{
            "WORKOS_ARCHIVE_ROOT":str(self.path),
            "WORKOS_EVIDENCE_DB_MAX_BYTES":str(1024*1024),
            "WORKOS_EVIDENCE_BACKFILL_ALLOW":"1",
        })
        self.env.start()
        server.init_db()

    def tearDown(self):
        server.DB_PATH=self.old_db
        self.env.stop()
        self.folder.cleanup()

    def archive(self, data:bytes, filename:str, category="sales"):
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return original_archive.ingest(
                conn,io.BytesIO(data),len(data),filename,category,"2026-11"
            )

    def test_exact_csv_bytes_and_unmodified_values_are_in_database(self):
        data=b"order_id,amount,bank_account\r\n000123,100.50,000099\r\n"
        first=self.archive(data,"first.csv")
        second=self.archive(data,"renamed.csv")
        self.assertTrue(first["stored_in_database"])
        self.assertTrue(second["stored_in_database"])
        self.assertEqual(first["id"],second["id"])
        self.assertTrue(second["duplicate_content"])
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,first["id"]),data)
            parsed=evidence_db.source_rows(conn,first["id"],offset=1,limit=1)
            self.assertEqual(parsed["rows"],[["000123","100.50","000099"]])
            self.assertEqual(parsed["total_rows"],2)
            self.assertFalse(parsed["is_business_posted"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_original_documents").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_row_snapshots").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_import_batches").fetchone()[0],2)

    def test_exact_pdf_bytes_are_saved_even_without_parsed_rows(self):
        data=b"%PDF-1.4\x00\xff\x00\nOriginal paid invoice\n%%EOF"
        doc=self.archive(data,"invoice.pdf","input_invoice")
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,doc["id"]),data)
            preview=evidence_db.source_rows(conn,doc["id"])
            self.assertEqual(preview["parse_status"],"binary_only")
            self.assertEqual(preview["rows"],[])

    def test_unparseable_original_is_preserved_without_guessing(self):
        data=b"bank,notes\n1,\xff\xfe"
        uploaded=self.archive(data,"bank.csv","bank")
        with server.db() as conn:
            snapshot=evidence_db.source_rows(conn,uploaded["id"])
            self.assertEqual(snapshot["parse_status"],"unparsed")
            self.assertEqual(evidence_db.fetch(conn,uploaded["id"]),data)

    def test_immutable_triggers_block_updates_and_deletes(self):
        doc=self.archive(b"original,amount\nA,10\n","purchase.csv","purchase")
        with server.db() as conn:
            with self.assertRaisesRegex(sqlite3.DatabaseError,"immutable"):
                conn.execute("UPDATE evidence_original_documents SET exact_bytes=? WHERE sha256=?",
                             (b"altered",doc["id"]))
            with self.assertRaisesRegex(sqlite3.DatabaseError,"immutable"):
                conn.execute("DELETE FROM evidence_row_snapshots WHERE file_id=?",(doc["id"],))
            with self.assertRaisesRegex(sqlite3.DatabaseError,"immutable"):
                conn.execute("DELETE FROM evidence_import_batches WHERE file_id=?",(doc["id"],))
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,doc["id"]),b"original,amount\nA,10\n")

    def test_disk_copy_can_be_lost_without_losing_sql_original(self):
        binary=b"id,amount\nA100,123\n"
        doc=self.archive(binary,"sales.csv")
        key=f"{doc['id'][:2]}/{doc['id'][2:4]}/{doc['id']}"
        (self.path/key).unlink()
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,doc["id"]),binary)

    def test_capacity_guard_rejects_sql_upload_and_financial_import(self):
        with patch.dict(os.environ,{"WORKOS_EVIDENCE_DB_MAX_BYTES":"8"}):
            with self.assertRaisesRegex(ValueError,"capacity guard reached"):
                self.archive(b"longer than eight bytes","too-large.csv")
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_original_documents").fetchone()[0],0)

    def test_backfill_legacy_disk_original_without_rewriting_accounting(self):
        legacy=b"source,order\nCoffeeBear,A12\n"
        checksum=hashlib.sha256(legacy).hexdigest()
        key=f"{checksum[:2]}/{checksum[2:4]}/{checksum}"
        full=self.path/key
        full.parent.mkdir(parents=True,exist_ok=True)
        full.write_bytes(legacy)
        with server.db() as conn:
            conn.execute("INSERT INTO archive_files VALUES(?,?,?,?,?)",
                         (checksum,checksum,len(legacy),key,server.now()))
            conn.execute("INSERT INTO archive_uploads VALUES(?,?,?,?,?,?,?)",
                         ("legacy-upload-1",checksum,"legacy.csv","sales","2026-11",
                          "coffeebear",server.now()))
        before=backfill_evidence_db.migrate(apply=False)
        self.assertEqual(before["copied_uploads"],0)
        result=backfill_evidence_db.migrate(apply=True)
        self.assertEqual(result["copied_uploads"],1)
        again=backfill_evidence_db.migrate(apply=True)
        self.assertEqual(again["missing_originals"],0)
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,checksum),legacy)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_import_batches").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_v41_is_current_and_reentrant(self):
        server.init_db()
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],41)


if __name__=="__main__":
    unittest.main()
