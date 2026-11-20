"""Optional single-owner authentication for the Work OS *local-only* API.

When WORKOS_AUTH_PASSWORD_HASH is configured, every business/API route
requires an HttpOnly, SameSite=Strict session cookie. No remote-facing
deployment is supported here: a reverse proxy and production identity
provider are intentionally out of scope.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict
from http.cookies import SimpleCookie

SESSION_SECONDS = 8 * 3600
ATTEMPT_WINDOW = 10 * 60
MAX_FAILED_ATTEMPTS = 5
_lock = threading.RLock()
_sessions: dict[str, float] = {}
_failed: dict[str, list[float]] = defaultdict(list)


def enabled() -> bool:
    return bool(os.environ.get("WORKOS_AUTH_PASSWORD_HASH", "").strip())


def password_hash(password: str, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("operator password must contain at least 12 characters")
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$" + salt.hex() + "$" + digest.hex()


def configured_hash_valid() -> bool:
    encoded = os.environ.get("WORKOS_AUTH_PASSWORD_HASH", "").strip()
    if not encoded:
        return True
    try:
        algo, cost, salt, digest = encoded.split("$")
        return (algo == "scrypt" and cost == "16384" and len(bytes.fromhex(salt)) == 16
                and len(bytes.fromhex(digest)) == 64)
    except (ValueError, TypeError):
        return False


def password_matches(password: str) -> bool:
    encoded = os.environ.get("WORKOS_AUTH_PASSWORD_HASH", "").strip()
    if not configured_hash_valid() or not encoded:
        return False
    _, _, salt_hex, expected_hex = encoded.split("$")
    actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1)
    return hmac.compare_digest(actual, bytes.fromhex(expected_hex))


def issue_session(ip: str, password: str) -> str | None:
    now = time.monotonic()
    with _lock:
        recent = [stamp for stamp in _failed[ip] if now - stamp < ATTEMPT_WINDOW]
        _failed[ip] = recent
        if len(recent) >= MAX_FAILED_ATTEMPTS:
            return None
    matches = password_matches(password)
    with _lock:
        if not matches:
            _failed[ip].append(time.monotonic())
            return None
        _failed.pop(ip, None)
        token = secrets.token_urlsafe(32)
        _sessions[token] = time.time() + SESSION_SECONDS
        return token


def cookie_token(headers) -> str | None:
    try:
        cookie = SimpleCookie()
        cookie.load(headers.get("Cookie", ""))
        item = cookie.get("workos_session")
        return item.value if item else None
    except Exception:
        return None


def authenticated(headers) -> bool:
    if not enabled():
        return True
    token = cookie_token(headers)
    if not token:
        return False
    with _lock:
        expires = _sessions.get(token)
        if expires is None or expires <= time.time():
            _sessions.pop(token, None)
            return False
        return True


def end_session(headers) -> None:
    token = cookie_token(headers)
    if token:
        with _lock:
            _sessions.pop(token, None)


def session_cookie(token: str) -> str:
    # Local loopback HTTP only; production requires HTTPS and a secure cookie.
    return (f"workos_session={token}; Path=/api; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_SECONDS}")


def clear_cookie() -> str:
    return "workos_session=; Path=/api; HttpOnly; SameSite=Strict; Max-Age=0"


def origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # Non-browser local scripts may omit Origin.
    return origin in {
        "http://127.0.0.1:5175", "http://localhost:5175",
        "http://127.0.0.1:8103", "http://localhost:8103",
        "http://127.0.0.1:8106", "http://localhost:8106",
    }


def require_loopback_host(host: str) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(
            "This API currently supports local loopback access only. "
            "Do not expose financial records over the network without "
            "an authenticated HTTPS gateway and audited role permissions."
        )


def reset_for_tests() -> None:
    with _lock:
        _sessions.clear()
        _failed.clear()
