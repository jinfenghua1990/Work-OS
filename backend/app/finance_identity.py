"""Local named users and two-person approval for personal advance vouchers.

Opt-in with WORKOS_AUTH_MODE=named. This is a loopback-only identity backend,
not a public SSO provider. User accounts are provisioned by a local admin CLI.
Passwords are scrypt hashes; sessions and failed-attempt counters are per-process.
"""
from __future__ import annotations
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from uuid import uuid4

ROLES = {"owner", "finance_clerk", "finance_approver", "viewer"}
SENSITIVE = {
    "/api/personal-advances/approve",
    "/api/periods/close",
    "/api/periods/close/approve",
    "/api/finance/reviews/approve",
    "/api/journals/adjustments",
    "/api/bank/allocate",
    "/api/invoices/tax-status",
    "/api/opening-balances",
    "/api/bank/fx-review",
    "/api/open-items/fx-review",
    # These are finance attestations or accounting changes, not source entry.
    "/api/tax/rates",
    "/api/bank/reconciliation",
    "/api/bank/accounts/coverage",
    "/api/expenses/reconciliation/coverage",
    "/api/inventory/adjustments",
    "/api/platform-income/reconciliation",
}
# Dynamic paths cannot be safely governed by an exact-path set.
SENSITIVE_DYNAMIC = (
    re.compile(r"^/api/purchase-receipts/[^/]+/edit$"),
    re.compile(r"^/api/sales/lines/[^/]+/(?:return|refund)$"),
    re.compile(r"^/api/expenses/allocations/[^/]+/reverse$"),
)
MAX_FAILURES = 5
FAILURE_WINDOW = 600
SESSION_SECONDS = 8 * 3600
_session_lock = threading.RLock()
_sessions: dict[str,tuple[float,str]] = {}
_failures: dict[str,list[float]] = defaultdict(list)


def enabled() -> bool:
    return os.environ.get("WORKOS_AUTH_MODE", "single").strip().lower() == "named"


