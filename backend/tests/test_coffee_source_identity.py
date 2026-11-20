"""Local offline smoke tests: python -m unittest discover -s backend/tests -v."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from coffee_source_identity import normalize_sale_source


class CoffeeSaleIdentityTests(unittest.TestCase):
    def test_renaming_file_does_not_change_identity(self):
        row = {"channel": "Taobao", "store_name": "旗舰店", "external_order_no": "T100", "external_line_id": "1", "source_row_key": "old.csv:1", "source": "old.csv"}
        renamed = {**row, "source_row_key": "new.csv:1", "source": "new.csv"}
        self.assertEqual(normalize_sale_source(row)["source_row_key"], normalize_sale_source(renamed)["source_row_key"])
        self.assertEqual(normalize_sale_source(row)["source"], "channel:taobao")

    def test_different_order_or_store_is_not_duplicate(self):
        row = {"channel": "taobao", "store_name": "A", "external_order_no": "T100", "external_line_id": "1"}
        key = normalize_sale_source(row)["source_row_key"]
        self.assertNotEqual(key, normalize_sale_source({**row, "store_name": "B"})["source_row_key"])
        self.assertNotEqual(key, normalize_sale_source({**row, "external_order_no": "T101"})["source_row_key"])

    def test_reject_partial_natural_key(self):
        with self.assertRaisesRegex(ValueError, "requires"):
            normalize_sale_source({"channel": "jd", "external_order_no": "A1", "source_row_key": "legacy:1"})

    def test_refund_does_not_post_as_new_sale(self):
        with self.assertRaisesRegex(ValueError, "separate workflows"):
            normalize_sale_source({"event_type": "refund", "source_row_key": "anything"})

    def test_legacy_client_keeps_existing_key(self):
        identity = normalize_sale_source({"source_row_key": "old-file.csv:2", "source": "old-file.csv", "channel": "jd"})
        self.assertFalse(identity["structured"])
        self.assertEqual(identity["source_row_key"], "old-file.csv:2")


if __name__ == "__main__":
    unittest.main()
