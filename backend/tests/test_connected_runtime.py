"""Local connected runtime refuses a silent SQLite database fallback."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"app"))
import server
import run_connected

class ConnectedRuntimeChecks(unittest.TestCase):
    def test_offline_database_is_rejected_as_shared(self):
        with patch.object(server,"DB_BACKEND","sqlite"):
            with self.assertRaisesRegex(RuntimeError,"SQLite"):
                run_connected.check_configuration()

    def test_shared_neon_requires_auth_r2_and_backup_location(self):
        with patch.object(server,"DB_BACKEND","postgres"),patch.object(
            server,"DATABASE_URL","postgresql://localhost/workos"
        ):
            with patch.dict(os.environ,{
                "WORKOS_AUTH_PASSWORD_HASH":"",
                "WORKOS_R2_ACCOUNT_ID":"",
                "WORKOS_ARCHIVE_ROOT":"",
            }):
                with self.assertRaisesRegex(RuntimeError,"WORKOS_AUTH_PASSWORD_HASH"):
                    run_connected.check_configuration()

    def test_configured_local_neon_mode_can_pass_without_touching_db(self):
        password=server.local_auth.password_hash("secure-test-only-2026")
        env={
            "WORKOS_AUTH_PASSWORD_HASH":password,
            "WORKOS_R2_ACCOUNT_ID":"f"*32,
            "WORKOS_R2_BUCKET":"work-os-originals",
            "WORKOS_R2_ACCESS_KEY_ID":"test-only",
            "WORKOS_R2_SECRET_ACCESS_KEY":"test-only",
            "WORKOS_ARCHIVE_ROOT":str(ROOT.resolve()),
            "WORKOS_HOST":"127.0.0.1",
            "WORKOS_EVIDENCE_STORAGE":"r2",
        }
        with patch.object(server,"DB_BACKEND","postgres"),patch.object(
            server,"DATABASE_URL","postgresql://localhost/workos"
        ),patch.dict(os.environ,env):
            run_connected.check_configuration()

if __name__=="__main__":
    unittest.main()
