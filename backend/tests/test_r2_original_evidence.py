"""Cloudflare R2 integration tests without real credentials or network."""
import hashlib
import io
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import evidence_db
import r2_evidence

class FakeS3:
    def __init__(self):
        self.objects={}
        self.puts=0
    def put_object(self,**kwargs):
        index=(kwargs["Bucket"],kwargs["Key"])
        if index in self.objects:
            err=ValueError("exists")
            err.response={"Error":{"Code":"PreconditionFailed"}}
            raise err
        self.puts+=1
        self.objects[index]=bytes(kwargs["Body"])
    def get_object(self,**kwargs):
        return {"Body":io.BytesIO(self.objects[(kwargs["Bucket"],kwargs["Key"])])}

class PgCompatibleSqlite:
    is_postgres=True
    def __init__(self):
        self.db=sqlite3.connect(":memory:",isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
            CREATE TABLE evidence_original_documents (
                sha256 TEXT PRIMARY KEY,size_bytes INTEGER NOT NULL,
                exact_bytes BLOB,created_at TEXT NOT NULL);
            CREATE TABLE evidence_r2_locations (
                sha256 TEXT PRIMARY KEY,bucket TEXT NOT NULL,
                object_key TEXT NOT NULL UNIQUE,size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE evidence_row_snapshots (
                file_id TEXT,parser_version TEXT,raw_rows_json TEXT,
                row_count INTEGER,parse_status TEXT,parse_error TEXT,
                created_at TEXT,PRIMARY KEY(file_id,parser_version));
            CREATE TABLE evidence_import_batches (
                upload_id TEXT PRIMARY KEY,file_id TEXT,original_name TEXT,
                category TEXT,period TEXT,source_system TEXT,created_at TEXT);
        """)
    def execute(self,sql,parameters=()):
        return self.db.execute(sql.replace("evidence.","evidence_"),parameters)

class R2OriginalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn=PgCompatibleSqlite()
        self.s3=FakeS3()
        self.env=patch.dict(os.environ,{
            "WORKOS_EVIDENCE_STORAGE":"r2",
            "WORKOS_R2_ACCOUNT_ID":"1"*32,
            "WORKOS_R2_BUCKET":"work-os-originals",
            "WORKOS_R2_ACCESS_KEY_ID":"test-only",
            "WORKOS_R2_SECRET_ACCESS_KEY":"test-only",
        })
        self.env.start()
        self.client=patch.object(r2_evidence,"_client",return_value=self.s3)
        self.client.start()
    def tearDown(self):
        self.client.stop()
        self.env.stop()
        self.conn.db.close()
    def test_original_bytes_are_in_r2_and_not_in_neon(self):
        raw=b"order_id,price\r\n000123,0010.50\r\n"
        file_id=hashlib.sha256(raw).hexdigest()
        first=r2_evidence.store(self.conn,file_id,raw,"batch1","statement.csv","bank","2026-11")
        second=r2_evidence.store(self.conn,file_id,raw,"batch2","renamed.csv","bank","2026-11")
        self.assertTrue(first["stored_in_r2"])
        self.assertFalse(first["stored_in_database"])
        self.assertTrue(second["stored_in_r2"])
        self.assertEqual(self.s3.puts,1)
        self.assertEqual(r2_evidence.fetch(self.conn,file_id),raw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM evidence.import_batches").fetchone()[0],2)
        self.assertIsNone(self.conn.execute("SELECT exact_bytes FROM evidence.original_documents").fetchone()[0])
        self.assertEqual(evidence_db.fetch(self.conn,file_id),None)
        self.assertIn("000123",self.conn.execute("SELECT raw_rows_json FROM evidence.row_snapshots").fetchone()[0])
    def test_tampered_remote_object_rejected(self):
        raw=b"untouched original"
        file_id=hashlib.sha256(raw).hexdigest()
        r2_evidence.store(self.conn,file_id,raw,"batch1","original.pdf","input_invoice","2026-11")
        identifier=next(iter(self.s3.objects))
        self.s3.objects[identifier]=b"tampered"
        with self.assertRaisesRegex(ValueError,"SHA-256"):
            r2_evidence.fetch(self.conn,file_id)
    def test_unavailable_remote_causes_no_sql_archiving(self):
        raw=b"test content"
        file_id=hashlib.sha256(raw).hexdigest()
        with patch.object(self.s3,"get_object",side_effect=OSError("network outage")):
            with self.assertRaises(OSError):
                r2_evidence.store(self.conn,file_id,raw,"batch1","original.csv","bank","2026-11")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM evidence.r2_locations").fetchone()[0],0)
    def test_missing_r2_credentials_rejected(self):
        del os.environ["WORKOS_R2_ACCESS_KEY_ID"]
        with self.assertRaisesRegex(ValueError,"not configured"):
            r2_evidence._config()

if __name__=="__main__":
    unittest.main()
