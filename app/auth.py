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

from .database import get_setting

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


def make_cookie(role: str, secret: str) -> str:
    ts = str(int(time.time()))
    payload = f"{role}.{ts}"
    return f"{payload}.{_sign(secret, payload)}"


def read_cookie(value: str, secret: str) -> str | None:
    """Return the role if the cookie is valid and unexpired, else None."""
    if not value or not secret:
        return None
    try:
        role, ts, sig = value.split(".", 2)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(secret, f"{role}.{ts}")):
        return None
    try:
        if int(ts) + COOKIE_TTL < time.time():
            return None
    except ValueError:
        return None
    return role if role in ("owner", "guest") else None


# ── cached auth config (refreshed on settings change) ───────────────────────

_cache = {"loaded": False, "owner_hash": "", "guest_hash": "", "secret": "", "guest_rag": False}


async def _load() -> dict:
    if not _cache["loaded"]:
        _cache["owner_hash"] = await get_setting("owner_password_hash")
        _cache["guest_hash"] = await get_setting("guest_password_hash")
        _cache["secret"] = await get_setting("session_secret")
        _cache["guest_rag"] = (await get_setting("guest_rag_enabled")) == "1"
        _cache["loaded"] = True
    return _cache


def invalidate():
    """Call after any auth-related settings change."""
    _cache["loaded"] = False


async def owner_configured() -> bool:
    return bool((await _load())["owner_hash"])


async def guest_rag_enabled() -> bool:
    return bool((await _load())["guest_rag"])


async def current_role(request) -> str:
    """Resolve the role for a request. Open-by-default: no owner password → owner."""
    cfg = await _load()
    if not cfg["owner_hash"]:
        return "owner"  # back-compat: fully open single-user mode
    role = read_cookie(request.cookies.get(COOKIE_NAME, ""), cfg["secret"])
    return role or "guest"


async def login_role(password: str) -> str | None:
    """Return 'owner'/'guest' if the password matches, else None."""
    cfg = await _load()
    if cfg["owner_hash"] and verify_password(password, cfg["owner_hash"]):
        return "owner"
    if cfg["guest_hash"] and verify_password(password, cfg["guest_hash"]):
        return "guest"
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
