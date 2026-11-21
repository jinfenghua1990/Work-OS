#!/usr/bin/env python3
"""Compare ALSVID and Work OS PostgreSQL finance data without mutating either.

Local Mac/NAS command:
    python backend/audit_alsvid_finance.py > alsvid-finance-audit.json

Requires distinct ALSVID_DATABASE_URL and WORKOS_DATABASE_URL environment vars
(or local ignored .env). PostgreSQL sessions use transaction READ ONLY.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP=Path(__file__).resolve().parent/"app"
sys.path.insert(0,str(APP))

from alsvid_cross_db_audit import compare

ROW_LIMIT=5000


def load_local_env()->None:
    path=Path(__file__).resolve().parents[1]/".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        raw=raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        name,value=raw.split("=",1)
        name=name.strip()
        if name in {"WORKOS_DATABASE_URL","ALSVID_DATABASE_URL"}:
            os.environ.setdefault(name,value.strip().strip(" \t\"'"))


def _query(conn,sql:str)->list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        names=[col.name for col in cursor.description]
        data=cursor.fetchmany(ROW_LIMIT+1)
    if len(data)>ROW_LIMIT:
        raise ValueError(
            "audit source exceeds 5,000 rows; use a reviewed scoped export "
            "rather than a silent incomplete migration comparison"
        )
    return [dict(zip(names,row)) for row in data]


def fetch_snapshot(url:str,which:str)->dict:
    if not url.startswith(("postgresql://","postgres://")):
        raise ValueError(which+" requires a PostgreSQL URL")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Install backend/requirements.txt to use read-only PostgreSQL audit"
        ) from exc
    # Never disclose connection strings or credentials in exceptions/errors.
    try:
        with psycopg.connect(
            url,autocommit=True,sslmode="require",
            connect_timeout=12,
        ) as conn:
            conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            try:
                if which=="alsvid":
                    if not _query(conn,
                        "SELECT to_regclass('alsvid_v2.finance_obligations') AS found"
                    )[0]["found"]:
                        raise ValueError("ALSVID production finance tables were not found")
                    output={
                        "obligations":_query(conn,
                            "SELECT id::text AS id,obligation_number,direction,"
                            "currency,original_amount::text AS original_amount,"
                            "source_type,source_id::text AS source_id "
                            "FROM alsvid_v2.finance_obligations "
                            "ORDER BY created_at,id LIMIT 5001"),
                        "payments":_query(conn,
                            "SELECT id::text AS id,payment_number,direction,"
                            "currency,amount::text AS amount,"
                            "value_date::text AS value_date,external_reference "
                            "FROM alsvid_v2.finance_payments "
                            "ORDER BY created_at,id LIMIT 5001"),
                        "finance_documents":_query(conn,
                            "SELECT id::text AS id,document_type,document_number "
                            "FROM alsvid_v2.finance_documents "
                            "ORDER BY created_at,id LIMIT 5001"),
                    }
                elif which=="workos":
                    if not _query(conn,
                        "SELECT to_regclass('public.receivables_payables') AS found"
                    )[0]["found"]:
                        raise ValueError("Work OS finance tables were not found")
                    output={
                        "open_items":_query(conn,
                            "SELECT id,source_type,source_id,direction,"
                            "original_amount,currency FROM public.receivables_payables "
                            "WHERE source_type LIKE 'alsvid_%' ORDER BY id LIMIT 5001"),
                        "bank_transactions":_query(conn,
                            "SELECT id,direction,amount,currency,bank_reference,"
                            "transaction_on FROM public.bank_transactions "
                            "ORDER BY transaction_on DESC,id LIMIT 5001"),
                        "invoices":_query(conn,
                            "SELECT id,source_system,source_document_id "
                            "FROM public.invoices WHERE lower(source_system) "
                            "IN ('alsvid','alsvid-admin','alsvid_admin') "
                            "ORDER BY id LIMIT 5001"),
                    }
                else:
                    raise ValueError("unknown audit database")
                conn.execute("COMMIT")
                return output
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except (ValueError,RuntimeError):
        raise
    except Exception as exc:
        # libpq may render conninfo containing secrets in error strings.
        raise RuntimeError(
            which+" PostgreSQL read-only connection or query failed; "
            "verify credentials, network, branch and tables locally"
        ) from exc


def main()->int:
    load_local_env()
    alsvid=os.environ.get("ALSVID_DATABASE_URL","").strip()
    workos=os.environ.get("WORKOS_DATABASE_URL","").strip()
    if not alsvid or not workos:
        print("Missing ALSVID_DATABASE_URL or WORKOS_DATABASE_URL in private local environment",
              file=sys.stderr)
        return 2
    if alsvid==workos:
        print("ALSVID and Work OS must be separate database URLs",file=sys.stderr)
        return 2
    try:
        before=fetch_snapshot(alsvid,"alsvid")
        after=fetch_snapshot(workos,"workos")
        result=compare(before,after)
    except (ValueError,RuntimeError) as exc:
        print("Read-only audit failed: "+str(exc),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
