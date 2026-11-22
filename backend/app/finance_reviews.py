"""Two-person review of named-mode settlement, journal, and tax-state writes.

A review only stages an immutable JSON payload. Actual accounting actions and
the approval audit are committed together by the API using a single connection.
No generic URL, SQL, or arbitrarily selected handler can be executed.
"""
from __future__ import annotations

import json
import hashlib
from uuid import uuid4

ACTIONS = {
    "bank_allocation": "/api/bank/allocate",
    "manual_journal": "/api/journals/adjustments",
    "invoice_tax_status": "/api/invoices/tax-status",
}
MAX_PAYLOAD_BYTES = 64000


def _canonical(payload):
    if not isinstance(payload,dict) or not payload:
        raise ValueError("approval payload must be a non-empty object")
    data=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    if len(data.encode("utf-8"))>MAX_PAYLOAD_BYTES:
        raise ValueError("approval payload is too large")
    return data


def submit(conn, raw:dict, actor:dict, now):
    if not actor or actor["role"] not in {"owner","finance_approver","finance_clerk"}:
        raise PermissionError("named finance user required to submit approval")
    action=str(raw.get("action_type") or "").strip()
    if action not in ACTIONS:
        raise ValueError("unsupported approval action")
    source_key=str(raw.get("source_key") or "").strip()
    if not source_key or len(source_key)>160:
        raise ValueError("approval source_key required (1–160 characters)")
    payload=_canonical(raw.get("payload"))
    prior=conn.execute("SELECT * FROM finance_action_reviews WHERE source_key=?",(source_key,)).fetchone()
    if prior:
        if prior["action_type"]!=action or prior["payload"]!=payload or prior["submitted_by_user_id"]!=actor["id"]:
            raise ValueError("approval source_key already belongs to a different request")
        return {"id":prior["id"],"status":prior["status"],"action_type":action,"duplicate":True}

    # Ensure the invoice still matches the state that the submitter saw;
    # otherwise a stale approval could accidentally overwrite a later review.
    expectation=None
    data=json.loads(payload)
    if action=="invoice_tax_status":
        invoice_id=data.get("invoice_id")
        invoice=conn.execute("SELECT tax_status,tax_evidence_ref FROM invoices WHERE id=?",(invoice_id,)).fetchone()
        if not invoice:
            raise ValueError("invoice not found")
        expectation=json.dumps({
            "tax_status":invoice["tax_status"],
            "tax_evidence_ref":invoice["tax_evidence_ref"]
        },ensure_ascii=False,sort_keys=True,separators=(",",":"))

    # Deduplicate business intent even when a caller picks a fresh request ID.
    # Client-supplied audit identity / execution keys never define the action.
    fingerprint_data=json.loads(payload)
    if action=="bank_allocation":
        fingerprint_data.pop("action_key",None)
    if action=="invoice_tax_status":
        fingerprint_data.pop("changed_by",None)
    normalized=_canonical(fingerprint_data)
    fingerprint=hashlib.sha256(
        (action+chr(31)+normalized+chr(31)+(expectation or "")).encode("utf-8")
    ).hexdigest()
    duplicate=conn.execute(
        "SELECT id,status,submitted_by_user_id FROM finance_action_reviews "
        "WHERE action_fingerprint=? AND status IN ('pending','approved')",
        (fingerprint,)
    ).fetchone()
    if duplicate:
        if duplicate["submitted_by_user_id"]!=actor["id"]:
            raise ValueError("same financial action is already pending or approved by another submitter")
        return {"id":duplicate["id"],"status":duplicate["status"],
                "action_type":action,"duplicate":True}
    request_id=str(uuid4())
    conn.execute(
        "INSERT INTO finance_action_reviews "
        "(id,source_key,action_type,payload,action_fingerprint,expected_before,"
        "submitted_by_user_id,submitted_at,status) "
        "VALUES(?,?,?,?,?,?,?,?,'pending')",
        (request_id,source_key,action,payload,fingerprint,expectation,actor["id"],now())
    )
    return {"id":request_id,"action_type":action,"status":"pending","duplicate":False}


