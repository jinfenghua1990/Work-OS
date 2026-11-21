"""Batch invoice register with source immutability and original archive linkage.

Only tax_status='pending' is created: importing a file never claims that
a purchase invoice was certified or deductible at the tax authority.
"""
from __future__ import annotations
import hashlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4
import original_archive

MAX_ROWS=100
MAX_IDENTITY_LENGTH=160


def _money(value,field):
    try:
        amount=Decimal(str(value).replace(",",""))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise ValueError(f"{field}: invalid decimal amount") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise ValueError(f"{field}: expected a finite amount with at most 2 decimals")
    return amount


def _rate(value):
    try: number=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise ValueError("invalid foreign exchange rate") from exc
    if not number.is_finite() or number<=0 or number.as_tuple().exponent < -8:
        raise ValueError("exchange rate must be positive with no more than 8 decimals")
    return number


def _date(value,name):
    day=str(value or "").strip()
    try:
        if datetime.strptime(day,"%Y-%m-%d").strftime("%Y-%m-%d")!=day:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    return day


def normalize(row:dict)->dict:
    if not isinstance(row,dict):raise ValueError("invoice entry must be an object")
    number=str(row.get("invoice_number") or "").strip()
    party=str(row.get("counterparty") or "").strip()
    direction=str(row.get("direction") or "").strip().lower()
    if direction in ("进项","进项发票","input"):direction="purchase"
    if direction in ("销项","销项发票","output"):direction="sales"
    if direction not in ("sales","purchase") or not number or not party:
        raise ValueError("invoice number, counterparty and direction required")
    if len(number)>100 or len(party)>200:
        raise ValueError("invoice identity too long")
    day=_date(row.get("issued_on"),"issued_on")
    currency=str(row.get("currency") or "CNY").strip().upper()
    if not re.fullmatch("[A-Z]{3}",currency):
        raise ValueError("currency must be a 3-letter ISO code")
    total=_money(row.get("total_amount"),"total_amount")
    tax=_money(row.get("tax_amount","0"),"tax_amount")
    if total<=0 or tax<0 or tax>total:raise ValueError("invalid invoice total/tax amount")
    if currency=="CNY":
        supplied=row.get("exchange_rate")
        if supplied not in (None,"","1","1.0","1.00",1) and _rate(supplied)!=1:
            raise ValueError("CNY invoice exchange rate must equal 1")
        rate=Decimal("1")
        rate_date=day
    else:
        if not row.get("exchange_rate") or not row.get("rate_date"):
            raise ValueError("foreign invoices require an exchange rate and its date")
        rate=_rate(row["exchange_rate"])
        rate_date=_date(row["rate_date"],"rate_date")
    identity="\x1f".join((direction,party,number))
    natural_key=hashlib.sha256(identity.encode("utf-8")).hexdigest()
    provided_key=str(row.get("source_key") or "").strip()
    if provided_key and len(provided_key)>MAX_IDENTITY_LENGTH:
        raise ValueError("source_key too long")
    source_key=provided_key or ("invoice:natural:"+natural_key)
    return {"invoice_number":number,"direction":direction,"counterparty":party,
            "issued_on":day,"total_amount":str(total),"tax_amount":str(tax),
            "currency":currency,"exchange_rate":str(rate),"rate_date":rate_date,
            "source_key":source_key,"source_system":"invoice_import",
            "source_document_id":natural_key}


def import_batch(conn,data:dict,ensure_open,now)->dict:
    file_id=str(data.get("archive_file_id") or "").strip()
    if not re.fullmatch("[0-9a-f]{64}",file_id):
        raise ValueError("an archived original file ID is mandatory")
    if not conn.execute("SELECT 1 FROM archive_files WHERE id=?",(file_id,)).fetchone():
        raise ValueError("the archived invoice source file does not exist")
    rows=data.get("rows")
    if not isinstance(rows,list) or not (1<=len(rows)<=MAX_ROWS):
        raise ValueError(f"invoice batch must contain 1-{MAX_ROWS} rows")
    created=[];duplicates=[];rejected=[]
    for index,row in enumerate(rows,1):
        savepoint=f"invoice_batch_{index}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            invoice=normalize(row)
            # Invoice direction, issuer/counterparty and invoice number form
            # an immutable natural identity even when re-imported from a
            # renamed file or a different month.
            existing=conn.execute(
                "SELECT * FROM invoices WHERE (direction=? AND counterparty=? AND invoice_number=?) "
                "OR source_key=?",
                (invoice["direction"],invoice["counterparty"],
                 invoice["invoice_number"],invoice["source_key"]),
            ).fetchall()
            if len(existing)>1:
                raise ValueError("ambiguous existing invoice identity: manual reconciliation required")
            if existing:
                previous=existing[0]
                fields=("invoice_number","direction","counterparty","issued_on",
                        "total_amount","tax_amount","currency","exchange_rate","rate_date")
                for key in fields:
                    left=previous[key]
                    right=invoice[key]
                    if key in ("total_amount","tax_amount","exchange_rate"):
                        equal=Decimal(str(left))==Decimal(str(right))
                    else:
                        equal=str(left)==str(right)
                    if not equal:
                        raise ValueError("invoice identity already exists with conflicting data")
                original_archive.link_document(conn,file_id,"finance.invoice",previous["id"])
                duplicates.append({"row":index,"id":previous["id"],
                                   "invoice_number":invoice["invoice_number"]})
            else:
                ensure_open(conn,invoice["issued_on"])
                invoice_id=str(uuid4())
                conn.execute(
                    "INSERT INTO invoices "
                    "(id,invoice_number,direction,counterparty,issued_on,total_amount,"
                    "tax_amount,currency,source_key,tax_status,tax_evidence_ref,"
                    "exchange_rate,rate_date,created_at,source_system,source_document_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (invoice_id,invoice["invoice_number"],invoice["direction"],
                     invoice["counterparty"],invoice["issued_on"],
                     invoice["total_amount"],invoice["tax_amount"],
                     invoice["currency"],invoice["source_key"],"pending",None,
                     invoice["exchange_rate"],invoice["rate_date"],now(),
                     invoice["source_system"],invoice["source_document_id"]),
                )
                original_archive.link_document(conn,file_id,"finance.invoice",invoice_id)
                created.append({"row":index,"id":invoice_id,
                                "invoice_number":invoice["invoice_number"]})
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            rejected.append({"row":index,"error":str(exc)})
    return {"created":created,"duplicates":duplicates,"rejected":rejected,
            "tax_credit_recognized":False,"original_archive_id":file_id}
