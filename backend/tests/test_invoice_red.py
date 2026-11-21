"""Red-note, immutable tax-rate and historical accounting safety regression."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import invoice_red
import original_archive
import monthly_flow


class CreditNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original_db=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"workos.sqlite3"
        self.env=patch.dict(os.environ,{"WORKOS_ARCHIVE_ROOT":str(Path(self.temp.name)/"originals")})
        self.env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,data:(status,data)
        self.blue_file=self.archive(b"BLUE-DOCUMENT")
        self.red_file=self.archive(b"RED-DOCUMENT")
        response=self.api.create_invoice({
            "invoice_number":"00000123","direction":"sales",
            "counterparty":"Sample Ltd","issued_on":"2026-10-15",
            "total_amount":"100.00","tax_amount":"13.00","currency":"CNY",
            "source_key":"blue-123","archive_file_id":self.blue_file,
        })
        self.assertEqual(response[0],201,response)
        self.blue_id=response[1]["id"]

    def tearDown(self):
        self.env.stop()
        server.DB_PATH=self.original_db
        self.temp.cleanup()

    def archive(self,raw):
        with server.db() as conn:
            return original_archive.ingest(
                conn,io.BytesIO(raw),len(raw),"invoice.pdf",
                "output_invoice","2026-11"
            )["id"]

    def register(self,number="R-01",amount="60.00",tax="7.80",
                 issued="2026-11-20",key="red-001",reason="discount"):
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return invoice_red.register_credit(conn,{
                "source_key":key,"blue_invoice_id":self.blue_id,
                "invoice_number":number,"archive_file_id":self.red_file,
                "issued_on":issued,"total_amount":amount,
                "tax_amount":tax,"reason":reason,
            },server.ensure_open,server.now,original_archive.link_document)

    def test_schema_version_44_and_readiness(self):
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],44)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoice_credit_notes").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tax_rate_rules").fetchone()[0],0)

    def test_red_is_idempotent_preserves_original_and_does_not_post(self):
        first=self.register()
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["net_amount"],"40.00")
        again=self.register()
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["net_amount"],"40.00")
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoice_credit_notes").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links WHERE source_system='finance.invoice.red'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT total_amount FROM invoices WHERE id=?",(self.blue_id,)).fetchone()[0],"100.00")

    def test_excessive_red_tax_and_conflicting_identity_rejected(self):
        self.register()
        with self.assertRaisesRegex(ValueError,"exceed"):
            self.register(number="R-02",amount="41.00",tax="5.00",key="red-002")
        with self.assertRaisesRegex(ValueError,"exceed"):
            self.register(number="R-02",amount="40.00",tax="5.30",key="red-002")
        with self.assertRaisesRegex(ValueError,"conflicting"):
            self.register(number="R-01",amount="10.00",tax="1.30",key="red-001")
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoice_credit_notes").fetchone()[0],1)

    def test_closed_original_period_allows_later_red_and_monthly_ledger(self):
        with server.db() as conn:
            conn.execute(
                "INSERT INTO period_locks(period,closed_at,closed_by,reviewed_checks,journal_count) "
                "VALUES(?,?,?,?,?)",("2026-10",server.now(),"reviewer","[]",0)
            )
        self.register()
        with server.db() as conn:
            oct_result=monthly_flow.report(conn,"2026-10")
            nov_result=monthly_flow.report(conn,"2026-11")
        self.assertEqual(oct_result["invoices"]["currencies"][0]["output_total"],"100.00")
        self.assertEqual(oct_result["invoices"]["currencies"][0]["output_red_total"],"0.00")
        self.assertEqual(nov_result["invoices"]["currencies"][0]["output_red_total"],"60.00")
        self.assertEqual(nov_result["invoices"]["currencies"][0]["output_net_movement"],"-60.00")
        self.assertEqual(nov_result["invoices"]["red_document_count"],1)

    def test_closed_red_period_rejected(self):
        with server.db() as conn:
            conn.execute(
                "INSERT INTO period_locks(period,closed_at,closed_by,reviewed_checks,journal_count) "
                "VALUES(?,?,?,?,?)",("2026-11",server.now(),"reviewer","[]",0)
            )
        with self.assertRaisesRegex(ValueError,"closed"):
            self.register()
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoice_credit_notes").fetchone()[0],0)

    def test_tax_rate_reference_versioning_and_no_vat_posting(self):
        data={"rate_code":"vat_standard","valid_from":"2026-11-01","valid_to":"2026-11-30",
              "rate_percent":"13","description":"仅测试样例","source_reference":"test policy",
              "created_by":"reviewer-1"}
        with server.db() as conn:
            first=invoice_red.register_rate(conn,data,server.ensure_open,server.now)
            again=invoice_red.register_rate(conn,data,server.ensure_open,server.now)
            self.assertFalse(first["duplicate"])
            self.assertTrue(again["duplicate"])
            self.assertEqual(len(invoice_red.list_rates(conn)),1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
            with self.assertRaisesRegex(ValueError,"overlap"):
                invoice_red.register_rate(conn,{**data,"valid_from":"2026-11-15","valid_to":"2026-12-15"},
                                          server.ensure_open,server.now)
            with self.assertRaisesRegex(ValueError,"immutable"):
                invoice_red.register_rate(conn,{**data,"rate_percent":"9"},
                                          server.ensure_open,server.now)

    def test_red_after_existing_link_flags_reconciliation(self):
        with server.db() as conn:
            conn.execute(
                "INSERT INTO receivables_payables "
                "(id,direction,source_type,source_id,counterparty,original_amount,"
                "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("ar1","receivable","sale","test","Sample Ltd","100.00","0.00","CNY",None,server.now())
            )
        status,_=self.api.link_invoice({
            "invoice_id":self.blue_id,"target_type":"receivable",
            "target_id":"ar1","amount":"100.00"
        })
        self.assertEqual(status,201)
        result=self.register()
        self.assertTrue(result["link_review_required"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT linked_amount FROM invoice_links").fetchone()[0],"100.00")
        with self.assertRaisesRegex(ValueError,"links exceed"):
            self.api.link_invoice({
                "invoice_id":self.blue_id,"target_type":"receivable",
                "target_id":"ar1","amount":"10.00","action_key":"extra-link"
            })


if __name__=="__main__":
    unittest.main()
