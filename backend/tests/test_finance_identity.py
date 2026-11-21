"""Named local finance accounts and real two-person claim approval."""
from __future__ import annotations

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
import finance_identity
import personal_advances


class NamedFinanceApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_path=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"finance.sqlite3"
        self.env=patch.dict(os.environ,{
            "WORKOS_AUTH_MODE":"named",
            "WORKOS_ARCHIVE_ROOT":str(Path(self.tmp.name)/"originals"),
            "WORKOS_EVIDENCE_STORAGE":"database",
        })
        self.env.start()
        finance_identity.reset_for_tests()
        server.init_db()
        with server.db() as conn:
            self.clerk=finance_identity.create_user(conn,"accountant01","a"*18,"finance_clerk")
            self.approver=finance_identity.create_user(conn,"approver01","b"*18,"finance_approver")
            self.owner=finance_identity.create_user(conn,"owner01","c"*18,"owner")
            self.viewer=finance_identity.create_user(conn,"viewer01","d"*18,"viewer")
            data=b"ACTUAL PERSONAL PAYMENT EVIDENCE"
            self.proof=original_archive.ingest(
                conn,io.BytesIO(data),len(data),"receipt.pdf","expense","2026-11"
            )["id"]
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)
        self.api._principal=self.clerk

    def tearDown(self):
        finance_identity.reset_for_tests()
        self.env.stop()
        server.DB_PATH=self.old_path
        self.tmp.cleanup()

    def claim(self,key="personal-proof-01"):
        return {
            "source_key":key,"claimant":"业务员工","merchant":"软件供应商",
            "paid_on":"2026-11-18","category":"software","amount":"100.00",
            "mode":"expense","business_line":"corporate","entity_code":"main",
            "currency":"CNY","payment_reference":"PERSONAL-TXN-1",
            "archive_file_id":self.proof,
        }

    def test_schema45_no_default_user_and_roles(self):
        with server.db() as conn:
            version=conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertEqual(version,45)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM finance_users").fetchone()[0],4)
        self.assertFalse(finance_identity.can_write(self.viewer,"/api/expenses"))
        self.assertFalse(finance_identity.can_write(self.clerk,"/api/bank/allocate"))
        self.assertFalse(finance_identity.can_write(self.clerk,"/api/personal-advances/approve"))
        self.assertTrue(finance_identity.can_write(self.clerk,"/api/personal-advances"))
        self.assertTrue(finance_identity.can_write(self.approver,"/api/personal-advances/approve"))
        self.assertFalse(finance_identity.can_approve(self.clerk))

    def test_scrypt_named_login_revoked_on_deactivation(self):
        with server.db() as conn:
            self.assertIsNone(finance_identity.login(conn,"127.0.0.1","accountant01","wrong"))
            token=finance_identity.login(conn,"127.0.0.1","accountant01","a"*18)
            self.assertIsNotNone(token)
            principal=finance_identity.principal(conn,{"Cookie":finance_identity.cookie(token)})
            self.assertEqual(principal["username"],"accountant01")
            self.assertEqual(principal["role"],"finance_clerk")
            conn.execute("UPDATE finance_users SET active=0 WHERE id=?",(self.clerk["id"],))
            self.assertIsNone(finance_identity.principal(
                conn,{"Cookie":finance_identity.cookie(token)}
            ))

    def test_claimant_cannot_approve_own_claim_even_with_spoofed_payload(self):
        status,claim=self.api.create_personal_advance(self.claim())
        self.assertEqual(status,201,claim)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
        self.api._principal=self.clerk
        with self.assertRaisesRegex(PermissionError,"approver"):
            self.api.approve_personal_advance({
                "advance_id":claim["id"],"approved_by":"approver01",
            })
        self.api._principal=self.owner
        status2,own=self.api.create_personal_advance({
            **self.claim("another-source"),
            "payment_reference":"PERSONAL-TXN-2",
        })
        self.assertEqual(status2,201,own)
        with self.assertRaisesRegex(ValueError,"two different"):
            self.api.approve_personal_advance({
                "advance_id":own["id"],"approved_by":"approver01",
            })
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_independent_reviewer_sets_trusted_actor_and_journals_once(self):
        _,claim=self.api.create_personal_advance(self.claim())
        self.api._principal=self.approver
        status,approved=self.api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"FAKE-PAYLOAD-USER",
            "accounting_on":"2026-11-18",
        })
        self.assertEqual(status,201,approved)
        with server.db() as conn:
            review=conn.execute(
                "SELECT * FROM finance_personal_advance_reviews WHERE advance_id=?",
                (claim["id"],)
            ).fetchone()
            row=conn.execute(
                "SELECT * FROM personal_advances WHERE id=?",(claim["id"],)
            ).fetchone()
            self.assertEqual(review["submitted_by_user_id"],self.clerk["id"])
            self.assertEqual(review["approved_by_user_id"],self.approver["id"])
            self.assertEqual(row["approved_by"],"approver01")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],2)
        duplicate_status,duplicate=self.api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"anything"
        })
        self.assertEqual(duplicate_status,200,duplicate)
        self.assertTrue(duplicate["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],2)

    def test_legacy_unattributed_draft_is_fail_closed(self):
        with server.db() as conn:
            record=personal_advances.draft(conn,self.claim("unattributed"),server.now)
        self.api._principal=self.approver
        with self.assertRaisesRegex(ValueError,"identifiable preparer"):
            self.api.approve_personal_advance({
                "advance_id":record["id"],"approved_by":"fake",
            })
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)


if __name__=="__main__":
    unittest.main()