def ensure_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS finance_users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner','finance_clerk','finance_approver','viewer')),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS finance_personal_advance_reviews (
        advance_id TEXT PRIMARY KEY REFERENCES personal_advances(id),
        submitted_by_user_id TEXT NOT NULL REFERENCES finance_users(id),
        submitted_at TEXT NOT NULL,
        approved_by_user_id TEXT REFERENCES finance_users(id),
        approved_at TEXT,
        CHECK(approved_by_user_id IS NULL OR approved_by_user_id != submitted_by_user_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_finance_advances_preparer
        ON finance_personal_advance_reviews(submitted_by_user_id,submitted_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS finance_period_close_requests (
        id TEXT PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        period TEXT NOT NULL,
        review_checks TEXT NOT NULL,
        requested_by_user_id TEXT NOT NULL REFERENCES finance_users(id),
        requested_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','approved','cancelled')),
        approved_by_user_id TEXT REFERENCES finance_users(id),
        approved_at TEXT,
        cancelled_by_user_id TEXT REFERENCES finance_users(id),
        cancelled_at TEXT,
        CHECK(approved_by_user_id IS NULL OR approved_by_user_id != requested_by_user_id)
    )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ix_finance_pending_period_close
        ON finance_period_close_requests(period) WHERE status='pending'""")



    conn.execute("""CREATE TABLE IF NOT EXISTS finance_action_reviews (
        id TEXT PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        action_type TEXT NOT NULL CHECK(action_type IN ('bank_allocation','manual_journal','invoice_tax_status')),
        payload TEXT NOT NULL,
        action_fingerprint TEXT NOT NULL,
        expected_before TEXT,
        submitted_by_user_id TEXT NOT NULL REFERENCES finance_users(id),
        submitted_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','approved','cancelled')),
        approved_by_user_id TEXT REFERENCES finance_users(id),
        approved_at TEXT,
        cancelled_by_user_id TEXT REFERENCES finance_users(id),
        cancelled_at TEXT,
        result_payload TEXT,
        CHECK(approved_by_user_id IS NULL OR approved_by_user_id != submitted_by_user_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_finance_action_reviews_status
        ON finance_action_reviews(status,submitted_at)""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ix_finance_review_active_fingerprint
        ON finance_action_reviews(action_fingerprint)
        WHERE status IN ('pending','approved')""")


def _hash_password(password: str, salt: bytes | None = None) -> str:
    if not isinstance(password,str) or len(password)<12 or len(password)>1024:
        raise ValueError("password must contain 12 to 1024 characters")
    salt=salt if salt is not None else secrets.token_bytes(16)
    return "scrypt$16384$"+salt.hex()+"$"+hashlib.scrypt(
        password.encode(),salt=salt,n=2**14,r=8,p=1
    ).hex()


def _matches(password: str, stored: str) -> bool:
    try:
        if not isinstance(password,str) or len(password)>1024:
            return False
        algo,cost,salt_hex,expected_hex=stored.split("$")
        if algo!="scrypt" or cost!="16384":
            return False
        salt=bytes.fromhex(salt_hex)
        expected=bytes.fromhex(expected_hex)
        if len(salt)!=16 or len(expected)!=64:
            return False
        actual=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1)
        return hmac.compare_digest(actual,expected)
    except (ValueError,TypeError,UnicodeError):
        return False


def create_user(conn, username: str, password: str, role: str) -> dict:
    username=(username or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9._-]{2,49}",username):
        raise ValueError("username must contain 3-50 lowercase ASCII characters")
    if role not in ROLES:
        raise ValueError("invalid finance role")
    exists=conn.execute("SELECT 1 FROM finance_users WHERE username=?",(username,)).fetchone()
    if exists:
        raise ValueError("username already exists; user changes require audited administration")
    uid=str(uuid4())
    conn.execute("INSERT INTO finance_users(id,username,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)",
                 (uid,username,_hash_password(password),role,datetime.now(timezone.utc).isoformat()))
    return {"id":uid,"username":username,"role":role}


def _cookie_token(headers):
    try:
        cookie=SimpleCookie()
        cookie.load(headers.get("Cookie",""))
        item=cookie.get("workos_named")
        return item.value if item else None
    except Exception:
        return None


def cookie(token: str) -> str:
    return (f"workos_named={token}; Path=/api; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_SECONDS}")


def clear_cookie() -> str:
    return "workos_named=; Path=/api; HttpOnly; SameSite=Strict; Max-Age=0"


def login(conn,ip: str,username: str,password: str) -> str | None:
    if not isinstance(username,str) or len(username)>50 or not isinstance(password,str):
        return None
    username=username.strip().lower()
    stamp=time.monotonic()
    with _session_lock:
        recent=[v for v in _failures[ip] if stamp-v<FAILURE_WINDOW]
        _failures[ip]=recent
        if len(recent)>=MAX_FAILURES:
            return None
    user=conn.execute(
        "SELECT id,password_hash FROM finance_users WHERE username=? AND active=1",
        (username,)
    ).fetchone()
    valid = bool(user and _matches(password,user["password_hash"]))
    with _session_lock:
        if not valid:
            _failures[ip].append(time.monotonic())
            return None
        _failures.pop(ip,None)
        token=secrets.token_urlsafe(32)
        _sessions[token]=(time.time()+SESSION_SECONDS,user["id"])
        return token


def principal(conn,headers):
    token=_cookie_token(headers)
    if not token:
        return None
    with _session_lock:
        record=_sessions.get(token)
        if not record or record[0]<=time.time():
            _sessions.pop(token,None)
            return None
        uid=record[1]
    user=conn.execute(
        "SELECT id,username,role,active FROM finance_users WHERE id=?",(uid,)
    ).fetchone()
    if not user or not int(user["active"]):
        with _session_lock:
            _sessions.pop(token,None)
        return None
    return {"id":user["id"],"username":user["username"],"role":user["role"]}


def logout(headers):
    token=_cookie_token(headers)
    if token:
        with _session_lock:
            _sessions.pop(token,None)


def can_write(user,path: str) -> bool:
    if not user or user["role"]=="viewer":
        return False
    if user["role"]=="finance_clerk" and (
        path in SENSITIVE or any(rule.fullmatch(path) for rule in SENSITIVE_DYNAMIC)
    ):
        return False
    return user["role"] in ROLES


def can_approve(user) -> bool:
    return bool(user and user["role"] in ("owner","finance_approver"))


def record_submission(conn, advance_id: str, user: dict, now):
    if not user or user["role"]=="viewer":
        raise ValueError("only a named finance user can submit a claim")
    existing=conn.execute(
        "SELECT submitted_by_user_id FROM finance_personal_advance_reviews "
        "WHERE advance_id=?",(advance_id,)
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO finance_personal_advance_reviews"
        "(advance_id,submitted_by_user_id,submitted_at) VALUES(?,?,?)",
        (advance_id,user["id"],now())
    )


def verify_independent_approver(conn,advance_id: str,user: dict):
    if not can_approve(user):
        raise PermissionError("finance approver role required")
    record=conn.execute(
        "SELECT r.submitted_by_user_id,r.approved_by_user_id,p.status "
        "FROM finance_personal_advance_reviews r "
        "JOIN personal_advances p ON p.id=r.advance_id WHERE r.advance_id=?",
        (advance_id,)
    ).fetchone()
    if not record:
        raise ValueError("claim has no identifiable preparer; cannot independently approve")
    if record["submitted_by_user_id"]==user["id"]:
        raise ValueError("approver and preparer must be two different named accounts")
    if record["approved_by_user_id"] and record["approved_by_user_id"]!=user["id"]:
        raise ValueError("claim has already been approved by another account")
    return record


def record_approval(conn,advance_id: str,user: dict,now):
    conn.execute(
        "UPDATE finance_personal_advance_reviews "
        "SET approved_by_user_id=?,approved_at=? "
        "WHERE advance_id=? AND submitted_by_user_id!=? "
        "AND approved_by_user_id IS NULL",
        (user["id"],now(),advance_id,user["id"])
    )


def reset_for_tests():
    with _session_lock:
        _sessions.clear()
        _failures.clear()
