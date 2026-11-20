"""Finance regressions in isolated SQLite; never touches Neon."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server


class FinancePeriodAndBikeCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous_db=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"finance.sqlite3"
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)

    def tearDown(self):
        server.DB_PATH=self.previous_db
        self.tmp.cleanup()

    def test_closed_invoice_issue_month_allows_later_tax_confirmation(self):
        invoice={
            "source_key":"input-2026-10-1","invoice_number":"2026-10-1",
            "direction":"purchase","counterparty":"供应商甲","issued_on":"2026-10-15",
            "total_amount":"113.00","tax_amount":"13.00","currency":"CNY","exchange_rate":"1",
        }
        status,created=self.api.create_invoice(invoice)
        self.assertEqual(status,201,created)
        with server.db() as conn:
            conn.execute("INSERT INTO period_locks (period,closed_at,closed_by,reviewed_checks,journal_count) VALUES(?,?,?,?,?)",
                         ("2026-10","2026-11-01T00:00:00+00:00","test-reviewer","[]",0))
        status,result=self.api.update_invoice_tax_status({
            "invoice_id":created["id"],"tax_status":"certified_deductible",
            "tax_evidence_ref":"tax-office-verified-001","effective_on":"2026-11-20","changed_by":"test-reviewer",
        })
        self.assertEqual(status,200,result)
        self.assertEqual(result["tax_period"],"2026-11")
        self.assertFalse(result["tax_credit_recognized"])
        with server.db() as conn:
            history=conn.execute("SELECT effective_on,tax_period FROM invoice_tax_history WHERE invoice_id=?",(created["id"],)).fetchone()
            self.assertEqual(history["tax_period"],"2026-11")
            self.assertEqual(history["effective_on"],"2026-11-20")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_closed_tax_action_period_rejects_backdating(self):
        status,created=self.api.create_invoice({"source_key":"tax-2","invoice_number":"tax-2",
            "direction":"purchase","counterparty":"供应商甲","issued_on":"2026-10-01",
            "total_amount":"113","tax_amount":"13","currency":"CNY","exchange_rate":"1"})
        self.assertEqual(status,201,created)
        with server.db() as conn:
            conn.execute("INSERT INTO period_locks (period,closed_at,closed_by,reviewed_checks,journal_count) VALUES(?,?,?,?,?)",
                         ("2026-10","2026-11-01T00:00:00+00:00","test-reviewer","[]",0))
        with self.assertRaisesRegex(ValueError,"closed"):
            self.api.update_invoice_tax_status({"invoice_id":created["id"],"tax_status":"eligible",
                                                "effective_on":"2026-10-20"})
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT tax_status FROM invoices WHERE id=?",(created["id"],)).fetchone()[0],"pending")

    def test_bike_period_expense_vs_landed_cost_and_import_vat(self):
        status,order=self.api.create_bike_order({
            "alsvid_order_no":"ALSVID-PO-001","supplier":"测试自行车厂","sku":"A1-ULT-GRY-EUR",
            "contract_amount":"2000","quantity":2,"currency":"CNY","exchange_rate":"1",
            "rate_date":"2026-11-01","ordered_on":"2026-11-01","entity_code":"main",
            "milestones":[{"name":"全款","percentage":"100","due_on":"2026-11-25"}],
        })
        self.assertEqual(status,201,order)
        for source_key,cost_type,expected in [
            ("ware-1","warehouse","period_expense"),
            ("freight-1","freight","capitalized"),
            ("vat-1","import_vat","tax_pending"),
        ]:
            status,result=self.api.create_bike_landed_cost({
                "source_key":source_key,"bike_order_id":order["id"],"cost_type":cost_type,
                "counterparty":"测试货代","reference_no":source_key,
                "incurred_on":"2026-11-12","amount":"100","currency":"CNY",
                "exchange_rate":"1","rate_date":"2026-11-12",
            })
            self.assertEqual(status,201,result)
            self.assertEqual(result["cost_treatment"],expected)
        with server.db() as conn:
            rows=conn.execute("SELECT l.account,l.debit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.source_type='bike_landed_cost' AND l.debit!='0'").fetchall()
            accounts={row["account"] for row in rows}
            self.assertEqual(accounts,{"inventory:bike","operating_expense:logistics"})
            capitalized=conn.execute("SELECT SUM(CAST(base_amount_cny AS REAL)) FROM bike_landed_costs WHERE cost_treatment='capitalized'").fetchone()[0]
            self.assertEqual(capitalized,100.0)

    def test_other_capitalization_requires_basis(self):
        status,order=self.api.create_bike_order({
            "alsvid_order_no":"ALSVID-PO-002","supplier":"测试自行车厂","sku":"A1-ULT-GRY-EUR",
            "contract_amount":"2000","quantity":2,"currency":"CNY","exchange_rate":"1",
            "rate_date":"2026-11-01","ordered_on":"2026-11-01","entity_code":"main",
            "milestones":[{"name":"全款","percentage":"100","due_on":"2026-11-25"}],
        })
        self.assertEqual(status,201)
        data={"source_key":"other-1","bike_order_id":order["id"],"cost_type":"other",
              "cost_treatment":"capitalized","counterparty":"供应商","incurred_on":"2026-11-12",
              "amount":"100","currency":"CNY","exchange_rate":"1","rate_date":"2026-11-12"}
        with self.assertRaisesRegex(ValueError,"capitalization basis"):
            self.api.create_bike_landed_cost(data)
        status,result=self.api.create_bike_landed_cost({**data,"capitalization_basis":"到货前必要港口装卸费用"})
        self.assertEqual(status,201,result)
        self.assertTrue(result["capitalized"])


if __name__=="__main__":
    unittest.main()
