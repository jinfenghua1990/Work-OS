"""Personal-paid business expenses and supplier liabilities.

A personal payment never touches the company's bank ledger. A claim is first
drafted with exact archived payment evidence. Approval either accrues a single
expense payable to the individual, or *reassigns an already accrued supplier
payable* to the individual without creating a second expense/cost. The normal
bank-allocation endpoint later reimburses the individual's payable.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_advances (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK(mode IN ('expense','existing_payable')),
    claimant TEXT NOT NULL,
    merchant TEXT NOT NULL,
    paid_on TEXT NOT NULL,
    category TEXT,
    existing_payable_id TEXT REFERENCES receivables_payables(id),
    vendor_payable_id TEXT REFERENCES receivables_payables(id),
    expense_id TEXT UNIQUE REFERENCES expenses(id),
    employee_payable_id TEXT UNIQUE REFERENCES receivables_payables(id),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL CHECK(currency='CNY'),
    business_line TEXT NOT NULL,
    entity_code TEXT NOT NULL,
    project_code TEXT,
    payment_reference TEXT NOT NULL,
    archive_file_id TEXT NOT NULL REFERENCES archive_files(id),
    memo TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved')),
    accounting_on TEXT,
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_personal_advances_claimant
    ON personal_advances(claimant,status,paid_on);
CREATE INDEX IF NOT EXISTS ix_personal_advances_existing_payable
    ON personal_advances(existing_payable_id);
"""


def ensure_tables(conn):
    conn.executescript(SCHEMA)


def _day(value,name):
    value=str(value or "").strip()
    try:
        if datetime.strptime(value,"%Y-%m-%d").strftime("%Y-%m-%d")!=value:
            raise ValueError
    except ValueError as exc:
        raise ValueError(name+" must use YYYY-MM-DD") from exc
    return value


def _amount(value):
    try: number=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise ValueError("amount must be a positive CNY value") from exc
    if not number.is_finite() or number<=0 or number.as_tuple().exponent < -2:
        raise ValueError("amount must be positive with at most two decimal places")
    return number.quantize(Decimal("0.01"))


def _nonempty(value,name,limit=160):
    result=str(value or "").strip()
    if not result or len(result)>limit:
        raise ValueError(name+" is required and must be at most "+str(limit)+" characters")
    return result


def _validate_project(conn,line,entity,project):
    if line=="exhibition" and not project:
        raise ValueError("exhibition expenses must belong to an exhibition project")
    if project:
        linked=conn.execute(
            "SELECT entity_code FROM projects WHERE code=?",(project,),
        ).fetchone()
        if linked is None or linked["entity_code"]!=entity:
            raise ValueError("project must exist under the selected legal entity")


