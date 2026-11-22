"""Disposable PostgreSQL smoke tests: no Neon connection is permitted.

Run locally with an isolated, empty PostgreSQL database named workos_ci on
localhost, set WORKOS_TEST_POSTGRES_URL, then:
    python -m unittest backend/tests/test_postgres_live.py -v

CI starts a fresh PostgreSQL service container for these tests. They do NOT
run against user data, remote addresses, or production databases.
"""
import os
import hashlib
import io
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server
import evidence_db
import original_archive


class PostgresLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get("WORKOS_TEST_POSTGRES_URL", "").strip()
        if not url:
            raise unittest.SkipTest("No disposable PostgreSQL CI database configured")
        parsed = urlparse(url)
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.path != "/workos_ci":
            raise RuntimeError("Refusing tests: database must be workos_ci on localhost")
        if server._psycopg is None:
            raise unittest.SkipTest("psycopg driver not installed")
        cls.old_backend, cls.old_url = server.DB_BACKEND, server.DATABASE_URL
        server.DB_BACKEND, server.DATABASE_URL = "postgres", url
        try:
            server.init_db()
        except Exception:
            server.DB_BACKEND, server.DATABASE_URL = cls.old_backend, cls.old_url
            raise

    @classmethod
    def tearDownClass(cls):
        server.DB_BACKEND, server.DATABASE_URL = cls.old_backend, cls.old_url

    def test_new_postgres_schema_matches_current_version(self):
        with server.db() as conn:
            result = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            self.assertEqual(result["version"], server.LATEST_SCHEMA_VERSION)
            self.assertEqual(server.LATEST_SCHEMA_VERSION, result['version'])
            self.assertTrue(server.postgres_schema_status()["ready"])
            self.assertIsNotNone(
                conn.execute("SELECT to_regclass('public.archive_files') AS name").fetchone()["name"]
            )
            self.assertIsNotNone(
                conn.execute("SELECT to_regclass('public.sample_dispositions') AS name").fetchone()["name"]
            )

    def test_evidence_schema_preserves_exact_original_and_enforces_immutability(self):
        raw=b"order_id,price"+bytes((13,10))+b"000123,0010.50"+bytes((13,10))
        file_id=hashlib.sha256(raw).hexdigest()
        # A dummy legacy archive metadata row lets us exercise SQL evidence
        # independently from the disk/NAS mirror.
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO archive_files (id,sha256,size_bytes,storage_key,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (file_id,file_id,len(raw),f"{file_id[:2]}/{file_id[2:4]}/{file_id}",server.now()),
            )
            batch_id=f"ci-pg-evidence-{uuid4().hex}"
            conn.execute(
                "INSERT INTO archive_uploads "
                "(id,file_id,original_name,category,period,source_system,uploaded_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (batch_id,file_id,"test.csv","sales","2026-11","ci-test",server.now()),
            )
            receipt=evidence_db.store(
                conn,file_id,raw,batch_id,"test.csv","sales","2026-11","ci-test",
            )
            self.assertTrue(receipt["stored_in_database"])
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,file_id),raw)
            snapshot=evidence_db.source_rows(conn,file_id)
            self.assertEqual(snapshot["parse_status"],"parsed")
            self.assertEqual(snapshot["rows"][-1],["000123","0010.50"])
        with self.assertRaisesRegex(Exception,"immutable"):
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM evidence.original_documents WHERE sha256=?",
                    (file_id,),
                )
        with server.db() as conn:
            self.assertEqual(evidence_db.fetch(conn,file_id),raw)

    def test_purchase_and_receipt_are_persistent_across_connections(self):
        api = server.Api.__new__(server.Api)
        api.reply = lambda status, payload: (status, payload)
        marker = uuid4().hex[:10]
        po = f"PG-PO-{marker}"
        status, created = api.create_purchase({
            "number": po, "supplier": "本地测试供货商", "sku": "PG-CAPSULES",
            "item_type": "finished_goods", "quantity": 5,
            "unit_cost": "8", "ordered_on": "2026-11-20",
            "currency": "CNY", "entity_code": "main",
        })
        self.assertEqual(status, 201, created)
        status, receipt = api.receive(created["id"], {
            "source_key": f"PG-RECEIVE-{marker}", "quantity": 5,
            "received_on": "2026-11-20",
        })
        self.assertEqual(status, 201, receipt)
        with server.db() as conn:
            row = conn.execute(
                "SELECT ordered_qty,received_qty FROM purchases WHERE number=?",
                (po,),
            ).fetchone()
            self.assertIsNotNone(row, "COMMIT did not persist purchase in PostgreSQL")
            self.assertEqual((row["ordered_qty"], row["received_qty"]), (5, 5))
            movements = conn.execute(
                "SELECT COUNT(*) AS count FROM purchase_receipts WHERE purchase_id=?",
                (created["id"],),
            ).fetchone()["count"]
            self.assertEqual(movements, 1)

    def test_financial_error_rolls_back_every_statement(self):
        code = f"PG-ROLLBACK-{uuid4().hex[:10]}"
        with self.assertRaisesRegex(RuntimeError, "intentional"):
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO projects (id,code,name,entity_code,starts_on,ends_on,currency,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid4()), code, "测试项目", "main", "2026-11-20",
                     "2026-11-20", "CNY", server.now()),
                )
                raise RuntimeError("intentional transaction rollback")
        with server.db() as conn:
            present = conn.execute(
                "SELECT 1 AS present FROM projects WHERE code=?", (code,)
            ).fetchone()
            self.assertIsNone(present)

    def test_invoice_batch_import_works_in_disposable_postgres(self):
        """Only the disposable localhost CI database; metadata stub is not a real archive."""
        marker=uuid4().hex
        raw=("synthetic archived listing "+marker).encode("utf-8")
        sha=hashlib.sha256(raw).hexdigest()
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO archive_files(id,sha256,size_bytes,storage_key,created_at) "
                "VALUES(?,?,?,?,?)",
                (sha,sha,len(raw),sha[:2]+"/"+sha[2:4]+"/"+sha,server.now()),
            )
        api=server.Api.__new__(server.Api)
        api.reply=lambda status,payload:(status,payload)
        payload={"archive_file_id":sha,"rows":[
            {"invoice_number":"CI-PG-"+marker,"direction":"purchase",
             "counterparty":"Test Vendor Inc","issued_on":"2026-11-20",
             "total_amount":"110.00","tax_amount":"10.00",
             "currency":"CNY"},
        ]}
        status,result=api.import_invoices(payload)
        self.assertEqual(status,200,result)
        self.assertEqual(len(result["created"]),1,result)
        self.assertFalse(result["tax_credit_recognized"])
        status,again=api.import_invoices(payload)
        self.assertEqual(status,200,again)
        self.assertEqual(len(again["duplicates"]),1,again)
        with server.db() as conn:
            source_id=result["created"][0]["id"]
            invoice=conn.execute("SELECT tax_status FROM invoices WHERE id=?",(source_id,)).fetchone()
            self.assertEqual(invoice["tax_status"],"pending")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM archive_links WHERE file_id=? AND source_system='finance.invoice'",
                (sha,),
            ).fetchone()[0],1)

    def test_personal_advance_and_reimbursement_liability_in_disposable_postgres(self):
        """PostgreSQL must preserve the same payable transfer and no double cost."""
        marker=uuid4().hex
        raw=("personal-payment-only-test-"+marker).encode("utf-8")
        sha=hashlib.sha256(raw).hexdigest()
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO archive_files(id,sha256,size_bytes,storage_key,created_at) "
                "VALUES(?,?,?,?,?)",
                (sha,sha,len(raw),sha[:2]+"/"+sha[2:4]+"/"+sha,server.now()),
            )
        api=server.Api.__new__(server.Api)
        api.reply=lambda status,payload:(status,payload)
        claim_data={
            "source_key":"ci-personal-"+marker,"claimant":"测试垫付人",
            "merchant":"PG Test Vendor","mode":"expense","paid_on":"2026-11-18",
            "category":"software","amount":"35.00","currency":"CNY",
            "business_line":"corporate","entity_code":"main",
            "payment_reference":"TX-"+marker,"archive_file_id":sha,
        }
        status,claim=api.create_personal_advance(claim_data)
        self.assertEqual(status,201,claim)
        status,approved=api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"local-ci",
            "accounting_on":"2026-11-18",
        })
        self.assertEqual(status,201,approved)
        self.assertTrue(approved["vendor_payable_id"])
        self.assertTrue(approved["employee_payable_id"])
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT settled_amount FROM receivables_payables WHERE id=?",
                (approved["vendor_payable_id"],),
            ).fetchone()[0],"35.00")
            pending=conn.execute(
                "SELECT counterparty,settled_amount FROM receivables_payables WHERE id=?",
                (approved["employee_payable_id"],),
            ).fetchone()
            self.assertEqual(pending["counterparty"],"测试垫付人")
            self.assertEqual(pending["settled_amount"],"0.00")
            journal_count=conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE source_id=?",
                (claim["id"],),
            ).fetchone()[0]
            self.assertEqual(journal_count,2)
            links=conn.execute(
                "SELECT COUNT(*) FROM archive_links WHERE source_system='finance.personal_advance' "
                "AND source_document_id=?",(claim["id"],),
            ).fetchone()[0]
            self.assertEqual(links,1)
        status,again=api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"local-ci"
        })
        self.assertEqual(status,200,again)
        self.assertTrue(again["duplicate"])

    def test_named_finance_review_posts_atomically_in_disposable_postgres(self):
        """Different signed-in actors, one durable journal and approval audit."""
        import finance_identity
        marker=uuid4().hex[:10]
        with server.db() as conn:
            submitter=finance_identity.create_user(
                conn,"pgsubmit"+marker,"test-password-submitter-2026","finance_clerk"
            )
            approver=finance_identity.create_user(
                conn,"pgapprove"+marker,"test-password-approver-2026","finance_approver"
            )
        api=server.Api.__new__(server.Api)
        api.reply=lambda status,body:(status,body)
        api._principal=submitter
        data={
            "source_key":"pg-review-"+marker,
            "action_type":"manual_journal",
            "payload":{
                "source_key":"untrusted-"+marker,
                "posted_on":"2026-11-20","description":"PostgreSQL double entry review",
                "business_line":"corporate","entity_code":"main",
                "lines":[
                    {"account":"bank","debit":"5.00","credit":"0.00"},
                    {"account":"accounts_payable","debit":"0.00","credit":"5.00"}
                ],
            }
        }
        status,request=api.submit_finance_review(data)
        self.assertEqual(status,201,request)
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM journal_entries e "
                "WHERE e.source_type='manual_adjustment' AND e.memo='PostgreSQL double entry review'"
            ).fetchone()[0],0)
        api._principal=approver
        status,approved=api.approve_finance_review({"request_id":request["id"]})
        self.assertEqual(status,201,approved)
        self.assertEqual(approved["status"],"approved")
        status,repeated=api.approve_finance_review({"request_id":request["id"]})
        self.assertEqual(status,200,repeated)
        self.assertTrue(repeated["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM journal_entries e "
                "WHERE e.source_type='manual_adjustment' AND e.memo='PostgreSQL double entry review'"
            ).fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM finance_action_reviews "
                "WHERE id=? AND submitted_by_user_id=? AND approved_by_user_id=? "
                "AND status='approved'",
                (request["id"],submitter["id"],approver["id"])
            ).fetchone()[0],1)

    def test_v35_migration_against_disposable_pg_database(self):
        # Regression check: the connected Neon dev branch was at schema 35.
        # The DELETE is allowed only in the disposable localhost workos_ci DB.
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM schema_migrations WHERE version>35")
        before = server.postgres_schema_status()
        self.assertEqual(before["current_version"], 35)
        self.assertFalse(before["ready"])
        with self.assertRaisesRegex(RuntimeError, "refused automatic cloud database changes"):
            server.require_database_ready()
        # The real migration is invoked only explicitly in this disposable
        # CI database. API startup must never trigger it on Neon production.
        server.init_db()
        server.init_db()
        server.require_database_ready()
        with server.db() as conn:
            version = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()["version"]
            self.assertEqual(version, server.LATEST_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
