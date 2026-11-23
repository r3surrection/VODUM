"""Dashboard API keys stored in the database (fork addition).

A key is a random URL-safe token shown once at creation. Only its SHA-256
hash and the first ``PREFIX_LENGTH`` characters are stored. Authentication
compares hashes; ``last_used_at`` is advanced at most once per minute to keep
the read-heavy API from writing on every request. Keys are never logged.

The environment variable ``VODUM_DASHBOARD_API_KEY`` stays a valid bootstrap
key (see ``api.dashboard``); it is reported here only as "configured or not".
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

KEY_ENV = "VODUM_DASHBOARD_API_KEY"
PREFIX_LENGTH = 6
KEY_BYTES = 32  # 43 URL-safe characters
NAME_MAX_LENGTH = 80
LAST_USED_THROTTLE = timedelta(minutes=1)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(str(raw_key).encode("utf-8")).hexdigest()


def generate_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def env_key_configured() -> bool:
    return bool((os.environ.get(KEY_ENV) or "").strip())


def env_key_matches(given: str) -> bool:
    expected = (os.environ.get(KEY_ENV) or "").strip()
    given = (given or "").strip()
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


# ------------------------------------------------------------------ settings
def api_enabled(db) -> bool:
    row = db.query_one("SELECT dashboard_api_enabled FROM settings WHERE id = 1")
    if row is None or row["dashboard_api_enabled"] is None:
        return True
    return int(row["dashboard_api_enabled"]) == 1


def set_api_enabled(db, enabled: bool) -> None:
    db.execute("UPDATE settings SET dashboard_api_enabled = ? WHERE id = 1", (1 if enabled else 0,))


# ------------------------------------------------------------------ keys
def normalize_name(name) -> str:
    return " ".join(str(name or "").split())[:NAME_MAX_LENGTH]


def create_key(db, name: str, *, now: datetime | None = None) -> tuple[dict, str]:
    """Insert a new key; return (stored row without hash, clear-text key)."""
    clean = normalize_name(name)
    if not clean:
        raise ValueError("dashboard_api_key_name_required")
    raw = generate_key()
    created = _stamp(now or utcnow())
    cursor = db.execute(
        "INSERT INTO dashboard_api_keys (name, key_hash, key_prefix, created_at) VALUES (?, ?, ?, ?)",
        (clean, hash_key(raw), raw[:PREFIX_LENGTH], created),
    )
    row = {
        "id": int(cursor.lastrowid),
        "name": clean,
        "key_prefix": raw[:PREFIX_LENGTH],
        "created_at": created,
        "last_used_at": None,
        "revoked_at": None,
        "status": "active",
    }
    return row, raw


def list_keys(db) -> list[dict]:
    rows = db.query(
        "SELECT id, name, key_prefix, created_at, last_used_at, revoked_at "
        "FROM dashboard_api_keys ORDER BY revoked_at IS NOT NULL, created_at DESC, id DESC"
    )
    result = []
    for row in rows or []:
        item = dict(row)
        item["status"] = "revoked" if item.get("revoked_at") else "active"
        result.append(item)
    return result


def revoke_key(db, key_id: int, *, now: datetime | None = None) -> bool:
    cursor = db.execute(
        "UPDATE dashboard_api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_stamp(now or utcnow()), int(key_id)),
    )
    return cursor.rowcount > 0


def authenticate(db, given: str, *, now: datetime | None = None) -> bool:
    """True when ``given`` matches a non-revoked stored key; advances last_used_at."""
    given = (given or "").strip()
    if not given:
        return False
    digest = hash_key(given)
    row = db.query_one(
        "SELECT id, key_hash, last_used_at FROM dashboard_api_keys WHERE key_hash = ? AND revoked_at IS NULL",
        (digest,),
    )
    if row is None or not hmac.compare_digest(str(row["key_hash"]), digest):
        return False
    moment = now or utcnow()
    last = _parse(row["last_used_at"])
    if last is None or moment - last >= LAST_USED_THROTTLE:
        db.execute("UPDATE dashboard_api_keys SET last_used_at = ? WHERE id = ?", (_stamp(moment), row["id"]))
    return True
