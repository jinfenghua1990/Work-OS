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


    def test_bike_operating_expense_and_personal_vendor_invoice_are_attributed(self):
        now=server.now()
        with server.db() as conn:
            conn.execute(
                "INSERT INTO expenses "
                "(id,source_key,incurred_on,category,counterparty,amount,currency,"
                "memo,created_at,business_line,entity_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("exp1","bike-exp1","2026-11-12","logistics","德国物流公司",
                 "250.00","CNY","",now,"bike","main")
            )
            conn.execute(
                "INSERT INTO expenses "
                "(id,source_key,incurred_on,category,counterparty,amount,currency,"
                "memo,created_at,business_line,entity_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("exp2","coffee-exp2","2026-11-12","logistics","咖啡物流公司",
                 "80.00","CNY","",now,"coffee","main")
            )
            for row_id,exp,merchant,amount in (
                ("pay1","exp1","德国物流公司","250.00"),
                ("pay2","exp2","咖啡物流公司","80.00"),
            ):
                conn.execute(
                    "INSERT INTO receivables_payables "
                    "(id,direction,source_type,source_id,counterparty,original_amount,"
                    "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row_id,"payable","operating_expense",exp,merchant,
                     amount,"0.00","CNY",None,now)
                )
            for invoice_id,merchant,target in (
                ("inv-bike","德国物流公司","pay1"),
                ("inv-coffee","咖啡物流公司","pay2")
            ):
                conn.execute(
                    "INSERT INTO invoices "
                    "(id,invoice_number,direction,counterparty,issued_on,"
                    "total_amount,tax_amount,currency,source_key,tax_status,"
                    "exchange_rate,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (invoice_id,invoice_id,"purchase",merchant,"2026-11-12",
                     "250.00" if target=="pay1" else "80.00","0","CNY",
                     invoice_id,"pending","1",now)
                )
                conn.execute(
                    "INSERT INTO invoice_links "
                    "(id,invoice_id,target_type,target_id,linked_amount,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    ("link-"+invoice_id,invoice_id,"payable",target,
                     "250.00" if target=="pay1" else "80.00",now)
                )
            report=alsvid_finance_summary.overview(conn,"2026-11")
        self.assertEqual(report["financial"]["accounts_payable"],
                         [{"currency":"CNY","amount":"250.00"}])
        self.assertEqual(report["financial"]["invoices"]["input"],1)
        self.assertEqual(report["financial"]["invoice_linked"],
                         [{"currency":"CNY","amount":"250.00"}])
        self.assertFalse(any(x["reference"]=="pay2" for x in report["exceptions"]["items"]))

    def test_same_bank_original_missing_counted_once_for_multiple_alsvid_allocations(self):
        now=server.now()
        with server.db() as conn:
            conn.execute(
                "INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,"
                "description,direction,amount,currency,created_at,exchange_rate,"
                "rate_date,rate_source,base_amount_cny)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bank1","bank1","2026-11-19","公司银行","TX1",
                 "自行车两笔付款","out","100.00","CNY",now,"1",
                 "2026-11-19","functional_currency","100.00")
            )
            for identifier in ("a","b"):
                conn.execute(
                    "INSERT INTO receivables_payables "
                    "(id,direction,source_type,source_id,counterparty,"
                    "original_amount,settled_amount,currency,due_on,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("p-"+identifier,"payable","bike_milestone",identifier,
                     "供应商","50.00","0.00","CNY",None,now)
                )
                conn.execute(
                    "INSERT INTO bank_allocations "
                    "(id,bank_transaction_id,target_type,target_id,amount,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    ("alloc-"+identifier,"bank1","payable",
                     "p-"+identifier,"50.00",now)
                )
            result=alsvid_finance_summary.overview(conn,"2026-11")
        self.assertEqual(result["financial"]["cash_paid"],
                         [{"currency":"CNY","amount":"100.00"}])
        self.assertEqual(result["financial"]["bank_allocation_count"],2)
        self.assertEqual(
            sum(1 for x in result["exceptions"]["items"]
                if x["code"]=="bank_original_missing"),1
        )

    def test_month_cash_is_not_reported_as_historical_payable_balance(self):
        with server.db() as conn:
            result=alsvid_finance_summary.overview(conn,"2026-10")
        self.assertEqual(result["period_scope"]["cash"],"selected_month")
        self.assertEqual(result["period_scope"]["payables"],
                         "current_outstanding_not_historical_month_end")
        self.assertFalse(result["financial"]["ledger_reference_complete"])

    def test_alsvid_invoice_without_business_identity_requires_review(self):
        now=server.now()
        with server.db() as conn:
            conn.execute(
                "INSERT INTO invoices "
                "(id,invoice_number,direction,counterparty,issued_on,"
                "total_amount,tax_amount,currency,source_key,tax_status,"
                "exchange_rate,created_at,source_system)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("inv-1","INV-1","purchase","供应商","2026-11-19",
                 "100.00","0","CNY","INV-1","pending","1",now,"alsvid")
            )
            result=alsvid_finance_summary.overview(conn,"2026-11")
        self.assertEqual(result["financial"]["invoices"]["without_business_link"],1)
        self.assertTrue(any(x["code"]=="invoice_source_identity_missing"
                            for x in result["exceptions"]["items"]))

    def test_invalid_period_never_silently_returns_empty_as_success(self):
        with server.db() as conn:
            for invalid in ("2026-13","2026-11-20","wrong",None):
                with self.assertRaises(ValueError):
                    alsvid_finance_summary.overview(conn,invalid)

if __name__=="__main__":
    unittest.main()