def draft(conn,raw,now):
    if not isinstance(raw,dict):
        raise ValueError("personal advance must be an object")
    source_key=_nonempty(raw.get("source_key"),"source_key")
    mode=str(raw.get("mode") or "expense")
    if mode not in ("expense","existing_payable"):
        raise ValueError("mode must be expense or existing_payable")
    claimant=_nonempty(raw.get("claimant"),"person who paid")
    merchant=_nonempty(raw.get("merchant"),"vendor or merchant")
    paid_on=_day(raw.get("paid_on"),"paid_on")
    value=_amount(raw.get("amount"))
    currency=str(raw.get("currency","CNY")).upper()
    if currency!="CNY":
        raise ValueError("personal reimbursements currently require CNY; foreign-currency reconciliation needs separate FX review")
    line=str(raw.get("business_line") or "corporate").strip()
    if line not in {"coffee","bike","exhibition","corporate"}:
        raise ValueError("business_line is not supported")
    entity=_nonempty(raw.get("entity_code") or "main","entity_code",80)
    project=str(raw.get("project_code") or "").strip() or None
    _validate_project(conn,line,entity,project)
    category=str(raw.get("category") or "").strip() or None
    target=str(raw.get("existing_payable_id") or "").strip() or None
    if mode=="expense":
        from server import EXPENSE_CATEGORIES
        if category not in EXPENSE_CATEGORIES:
            raise ValueError("expense category is required for a newly recognized expense")
        if target:
            raise ValueError("expense claim cannot also reference a supplier payable")
    else:
        if not target:
            raise ValueError("existing supplier payable must be selected to avoid posting cost twice")
        if category:
            raise ValueError("existing payable claim must not create a second expense category")
        supplier=conn.execute(
            "SELECT id,counterparty,currency,original_amount,settled_amount,direction "
            "FROM receivables_payables WHERE id=?",(target,),
        ).fetchone()
        if not supplier or supplier["direction"]!="payable" or supplier["currency"]!="CNY":
            raise ValueError("selected supplier payable is missing or not CNY")
        if supplier["counterparty"]!=merchant:
            raise ValueError("merchant must match the vendor on the existing payable")
        if Decimal(str(supplier["original_amount"]))-Decimal(str(supplier["settled_amount"]))<value:
            raise ValueError("selected supplier payable does not have enough outstanding balance")
    reference=_nonempty(raw.get("payment_reference"),"personal payment reference",240)
    file_id=str(raw.get("archive_file_id") or "").strip().lower()
    if not re.fullmatch("[0-9a-f]{64}",file_id):
        raise ValueError("archived personal payment proof ID is required")
    if conn.execute("SELECT 1 FROM archive_files WHERE id=?",(file_id,)).fetchone() is None:
        raise ValueError("the personal payment proof has not been archived")
    memo=str(raw.get("memo") or "").strip()
    if len(memo)>1000:
        raise ValueError("memo must not exceed 1000 characters")
    normalized={
        "source_key":source_key,"mode":mode,"claimant":claimant,"merchant":merchant,
        "paid_on":paid_on,"category":category,"existing_payable_id":target,
        "amount":str(value),"currency":currency,"business_line":line,
        "entity_code":entity,"project_code":project,"payment_reference":reference,
        "archive_file_id":file_id,"memo":memo,
    }
    earlier=conn.execute("SELECT * FROM personal_advances WHERE source_key=?",(source_key,)).fetchone()
    if earlier:
        for field,expected in normalized.items():
            if (earlier[field] if earlier[field] is not None else None)!=expected:
                raise ValueError("personal payment source already recorded with conflicting data")
        return {"id":earlier["id"],"status":earlier["status"],"duplicate":True}
    # A different CSV export or proof file must not create a second claim
    # for the same actual personal card transfer. The business details must
    # agree; conflicting details need explicit finance review.
    previous=conn.execute(
        "SELECT * FROM personal_advances WHERE claimant=? AND merchant=? "
        "AND paid_on=? AND amount=? AND currency=? AND payment_reference=? "
        "ORDER BY created_at LIMIT 2",
        (claimant,merchant,paid_on,str(value),currency,reference),
    ).fetchall()
    if previous:
        if len(previous)>1:
            raise ValueError("multiple existing personal payment claims require manual reconciliation")
        earlier=previous[0]
        fields=("mode","category","existing_payable_id","business_line",
                "entity_code","project_code","memo")
        if any(earlier[field]!=normalized[field] for field in fields):
            raise ValueError("personal payment already exists with conflicting business allocation")
        from original_archive import link_document
        link_document(conn,file_id,"finance.personal_advance",earlier["id"])
        return {"id":earlier["id"],"status":earlier["status"],"duplicate":True}
    claim_id=str(uuid4())
    conn.execute(
        "INSERT INTO personal_advances "
        "(id,source_key,mode,claimant,merchant,paid_on,category,existing_payable_id,"
        "amount,currency,business_line,entity_code,project_code,payment_reference,"
        "archive_file_id,memo,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (claim_id,source_key,mode,claimant,merchant,paid_on,category,target,
         str(value),currency,line,entity,project,reference,file_id,memo,"draft",now()),
    )
    from original_archive import link_document
    link_document(conn,file_id,"finance.personal_advance",claim_id)
    return {"id":claim_id,"status":"draft","duplicate":False}


