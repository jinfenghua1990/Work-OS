"""Read-only monthly bank, business settlement, invoice, GL reconciliation."""
import io
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server
import finance_reconcile
import original_archive
import bank_matching


class ThreeWayFinancialReconciliation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"workos.sqlite3"
        self.files=patch.dict(os.environ,{
            "WORKOS_ARCHIVE_ROOT":str(Path(self.temp.name)/"originals")
        })
        self.files.start()
        server.init_db()
        self.api=server.Api.__new__(server.Api)
        self.api.reply=lambda status,payload:(status,payload)

    def tearDown(self):
        server.DB_PATH=self.old_path
        self.files.stop()
        self.temp.cleanup()

    def bank(self,key,amount,currency="CNY",reference=None):
        with server.db() as conn:
            conn.execute(
                "INSERT INTO bank_transactions "
                "(id,source_key,transaction_on,account_name,bank_reference,description,"
                "direction,amount,currency,created_at,exchange_rate,rate_date,rate_source,base_amount_cny)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key,key,"2026-11-20","银行账户A",reference or key,
                 "支付给常州示例科技有限公司货款","out",amount,currency,server.now(),
                 "1","2026-11-20","functional_currency",amount),
            )

    def original(self,filename,raw,category):
        with server.db() as conn:
            info=original_archive.ingest(conn,io.BytesIO(raw),len(raw),
                                         filename,category,"2026-11")
            return info["id"]

    def test_two_bank_payments_one_payable_and_invoice_reconcile(self):
        status,account=self.api.create_bank_account({
            "account_name":"银行账户A","currency":"CNY"
        })
        self.assertEqual(status,201)
        with server.db() as conn:
            conn.execute("INSERT INTO receivables_payables "
                "(id,direction,source_type,source_id,counterparty,original_amount,settled_amount,currency,due_on,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("due1","payable","purchase_receipt","purchase1","常州示例科技有限公司",
                 "10000.00","0.00","CNY","2026-11-30",server.now()))
            conn.execute("INSERT INTO bank_account_coverage VALUES(?,?,?,?)",
                         ("2026-11",1,"账户已确认",server.now()))
        self.bank("payment1","3000.00")
        self.bank("payment2","7000.00")
        with server.db() as conn:
            matches=bank_matching.from_database(conn)
            self.assertEqual(matches["suggestion_count"],2,matches)
            self.assertTrue(all(x["is_partial"] for x in matches["suggestions"]
                                if x["amount"]=="3000.00"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],0)
        for key,amount in (("payment1","3000.00"),("payment2","7000.00")):
            status,result=self.api.allocate_bank({
                "bank_transaction_id":key,
                "allocations":[{"target_type":"payable","target_id":"due1","amount":amount}]
            })
            self.assertEqual(status,201,result)
        with server.db() as conn:
            settled=conn.execute(
                "SELECT settled_amount FROM receivables_payables WHERE id='due1'"
            ).fetchone()
            self.assertEqual(Decimal(settled[0]),Decimal("10000"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_allocations").fetchone()[0],2)
            cash_flow=conn.execute(
                "SELECT COALESCE(SUM(CAST(l.credit AS NUMERIC)-CAST(l.debit AS NUMERIC)),0) "
                "FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id "
                "WHERE e.source_type='bank_allocation' AND l.account='bank'"
            ).fetchone()[0]
            self.assertEqual(Decimal(str(cash_flow)),Decimal("10000"))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM journal_lines WHERE account='cost_of_goods_sold'"
            ).fetchone()[0],0)

            conn.execute(
                "INSERT INTO bank_statement_reconciliations "
                "(id,bank_account_id,period,opening_balance,closing_balance,"
                "statement_reference,note,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                ("statement1",account["id"],"2026-11","50000.00","40000.00",
                 "original-statement-2026-11","",server.now())
            )
        original_bank=self.original("bank-statement.csv",b"date,reference,amount\n20,payment1,3000\n20,payment2,7000\n","bank")
        original_invoice=self.original("invoice.pdf",b"pdf-test-byte-contents","input_invoice")
        with server.db() as conn:
            original_archive.link_document(conn,original_bank,"finance.bank_transaction","payment1")
            original_archive.link_document(conn,original_bank,"finance.bank_transaction","payment2")
        status,invoice=self.api.create_invoice({
            "archive_file_id":original_invoice,
            "source_key":"inv-123","invoice_number":"INV-123",
            "direction":"purchase","counterparty":"常州示例科技有限公司",
            "issued_on":"2026-11-20","total_amount":"10000.00",
            "tax_amount":"0.00","currency":"CNY","exchange_rate":"1"
        })
        self.assertEqual(status,201,invoice)
        status,linked=self.api.link_invoice({
            "invoice_id":invoice["id"],"target_type":"payable",
            "target_id":"due1","amount":"10000.00"
        })
        self.assertEqual(status,201,linked)
        with server.db() as conn:
            snapshot=finance_reconcile.monthly(conn,"2026-11")
        self.assertEqual(snapshot["status"],"balanced_with_pending_items",snapshot)
        self.assertEqual(snapshot["accounts"][0]["status"],"matched")
        self.assertEqual(snapshot["accounts"][0]["remaining"],"0.00")
        self.assertEqual(snapshot["accounts"][0]["posting_gap_cny"],"0.00")
        self.assertEqual(snapshot["bank"]["posting_error_count"],0)
        self.assertEqual(snapshot["bank"]["missing_originals"],0)
        self.assertEqual(snapshot["invoice"]["unlinked"],0)
        self.assertEqual(snapshot["invoice"]["tax_pending"],1)

    def test_no_false_green_with_missing_statement_original_or_posting(self):
        self.api.create_bank_account({"account_name":"银行账户A","currency":"CNY"})
        self.bank("a","100.00")
        with server.db() as conn:
            data=finance_reconcile.monthly(conn,"2026-11")
        self.assertEqual(data["status"],"review_required")
        self.assertEqual(data["accounts"][0]["status"],"statement_missing")
        self.assertEqual(data["bank"]["unallocated_count"],1)
        self.assertEqual(data["bank"]["missing_originals"],1)

    def test_mixed_currency_legacy_row_must_not_poison_cny_account_close(self):
        status,account=self.api.create_bank_account({
            "account_name":"银行账户A","currency":"CNY"
        })
        self.assertEqual(status,201)
        self.bank("cny","100.00")
        self.bank("eur","50.00",currency="EUR")
        with server.db() as conn:
            conn.execute("INSERT INTO bank_statement_reconciliations "
                "(id,bank_account_id,period,opening_balance,closing_balance,"
                "statement_reference,note,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                ("rec1",account["id"],"2026-11","500.00","400.00",
                 "statement","",server.now()))
            readiness=server.period_close_readiness(conn,"2026-11")
            self.assertEqual(readiness["unreconciled_bank_accounts"],0,readiness)
            month=finance_reconcile.monthly(conn,"2026-11")
        self.assertEqual(month["accounts"][0]["outflow"],"100.00")
        self.assertEqual(len(month["unknown_accounts"]),1)

    def test_reconciliation_does_not_write_any_ledger_entries(self):
        self.api.create_bank_account({"account_name":"银行账户A","currency":"CNY"})
        self.bank("only","50.00")
        with server.db() as conn:
            before=conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
            finance_reconcile.monthly(conn,"2026-11")
            after=conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        self.assertEqual(before,after)

    def test_partial_matching_never_exceeds_open_balance(self):
        bank_template=lambda i,m:{
            "id":i,"transaction_on":"2026-11-20","bank_reference":"TX-"+i,
            "description":"支付常州示例科技有限公司","direction":"out",
            "amount":m,"allocated_amount":"0","base_amount_cny":m,"currency":"CNY"
        }
        due={"id":"due1","counterparty":"常州示例科技有限公司",
             "direction":"payable","currency":"CNY","original_amount":"100",
             "settled_amount":"0","original_amount_cny":"100"}
        result=bank_matching.propose([
            bank_template("1","30"),bank_template("2","70")
        ],[due])
        self.assertEqual(len(result),2)
        result=bank_matching.propose([
            bank_template("1","30"),bank_template("2","80")
        ],[due])
        self.assertEqual(result,[])
        due2={**due,"id":"due2","original_amount":"200","original_amount_cny":"200"}
        result=bank_matching.propose([bank_template("1","30")],[due,due2])
        self.assertEqual(result,[])
        
if __name__=="__main__":
    unittest.main()