def load_for_approval(conn, request_id:str, actor:dict):
    if not actor or actor["role"] not in {"owner","finance_approver"}:
        raise PermissionError("named finance approver required")
    review=conn.execute(
        "SELECT * FROM finance_action_reviews WHERE id=?",(request_id,)
    ).fetchone()
    if not review:
        raise ValueError("approval request not found")
    if review["status"]=="approved":
        return review,None
    if review["status"]!="pending":
        raise ValueError("approval request is no longer pending")
    if review["submitted_by_user_id"]==actor["id"]:
        raise ValueError("preparer and reviewer must be different named accounts")
    if review["action_type"] not in ACTIONS:
        raise ValueError("unknown approval action")
    data=json.loads(review["payload"])
    if review["action_type"]=="invoice_tax_status":
        invoice=conn.execute(
            "SELECT tax_status,tax_evidence_ref FROM invoices WHERE id=?",
            (data.get("invoice_id"),)
        ).fetchone()
        if not invoice:
            raise ValueError("invoice not found at approval")
        before=json.dumps({
            "tax_status":invoice["tax_status"],
            "tax_evidence_ref":invoice["tax_evidence_ref"]
        },ensure_ascii=False,sort_keys=True,separators=(",",":"))
        if before!=review["expected_before"]:
            raise ValueError("invoice tax status changed after submission; request a new review")
    return review,data


def record_approval(conn,review,actor,now,result):
    serialized=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    changed=conn.execute(
        "UPDATE finance_action_reviews "
        "SET status='approved',approved_by_user_id=?,approved_at=?,result_payload=? "
        "WHERE id=? AND status='pending' AND submitted_by_user_id!=?",
        (actor["id"],now(),serialized,review["id"],actor["id"])
    )
    if changed.rowcount!=1:
        raise ValueError("independent finance review was not recorded")
    return {"id":review["id"],"status":"approved",
            "action_type":review["action_type"],
            "approved_by":actor["username"],"result":result,
            "duplicate":False}


def already_approved(review):
    result=json.loads(review["result_payload"]) if review["result_payload"] else {}
    return {"id":review["id"],"status":"approved",
            "action_type":review["action_type"],
            "duplicate":True,"result":result}


def cancel(conn,request_id,actor,now):
    if not actor:
        raise PermissionError("named finance login required")
    review=conn.execute(
        "SELECT * FROM finance_action_reviews WHERE id=?",(request_id,)
    ).fetchone()
    if not review:
        raise ValueError("approval request not found")
    if review["submitted_by_user_id"]!=actor["id"] and actor["role"]!="owner":
        raise PermissionError("only original requester or owner can withdraw")
    if review["status"]=="approved":
        raise ValueError("completed finance approval cannot be withdrawn")
    if review["status"]=="cancelled":
        return {"id":request_id,"status":"cancelled","duplicate":True}
    conn.execute(
        "UPDATE finance_action_reviews "
        "SET status='cancelled',cancelled_by_user_id=?,cancelled_at=? "
        "WHERE id=? AND status='pending'",
        (actor["id"],now(),request_id)
    )
    return {"id":request_id,"status":"cancelled","duplicate":False}


def list_requests(conn):
    return [dict(x) for x in conn.execute(
        "SELECT r.id,r.action_type,r.source_key,r.status,r.submitted_at,"
        "r.approved_at,r.cancelled_at,r.payload,r.result_payload,"
        "submitter.username AS submitted_by,approver.username AS approved_by,"
        "canceller.username AS cancelled_by "
        "FROM finance_action_reviews r "
        "JOIN finance_users submitter ON submitter.id=r.submitted_by_user_id "
        "LEFT JOIN finance_users approver ON approver.id=r.approved_by_user_id "
        "LEFT JOIN finance_users canceller ON canceller.id=r.cancelled_by_user_id "
        "ORDER BY r.submitted_at DESC,r.id DESC LIMIT 200"
    ).fetchall()]
