"""Personal payment approval and reimbursement: no duplicate expense or bank fact."""
import io
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import original_archive
import server


class PersonalAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=server.DB_PATH
        server.DB_PATH=Path(self.tmp.name)/"workos.sqlite3"
        self.archive_env=patch.dict(os.environ,{
            "WORKOS_ARCHIVE_ROOT":str(Path(self.tmp.name)/"originals"),
            "WORKOS_EVIDENCE_STORAGE":"database"
        })
        self.archive_env.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)
        raw=b"personal card payment receipt exact bytes\n"
        with server.db() as conn:
            self.proof=original_archive.ingest(conn,io.BytesIO(raw),len(raw),
                "card-proof.pdf","expense","2026-11")["id"]
    def tearDown(self):
        self.archive_env.stop()
        server.DB_PATH=self.db
        self.tmp.cleanup()
    def data(self,key="personal-001",mode="expense"):
        return {
            "source_key":key,"claimant":"李晓明","merchant":"某供应商",
            "paid_on":"2026-11-18","category":"software" if mode=="expense" else None,
            "amount":"120.00","mode":mode,"business_line":"corporate",
            "entity_code":"main","currency":"CNY","payment_reference":"个人卡支付TX01",
            "archive_file_id":self.proof,"memo":"公司软件费",
        }
    def test_personal_expense_draft_approval_and_bank_reimbursement(self):
        status,claim=self.api.create_personal_advance(self.data())
        self.assertEqual(status,201,claim)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM receivables_payables").fetchone()[0],0)
        status,again=self.api.create_personal_advance(self.data())
        self.assertEqual(status,200,again)
        self.assertTrue(again["duplicate"])
        status,approved=self.api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"财务审核","accounting_on":"2026-11-18"
        })
        self.assertEqual(status,201,approved)
        self.assertTrue(approved["employee_payable_id"])
        status,again=self.api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"财务审核"
        })
        self.assertEqual(status,200,again)
        self.assertTrue(again["duplicate"])
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM receivables_payables").fetchone()[0],2)
            person=conn.execute("SELECT * FROM receivables_payables WHERE source_type='personal_advance'").fetchone()
            self.assertEqual(person["counterparty"],"李晓明")
            self.assertEqual(person["source_type"],"personal_advance")
            expense=conn.execute("SELECT * FROM expenses").fetchone()
            self.assertEqual(expense["counterparty"],"某供应商")
            supplier=conn.execute(
                "SELECT counterparty,original_amount,settled_amount FROM receivables_payables "
                "WHERE id=?",(approved["vendor_payable_id"],)
            ).fetchone()
            self.assertEqual(supplier["counterparty"],"某供应商")
            self.assertEqual(supplier["original_amount"],"120.00")
            self.assertEqual(supplier["settled_amount"],"120.00")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_links").fetchone()[0],1)
            before=conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        self.api.create_bank_account({"account_name":"公司公账","currency":"CNY"})
        with server.db() as conn:
            conn.execute(
                "INSERT INTO bank_transactions (id,source_key,transaction_on,account_name,"
                "bank_reference,description,direction,amount,currency,created_at,exchange_rate,"
                "rate_date,rate_source,base_amount_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bank-person","bank-person","2026-11-20","公司公账","TX201",
                 "报销李晓明垫付款","out","120.00","CNY",server.now(),
                 "1","2026-11-20","functional_currency","120.00"),
            )
        status,paid=self.api.allocate_bank({
            "bank_transaction_id":"bank-person",
            "allocations":[{"target_type":"payable","target_id":approved["employee_payable_id"],
                            "amount":"120.00"}],
        })
        self.assertEqual(status,201,paid)
        # The tax invoice is issued by the vendor, not the individual.
        status,invoice=self.api.create_invoice({
            "source_key":"invoice-vendor-001","invoice_number":"00001234",
            "direction":"purchase","counterparty":"某供应商","issued_on":"2026-11-18",
            "total_amount":"120.00","tax_amount":"0","currency":"CNY",
            "exchange_rate":"1","archive_file_id":self.proof,
        })
        self.assertEqual(status,201,invoice)
        status,linked=self.api.link_invoice({
            "invoice_id":invoice["id"],"target_type":"payable",
            "target_id":approved["vendor_payable_id"],"amount":"120.00",
        })
        self.assertEqual(status,201,linked)
        with server.db() as conn:
            state=conn.execute("SELECT * FROM receivables_payables WHERE id=?",
                               (approved["employee_payable_id"],)).fetchone()
            self.assertEqual(Decimal(state["settled_amount"]),Decimal("120.00"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],before+1)
            claim_row=self.api.create_personal_advance(self.data())[1]
            self.assertEqual(claim_row["status"],"approved")
            from personal_advances import list_claims
            item=list_claims(conn)[0]
            self.assertEqual(item["reimbursement_status"],"reimbursed")
            self.assertEqual(item["unreimbursed_amount"],"0.00")

    def test_existing_supplier_payable_transfers_without_second_expense(self):
        with server.db() as conn:
            conn.execute("INSERT INTO receivables_payables "
                "(id,direction,source_type,source_id,counterparty,original_amount,"
                "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("supplier","payable","purchase_receipt","receipt-001","某供应商",
                 "200.00","0.00","CNY",None,server.now()))
            before=conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        values=self.data("personal-supplier","existing_payable")
        values["existing_payable_id"]="supplier"
        status,claim=self.api.create_personal_advance(values)
        self.assertEqual(status,201,claim)
        status,approved=self.api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"owner"
        })
        self.assertEqual(status,201,approved)
        self.assertIsNone(approved["expense_id"])
        with server.db() as conn:
            supplier=conn.execute("SELECT settled_amount FROM receivables_payables WHERE id='supplier'").fetchone()
            self.assertEqual(supplier[0],"120.00")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],before+1)
            gl=conn.execute("SELECT SUM(CAST(debit AS NUMERIC)-CAST(credit AS NUMERIC)) "
                            "FROM journal_lines WHERE account='accounts_payable'").fetchone()[0]
            self.assertEqual(Decimal(str(gl)),Decimal("0"))
        with self.assertRaisesRegex(ValueError,"enough outstanding"):
            values=self.data("another","existing_payable")
            values["existing_payable_id"]="supplier"
            values["amount"]="100"
            self.api.create_personal_advance(values)

    def test_identical_personal_payment_in_new_file_cannot_double_accrue(self):
        status,first=self.api.create_personal_advance(self.data("first-import"))
        self.assertEqual(status,201,first)
        newer=b"another export of identical personal card payment"
        with server.db() as conn:
            new_proof=original_archive.ingest(
                conn,io.BytesIO(newer),len(newer),
                "personal-bank-export.csv","expense","2026-11",
            )["id"]
        copied=self.data("new-batch-other-source")
        copied["archive_file_id"]=new_proof
        status,result=self.api.create_personal_advance(copied)
        self.assertEqual(status,200,result)
        self.assertTrue(result["duplicate"])
        self.assertEqual(first["id"],result["id"])
        with server.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM personal_advances"
            ).fetchone()[0],1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM archive_links WHERE source_system='finance.personal_advance'"
            ).fetchone()[0],2)
        conflict=self.data("different-category")
        conflict["category"]="travel"
        with self.assertRaisesRegex(ValueError,"conflicting business allocation"):
            self.api.create_personal_advance(conflict)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)

    def test_foreign_currency_or_missing_proof_does_not_post(self):
        value=self.data();value["currency"]="EUR"
        with self.assertRaisesRegex(ValueError,"CNY"):
            self.api.create_personal_advance(value)
        value=self.data();value["archive_file_id"]="f"*64
        with self.assertRaisesRegex(ValueError,"not been archived"):
            self.api.create_personal_advance(value)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM personal_advances").fetchone()[0],0)

    def test_closed_month_denies_approval_but_keeps_original_claim(self):
        claim=self.api.create_personal_advance(self.data())[1]
        with server.db() as conn:
            conn.execute("INSERT INTO period_locks (period,closed_at,closed_by,"
                "reviewed_checks,journal_count) VALUES(?,?,?,?,?)",
                ("2026-11",server.now(),"reviewer","[]",0))
        with self.assertRaisesRegex(ValueError,"closed"):
            self.api.approve_personal_advance({
                "advance_id":claim["id"],"approved_by":"owner","accounting_on":"2026-11-18"
            })
        with server.db() as conn:
            state=conn.execute("SELECT status FROM personal_advances WHERE id=?",
                               (claim["id"],)).fetchone()
            self.assertEqual(state[0],"draft")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
        status,out=self.api.approve_personal_advance({
            "advance_id":claim["id"],"approved_by":"owner","accounting_on":"2026-12-01"
        })
        self.assertEqual(status,201,out)

if __name__=="__main__":
    unittest.main()
