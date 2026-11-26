"""The committed deploy snapshots must match the code schema and ship empty."""
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_deploy_snapshots as guard


class DeploySnapshotGuardTests(unittest.TestCase):
    def test_committed_snapshots_are_shippable(self):
        self.assertEqual(guard.main([]), 0)

    def test_version_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "workos.sqlite3"
            copy.write_bytes(guard.SNAPSHOTS[0].read_bytes())
            with sqlite3.connect(copy) as conn:
                conn.execute("DELETE FROM schema_migrations WHERE version=46")
            self.assertEqual(guard.main([str(copy)]), 1)

    def test_business_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "workos.sqlite3"
            copy.write_bytes(guard.SNAPSHOTS[0].read_bytes())
            with sqlite3.connect(copy) as conn:
                conn.execute(
                    "INSERT INTO purchases(number,supplier,sku,item_type,ordered_qty,"
                    "unit_cost,created_at) VALUES('CI-1','x','s','finished_goods',1,"
                    "'1.00','2026-01-01T00:00:00Z')"
                )
            self.assertEqual(guard.main([str(copy)]), 1)


if __name__ == "__main__":
    unittest.main()
