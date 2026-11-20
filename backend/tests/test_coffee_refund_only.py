"""Coffee refund-only tests: no phantom stock, no duplicate revenue reversals."""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import coffee_refund_only


class CoffeeRefundWithoutRestockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.prior_db=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"workos.sqlite3"
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda code,payload:(code,payload)
        status,purchase=self.api.create_purchase({
            "number":"P-CFF-REFUND-01","supplier":"咖啡成品工厂",
            "sku":"COFFEE-REFUND","item_type":"finished_goods",
            "quantity":20,"unit_cost":"10","ordered_on":"2026-11-01",
            "currency":"CNY","exchange_rate":"1"
        })
        self.assertEqual(status,201,purchase)
        status,receipt=self.api.receive(purchase["id"],{
            "source_key":"REC-CFF-REFUND-01","quantity":20,"received_on":"2026-11-02"
        })
        self.assertEqual(status,201,receipt)
        status,_=self.api.save_packaging_rule({
            "sku":"COFFEE-REFUND","packaging_included":True
        })
        self.assertEqual(status,200)
        status,sale=self.api.import_sale_row({
            "source_row_key":"SRC-CFF-REFUND-01","source":"test.csv",
            "sold_on":"2026-11-05","sku":"COFFEE-REFUND",
            "quantity":10,"unit_price":"20","channel":"shop",
            "business_line":"coffee","entity_code":"main"
        })
        self.assertEqual(status,201,sale)
        with server.db() as conn:
            self.line=conn.execute("SELECT id FROM sale_lines").fetchone()["id"]
            self.receivable=conn.execute(
                "SELECT id FROM receivables_payables WHERE source_type='sale'"
            ).fetchone()["id"]

    def tearDown(self):
        server.DB_PATH=self.prior_db
        self.tmp.cleanup()

    def refund(self,source_key,amount):
        return self.api.refund_sale_line(self.line,{
            "source_key":source_key,"refunded_on":"2026-11-06",
            "amount":amount,"reason":"平台客户仅退款，无实物退回"
        })

    def test_partial_refund_preserves_stock_and_cogs(self):
        status,refund=self.refund("REF-001","50")
        self.assertEqual(status,201,refund)
        self.assertEqual(refund["amount_cny"],"50.00")
        self.assertEqual(refund["cogs_reversed_cny"],"0.00")
        self.assertFalse(refund["stock_restored"])
        with server.db() as conn:
            left=conn.execute(
                "SELECT SUM(remaining_qty) FROM stock_layers WHERE sku='COFFEE-REFUND'"
            ).fetchone()[0]
            self.assertEqual(left,10)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inventory_movements WHERE movement_type='sale_return'"
            ).fetchone()[0],0)
            receivable=conn.execute("SELECT original_amount FROM receivables_payables WHERE id=?",
                                    (self.receivable,)).fetchone()[0]
            self.assertEqual(Decimal(receivable),Decimal("150.00"))
            self.assertEqual(coffee_refund_only.remaining_revenue(conn,conn.execute(
                "SELECT * FROM sale_lines WHERE id=?",(self.line,)
            ).fetchone()),Decimal("150.00"))

    def test_refund_then_physical_return_never_over_reverses_revenue(self):
        self.refund("REF-002","50")
        status,returned=self.api.return_sale_line(self.line,{
            "source_key":"RETURN-002","quantity":3,
            "packaging_quantity":0,"returned_on":"2026-11-07"
        })
        self.assertEqual(status,201,returned)
        self.assertEqual(returned["revenue_reversed_cny"],"60.00")
        self.assertEqual(returned["cogs_reversed_cny"],"30.00")
        with self.assertRaisesRegex(ValueError,"exceeds remaining original sale revenue"):
            self.refund("REF-TOO-MUCH","100")
        status,final=self.refund("REF-FINAL","90")
        self.assertEqual(status,201,final)
        status,rest=self.api.return_sale_line(self.line,{
            "source_key":"RETURN-FINAL","quantity":7,
            "packaging_quantity":0,"returned_on":"2026-11-08"
        })
        self.assertEqual(status,201,rest)
        self.assertEqual(rest["revenue_reversed_cny"],"0.00")
        self.assertEqual(rest["cogs_reversed_cny"],"70.00")
        with server.db() as conn:
            remaining=conn.execute("SELECT SUM(remaining_qty) FROM stock_layers WHERE sku='COFFEE-REFUND'").fetchone()[0]
            self.assertEqual(remaining,20)
            revenue=conn.execute(
                "SELECT l.debit,l.credit FROM journal_lines l "
                "WHERE l.account='sales_revenue'"
            ).fetchall()
            net_revenue=sum((Decimal(r["credit"])-Decimal(r["debit"]) for r in revenue),Decimal(0))
            self.assertEqual(net_revenue,Decimal(0))
            cogs=conn.execute(
                "SELECT l.debit,l.credit FROM journal_lines l WHERE l.account='cost_of_goods_sold'"
            ).fetchall()
            net_cogs=sum((Decimal(r["debit"])-Decimal(r["credit"]) for r in cogs),Decimal(0))
            self.assertEqual(net_cogs,Decimal(0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sales_refunds").fetchone()[0],2)

    def test_replayed_source_key_is_idempotent_not_a_second_refund(self):
        status,original=self.refund("REF-IDENTICAL","25")
        self.assertEqual(status,201,original)
        status,again=self.refund("REF-IDENTICAL","25")
        self.assertEqual(status,200,again)
        self.assertTrue(again["duplicate"])
        with self.assertRaisesRegex(ValueError,"different"):
            self.refund("REF-IDENTICAL","30")
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sales_refunds").fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE source_type='sales_refund_only'"
            ).fetchone()[0],1)

    def test_partly_collected_sale_creates_customer_refund_payable(self):
        status,bank=self.api.import_bank({"rows":[{
            "source_key":"BANK-PAID-001","account_name":"测试银行",
            "bank_reference":"RECEIVED-001","transaction_on":"2026-11-05",
            "description":"客户部分付款","direction":"in","amount":"150",
            "currency":"CNY",
        }]})
        self.assertEqual(status,200,bank)
        self.assertEqual(len(bank["created"]),1,bank)
        status,allocation=self.api.allocate_bank({
            "bank_transaction_id":bank["created"][0]["id"],
            "allocations":[{"target_type":"receivable",
                            "target_id":self.receivable,"amount":"150"}],
        })
        self.assertIn(status,(200,201),allocation)
        status,refund=self.refund("REF-COLLECTED-001","100")
        self.assertEqual(status,201,refund)
        self.assertEqual(refund["ar_credit_cny"],"50.00")
        self.assertEqual(refund["customer_refund_due_cny"],"50.00")
        with server.db() as conn:
            receivable=conn.execute("SELECT original_amount,settled_amount FROM receivables_payables WHERE id=?",
                                     (self.receivable,)).fetchone()
            self.assertEqual(Decimal(receivable["original_amount"]),Decimal("150.00"))
            self.assertEqual(Decimal(receivable["settled_amount"]),Decimal("150.00"))
            payable=conn.execute(
                "SELECT original_amount FROM receivables_payables WHERE source_type='sales_refund_only'"
            ).fetchone()
            self.assertEqual(Decimal(payable["original_amount"]),Decimal("50.00"))
            remaining=conn.execute("SELECT SUM(remaining_qty) FROM stock_layers WHERE sku='COFFEE-REFUND'").fetchone()[0]
            self.assertEqual(remaining,10)

    def test_refund_key_cannot_be_used_as_physical_return(self):
        self.refund("REF-SHARED","10")
        with self.assertRaisesRegex(ValueError,"refund-only event"):
            self.api.return_sale_line(self.line,{
                "source_key":"REF-SHARED","quantity":1,
                "packaging_quantity":0,"returned_on":"2026-11-07"
            })

    def test_historical_schema_migration_can_repeat(self):
        server.init_db()
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                server.LATEST_SCHEMA_VERSION)


if __name__=="__main__":
    unittest.main()
