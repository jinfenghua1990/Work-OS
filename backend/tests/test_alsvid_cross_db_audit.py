"""Only local in-memory fixtures; never opens either Neon database."""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout,redirect_stderr
from datetime import date

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import alsvid_cross_db_audit as audit
import audit_alsvid_finance as cli


class AlsvidCrossDatabaseAuditTests(unittest.TestCase):
    def obligation(self,identifier="a",amount="100.00",currency="CNY",
                   direction="AP",source_type="MANUAL"):
        return {"id":identifier,"original_amount":amount,"currency":currency,
                "direction":direction,"source_type":source_type}
    def payment(self,identifier="p",when="2026-11-19",reference=""):
        return {"id":identifier,"amount":"100.00","currency":"CNY",
                "direction":"OUTBOUND","value_date":when,
                "external_reference":reference}
    def test_legacy_obligations_and_future_legacy_payment_cannot_auto_post(self):
        a={"obligations":[self.obligation(str(i)) for i in range(12)],
           "payments":[self.payment("future","2026-12-15"),
                       self.payment("missing","2026-11-19")],
           "finance_documents":[]}
        result=audit.compare(a,{"open_items":[],"bank_transactions":[],"invoices":[]},
                             as_of=date(2026,11,21))
        self.assertEqual(result["counts"]["alsvid_obligations"],12)
        self.assertEqual(result["counts"]["workos_unmapped_obligations"],12)
        self.assertEqual(result["counts"]["future_dated_payments"],1)
        self.assertEqual(result["counts"]["bank_unique_reference_matches"],0)
        self.assertFalse(result["safe_to_auto_post"])
        self.assertTrue(result["read_only"])
        self.assertIn("legacy_manual_obligation",{r["code"] for r in result["issues"]})
        self.assertIn("future_dated_alsvid_payment",
                      {r["code"] for r in result["issues"]})
    def test_unique_identity_amount_direction_currency_match_only(self):
        a={"obligations":[self.obligation("unique","102.20")]}
        w={"open_items":[{"id":"w1","source_type":"alsvid_finance_obligation",
            "source_id":"unique","direction":"payable","currency":"CNY",
            "original_amount":"102.200000"}]}
        result=audit.compare(a,w,as_of=date(2026,11,21))
        self.assertEqual(result["counts"]["workos_mapped_obligations"],1)
        self.assertEqual(result["counts"]["workos_unmapped_obligations"],0)
        self.assertEqual(result["status"],"no_known_differences")
        w["open_items"][0]["currency"]="EUR"
        result=audit.compare(a,w,as_of=date(2026,11,21))
        self.assertIn("mapped_obligation_conflict",
                      {r["code"] for r in result["issues"]})
    def test_bank_reference_is_not_enough_when_date_or_amount_differs(self):
        a={"payments":[self.payment("first","2026-11-19","BANKTX")]}
        w={"bank_transactions":[{"id":"work1","bank_reference":"BANKTX",
           "direction":"out","amount":"100.00","currency":"CNY",
           "transaction_on":"2026-11-20"}]}
        result=audit.compare(a,w,as_of=date(2026,11,21))
        self.assertEqual(result["counts"]["bank_unique_reference_matches"],0)
        w["bank_transactions"][0]["transaction_on"]="2026-11-19"
        result=audit.compare(a,w,as_of=date(2026,11,21))
        self.assertEqual(result["counts"]["bank_unique_reference_matches"],1)
        self.assertFalse(result["safe_to_auto_post"])
    def test_one_company_bank_row_cannot_match_two_alsvid_payments(self):
        a={"payments":[self.payment("a","2026-11-19","BANKTX"),
                       self.payment("b","2026-11-19","BANKTX")]}
        w={"bank_transactions":[{"id":"work1","bank_reference":"BANKTX",
           "direction":"out","amount":"100.00","currency":"CNY",
           "transaction_on":"2026-11-19"}]}
        result=audit.compare(a,w,as_of=date(2026,11,21))
        self.assertEqual(result["counts"]["bank_unique_reference_matches"],1)
        self.assertEqual(result["counts"]["payments_requiring_review"],1)
        self.assertIn("payment_duplicate_bank_mapping",
                      {r["code"] for r in result["issues"]})
    def test_orphan_and_duplicate_finance_invoice_source_flags(self):
        a={"obligations":[]}
        w={"open_items":[{"id":"p","source_type":"alsvid_finance_obligation",
            "source_id":"absent","direction":"payable",
            "currency":"CNY","original_amount":"100.00"}],
           "invoices":[
            {"id":"i1","source_system":"alsvid","source_document_id":"x"},
            {"id":"i2","source_system":"alsvid","source_document_id":"x"},
           ]}
        result=audit.compare(a,w,as_of=date(2026,11,21))
        self.assertIn("orphan_workos_mapping",{r["code"] for r in result["issues"]})
        self.assertIn("invoice_duplicate_source",{r["code"] for r in result["issues"]})
    def test_invalid_payment_date_rejected(self):
        result=audit.compare({"payments":[self.payment("p","not-a-date")]},{},
                             as_of=date(2026,11,21))
        self.assertIn("invalid_payment_date",{r["code"] for r in result["issues"]})
        self.assertEqual(result["counts"]["payments_requiring_review"],1)
    def test_cli_never_connects_when_environment_missing_or_same(self):
        with patch.dict(os.environ,{},clear=True):
            with patch.object(cli,"load_local_env",return_value=None):
                with patch.object(cli,"fetch_snapshot") as fetch:
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(),2)
                    fetch.assert_not_called()
        with patch.dict(os.environ,{
            "ALSVID_DATABASE_URL":"postgresql://host/db",
            "WORKOS_DATABASE_URL":"postgresql://host/db",
        },clear=True):
            with patch.object(cli,"load_local_env",return_value=None):
                with patch.object(cli,"fetch_snapshot") as fetch:
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(),2)
                    fetch.assert_not_called()
    def test_cli_uses_read_only_snapshot_results_and_emits_report(self):
        def snapshot(url,which):
            return {"obligations":[self.obligation()]} if which=="alsvid" else {
                "open_items":[]}
        with patch.dict(os.environ,{
            "ALSVID_DATABASE_URL":"postgresql://alsvid/db",
            "WORKOS_DATABASE_URL":"postgresql://workos/db",
        },clear=True):
            with patch.object(cli,"load_local_env",return_value=None):
                with patch.object(cli,"fetch_snapshot",side_effect=snapshot) as fetch:
                    output=io.StringIO()
                    with redirect_stdout(output):
                        status=cli.main()
        self.assertEqual(status,0)
        self.assertIn('"safe_to_auto_post": false',output.getvalue())
        self.assertEqual(fetch.call_count,2)

if __name__=="__main__":
    unittest.main()
