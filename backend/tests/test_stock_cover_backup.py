"""Stock cover and verified NAS-style SQLite/asset backup tests."""
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))
sys.path.insert(0,str(BACKEND/"app"))

import server
import original_archive
import backup_local
from coffee_stock_cover import stock_cover


class CoffeeStockCoverBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous_db=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"database.sqlite3"
        self.archive=Path(self.tmp.name)/"archive"
        self.env=patch.dict("os.environ",{"WORKOS_ARCHIVE_ROOT":str(self.archive)})
        self.env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)

    def tearDown(self):
        self.env.stop()
        server.DB_PATH=self.previous_db
        self.tmp.cleanup()

    def test_cover_uses_recent_sales_and_includes_out_of_stock_skus(self):
        today=date.today().isoformat()
        status,purchase=self.api.create_purchase({"number":"COVER-PO-1","supplier":"测试工厂",
            "sku":"COVER-SKU","item_type":"finished_goods","quantity":100,
            "unit_cost":"10","ordered_on":today})
        self.assertEqual(status,201)
        status,_=self.api.receive(purchase["id"],{"source_key":"COVER-REC-1",
            "quantity":100,"received_on":today})
        self.assertEqual(status,201)
        self.api.save_packaging_rule({"sku":"COVER-SKU","packaging_included":True})
        status,sale=self.api.import_sale_row({"source_row_key":"cover-row-1",
            "sold_on":today,"source":"cover.csv","sku":"COVER-SKU",
            "quantity":10,"unit_price":"20","channel":"test"})
        self.assertEqual(status,201,sale)
        with server.db() as conn:
            data=stock_cover(conn)
        item=next(row for row in data["items"] if row["sku"]=="COVER-SKU")
        self.assertEqual(item["on_hand"],90)
        self.assertEqual(item["last_30_day_net_sales"],10)
        self.assertEqual(item["estimated_cover_days"],"270.00")
        # Consume the remaining stock: an out-of-stock SKU must stay visible.
        status,sale=self.api.import_sale_row({"source_row_key":"cover-row-2",
            "sold_on":today,"source":"cover.csv","sku":"COVER-SKU",
            "quantity":90,"unit_price":"20","channel":"test"})
        self.assertEqual(status,201,sale)
        with server.db() as conn:
            zero=next(row for row in stock_cover(conn)["items"] if row["sku"]=="COVER-SKU")
        self.assertEqual(zero["on_hand"],0)
        self.assertEqual(zero["estimated_cover_days"],"0.00")
        self.assertEqual(zero["status"],"less_than_30_days")

    def test_backup_copies_exact_original_bytes_and_database(self):
        doc=b"date,bank,amount\n2026-11-20,bank,100.00"
        with server.db() as conn:
            evidence=original_archive.ingest(conn,io.BytesIO(doc),len(doc),
                "bank.csv","bank","2026-11")
        folder=backup_local.checked_backup(server.DB_PATH,self.archive,Path(self.tmp.name)/"backup")
        status=backup_local.verify_backup(folder)
        self.assertTrue(status["database_verified"])
        self.assertEqual(status["files_verified"],1)
        original=folder/"archive"/evidence["id"][:2]/evidence["id"][2:4]/evidence["id"]
        self.assertEqual(original.read_bytes(),doc)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0],1)

    def test_backup_detects_tampering(self):
        folder=backup_local.checked_backup(server.DB_PATH,self.archive,Path(self.tmp.name)/"backup")
        (folder/"workos.sqlite3").write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError,"checksum"):
            backup_local.verify_backup(folder)


if __name__=="__main__":
    unittest.main()
