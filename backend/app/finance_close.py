"""Atomic, independently approved month-end close in named-account mode.

The close request is a separate auditable record. Only a different, signed-in
approver can insert the period lock; the lock and approval share one database
transaction. Existing single-owner local close logic stays untouched.
"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4


def _period(raw):
    if not isinstance(raw,str) or len(raw)!=7:
        raise ValueError("period must use YYYY-MM")
    try:
        if datetime.strptime(raw,"%Y-%m").strftime("%Y-%m") != raw:
            raise ValueError
    except ValueError as exc:
        raise ValueError("period must use YYYY-MM") from exc
    return raw


def _reviewed_checks(raw, required_checks):
    if not isinstance(raw,list) or not all(isinstance(x,str) for x in raw):
        raise ValueError("all close review checks must be selected")
    required={x["key"] for x in required_checks}
    if len(raw)!=len(required) or set(raw)!=required:
        missing=sorted(required-set(raw))
        raise ValueError("all close checks require explicit acknowledgment: "+", ".join(missing))
    return json.dumps(sorted(required),ensure_ascii=False)


def _readiness(conn,period,check):
    ready=check(conn,period)
    if ready["blockers"] or not ready["can_close"]:
        problems=", ".join(item["label"] for item in ready["blockers"])
        raise ValueError("period has unresolved blocking items: "+(problems or "review incomplete"))
    return ready


def submit(conn, raw, user, now, required_checks, check):
    if not user or user["role"] not in {"owner","finance_clerk","finance_approver"}:
        raise PermissionError("named finance submitter required")
    period=_period(raw.get("period"))
    checks=_reviewed_checks(raw.get("reviewed_checks"),required_checks)
    key=str(raw.get("source_key") or "").strip()
    if not key or len(key)>160:
        raise ValueError("stable source_key must be provided (1–160 characters)")
    earlier=conn.execute(
        "SELECT * FROM finance_period_close_requests WHERE source_key=?",(key,)
    ).fetchone()
    if earlier:
        if (earlier["period"]!=period or earlier["review_checks"]!=checks
                or earlier["requested_by_user_id"]!=user["id"]):
            raise ValueError("source_key was used for a different close request")
        return {"id":earlier["id"],"period":period,"status":earlier["status"],
                "duplicate":True}
    closed=conn.execute(
        "SELECT 1 FROM period_locks WHERE period=?",(period,)
    ).fetchone()
    if closed:
        raise ValueError("period is already closed")
    pending=conn.execute(
        "SELECT 1 FROM finance_period_close_requests "
        "WHERE period=? AND status='pending'",(period,)
    ).fetchone()
    if pending:
        raise ValueError("period already has a pending close request")
    ready=_readiness(conn,period,check)
    request_id=str(uuid4())
    conn.execute(
        "INSERT INTO finance_period_close_requests "
        "(id,source_key,period,review_checks,requested_by_user_id,"
        "requested_at,status) VALUES(?,?,?,?,?,?,'pending')",
        (request_id,key,period,checks,user["id"],now())
    )
    return {"id":request_id,"period":period,"status":"pending","duplicate":False,
            "journal_count_at_request":ready["journal_count"],
            "pending_independent_approval":True}


def approve(conn, raw, user, now, check):
    if not user or user["role"] not in {"owner","finance_approver"}:
        raise PermissionError("named finance approver required")
    request_id=str(raw.get("request_id") or "").strip()
    row=conn.execute(
        "SELECT * FROM finance_period_close_requests WHERE id=?",
        (request_id,)
    ).fetchone()
    if not row:
        raise ValueError("close request not found")
    if row["status"]=="approved":
        return {"id":request_id,"period":row["period"],"status":"closed",
                "duplicate":True,"approved_by_user_id":row["approved_by_user_id"]}
    if row["status"]!="pending":
        raise ValueError("close request is not pending")
    if row["requested_by_user_id"]==user["id"]:
        raise ValueError("requester and reviewer must be two different accounts")
    if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(row["period"],)).fetchone():
        raise ValueError("period was already closed by another action")
    ready=_readiness(conn,row["period"],check)
    stamp=now()
    checks=json.loads(row["review_checks"])
    conn.execute(
        "INSERT INTO period_locks "
        "(period,closed_at,closed_by,reviewed_checks,journal_count) "
        "VALUES(?,?,?,?,?)",
        (row["period"],stamp,user["username"],json.dumps(checks),ready["journal_count"])
    )
    updated=conn.execute(
        "UPDATE finance_period_close_requests "
        "SET status='approved',approved_by_user_id=?,approved_at=? "
        "WHERE id=? AND status='pending' AND requested_by_user_id!=?",
        (user["id"],stamp,request_id,user["id"])
    )
    if updated.rowcount!=1:
        raise ValueError("independent approval could not be recorded")
    return {"id":request_id,"period":row["period"],"status":"closed",
            "closed_by":user["username"],"closed_at":stamp,
            "journal_count":ready["journal_count"],"reviewed_checks":checks,
            "duplicate":False,"independent_approval":True}


def cancel(conn,raw,user,now):
    if not user:
        raise PermissionError("named finance login required")
    request_id=str(raw.get("request_id") or "").strip()
    row=conn.execute(
        "SELECT * FROM finance_period_close_requests WHERE id=?",(request_id,)
    ).fetchone()
    if not row:
        raise ValueError("close request not found")
    if row["requested_by_user_id"]!=user["id"] and user["role"]!="owner":
        raise PermissionError("only requester or owner can withdraw a pending close")
    if row["status"]=="approved":
        raise ValueError("approved close cannot be withdrawn")
    if row["status"]=="cancelled":
        return {"id":request_id,"status":"cancelled","duplicate":True}
    conn.execute(
        "UPDATE finance_period_close_requests SET status='cancelled',"
        "cancelled_by_user_id=?,cancelled_at=? "
        "WHERE id=? AND status='pending'",
        (user["id"],now(),request_id)
    )
    return {"id":request_id,"status":"cancelled","duplicate":False}


def requests(conn):
    return [dict(row) for row in conn.execute(
        "SELECT r.id,r.period,r.status,r.requested_at,r.approved_at,r.cancelled_at,"
        "r.review_checks,r.source_key,"
        "requester.username AS requested_by,approver.username AS approved_by,"
        "canceller.username AS cancelled_by "
        "FROM finance_period_close_requests r "
        "JOIN finance_users requester ON requester.id=r.requested_by_user_id "
        "LEFT JOIN finance_users approver ON approver.id=r.approved_by_user_id "
        "LEFT JOIN finance_users canceller ON canceller.id=r.cancelled_by_user_id "
        "ORDER BY r.requested_at DESC,r.id DESC LIMIT 200"
    ).fetchall()]
