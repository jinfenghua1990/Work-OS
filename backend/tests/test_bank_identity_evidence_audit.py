"""Bank identity and archive audit regressions, using only temporary SQLite/data."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import bank_source_identity
import original_archive
import evidence_audit
import server


class BankAndEvidenceRegression(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.previous_db=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"workos.sqlite3"
        self.archive=Path(self.temp.name)/"originals"
        self.env=patch.dict(os.environ,{"WORKOS_ARCHIVE_ROOT":str(self.archive)})
        self.env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,data:(status,data)
        raw=b"bank\n2026-11-20,TX001,100\n"
        with server.db() as conn:
            record=original_archive.ingest(conn,io.BytesIO(raw),len(raw),
                "bank-nov.csv","bank","2026-11")
        self.archive_id=record["id"]

    def tearDown(self):
        server.DB_PATH=self.previous_db
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def row(filename="nov.csv",ref="TX001",amount="100"):
        return {
            "source_key":f"{filename}:2",
            "transaction_on":"2026-11-20",
            "account_name":"中国银行尾号8888",
            "bank_reference":ref,
            "description":"货款入账",
            "direction":"in",
            "amount":amount,
            "currency":"CNY",
        }

    def upload(self,row,strict=True):
        return self.api.import_bank({
            "strict_identity":strict,
            "archive_file_id":self.archive_id,
            "rows":[row],
        })

    def test_identical_bank_statement_renamed_remains_one_transaction(self):
        first,payload=self.upload(self.row("first-file.csv"))
        self.assertEqual(first,200,payload)
        self.assertEqual(len(payload["created"]),1,payload)
        canonical=payload["created"][0]["canonical_source_key"]
        self.assertTrue(canonical.startswith("bank:v3:"))
        second,again=self.upload(self.row("renamed-file.xlsx"))
        self.assertEqual(second,200,again)
        self.assertEqual(again["duplicates"],["renamed-file.xlsx:2"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT source_key FROM bank_transactions").fetchone()[0],canonical)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_changed_bank_amount_is_conflict_not_new_transaction(self):
        self.upload(self.row())
        status,response=self.upload(self.row("updated.csv",amount="200"))
        self.assertEqual(status,200)
        self.assertEqual(len(response["created"]),0)
        self.assertEqual(len(response["rejected"]),1)
        self.assertIn("different business data",response["rejected"][0]["error"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0],1)

    def test_missing_bank_reference_cannot_be_auto_posted(self):
        status,response=self.upload(self.row(ref=""))
        self.assertEqual(status,200)
        self.assertEqual(len(response["created"]),0)
        self.assertIn("bank-issued transaction reference",response["rejected"][0]["error"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0],0)

    def test_legacy_file_row_is_matched_by_natural_bank_reference(self):
        status,result=self.upload(self.row(filename="old-nov.csv"),strict=False)
        self.assertEqual(status,200,result)
        self.assertEqual(len(result["created"]),1)
        second,again=self.upload(self.row("fresh-reexport.xlsx"))
        self.assertEqual(second,200,again)
        self.assertEqual(len(again["created"]),0)
        self.assertEqual(len(again["duplicates"]),1)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0],1)

    def test_duplicate_legacy_ids_require_manual_review(self):
        self.upload(self.row(filename="old-one.csv"),strict=False)
        self.upload(self.row(filename="old-two.csv"),strict=False)
        status,result=self.upload(self.row("new.xlsx"))
        self.assertEqual(status,200,result)
        self.assertEqual(len(result["rejected"]),1)
        self.assertIn("multiple historical transactions",result["rejected"][0]["error"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0],2)

    def test_audit_verifies_bytes_and_business_references(self):
        self.upload(self.row())
        with server.db() as conn:
            audit=evidence_audit.audit_evidence(conn)
        self.assertEqual(audit["status"],"passed",audit)
        self.assertEqual(audit["file_total"],1)
        self.assertEqual(audit["link_total"],1)
        self.assertEqual(audit["missing_files"],0)
        self.assertEqual(audit["broken_business_links"],0)
        self.assertEqual(audit["files_without_business_links"],0)
        self.assertEqual(audit["checked_known_business_links"],1)

    def test_audit_detects_corruption_and_dangling_business_link(self):
        with server.db() as conn:
            original_archive.link_document(conn,self.archive_id,
                "finance.bank_transaction","missing-transaction-id")
        file_path=self.archive/self.archive_id[:2]/self.archive_id[2:4]/self.archive_id
        file_path.write_bytes(b"tampered!")
        with server.db() as conn:
            audit=evidence_audit.audit_evidence(conn)
        self.assertEqual(audit["status"],"needs_attention")
        self.assertEqual(audit["corrupted_files"],1)
        self.assertEqual(audit["broken_business_links"],1)

    def test_historic_bank_row_without_original_is_reported(self):
        status,response=self.api.import_bank({
            "rows":[self.row(filename="historical.csv")]
        })
        self.assertEqual(status,200,response)
        self.assertEqual(len(response["created"]),1)
        with server.db() as conn:
            audit=evidence_audit.audit_evidence(conn)
        self.assertEqual(audit["status"],"needs_attention")
        self.assertEqual(audit["business_records_without_evidence"],1)
        self.assertEqual(audit["business_without_evidence_by_type"]["finance.bank_transaction"],1)
        self.assertEqual(audit["missing_files"],0)

    def test_identity_ignores_filename_and_preserves_reference(self):
        row=self.row("nov.csv",ref="000098")
        self.assertEqual(bank_source_identity.bank_reference_identity(row),
                         bank_source_identity.bank_reference_identity(self.row("renamed.xlsx",ref="000098")))
        self.assertNotEqual(bank_source_identity.bank_reference_identity(row),
                            bank_source_identity.bank_reference_identity(self.row(ref="98")))


if __name__=="__main__":
    unittest.main()
