"""Isolated monthly marketplace settlement import tests.

Original files are archived by content; reusing one source never writes a
second reconciliation or journal. Revisions require new document evidence.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import original_archive
import server


class PlatformIncomeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "workos.sqlite3"
        self.env = patch.dict(os.environ,{
            "WORKOS_ARCHIVE_ROOT":str(Path(self.temp.name) / "archive")
        })
        self.env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)

    def tearDown(self):
        server.DB_PATH=self.original_db
        self.env.stop()
        self.temp.cleanup()

    def archive(self,name="nov.csv",data=b"platform,store,gross\nA,shop,120\n"):
        with server.db() as conn:
            return original_archive.ingest(
                conn, io.BytesIO(data), len(data), name, "sales", "2026-11",
                "coffee.platform",
            )

    def payload(self,file_id,price="120.00",operator="张三"):
        return {
            "period":"2026-11","platform":"淘宝","store_name":"官方店铺",
            "gross_amount":price,"refund_amount":"10",
            "subsidy_amount":"0","platform_fee_amount":"5",
            "settlement_amount":"105",
            "bank_received_amount":"105","declared_sales_amount":"110",
            "evidence_ref":f"sha256:{file_id}#row:2",
            "archive_file_id":file_id,
            "note":"","changed_by":operator,
            "action_key":":".join(quote(x,safe="") for x in (
                "platform-import-v1",file_id,"2026-11","淘宝","官方店铺"
            )),
        }

    def test_identical_file_renamed_does_not_rewrite_reconciliation(self):
        first=self.archive(name="nov.csv")
        second=self.archive(name="renamed.csv")
        self.assertEqual(first["id"],second["id"])
        status,created=self.api.save_platform_income_reconciliation(self.payload(first["id"]))
        self.assertEqual(status,201,created)
        status,replayed=self.api.save_platform_income_reconciliation(self.payload(second["id"]))
        self.assertEqual(status,200,replayed)
        self.assertTrue(replayed["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM platform_income_reconciliations"
            ).fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM platform_income_reconciliation_history"
            ).fetchone()[0],0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM platform_income_reconciliation_requests"
            ).fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM archive_links WHERE source_system='coffee.platform_income'"
            ).fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM journal_entries"
            ).fetchone()[0],0)

    def test_change_in_same_file_id_is_rejected_not_silent(self):
        original=self.archive()
        self.assertEqual(self.api.save_platform_income_reconciliation(self.payload(original["id"]))[0],201)
        with self.assertRaisesRegex(ValueError,"different platform income data"):
            self.api.save_platform_income_reconciliation(
                self.payload(original["id"],price="150.00")
            )
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT gross_amount FROM platform_income_reconciliations"
            ).fetchone()[0],"120.00")

    def test_new_statement_can_create_audited_revision(self):
        old=self.archive()
        new=self.archive("nov-updated.csv",b"platform,store,gross\nA,shop,150\n")
        self.assertEqual(self.api.save_platform_income_reconciliation(self.payload(old["id"]))[0],201)
        status,updated=self.api.save_platform_income_reconciliation(
            self.payload(new["id"],price="150.00")
        )
        self.assertEqual(status,201,updated)
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM platform_income_reconciliation_history"
            ).fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM archive_links WHERE source_system='coffee.platform_income'"
            ).fetchone()[0],2)
            self.assertEqual(conn.execute(
                "SELECT gross_amount FROM platform_income_reconciliations"
            ).fetchone()[0],"150.00")


if __name__=="__main__":
    unittest.main()
