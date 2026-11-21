"""Controlled red-invoice register and versioned tax-rate reference data.

Red invoices are evidence facts, not automatic reversals of posted revenue or VAT.
All amounts are positive source-document amounts; accounting adjustments require a
separate reviewed business event, never inferred from the invoice alone.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4

CENT = Decimal("0.01")
RATE_UNIT = Decimal("0.0001")
REASONS = {"return", "discount", "reissue", "correction"}


def _money(value, label):
    try:
        n = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError(f"{label} must be a valid decimal") from exc
    if not n.is_finite() or n < 0 or n != n.quantize(CENT):
        raise ValueError(f"{label} must be non-negative with at most two decimal places")
    return n


def _day(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must use YYYY-MM-DD")
    try:
        value = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD") from exc
    return value


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS invoice_credit_notes (
        id TEXT PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        blue_invoice_id TEXT NOT NULL REFERENCES invoices(id),
        invoice_number TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('sales','purchase')),
        counterparty TEXT NOT NULL,
        issued_on TEXT NOT NULL,
        total_amount TEXT NOT NULL,
        tax_amount TEXT NOT NULL,
        currency TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN ('return','discount','reissue','correction')),
        archive_file_id TEXT NOT NULL REFERENCES archive_files(id),
        created_at TEXT NOT NULL,
        UNIQUE(direction,counterparty,invoice_number)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_invoice_credit_blue
        ON invoice_credit_notes(blue_invoice_id,issued_on)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tax_rate_rules (
        rate_code TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        rate_percent TEXT NOT NULL,
        description TEXT NOT NULL,
        source_reference TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(rate_code,valid_from),
        CHECK(valid_to IS NULL OR valid_to >= valid_from)
    )""")


def credit_totals(conn, blue_invoice_id):
    rows = conn.execute(
        "SELECT total_amount,tax_amount FROM invoice_credit_notes WHERE blue_invoice_id=?",
        (blue_invoice_id,)
    ).fetchall()
    return (
        sum((Decimal(row["total_amount"]) for row in rows), Decimal(0)),
        sum((Decimal(row["tax_amount"]) for row in rows), Decimal(0)),
        len(rows),
    )


def net_for(conn, blue_invoice_id):
    row = conn.execute(
        "SELECT total_amount,tax_amount FROM invoices WHERE id=?",
        (blue_invoice_id,)
    ).fetchone()
    if not row:
        raise ValueError("blue invoice does not exist")
    credited, credited_tax, count = credit_totals(conn, blue_invoice_id)
    net = Decimal(row["total_amount"]) - credited
    net_tax = Decimal(row["tax_amount"]) - credited_tax
    if net < 0 or net_tax < 0:
        raise ValueError("red invoices exceed original invoice; review source documents")
    return {"net_amount": str(net.quantize(CENT)),
            "net_tax_amount": str(net_tax.quantize(CENT)),
            "credited_amount": str(credited.quantize(CENT)),
            "credited_tax_amount": str(credited_tax.quantize(CENT)),
            "red_count": count}


def list_credit_notes(conn):
    return [dict(row) for row in conn.execute(
        "SELECT r.*,i.invoice_number AS blue_invoice_number,i.tax_status AS blue_tax_status "
        "FROM invoice_credit_notes r JOIN invoices i ON i.id=r.blue_invoice_id "
        "ORDER BY r.issued_on DESC,r.created_at DESC,r.id DESC"
    ).fetchall()]


