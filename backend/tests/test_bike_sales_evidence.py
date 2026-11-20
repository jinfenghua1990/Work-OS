"""Bicycle sales are single source financial events, not file-name based events."""
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


class BikeSalesEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"bike.sqlite3"
        self.env=patch.dict(os.environ,{"WORKOS_ARCHIVE_ROOT":str(Path(self.tmp.name)/"archive")})
        self.env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda code,payload:(code,payload)
        code,order=self.api.create_bike_order({
            "alsvid_order_no":"ALSV-PO-BIKE-01",
            "supplier":"测试自行车厂","sku":"A1-ULT-GRY-EUR",
            "contract_amount":"2000","quantity":2,"currency":"CNY",
            "exchange_rate":"1","rate_date":"2026-11-01",
            "ordered_on":"2026-11-01","entity_code":"main",
            "milestones":[{"name":"全款","percentage":"100","due_on":"2026-11-25"}],
        })
        self.assertEqual(code,201,order)
        received_code,receipt=self.api.receive_bike_order({
            "source_key":"receive-bike-01","alsvid_order_no":"ALSV-PO-BIKE-01",
            "received_on":"2026-11-05","quantity":2,
        })
        self.assertEqual(received_code,201,receipt)
        raw=b"external_order_no,quantity,net_revenue\nB100,1,1500"
        with server.db() as conn:
            archive=original_archive.ingest(conn,io.BytesIO(raw),len(raw),"bike.csv","sales","2026-11")
        self.file_id=archive["id"]

    def tearDown(self):
        server.DB_PATH=self.previous
        self.env.stop()
        self.tmp.cleanup()

    def payload(self,price="1500"):
        return {"source_key":"bike:sale:v2:shop:ALSV-PO-BIKE-01:B100:line-1",
                "source":"bike.sales","alsvid_order_no":"ALSV-PO-BIKE-01",
                "external_order_no":"B100","channel":"shop",
                "sold_on":"2026-11-20","quantity":1,
                "net_revenue":price,"currency":"CNY",
                "exchange_rate":"1","rate_date":"2026-11-20",
                "archive_file_id":self.file_id}

    def test_original_evidence_and_duplicate_posting(self):
        status,posted=self.api.import_bike_sale(self.payload())
        self.assertEqual(status,201,posted)
        self.assertEqual(posted["revenue_cny"],"1500.00")
        self.assertEqual(posted["cogs_cny"],"1000.00")
        status,duplicate=self.api.import_bike_sale(self.payload())
        self.assertEqual(status,200,duplicate)
        self.assertTrue(duplicate["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bike_sales").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='bike_sale'").fetchone()[0],1)
            link=conn.execute("SELECT file_id,source_system,source_document_id FROM archive_links").fetchone()
            self.assertEqual(link["file_id"],self.file_id)
            self.assertEqual(link["source_system"],"bike.sale")
            self.assertEqual(link["source_document_id"],posted["id"])

    def test_same_identity_changed_amount_requires_review(self):
        self.assertEqual(self.api.import_bike_sale(self.payload())[0],201)
        with self.assertRaisesRegex(ValueError,"different payload"):
            self.api.import_bike_sale(self.payload(price="1600"))
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bike_sales").fetchone()[0],1)


if __name__ == "__main__":
    unittest.main()
