"""ALSVID read-only finance workbench. Local ephemeral SQLite only."""
import sys
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import alsvid_finance_summary


class AlsvidReadOnlyFinanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.previous_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"workos.sqlite3"
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)

    def tearDown(self):
        server.DB_PATH=self.previous_path
        self.temp.cleanup()

    def test_never_fakes_alsvid_business_data(self):
        with server.db() as conn:
            report=alsvid_finance_summary.overview(conn,"2026-11")
            self.assertEqual(report["integration"]["status"],"not_connected")
            self.assertFalse(report["integration"]["connected"])
            self.assertIsNone(report["business"]["purchase_order_count"])
            self.assertIsNone(report["business"]["sales_order_count"])
            self.assertIsNone(report["business"]["inventory_count"])
            self.assertFalse(report["financial"]["ledger_reference_complete"])
            self.assertTrue(report["read_only"])
            self.assertEqual(report["exceptions"]["blocking"],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_only_linked_alsvid_bank_allocations_are_attributed(self):
        now=server.now()
        with server.db() as conn:
            # Mixed business in the *same bank transaction*: only bike
            # allocation belongs to the ALSVID workbench.
            conn.execute(
                "INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,description,"
                "direction,amount,currency,created_at,exchange_rate,rate_date,rate_source,base_amount_cny)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bank1","bank1","2026-11-20","公司账户","TX-MIXED",
                 "项目共付","out","1000.00","CNY",now,"1","2026-11-20",
                 "functional_currency","1000.00")
            )
            for item,source,amount in (
                ("bike_pay","bike_milestone","300.00"),
                ("coffee_pay","purchase_receipt","700.00"),
            ):
                conn.execute(
                    "INSERT INTO receivables_payables "
                    "(id,direction,source_type,source_id,counterparty,original_amount,"
                    "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (item,"payable",source,item,"供应商",amount,"0.00","CNY",None,now)
                )
                conn.execute(
                    "INSERT INTO bank_allocations "
                    "(id,bank_transaction_id,target_type,target_id,amount,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (item+"-alloc","bank1","payable",item,amount,now)
                )
            before=conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
            report=alsvid_finance_summary.overview(conn,"2026-11")
            after=conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        self.assertEqual(report["financial"]["cash_paid"],[
            {"currency":"CNY","amount":"300.00"}
        ])
        self.assertEqual(report["financial"]["bank_allocation_count"],1)
        self.assertEqual(before,after)

    def test_alsvid_invoice_uses_own_identity_not_all_company_invoices(self):
        now=server.now()
        with server.db() as conn:
            for item,source in (("bike1","bike_milestone"),("coffee1","purchase_receipt")):
                conn.execute(
                    "INSERT INTO receivables_payables "
                    "(id,direction,source_type,source_id,counterparty,original_amount,"
                    "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (item,"payable",source,item,"供应商","100","0","CNY",None,now)
                )
            for identifier,origin in (("bike_invoice","alsvid"),("coffee_invoice","coffee")):
                conn.execute(
                    "INSERT INTO invoices "
                    "(id,invoice_number,direction,counterparty,issued_on,total_amount,"
                    "tax_amount,currency,source_key,tax_status,exchange_rate,created_at,source_system)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier,identifier,"purchase","供应商","2026-11-20","100",
                     "0","CNY",identifier,"pending","1",now,origin)
                )
            conn.execute(
                "INSERT INTO invoice_links "
                "(id,invoice_id,target_type,target_id,linked_amount,created_at)"
                " VALUES(?,?,?,?,?,?)",
                ("link1","bike_invoice","payable","bike1","100",now),
            )
            report=alsvid_finance_summary.overview(conn,"2026-11")
        self.assertEqual(report["financial"]["invoices"]["input"],1)
        self.assertEqual(report["financial"]["invoices"]["tax_pending"],1)
        self.assertEqual(report["financial"]["invoice_linked"],[
            {"currency":"CNY","amount":"100.00"}
        ])
        self.assertTrue(any(x["code"]=="invoice_original_missing" for x in
                            report["exceptions"]["items"]))

    def test_prohibit_duplicate_bike_business_writes_at_http_dispatch(self):
        self.api.path="/api/bike/orders"
        self.api.headers={}
        self.api._session_valid=lambda:True
        with patch.object(server.local_auth,"origin_allowed",return_value=True):
            status,body=self.api.do_POST()
        self.assertEqual(status,410)
        self.assertEqual(body["code"],"ALSV_BIKE_WRITES_DISABLED")

    def test_invalid_period_never_silently_returns_empty_as_success(self):
        with server.db() as conn:
            for invalid in ("2026-13","2026-11-20","wrong",None):
                with self.assertRaises(ValueError):
                    alsvid_finance_summary.overview(conn,invalid)

if __name__=="__main__":
    unittest.main()
