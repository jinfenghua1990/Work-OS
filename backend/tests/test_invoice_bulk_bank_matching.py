"""Invoice bulk import and bank-match proposal regressions: isolated SQLite."""
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
import original_archive
import invoice_bulk
import bank_matching

class ImportAndMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.previous_db=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"workos.sqlite3"
        self.env=patch.dict(os.environ,{"WORKOS_ARCHIVE_ROOT":str(Path(self.temp.name)/"originals")})
        self.env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)
        self.archive_id=self.archive(b"invoice_number,amount\r\n00000123,100.00\r\n")
    def tearDown(self):
        self.env.stop()
        server.DB_PATH=self.previous_db
        self.temp.cleanup()
    def archive(self,data):
        with server.db() as conn:
            return original_archive.ingest(conn,io.BytesIO(data),len(data),
                "invoices.csv","input_invoice","2026-11")["id"]
    def row(self,number="00000123",total="100.00"):
        return {"invoice_number":number,"direction":"purchase",
                "counterparty":"常州示例科技有限公司","issued_on":"2026-11-20",
                "total_amount":total,"tax_amount":"11.50","currency":"CNY"}
    def test_invoice_deduplicated_on_number_issuer_direction(self):
        status,first=self.api.import_invoices({"archive_file_id":self.archive_id,"rows":[self.row()]})
        self.assertEqual(status,200,first)
        self.assertEqual(len(first["created"]),1,first)
        second_archive=self.archive(b"re-export same invoice\n")
        status,second=self.api.import_invoices({"archive_file_id":second_archive,"rows":[self.row()]})
        self.assertEqual(status,200,second)
        self.assertEqual(len(second["duplicates"]),1,second)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links").fetchone()[0],2)
            self.assertEqual(conn.execute("SELECT tax_status FROM invoices").fetchone()[0],"pending")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
    def test_conflict_and_rejected_row_do_not_post(self):
        self.api.import_invoices({"archive_file_id":self.archive_id,"rows":[self.row()]})
        status,result=self.api.import_invoices({"archive_file_id":self.archive_id,"rows":[
            self.row(total="200.00"),self.row(number="00000124",total="-2"),
            self.row(number="00000125")
        ]})
        self.assertEqual(status,200,result)
        self.assertEqual((len(result["created"]),len(result["rejected"])),(1,2),result)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links").fetchone()[0],2)
    def test_requires_existing_original_and_foreign_rate(self):
        with self.assertRaisesRegex(ValueError,"archived invoice source file"):
            self.api.import_invoices({"archive_file_id":"a"*64,"rows":[self.row()]})
        value=self.row();value["currency"]="EUR"
        status,result=self.api.import_invoices({"archive_file_id":self.archive_id,"rows":[value]})
        self.assertEqual(status,200)
        self.assertIn("exchange rate",result["rejected"][0]["error"])
    def test_closed_period_not_imported(self):
        with server.db() as conn:
            conn.execute("INSERT INTO period_locks (period,closed_at,closed_by,reviewed_checks,journal_count) VALUES(?,?,?,?,?)",
                         ("2026-11",server.now(),"reviewer","[]",0))
        status,result=self.api.import_invoices({"archive_file_id":self.archive_id,"rows":[self.row()]})
        self.assertEqual(status,200)
        self.assertEqual(len(result["rejected"]),1)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],0)
    def test_readonly_bank_match_is_unique_and_never_posts(self):
        with server.db() as conn:
            conn.execute("INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,description,"
                "direction,amount,currency,created_at,exchange_rate,rate_date,rate_source,base_amount_cny)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bank1","bank:1","2026-11-20","账户1","TX001",
                 "支付给常州示例科技有限公司采购款","out","100.00","CNY",
                 server.now(),"1","2026-11-20","functional_currency","100.00"))
            conn.execute("INSERT INTO receivables_payables "
                "(id,direction,source_type,source_id,counterparty,original_amount,settled_amount,currency,due_on,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("payable1","payable","purchase_receipt","po1","常州示例科技有限公司",
                 "100.00","0.00","CNY",None,server.now()))
            first=bank_matching.from_database(conn)
            self.assertEqual(first["suggestion_count"],1,first)
            self.assertTrue(first["suggestions"][0]["requires_confirmation"])
            self.assertEqual(first["suggestions"][0]["target_id"],"payable1")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_allocations").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
            conn.execute("INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,description,"
                "direction,amount,currency,created_at,exchange_rate,rate_date,rate_source,base_amount_cny)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bank2","bank:2","2026-11-20","账户1","TX002",
                 "支付给常州示例科技有限公司采购款","out","100.00","CNY",
                 server.now(),"1","2026-11-20","functional_currency","100.00"))
            duplicate=bank_matching.from_database(conn)
            self.assertEqual(duplicate["suggestion_count"],0,duplicate)
    def test_bank_short_names_do_not_autoguess(self):
        bank={"id":"bank","amount":"100","allocated_amount":"0","base_amount_cny":"100",
              "bank_reference":"TX","transaction_on":"2026-11-20","direction":"in",
              "currency":"CNY","description":"AB收入"}
        target={"id":"target","direction":"receivable","counterparty":"AB",
                "original_amount":"100","settled_amount":"0",
                "original_amount_cny":"100","currency":"CNY"}
        self.assertEqual(bank_matching.propose([bank],[target]),[])

if __name__=="__main__":
    unittest.main()
