"""Connected NAS backup checksum validation without Neon credentials or pg_dump."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import backup_connected as backup


class ConnectedBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/"originals"
        self.root.mkdir()
        self.raw=b"invoice-id,amount\r\n00001,100.20\r\n"
        self.sha=hashlib.sha256(self.raw).hexdigest()
        self.key=self.sha[:2]+"/"+self.sha[2:4]+"/"+self.sha
        target=self.root/self.key
        target.parent.mkdir(parents=True)
        target.write_bytes(self.raw)
        self.file={"id":self.sha,"sha256":self.sha,"storage_key":self.key,
                   "size_bytes":len(self.raw)}
    def tearDown(self):
        self.tmp.cleanup()
    def test_exact_bytes_and_checksum_verified(self):
        result=backup.validate_nas_mirror(self.root,[self.file])
        self.assertEqual(result["verified_original_files"],1)
        self.assertEqual(result["verified_original_bytes"],len(self.raw))
    def test_tamper_or_path_traversal_refused(self):
        with self.assertRaisesRegex(ValueError,"non-canonical"):
            backup.validate_nas_mirror(self.root,[{**self.file,"storage_key":"../secret"}])
        (self.root/self.key).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError,"size mismatch|SHA-256 mismatch"):
            backup.validate_nas_mirror(self.root,[self.file])
    def test_libpq_environment_keeps_password_out_of_arguments(self):
        url="postgresql://workos:my-pass%40123@ep-abc.neon.tech:5432/neondb?sslmode=require"
        env=backup.pg_environment(url,{"PATH":"/usr/bin"})
        self.assertEqual(env["PGPASSWORD"],"my-pass@123")
        self.assertEqual(env["PGHOST"],"ep-abc.neon.tech")
        self.assertEqual(env["PGDATABASE"],"neondb")
        self.assertEqual(env["PGSSLMODE"],"require")
        self.assertEqual(env["PATH"],"/usr/bin")
        self.assertNotIn(url,str(["pg_dump","--format=custom","--file","test.dump"]))
    def test_insecure_connection_or_pooler_refused(self):
        with self.assertRaisesRegex(ValueError,"TLS"):
            backup.pg_environment("postgresql://u:p@host/neondb?sslmode=disable")
        with self.assertRaisesRegex(ValueError,"direct"):
            backup.pg_environment("postgresql://u:p@ep-abc-pooler.neon.tech/neondb")
    def test_saved_backup_integrity_check_does_not_claim_restore(self):
        folder=Path(self.tmp.name)/"backup"
        folder.mkdir()
        data=b"synthetic compressed PostgreSQL dump placeholder"
        (folder/"database.dump").write_bytes(data)
        meta={
            "database_dump_sha256":hashlib.sha256(data).hexdigest(),
            "originals_root":str(self.root),
            "original_files":[self.file],
            "verified_original_files":1,
        }
        (folder/"manifest.json").write_text(json.dumps(meta),encoding="utf-8")
        status=backup.verify_saved_backup(folder)
        self.assertEqual(status["status"],"backup_checksums_verified")
        self.assertFalse(status["recovery_restore_tested"])
        (folder/"database.dump").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError,"database dump has changed"):
            backup.verify_saved_backup(folder)
    def test_shared_backup_never_silently_uses_sqlite(self):
        with patch.dict("os.environ",{"WORKOS_DB_BACKEND":"sqlite"}):
            with self.assertRaisesRegex(ValueError,"never use SQLite"):
                backup.backup()


if __name__=="__main__":
    unittest.main()
