"""Lightweight owner/guest access control — stdlib only (no extra deps).

Design (see plan): the app is OPEN by default. As soon as an owner password is
configured, unauthenticated requests become read-only "guest" and a signed
session cookie unlocks "owner". A single ASGI middleware (in main.py) enforces a
read-only allowlist; this module only provides the primitives.
"""
import hashlib
import hmac
import secrets
import time

import aiosqlite

from .database import DB_PATH, get_setting

COOKIE_NAME = "ps_session"
COOKIE_TTL = 60 * 60 * 24 * 30  # 30 days
_PBKDF2_ITERS = 200_000

# ── password hashing ────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    """Return 'salt_hex$hash_hex' using PBKDF2-HMAC-SHA256."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PBKDF2_ITERS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), _PBKDF2_ITERS)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── signed session cookie ───────────────────────────────────────────────────

def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(role: str, secret: str, username: str = "") -> str:
    ts = str(int(time.time()))
    # username is restricted to [A-Za-z0-9_-] at creation, so '.' never appears.
    payload = f"{role}.{username}.{ts}"
    return f"{payload}.{_sign(secret, payload)}"


def read_cookie(value: str, secret: str):
    """Return (role, username) if the cookie is valid and unexpired, else None.

    Accepts the new 4-segment format `role.username.ts.sig` and the legacy
    3-segment `role.ts.sig` (username → '')."""
    if not value or not secret:
        return None
    parts = value.split(".")
    if len(parts) == 4:
        role, username, ts, sig = parts
        payload = f"{role}.{username}.{ts}"
    elif len(parts) == 3:
        role, ts, sig = parts
        username, payload = "", f"{role}.{ts}"
    else:
        return None
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        if int(ts) + COOKIE_TTL < time.time():
            return None
    except ValueError:
        return None
    if role not in ("owner", "guest"):
        return None
    return role, username


# ── cached auth config (refreshed on settings change) ───────────────────────

_cache = {"loaded": False, "owner_hash": "", "guest_hash": "", "secret": "",
          "guest_rag": False, "access_mode": "open"}


async def _load() -> dict:
    if not _cache["loaded"]:
        _cache["owner_hash"] = await get_setting("owner_password_hash")
        _cache["guest_hash"] = await get_setting("guest_password_hash")
        _cache["secret"] = await get_setting("session_secret")
        _cache["guest_rag"] = (await get_setting("guest_rag_enabled")) == "1"
        _cache["access_mode"] = (await get_setting("access_mode")) or "open"
        _cache["loaded"] = True
    return _cache


async def access_mode() -> str:
    return (await _load())["access_mode"]


async def _get_user(username: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT username, password_hash, role FROM users WHERE username=?",
            (username,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


def invalidate():
    """Call after any auth-related settings change."""
    _cache["loaded"] = False


async def owner_configured() -> bool:
    return bool((await _load())["owner_hash"])


async def guest_rag_enabled() -> bool:
    return bool((await _load())["guest_rag"])


async def current_user(request):
    """Resolve (role, username) for a request.

    Open-by-default: no owner password → owner for everyone. Once an owner
    password is set, an invalid/missing cookie is a read-only 'guest' — except
    in access_mode='friends_only', where it becomes 'anon' (no read access)."""
    cfg = await _load()
    if not cfg["owner_hash"]:
        return "owner", ""  # back-compat: fully open single-user mode
    parsed = read_cookie(request.cookies.get(COOKIE_NAME, ""), cfg["secret"])
    if parsed:
        return parsed
    if cfg["access_mode"] == "friends_only":
        return "anon", ""
    return "guest", ""


async def current_role(request) -> str:
    role, _ = await current_user(request)
    return role


async def login_role(password: str) -> str | None:
    """Return 'owner'/'guest' if the password matches, else None (legacy)."""
    cfg = await _load()
    if cfg["owner_hash"] and verify_password(password, cfg["owner_hash"]):
        return "owner"
    if cfg["guest_hash"] and verify_password(password, cfg["guest_hash"]):
        return "guest"
    return None


async def login_user(username: str, password: str):
    """Return (role, username) on success, else None.

    Empty username → owner / shared-guest password (legacy single-field login).
    A username → look up the named friend account and verify its password."""
    cfg = await _load()
    username = (username or "").strip()
    if not username:
        if cfg["owner_hash"] and verify_password(password, cfg["owner_hash"]):
            return "owner", ""
        if cfg["guest_hash"] and verify_password(password, cfg["guest_hash"]):
            return "guest", ""
        return None
    row = await _get_user(username)
    if row and verify_password(password, row["password_hash"]):
        return (row["role"] or "guest"), username
    return None


async def session_secret() -> str:
    return (await _load())["secret"]


# ── simple per-IP token bucket for guest RAG/chat ───────────────────────────

_rag_hits: dict[str, list[float]] = {}
_RAG_LIMIT = 10          # questions
_RAG_WINDOW = 60 * 60    # per hour


def allow_rag(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _rag_hits.get(ip, []) if now - t < _RAG_WINDOW]
    if len(hits) >= _RAG_LIMIT:
        _rag_hits[ip] = hits
        return False
    hits.append(now)
    _rag_hits[ip] = hits
    return True
