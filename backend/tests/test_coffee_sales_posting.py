"""End-to-end offline checks for the coffee sale import identity and ledger.
Run: python -m unittest discover -s backend/tests -v
No Neon connection, credentials, or actual financial data is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server


class CoffeeSalesPostingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "workos.sqlite3"
        server.init_db()
        self.handler = server.Api.__new__(server.Api)
        self.handler.reply = lambda status, payload: (status, payload)
        for number, sku, category, cost in (
            ("PO-FG-1", "COFFEE-01", "finished_goods", "10"),
            ("PO-PKG-1", "BOX-01", "packaging", "2"),
        ):
            status, result = self.handler.create_purchase({
                "number": number, "supplier": "测试供应商", "sku": sku,
                "item_type": category, "quantity": 100, "unit_cost": cost,
                "currency": "CNY", "exchange_rate": "1", "ordered_on": "2026-11-01",
            })
            self.assertEqual(status, 201)
            status, _ = self.handler.receive(result["id"], {
                "source_key": f"RECEIPT-{number}", "quantity": 100, "received_on": "2026-11-02",
            })
            self.assertEqual(status, 201)
        status, _ = self.handler.save_packaging_rule({
            "sku": "COFFEE-01", "packaging_sku": "BOX-01", "packaging_per_unit": 1,
        })
        self.assertEqual(status, 200)

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def sale_payload(file_name="november.csv", price="20", order="ORDER-001", event="sale"):
        return {
            "source_row_key": f"{file_name}:line-1", "source": file_name,
            "source_file": file_name, "event_type": event,
            "channel": "TAOBAO", "store_name": "Coffee Shop",
            "external_order_no": order, "external_line_id": "line-1",
            "sold_on": "2026-11-05", "sku": "COFFEE-01",
            "quantity": 10, "unit_price": price,
            "business_line": "coffee", "entity_code": "main",
        }

    def test_filename_change_does_not_duplicate_inventory_or_journal(self):
        first_status, first = self.handler.import_sale_row(self.sale_payload())
        self.assertEqual(first_status, 201)
        self.assertEqual(first["result"]["revenue"], "200.00")
        self.assertEqual(first["result"]["cost_of_goods_sold"], "120.00")
        self.assertEqual(first["result"]["gross_profit"], "80.00")
        second_status, second = self.handler.import_sale_row(self.sale_payload("renamed.csv"))
        self.assertEqual(second_status, 200)
        self.assertTrue(second["duplicate"])
        with server.db() as conn:
            rows = conn.execute("SELECT sku,SUM(remaining_qty) AS qty FROM stock_layers GROUP BY sku").fetchall()
            self.assertEqual({row["sku"]: row["qty"] for row in rows}, {"COFFEE-01": 90, "BOX-01": 90})
            count = conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='sale'").fetchone()[0]
            self.assertEqual(count, 1)

    def test_same_order_changed_price_requires_review(self):
        self.handler.import_sale_row(self.sale_payload())
        with self.assertRaisesRegex(ValueError, "different business data"):
            self.handler.import_sale_row(self.sale_payload("second-file.csv", price="21"))
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='sale'").fetchone()[0], 1)

    def test_refund_is_not_another_sale_or_stock_receipt(self):
        with self.assertRaisesRegex(ValueError, "separate workflows"):
            self.handler.import_sale_row(self.sale_payload(event="refund"))
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sale_lines").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT SUM(remaining_qty) FROM stock_layers WHERE sku='COFFEE-01'").fetchone()[0], 100)

    def test_new_order_with_same_line_number_is_distinct(self):
        self.assertEqual(self.handler.import_sale_row(self.sale_payload())[0], 201)
        self.assertEqual(self.handler.import_sale_row(self.sale_payload("other.csv", order="ORDER-002"))[0], 201)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='sale'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
