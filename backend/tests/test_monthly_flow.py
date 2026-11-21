"""Monthly bank/invoice flow accounting-source separation regression."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import monthly_flow

class MonthlyFlowReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"workos.sqlite3"
        server.init_db()
    def tearDown(self):
        server.DB_PATH=self.previous
        self.tmp.cleanup()
    def test_currencies_are_never_merged_and_invoice_is_not_revenue(self):
        with server.db() as conn:
            for ident,currency,direction,value,kind in [
                ("a","CNY","in","0.10","operating"),
                ("b","CNY","in","0.20","operating"),
                ("c","CNY","out","0.05","transfer"),
                ("d","EUR","in","20.00","operating"),
            ]:
                conn.execute(
                    "INSERT INTO bank_transactions "
                    "(id,source_key,transaction_on,account_name,bank_reference,description,"
                    "direction,amount,currency,created_at,cash_flow_category) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (ident,"src"+ident,"2026-11-21","account","TX"+ident,
                     "test",direction,value,currency,server.now(),kind),
                )
            conn.execute("INSERT INTO bank_allocations VALUES(?,?,?,?,?,?)",
                         ("allocation","a","receivable","target","0.05",server.now()))
            for ident,direction,currency,total in [
                ("input","purchase","CNY","300.12"),
                ("sale","sales","CNY","500.27"),
                ("euro","sales","EUR","50.00"),
            ]:
                conn.execute("INSERT INTO invoices "
                    "(id,invoice_number,direction,counterparty,issued_on,total_amount,"
                    "tax_amount,currency,source_key,tax_status,exchange_rate,rate_date,created_at)"
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident,"INV-"+ident,direction,"Company","2026-11-18",
                     total,"0",currency,"src-"+ident,"pending","1","2026-11-18",server.now()))
            report=monthly_flow.report(conn,"2026-11")
        cny=next(x for x in report["bank"]["currencies"] if x["currency"]=="CNY")
        eur=next(x for x in report["bank"]["currencies"] if x["currency"]=="EUR")
        self.assertEqual((cny["inflow"],cny["outflow"],cny["net"]),("0.30","0.05","0.25"))
        self.assertEqual(cny["transfer_out"],"0.05")
        self.assertEqual(cny["unallocated"],"0.30")
        self.assertEqual(cny["unmatched"],3)
        self.assertEqual(eur["inflow"],"20.00")
        invoices=next(x for x in report["invoices"]["currencies"] if x["currency"]=="CNY")
        self.assertEqual(invoices["input_total"],"300.12")
        self.assertEqual(invoices["output_total"],"500.27")
        self.assertEqual(invoices["pending_tax"],2)
        self.assertEqual(report["bank"]["transaction_count"],4)
        self.assertFalse(report["closed"])
        with server.db() as conn:
            other=monthly_flow.report(conn,"2026-10")
        self.assertEqual(other["bank"]["transaction_count"],0)
        self.assertEqual(other["invoices"]["document_count"],0)
    def test_invalid_month_rejected(self):
        with server.db() as conn:
            with self.assertRaisesRegex(ValueError,"YYYY-MM"):
                monthly_flow.report(conn,"2026-13")
    def test_negative_unallocated_is_not_silently_hidden(self):
        with server.db() as conn:
            conn.execute("INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,description,"
                "direction,amount,currency,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("over","bad","2026-11-19","account","TXBAD","test","in","1","CNY",server.now()))
            conn.execute("INSERT INTO bank_allocations VALUES(?,?,?,?,?,?)",
                         ("a1","over","receivable","target","2",server.now()))
            with self.assertRaisesRegex(ValueError,"allocation exceeds"):
                monthly_flow.report(conn,"2026-11")

if __name__=="__main__":
    unittest.main()