def approve(conn,raw,now,ensure_open,journal):
    if not isinstance(raw,dict):
        raise ValueError("approval must be an object")
    claim_id=_nonempty(raw.get("advance_id"),"advance_id",64)
    actor=_nonempty(raw.get("approved_by"),"approved_by",120)
    record=conn.execute("SELECT * FROM personal_advances WHERE id=?",(claim_id,)).fetchone()
    if record is None:
        raise ValueError("personal advance was not found")
    if record["status"]=="approved":
        return {"id":claim_id,"status":"approved","payable_id":record["employee_payable_id"],
                "duplicate":True}
    accounting_on=_day(raw.get("accounting_on") or record["paid_on"],"accounting_on")
    ensure_open(conn,accounting_on)
    amount=Decimal(record["amount"])
    mode=record["mode"]
    expense_id=None
    vendor_payable_id=None
    if mode=="expense":
        expense_id=str(uuid4())
        conn.execute(
            "INSERT INTO expenses "
            "(id,source_key,incurred_on,category,counterparty,amount,currency,memo,"
            "created_at,business_line,entity_code,project_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (expense_id,"personal_advance:"+record["source_key"],accounting_on,
             record["category"],record["merchant"],record["amount"],"CNY",
             record["memo"],now(),record["business_line"],record["entity_code"],
             record["project_code"]),
        )
        journal(conn,"personal_advance_expense",claim_id,accounting_on,[
            ("operating_expense:"+record["category"],amount,Decimal(0)),
            ("accounts_payable",Decimal(0),amount),
        ],record["business_line"],record["entity_code"],record["project_code"])
        # The supplier invoice belongs to the merchant, not the card holder.
        # Record a supplier payable fully settled by the individual's payment
        # so the incoming invoice can still link to its proper counterparty.
        vendor_payable_id=str(uuid4())
        conn.execute(
            "INSERT INTO receivables_payables "
            "(id,direction,source_type,source_id,counterparty,original_amount,"
            "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (vendor_payable_id,"payable","operating_expense",expense_id,
             record["merchant"],record["amount"],record["amount"],
             "CNY",None,now()),
        )
        journal(conn,"personal_advance_vendor_transfer",claim_id,accounting_on,[
            ("accounts_payable",amount,Decimal(0)),
            ("accounts_payable",Decimal(0),amount),
        ],record["business_line"],record["entity_code"],record["project_code"])
    elif mode=="existing_payable":
        vendor=conn.execute(
            "SELECT * FROM receivables_payables WHERE id=?",
            (record["existing_payable_id"],),
        ).fetchone()
        if vendor is None or vendor["direction"]!="payable" or vendor["currency"]!="CNY":
            raise ValueError("supplier payable is unavailable")
        remaining=Decimal(str(vendor["original_amount"]))-Decimal(str(vendor["settled_amount"]))
        if remaining<amount:
            raise ValueError("supplier payable no longer has sufficient unallocated amount")
        vendor_payable_id=vendor["id"]
        fx=conn.execute("SELECT * FROM open_item_fx WHERE item_id=?",(vendor["id"],)).fetchone()
        if fx is None:
            raise ValueError("supplier payable lacks verified CNY carrying balance")
        fx_remaining=Decimal(str(fx["original_amount_cny"]))-Decimal(str(fx["settled_amount_cny"]))
        if fx_remaining<amount:
            raise ValueError("supplier payable CNY carrying amount is insufficient")
        conn.execute(
            "UPDATE receivables_payables SET settled_amount=? WHERE id=?",
            (str(Decimal(str(vendor["settled_amount"]))+amount),vendor["id"]),
        )
        conn.execute(
            "UPDATE open_item_fx SET settled_amount_cny=? WHERE item_id=?",
            (str(Decimal(str(fx["settled_amount_cny"]))+amount),vendor["id"]),
        )
        # Vendor liability changes to employee liability. This is not a
        # second purchase/cost. Both sides of the AP control account cancel.
        journal(conn,"personal_advance_vendor_transfer",claim_id,accounting_on,[
            ("accounts_payable",amount,Decimal(0)),
            ("accounts_payable",Decimal(0),amount),
        ],record["business_line"],record["entity_code"],record["project_code"])
    else:
        raise ValueError("invalid personal payment mode")
    employee_payable_id=str(uuid4())
    conn.execute(
        "INSERT INTO receivables_payables "
        "(id,direction,source_type,source_id,counterparty,original_amount,"
        "settled_amount,currency,due_on,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (employee_payable_id,"payable","personal_advance",claim_id,
         record["claimant"],record["amount"],"0.00","CNY",None,now()),
    )
    # SQLite trigger or existing PostgreSQL trigger creates open_item_fx,
    # but guarantee cross-backend idempotence when the trigger is absent.
    conn.execute(
        "INSERT OR IGNORE INTO open_item_fx "
        "(item_id,original_amount_cny,settled_amount_cny,exchange_rate,rate_date,rate_source)"
        " VALUES(?,?, '0.00','1',?,'personal_advance')",
        (employee_payable_id,record["amount"],accounting_on),
    )
    conn.execute(
        "UPDATE personal_advances SET status='approved',accounting_on=?,"
        "approved_by=?,approved_at=?,expense_id=?,vendor_payable_id=?,employee_payable_id=? WHERE id=?",
        (accounting_on,actor,now(),expense_id,vendor_payable_id,employee_payable_id,claim_id),
    )
    return {"id":claim_id,"status":"pending_reimbursement",
            "employee_payable_id":employee_payable_id,
            "expense_id":expense_id,"vendor_payable_id":vendor_payable_id,
            "amount":record["amount"],"currency":"CNY",
            "approved_on":accounting_on,"duplicate":False}



