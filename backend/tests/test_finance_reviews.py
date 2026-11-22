"""Named-finance approval audit: settlement, journal and invoice tax changes.

All tests are isolated SQLite with no real business sources or network calls.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import finance_identity


class FinanceActionReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_path=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"finance.sqlite3"
        self.env=patch.dict(os.environ,{"WORKOS_AUTH_MODE":"named"})
        self.env.start()
        finance_identity.reset_for_tests()
        server.init_db()
        with server.db() as conn:
            self.clerk=finance_identity.create_user(conn,"clerk01","c"*18,"finance_clerk")
            self.approver=finance_identity.create_user(conn,"approver01","a"*18,"finance_approver")
            self.owner=finance_identity.create_user(conn,"owner01","o"*18,"owner")
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,body:(status,body)
        self.api._principal=self.clerk

    def tearDown(self):
        finance_identity.reset_for_tests()
        self.env.stop()
        server.DB_PATH=self.old_path
        self.tmp.cleanup()

    def submit(self,action,payload,key="review-001"):
        return self.api.submit_finance_review({
            "action_type":action,"source_key":key,"payload":payload
        })

    def journal(self):
        return {
            "source_key":"spoofed-source-key",
            "posted_on":"2026-11-20",
            "description":"按月财务调整",
            "business_line":"corporate","entity_code":"main",
            "lines":[
                {"account":"bank","debit":"3.00","credit":"0"},
                {"account":"accounts_payable","debit":"0","credit":"3.00"},
            ]
        }

    def test_manual_journal_requires_distinct_approver_and_atomic_post(self):
        status,request=self.submit("manual_journal",self.journal())
        self.assertEqual(status,201,request)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM manual_journal_requests").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
        status,retry=self.submit("manual_journal",self.journal())
        self.assertEqual(status,200,retry)
        self.assertTrue(retry["duplicate"])
        with self.assertRaisesRegex(PermissionError,"approver"):
            self.api.approve_finance_review({"request_id":request["id"]})
        self.api._principal=self.owner
        status,other=self.submit("manual_journal",self.journal(),"owner-review")
        self.assertEqual(status,201,other)
        with self.assertRaisesRegex(ValueError,"different"):
            self.api.approve_finance_review({"request_id":other["id"]})
        self.api._principal=self.approver
        status,approved=self.api.approve_finance_review({"request_id":request["id"]})
        self.assertEqual(status,201,approved)
        self.assertEqual(approved["status"],"approved")
        self.assertEqual(approved["approved_by"],"approver01")
        status,again=self.api.approve_finance_review({"request_id":request["id"]})
        self.assertEqual(status,200,again)
        self.assertTrue(again["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM manual_journal_requests").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],1)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM manual_journal_requests WHERE source_key='spoofed-source-key'"
            ).fetchone())
            audit=conn.execute("SELECT * FROM finance_action_reviews WHERE id=?",
                               (request["id"],)).fetchone()
            self.assertEqual(audit["submitted_by_user_id"],self.clerk["id"])
            self.assertEqual(audit["approved_by_user_id"],self.approver["id"])
            self.assertEqual(audit["status"],"approved")

    def test_invalid_adjustment_rolls_back_review_and_journal_together(self):
        bad=self.journal()
        bad["lines"][1]["credit"]="2.00"
        _,request=self.submit("manual_journal",bad,"invalid-adjust")
        self.api._principal=self.approver
        with self.assertRaisesRegex(ValueError,"must balance"):
            self.api.approve_finance_review({"request_id":request["id"]})
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
            self.assertEqual(conn.execute(
                "SELECT status FROM finance_action_reviews WHERE id=?",
                (request["id"],)
            ).fetchone()[0],"pending")
        self.api._principal=self.clerk
        status,cancel=self.api.cancel_finance_review({"request_id":request["id"]})
        self.assertEqual(status,201,cancel)
        self.assertEqual(cancel["status"],"cancelled")

    def test_invoice_tax_approval_snapshots_status_and_trusted_reviewer(self):
        status,invoice=self.api.create_invoice({
            "source_key":"tax-source", "invoice_number":"TAX-REVIEW-1",
            "direction":"purchase","counterparty":"Supplier",
            "issued_on":"2026-11-17","total_amount":"113.00",
            "tax_amount":"13.00","currency":"CNY","exchange_rate":"1"
        })
        self.assertEqual(status,201,invoice)
        payload={"invoice_id":invoice["id"],"tax_status":"certified_deductible",
                 "tax_evidence_ref":"tax-authority-confirmed-1",
                 "effective_on":"2026-11-20","changed_by":"fake-user"}
        _,stale=self.submit("invoice_tax_status",payload,"tax-stale")
        # Simulate an earlier authorized change while the review is pending.
        self.api.update_invoice_tax_status({
            "invoice_id":invoice["id"],"tax_status":"eligible",
            "effective_on":"2026-11-19","changed_by":"prior-authorized-user"
        })
        self.api._principal=self.approver
        with self.assertRaisesRegex(ValueError,"changed after submission"):
            self.api.approve_finance_review({"request_id":stale["id"]})
        self.api._principal=self.clerk
        _,fresh=self.submit("invoice_tax_status",payload,"tax-fresh")
        self.api._principal=self.approver
        status,result=self.api.approve_finance_review({"request_id":fresh["id"]})
        self.assertEqual(status,201,result)
        with server.db() as conn:
            state=conn.execute("SELECT tax_status FROM invoices WHERE id=?",
                               (invoice["id"],)).fetchone()
            self.assertEqual(state[0],"certified_deductible")
            history=conn.execute(
                "SELECT changed_by FROM invoice_tax_history WHERE invoice_id=? "
                "ORDER BY changed_at DESC",(invoice["id"],)
            ).fetchall()
            self.assertIn("approver01",[r["changed_by"] for r in history])
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM finance_action_reviews WHERE status='approved'"
            ).fetchone()[0],1)

    def test_bank_allocation_approval_posts_once_and_keeps_open_balance(self):
        with server.db() as conn:
            conn.execute(
                "INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,description,"
                "direction,amount,currency,created_at,exchange_rate,rate_date,rate_source,base_amount_cny)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bank001","bank-001","2026-11-20","BANK-A","REF-001",
                 "付款给供应商","out","10.00","CNY",server.now(),
                 "1","2026-11-20","functional_currency","10.00")
            )
            conn.execute(
                "INSERT INTO receivables_payables "
                "(id,direction,source_type,source_id,counterparty,original_amount,"
                "settled_amount,currency,due_on,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("due001","payable","purchase_receipt","pr001","Vendor",
                 "10.00","0.00","CNY","2026-11-30",server.now())
            )
        payload={"bank_transaction_id":"bank001",
                 "allocations":[{"target_type":"payable","target_id":"due001",
                                 "amount":"10.00"}],
                 "action_key":"fake-submitter-key"}
        _,request=self.submit("bank_allocation",payload,"bank-review")
        status,dupe=self.submit("bank_allocation",{
            **payload,"action_key":"different-spoofed-retry-key"
        },"bank-review-same-business-different-key")
        self.assertEqual(status,200,dupe)
        self.assertEqual(dupe["id"],request["id"])
        self.assertTrue(dupe["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_allocations").fetchone()[0],0)
        self.api._principal=self.approver
        status,result=self.api.approve_finance_review({"request_id":request["id"]})
        self.assertEqual(status,201,result)
        status,again=self.api.approve_finance_review({"request_id":request["id"]})
        self.assertEqual(status,200,again)
        self.api._principal=self.clerk
        status,repeat=self.submit("bank_allocation",payload,"new-id-after-approval")
        self.assertEqual(status,200,repeat)
        self.assertEqual(repeat["id"],request["id"])
        self.assertTrue(repeat["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_allocations").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT settled_amount FROM receivables_payables "
                                          "WHERE id='due001'").fetchone()[0],"10.00")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM financial_action_requests").fetchone()[0],1)


if __name__=="__main__":
    unittest.main()
