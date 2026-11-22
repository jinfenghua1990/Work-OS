"""Read-only comparison of disposable PG source and restored clone.

Only CI databases localhost/workos_ci and localhost/workos_restore_ci are
accepted. The workflow itself creates, restores and deletes the clone.
This is NOT a production Neon/NAS/R2 restore verification.
"""
from __future__ import annotations

import hashlib
import os
from urllib.parse import urlsplit,urlunsplit

from psycopg import connect

FIXED_TABLES=(
    "public.schema_migrations",
    "public.purchases",
    "public.purchase_receipts",
    "public.receivables_payables",
    "public.journal_entries",
    "public.journal_lines",
    "public.bank_transactions",
    "public.bank_allocations",
    "public.invoices",
    "public.invoice_credit_notes",
    "public.finance_users",
    "public.finance_personal_advance_reviews",
    "public.finance_period_close_requests",
    "public.finance_action_reviews",
    "public.archive_files",
    "evidence.original_documents",
    "evidence.row_snapshots",
    "evidence.import_batches",
)


def checked_pair():
    raw=os.environ.get("WORKOS_TEST_POSTGRES_URL","").strip()
    if not raw:
        raise ValueError("WORKOS_TEST_POSTGRES_URL is missing")
    parsed=urlsplit(raw)
    if (parsed.scheme not in ("postgresql","postgres") or
            parsed.hostname not in {"127.0.0.1","localhost","::1"} or
            parsed.path!="/workos_ci" or not parsed.username):
        raise ValueError("CI verification permits only localhost/workos_ci")
    restored=urlunsplit(parsed._replace(path="/workos_restore_ci"))
    return raw,restored


def snapshot(connection):
    counts={}
    for table in FIXED_TABLES:
        counts[table]=connection.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]
    version=connection.execute(
        "SELECT MAX(version) FROM public.schema_migrations"
    ).fetchone()[0]
    docs=[]
    for sha,body in connection.execute(
        "SELECT sha256,exact_bytes FROM evidence.original_documents ORDER BY sha256"
    ).fetchall():
        exact=bytes(body)
        digest=hashlib.sha256(exact).hexdigest()
        if digest!=sha:
            raise AssertionError("restored original document SHA mismatch")
        docs.append((sha,len(exact),digest))
    journals=connection.execute(
        "SELECT id,posted_on,source_type,source_id FROM public.journal_entries ORDER BY id"
    ).fetchall()
    bank_allocs=connection.execute(
        "SELECT id,bank_transaction_id,target_id,amount FROM public.bank_allocations ORDER BY id"
    ).fetchall()
    approvals=connection.execute(
        "SELECT id,source_key,action_type,status,submitted_by_user_id,"
        "approved_by_user_id FROM public.finance_action_reviews ORDER BY id"
    ).fetchall()
    return dict(version=version,counts=counts,docs=docs,
                journals=journals,bank_allocs=bank_allocs,approvals=approvals)


def main():
    source,restored=checked_pair()
    with connect(source) as src,connect(restored) as dst:
        src.execute("SET TRANSACTION READ ONLY")
        dst.execute("SET TRANSACTION READ ONLY")
        original=snapshot(src)
        recovered=snapshot(dst)
    if original["version"]!=45:
        raise AssertionError("schema45 required before restore verification")
    if original!=recovered:
        raise AssertionError("restored database differs from source data")
    if not original["counts"]["public.journal_entries"]:
        raise AssertionError("CI restore has no journal data to compare")
    if not original["docs"]:
        raise AssertionError("CI restore has no immutable original bytes to compare")
    print(
        "PASS: disposable PostgreSQL custom dump restored to empty independent "
        "workos_restore_ci and matched source table counts, journal identity, "
        "bank allocations, finance approvals and exact evidence bytes"
    )


if __name__=="__main__":
    main()
