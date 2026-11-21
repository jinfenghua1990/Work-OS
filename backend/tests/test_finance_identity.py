"""Named local finance accounts and real two-person claim approval."""
from __future__ import annotations

import io
import json
import http.client
import threading
import os
import sys
import tempfile
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
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
        # Tax policy, reconciliation confirmations and dynamic reversals are
        # restricted by the backend, not simply hidden in the UI.
        for path in (
            "/api/tax/rates",
            "/api/bank/reconciliation",
            "/api/bank/accounts/coverage",
            "/api/expenses/reconciliation/coverage",
            "/api/inventory/adjustments",
            "/api/platform-income/reconciliation",
            "/api/purchase-receipts/receipt-001/edit",
            "/api/sales/lines/line-001/return",
            "/api/sales/lines/line-001/refund",
            "/api/expenses/allocations/alloc-001/reverse",
        ):
            with self.subTest(path=path):
                self.assertFalse(finance_identity.can_write(self.clerk,path))
                self.assertTrue(finance_identity.can_write(self.approver,path))
        self.assertTrue(finance_identity.can_write(self.clerk,"/api/tax/rates/draft"))
        self.assertTrue(finance_identity.can_write(self.clerk,"/api/invoices/red"))

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

    def test_http_named_session_roles_and_browser_gate(self):
        httpd=ThreadingHTTPServer(("127.0.0.1",0),server.Api)
        port=httpd.server_address[1]
        thread=threading.Thread(target=httpd.serve_forever,daemon=True)
        thread.start()
        def request(method,path,payload=None,cookie=None,origin=None):
            connection=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
            headers={}
            if payload is not None:
                headers["Content-Type"]="application/json"
            if cookie:
                headers["Cookie"]=cookie
            if origin:
                headers["Origin"]=origin
            connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
            response=connection.getresponse()
            body=response.read()
            result=(response.status,json.loads(body) if body else {},response.getheader("Set-Cookie"))
            connection.close()
            return result
        try:
            status,body,_=request("GET","/api/health")
            self.assertEqual(status,401,body)
            status,body,cookie=request("POST","/api/auth/login",{
                "username":"accountant01","password":"a"*18
            })
            self.assertEqual(status,200,body)
            cookie=cookie.split(";",1)[0]
            status,body,_=request("GET","/api/auth/status",cookie=cookie)
            self.assertEqual(status,200,body)
            self.assertEqual(body["username"],"accountant01")
            status,body,_=request("POST","/api/bank/allocate",{},cookie=cookie)
            self.assertEqual(status,403,body)
            status,body,_=request("POST","/api/personal-advances/approve",{},cookie=cookie)
            self.assertEqual(status,403,body)
            for restricted in (
                "/api/tax/rates",
                "/api/bank/accounts/coverage",
                "/api/expenses/allocations/abc/reverse",
                "/api/sales/lines/abc/refund",
            ):
                status,body,_=request("POST",restricted,{},cookie=cookie)
                self.assertEqual(status,403,(restricted,body))
            status,body,_=request("POST","/api/invoices",{},cookie=cookie)
            self.assertEqual(status,422,body)
            status,body,_=request("POST","/api/invoices",{},cookie=cookie,
                                   origin="https://attacker.example")
            self.assertEqual(status,403,body)
            viewer=request("POST","/api/auth/login",{
                "username":"viewer01","password":"d"*18
            })
            self.assertEqual(viewer[0],200)
            read_only_cookie=viewer[2].split(";",1)[0]
            status,body,_=request("POST","/api/expenses",{},cookie=read_only_cookie)
            self.assertEqual(status,403,body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

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