def register_credit(conn, payload, ensure_open, now, link_document):
    key = str(payload.get("source_key") or "").strip()
    blue_id = str(payload.get("blue_invoice_id") or "").strip()
    number = str(payload.get("invoice_number") or "").strip()
    original = str(payload.get("archive_file_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    issued = _day(payload.get("issued_on"), "issued_on")
    gross = _money(payload.get("total_amount"), "total_amount")
    tax = _money(payload.get("tax_amount"), "tax_amount")
    if not all((key, blue_id, number, original)) or len(key)>160 or len(number)>160:
        raise ValueError("source_key, blue_invoice_id, red invoice_number and original archive are required")
    if reason not in REASONS or gross <= 0 or tax > gross:
        raise ValueError("invalid red invoice reason, total amount or tax amount")
    blue = conn.execute("SELECT * FROM invoices WHERE id=?", (blue_id,)).fetchone()
    if not blue:
        raise ValueError("blue invoice does not exist")
    if issued < blue["issued_on"]:
        raise ValueError("red invoice date cannot precede blue invoice date")
    archived = conn.execute("SELECT id FROM archive_files WHERE id=?", (original,)).fetchone()
    if not archived:
        raise ValueError("red invoice original must be archived before registration")
    expected = {
        "blue_invoice_id": blue_id, "invoice_number": number,
        "direction": blue["direction"], "counterparty": blue["counterparty"],
        "issued_on": issued, "total_amount": str(gross),
        "tax_amount": str(tax), "currency": blue["currency"],
        "reason": reason,
    }
    prior = conn.execute(
        "SELECT * FROM invoice_credit_notes WHERE source_key=? OR "
        "(direction=? AND counterparty=? AND invoice_number=?)",
        (key, blue["direction"], blue["counterparty"], number)
    ).fetchall()
    if len(prior)>1:
        raise ValueError("red invoice identity conflict; manual reconciliation required")
    if prior:
        recorded = prior[0]
        if recorded["source_key"] != key or any(
            str(recorded[field]) != val for field,val in expected.items()
        ):
            raise ValueError("red invoice already registered with conflicting source facts")
        # Repeating the same source cannot silently replace its original document.
        if recorded["archive_file_id"] != original:
            raise ValueError("red invoice original differs from previously archived source")
        return {"id":recorded["id"],"duplicate":True,**net_for(conn, blue_id),
                "accounting_effect":"none"}
    credited, credited_tax, _ = credit_totals(conn, blue_id)
    if credited + gross > Decimal(blue["total_amount"]) or (
        credited_tax + tax > Decimal(blue["tax_amount"])
    ):
        raise ValueError("red invoice total/tax would exceed remaining blue invoice amount/tax")
    ensure_open(conn, issued)
    rid = str(uuid4())
    conn.execute(
        "INSERT INTO invoice_credit_notes "
        "(id,source_key,blue_invoice_id,invoice_number,direction,counterparty,"
        "issued_on,total_amount,tax_amount,currency,reason,archive_file_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid,key,blue_id,number,blue["direction"],blue["counterparty"],issued,
         str(gross),str(tax),blue["currency"],reason,original,now())
    )
    link_document(conn,original,"finance.invoice.red",rid)
    linked = sum((Decimal(row[0]) for row in conn.execute(
        "SELECT linked_amount FROM invoice_links WHERE invoice_id=?", (blue_id,)
    )),Decimal(0))
    after = net_for(conn,blue_id)
    return {"id":rid,"blue_invoice_id":blue_id,"duplicate":False,**after,
            "linked_amount":str(linked.quantize(CENT)),
            "link_review_required":linked>Decimal(after["net_amount"]),
            "vat_review_required":blue["tax_status"]=="certified_deductible",
            "accounting_effect":"none",
            "message":"红票已登记；收入/进项转出/退货需要单独核验业务事项，不会因票据自动过账"}


def list_rates(conn):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM tax_rate_rules ORDER BY rate_code,valid_from DESC"
    ).fetchall()]


def register_rate(conn,payload,ensure_open,now):
    code=str(payload.get("rate_code") or "").strip().upper()
    description=str(payload.get("description") or "").strip()
    source=str(payload.get("source_reference") or "").strip()
    actor=str(payload.get("created_by") or "").strip()
    start=_day(payload.get("valid_from"),"valid_from")
    end=payload.get("valid_to") or None
    if end:
        end=_day(end,"valid_to")
    if end and end<start:
        raise ValueError("valid_to must not precede valid_from")
    try:
        percent=Decimal(str(payload.get("rate_percent")))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise ValueError("rate_percent must be numeric") from exc
    if not percent.is_finite() or percent<0 or percent>100 or percent!=percent.quantize(RATE_UNIT):
        raise ValueError("rate_percent must be 0-100, at most four decimal places")
    if not code or len(code)>40 or not all(c.isascii() and (c.isupper() or c.isdigit() or c in "_-") for c in code):
        raise ValueError("rate_code must use 1-40 uppercase ASCII letters, digits, _ or -")
    if not description or not source or not actor:
        raise ValueError("description, authoritative source reference and operator are required")
    if max(len(description),len(source),len(actor))>300:
        raise ValueError("rate metadata too long")
    overlapping=conn.execute(
        "SELECT valid_from,valid_to FROM tax_rate_rules WHERE rate_code=? "
        "AND (valid_to IS NULL OR valid_to>=?) AND (? IS NULL OR valid_from<=?)",
        (code,start,end,end)
    ).fetchone()
    prior=conn.execute(
        "SELECT * FROM tax_rate_rules WHERE rate_code=? AND valid_from=?",
        (code,start)
    ).fetchone()
    normalized=str(percent)
    if prior:
        fields={"valid_to":end,"rate_percent":normalized,
                "description":description,"source_reference":source,"created_by":actor}
        if any(str(prior[k] or "") != str(v or "") for k,v in fields.items()):
            raise ValueError("existing tax-rate version is immutable; use a later effective date")
        return {"rate_code":code,"valid_from":start,"duplicate":True}
    if overlapping:
        raise ValueError("tax-rate validity periods may not overlap for one code")
    ensure_open(conn,start)
    conn.execute("INSERT INTO tax_rate_rules "
                 "(rate_code,valid_from,valid_to,rate_percent,description,source_reference,created_by,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (code,start,end,normalized,description,source,actor,now()))
    return {"rate_code":code,"valid_from":start,"duplicate":False,
            "tax_credit_recognized":False,"accounting_effect":"none"}