def import_drafts(conn,data,now):
    """Stage filtered personal BUSINESS payments from an archived CSV/XLSX.

    Import does not create a journal, bank transaction or company expense.
    Every row is explicitly reviewed before it can become reimbursable.
    """
    import hashlib
    if not isinstance(data,dict):
        raise ValueError("personal payment batch must be an object")
    file_id=str(data.get("archive_file_id") or "").strip().lower()
    if not re.fullmatch("[0-9a-f]{64}",file_id):
        raise ValueError("archived original file ID is required")
    if conn.execute("SELECT 1 FROM archive_files WHERE id=?",(file_id,)).fetchone() is None:
        raise ValueError("source payment listing is not archived")
    rows=data.get("rows")
    if not isinstance(rows,list) or not 1<=len(rows)<=100:
        raise ValueError("personal payment batch must contain 1 to 100 rows")
    created=[];duplicates=[];rejected=[]
    for i,item in enumerate(rows,1):
        savepoint=f"personal_batch_{i}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            if not isinstance(item,dict):
                raise ValueError("row must be an object")
            record=dict(item)
            record["archive_file_id"]=file_id
            record.setdefault("mode","expense")
            if not record.get("source_key"):
                natural="|".join(str(record.get(field) or "").strip() for field in
                    ("claimant","merchant","paid_on","payment_reference","amount","mode"))
                record["source_key"]="personal:source:"+hashlib.sha256(
                    natural.encode("utf-8")).hexdigest()
            out=draft(conn,record,now)
            result={"row":i,"id":out["id"]}
            (duplicates if out["duplicate"] else created).append(result)
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except (ValueError,TypeError,KeyError) as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            rejected.append({"row":i,"error":str(exc)})
    return {"created":created,"duplicates":duplicates,"rejected":rejected,
            "pending_review":True,"posted_to_ledger":False,
            "company_bank_transactions_created":0,"archive_file_id":file_id}


def list_claims(conn):
    rows=conn.execute(
        "SELECT p.*,r.original_amount AS employee_payable_amount,"
        "r.settled_amount AS reimbursed_amount "
        "FROM personal_advances p "
        "LEFT JOIN receivables_payables r ON r.id=p.employee_payable_id "
        "ORDER BY p.paid_on DESC,p.created_at DESC,p.id DESC",
    ).fetchall()
    result=[]
    for row in rows:
        record=dict(row)
        settled=Decimal(str(record["reimbursed_amount"] or "0"))
        original=Decimal(str(record["amount"]))
        if record["status"]=="draft":
            state="pending_review"
        elif settled>=original:
            state="reimbursed"
        elif settled>0:
            state="partially_reimbursed"
        else:
            state="pending_reimbursement"
        record["reimbursed_amount"]=str(settled)
        record["unreimbursed_amount"]=str(original-settled)
        record["reimbursement_status"]=state
        result.append(record)
    return result
