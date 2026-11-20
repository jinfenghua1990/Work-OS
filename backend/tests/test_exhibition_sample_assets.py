"""Exhibition sample FIFO accounting: transfer assets, return or expense, never both."""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import exhibition_assets


class ExhibitionSampleAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.original_db=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"exhibition.sqlite3"
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)
        status,_=self.api.create_project({
            "code":"EVT-2026-11","name":"咖啡展会",
            "entity_code":"main","starts_on":"2026-11-10",
            "ends_on":"2026-11-18",
        })
        self.assertEqual(status,201)
        for number,sku,item_type,cost in (
            ("PO-COFFEE","COFFEE-01","finished_goods","10"),
            ("PO-BOX","BOX-01","packaging","2"),
        ):
            status,purchase=self.api.create_purchase({
                "number":number,"supplier":"测试供应商",
                "sku":sku,"item_type":item_type,
                "quantity":20,"unit_cost":cost,
                "ordered_on":"2026-11-01",
                "entity_code":"main","currency":"CNY",
            })
            self.assertEqual(status,201,purchase)
            status,_=self.api.receive(purchase["id"],{
                "source_key":"RECEIPT-"+number,"quantity":20,
                "received_on":"2026-11-02",
            })
            self.assertEqual(status,201)
        self.api.save_packaging_rule({
            "sku":"COFFEE-01","packaging_sku":"BOX-01",
            "packaging_per_unit":1,
        })

    def tearDown(self):
        server.DB_PATH=self.original_db
        self.tmp.cleanup()

    def sample_dispatch(self):
        status,result=self.api.dispatch_sample({
            "source_key":"OUT-2026-001","project_code":"EVT-2026-11",
            "sku":"COFFEE-01","quantity":10,"dispatched_on":"2026-11-10",
        })
        self.assertEqual(status,201,result)
        self.assertEqual(result["cost"],"120.00")
        return result["id"]

    def test_asset_transfer_partial_return_consumption_and_full_resolution(self):
        dispatch_id=self.sample_dispatch()
        with server.db() as conn:
            self.assertEqual(exhibition_assets.outstanding_asset_value(conn,"2026-11-10"),Decimal("120.00"))
            row=conn.execute("SELECT accounting_treatment,quantity,returned_quantity,disposed_quantity FROM sample_dispatches WHERE id=?",(dispatch_id,)).fetchone()
            self.assertEqual(row["accounting_treatment"],"asset_transfer")
            self.assertEqual(row["disposed_quantity"],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_lines WHERE account='operating_expense:sample_cost'").fetchone()[0],0)

        status,returned=self.api.return_sample({
            "source_key":"RETURN-2026-001","dispatch_id":dispatch_id,
            "quantity":4,"returned_on":"2026-11-11",
        })
        self.assertEqual(status,201,returned)
        self.assertEqual(returned["restored_cost"],"48.00")
        status,disposed=self.api.dispose_sample({
            "source_key":"DISPOSE-2026-001","dispatch_id":dispatch_id,
            "quantity":3,"reason":"consumed","disposed_on":"2026-11-12",
        })
        self.assertEqual(status,201,disposed)
        self.assertEqual(disposed["total_cost"],"36.00")
        status,repeated=self.api.dispose_sample({
            "source_key":"DISPOSE-2026-001","dispatch_id":dispatch_id,
            "quantity":3,"reason":"consumed","disposed_on":"2026-11-12",
        })
        self.assertEqual(status,200,repeated)
        self.assertTrue(repeated["duplicate"])
        with self.assertRaisesRegex(ValueError,"exceeds"):
            self.api.return_sample({
                "source_key":"RETURN-TOO-MANY","dispatch_id":dispatch_id,
                "quantity":4,"returned_on":"2026-11-13",
            })
        status,last=self.api.return_sample({
            "source_key":"RETURN-2026-002","dispatch_id":dispatch_id,
            "quantity":3,"returned_on":"2026-11-13",
        })
        self.assertEqual(status,201,last)
        with server.db() as conn:
            self.assertEqual(exhibition_assets.outstanding_asset_value(conn,"2026-11-11"),Decimal("72.00"))
            self.assertEqual(exhibition_assets.outstanding_asset_value(conn,"2026-11-12"),Decimal("36.00"))
            self.assertEqual(exhibition_assets.outstanding_asset_value(conn,"2026-11-13"),Decimal("0.00"))
            lines=conn.execute(
                "SELECT l.account,l.debit,l.credit FROM journal_lines l "
                "JOIN journal_entries e ON e.id=l.entry_id "
                "WHERE e.project_code='EVT-2026-11'",
            ).fetchall()
            expense=sum((Decimal(x["debit"])-Decimal(x["credit"]) for x in lines
                         if x["account"]=="operating_expense:sample_cost"),Decimal(0))
            outside=sum((Decimal(x["debit"])-Decimal(x["credit"]) for x in lines
                         if x["account"]=="inventory:exhibition"),Decimal(0))
            self.assertEqual(expense,Decimal("36.00"))
            self.assertEqual(outside,Decimal("0.00"))
            stock={x["sku"]:x["qty"] for x in conn.execute(
                "SELECT sku,SUM(remaining_qty) AS qty FROM stock_layers GROUP BY sku")}
            self.assertEqual(stock,{"COFFEE-01":17,"BOX-01":17})
            row=conn.execute("SELECT returned_quantity,disposed_quantity FROM sample_dispatches WHERE id=?",(dispatch_id,)).fetchone()
            self.assertEqual((row["returned_quantity"],row["disposed_quantity"]),(7,3))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample_dispositions").fetchone()[0],1)

    def test_future_dated_and_nonexistent_disposition_are_rejected(self):
        dispatch_id=self.sample_dispatch()
        with self.assertRaisesRegex(ValueError,"precede"):
            self.api.dispose_sample({
                "source_key":"OLD-001","dispatch_id":dispatch_id,
                "quantity":1,"reason":"gifted","disposed_on":"2026-11-09",
            })
        with self.assertRaisesRegex(ValueError,"legacy"):
            with server.db() as conn:
                conn.execute("UPDATE sample_dispatches SET accounting_treatment='legacy_expensed' WHERE id=?",(dispatch_id,))
            self.api.dispose_sample({
                "source_key":"OLD-002","dispatch_id":dispatch_id,
                "quantity":1,"reason":"gifted","disposed_on":"2026-11-12",
            })


if __name__=="__main__":
    unittest.main()
