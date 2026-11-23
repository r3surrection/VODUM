"""Read-only JSON API for the Plex dashboard (fork addition, not part of upstream).

Every route is guarded by the ``X-Api-Key`` header. A request is accepted when
the header matches a non-revoked key of the ``dashboard_api_keys`` table (managed
on the admin page "Dashboard API", see ``core.dashboard_api_keys``) or the
``VODUM_DASHBOARD_API_KEY`` environment variable (bootstrap key). A missing,
empty or wrong key answers 404 so the API's existence is not visible; so does
the switch ``settings.dashboard_api_enabled = 0``.
The global access guard additionally answers 404 on the portal host and for any
non-GET method (see ``core.global_access_guard``).

No tokens, URLs or credentials are returned. E-mail addresses are returned only
with ``?pii=1`` (and a valid key).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from core import dashboard_api_keys as api_keys
from core.expiration_rules import parse_expiration_date
from db_manager import isolated_read_operation
from logging_utils import get_logger
from utils.version import load_app_version
from web.helpers import get_db

log = get_logger("api.dashboard")

API_PREFIX = "/api/dashboard/v1"
KEY_ENV = api_keys.KEY_ENV
dashboard_api = Blueprint("dashboard_api", __name__, url_prefix=API_PREFIX)

_STARTED = time.monotonic()
DEFAULT_USER_LIMIT = 1000
MAX_USER_LIMIT = 5000
MAX_EXPIRING_DAYS = 3650
LIVE_WINDOW_SECONDS = 120
HISTORY_DAYS = 7
GIFT_REASON_PREFIX = "Dashboard"
GIFT_DAYS_ALLOWED = (180, 365)  # the "six months" / "one year" presets of the gift form
GIFT_DEFAULT_DAYS = 365
GIFT_NOTE_MAX = 200
GIFT_IDEMPOTENCY_SECONDS = 600
GIFT_ELIGIBLE_STATUSES = ("active", "pre_expired", "reminder")
# A media request is open only in these states; everything else (removed, deleted,
# declined, failed, available, done, unknown values) is closed.
OPEN_REQUEST_STATES = ("pending_approval", "approved", "queued", "checking", "submitting")

# Vodum's stored status -> (API slug, label).
STATUS_MAP = {
    "active": ("active", "Active"),
    "pre_expired": ("expiring_soon", "Expiring soon"),
    "reminder": ("reminder", "Reminder"),
    "expired": ("expired", "Expired"),
    "invited": ("invited", "Invited"),
    "unfriended": ("removed", "Removed"),
    "suspended": ("suspended", "Suspended"),
    "unknown": ("unknown", "Unknown"),
}
STATUS_LABELS = {slug: label for slug, label in STATUS_MAP.values()}
STATUS_SLUGS = tuple(STATUS_LABELS)
# Users in these states are not part of the "expiring" list.
NOT_EXPIRING = ("invited", "unfriended")

_SECRET_RE = re.compile(
    r"(?i)((?:x-plex-)?(?:token|api[_-]?key|apikey|password|secret|authorization)[\"']?\s*[=:]\s*)[^\s&\"',;]+"
)
_MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _now() -> datetime:
    """Current UTC time; tests replace this."""
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    """UTC ISO 8601 (``2026-11-23T10:00:00Z``) for SQLite timestamps, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiry(value) -> date | None:
    return parse_expiration_date(value)


def _status(raw) -> str:
    return STATUS_MAP.get(str(raw or "").strip().lower(), STATUS_MAP["unknown"])[0]


def _normalize_status_filter(value: str) -> str | None:
    wanted = value.strip().lower().replace(" ", "_").replace("-", "_")
    if wanted in STATUS_SLUGS:
        return wanted
    if wanted in STATUS_MAP:
        return STATUS_MAP[wanted][0]
    return None


def _display_name(row) -> str:
    username = str(row["username"] or "").strip()
    if username:
        return username
    full = " ".join(
        part for part in (str(row["firstname"] or "").strip(), str(row["lastname"] or "").strip()) if part
    )
    return full or f"user-{row['id']}"


def _scrub(text, limit: int = 300) -> str | None:
    """Short error text without secret-looking values or mail addresses."""
    if text is None:
        return None
    cleaned = _SECRET_RE.sub(r"\1[redacted]", str(text))
    cleaned = _MAIL_RE.sub("[mail]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] or None


def _wants_pii() -> bool:
    return str(request.args.get("pii", "")).strip().lower() in ("1", "true", "yes")


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        abort(_bad_request(f"{name} must be an integer"))
    if value < minimum or value > maximum:
        abort(_bad_request(f"{name} must be between {minimum} and {maximum}"))
    return value


def _bad_request(message: str):
    response = jsonify({"error": message})
    response.status_code = 400
    return response


def _health_state(value) -> str:
    value = str(value or "").strip().lower()
    if value in ("up", "online"):
        return "up"
    if value in ("down", "offline"):
        return "down"
    return "unknown"


@dashboard_api.before_request
def _require_key():
    given = (request.headers.get("X-Api-Key") or "").strip()
    if not given:
        abort(404)
    db = get_db()
    if not api_keys.api_enabled(db):
        abort(404)
    if api_keys.env_key_matches(given):
        return None
    if api_keys.authenticate(db, given, now=_now()):
        return None
    abort(404)


@dashboard_api.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _json(payload: dict):
    payload = {"generated_at": _iso(_now()), **payload}
    return jsonify(payload)


# ------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------
def _load_accounts(db, user_ids=None, pii: bool = False) -> dict[int, list[dict]]:
    """Plex/Jellyfin accounts per logical user, with server and library names."""
    where, params = "", ()
    if user_ids is not None:
        if not user_ids:
            return {}
        where = "WHERE mu.vodum_user_id IN (%s)" % ",".join("?" * len(user_ids))
        params = tuple(user_ids)
    rows = db.query(
        f"""
        SELECT mu.id AS account_id, mu.vodum_user_id, mu.username, mu.email, mu.type,
               mu.role, mu.joined_at, mu.accepted_at,
               s.id AS server_id, s.name AS server_name, s.type AS server_type
        FROM media_users mu
        LEFT JOIN servers s ON s.id = mu.server_id
        {where}
        ORDER BY mu.vodum_user_id, s.name COLLATE NOCASE, mu.id
        """,
        params,
    ) or []
    libraries: dict[int, list[str]] = {}
    account_ids = [int(r["account_id"]) for r in rows]
    for start in range(0, len(account_ids), 500):
        chunk = account_ids[start:start + 500]
        for lib in db.query(
            """
            SELECT mul.media_user_id, l.name
            FROM media_user_libraries mul JOIN libraries l ON l.id = mul.library_id
            WHERE mul.media_user_id IN (%s)
            ORDER BY l.name COLLATE NOCASE
            """ % ",".join("?" * len(chunk)),
            tuple(chunk),
        ) or []:
            libraries.setdefault(int(lib["media_user_id"]), []).append(lib["name"])
    accounts: dict[int, list[dict]] = {}
    for r in rows:
        if r["vodum_user_id"] is None:
            continue
        account = {
            "account_id": int(r["account_id"]),
            "username": r["username"],
            "type": r["type"] or r["server_type"],
            "role": r["role"],
            "server_id": r["server_id"],
            "server": r["server_name"],
            "libraries": libraries.get(int(r["account_id"]), []),
        }
        if pii:
            account["email"] = r["email"]
        accounts.setdefault(int(r["vodum_user_id"]), []).append(account)
    return accounts


def _load_last_gifts(db, user_ids=None) -> dict[int, dict]:
    where, params = "", ()
    if user_ids is not None:
        if not user_ids:
            return {}
        where = "WHERE gu.vodum_user_id IN (%s)" % ",".join("?" * len(user_ids))
        params = tuple(user_ids)
    rows = db.query(
        f"""
        SELECT gu.vodum_user_id, r.id AS run_id, r.created_at, r.days_added, r.reason
        FROM subscription_gift_run_users gu
        JOIN subscription_gift_runs r ON r.id = gu.run_id
        {where}
        ORDER BY r.created_at ASC, r.id ASC
        """,
        params,
    ) or []
    latest: dict[int, dict] = {}
    for r in rows:  # ascending, so the last one wins
        latest[int(r["vodum_user_id"])] = _gift(r)
    return latest


def _gift(row) -> dict:
    return {
        "run_id": int(row["run_id"]),
        "at": _iso(row["created_at"]),
        "days_added": row["days_added"],
        "reason": _scrub(row["reason"], 200),
        "source": "Dashboard" if str(row["reason"] or "").startswith(GIFT_REASON_PREFIX) else "manual",
    }


def _user_dict(row, today: date, accounts, last_gift, pii: bool) -> dict:
    expiry = _expiry(row["expiration_date"])
    result = {
        "id": int(row["id"]),
        "display_name": _display_name(row),
        "status": _status(row["status"]),
        "status_label": STATUS_MAP.get(str(row["status"] or "").strip().lower(), STATUS_MAP["unknown"])[1],
        "plan_id": row["subscription_template_id"],
        "plan": row["plan_name"],
        "plan_is_lifetime": bool(row["plan_is_lifetime"]) if row["plan_name"] is not None else None,
        "expiration_date": expiry.isoformat() if expiry else None,
        "days_left": (expiry - today).days if expiry else None,
        "last_gift": last_gift,
        "renewal_method": row["renewal_method"],
        "renewal_date": _iso(row["renewal_date"]),
        "created_at": _iso(row["created_at"]),
        "status_changed_at": _iso(row["status_changed_at"]),
        "account_count": len(accounts),
        "accounts": [
            {k: a[k] for k in ("server_id", "server", "type", "libraries")} for a in accounts
        ],
    }
    if pii:
        result["email"] = row["email"]
        result["second_email"] = row["second_email"]
    return result


_USER_SELECT = """
    SELECT u.id, u.username, u.firstname, u.lastname, u.email, u.second_email,
           u.status, u.expiration_date, u.renewal_method, u.renewal_date,
           u.created_at, u.status_changed_at, u.subscription_template_id,
           t.name AS plan_name, t.is_lifetime AS plan_is_lifetime
    FROM vodum_users u
    LEFT JOIN subscription_templates t ON t.id = u.subscription_template_id
"""


@isolated_read_operation
def _build_users(db, status_filter, limit, pii):
    where, params = "", []
    if status_filter:
        raw = [k for k, (slug, _) in STATUS_MAP.items() if slug == status_filter]
        if status_filter == "unknown":
            known = [k for k in STATUS_MAP if k != "unknown"]
            where = "WHERE u.status IS NULL OR u.status NOT IN (%s)" % ",".join("?" * len(known))
            params = list(known)
        else:
            where = "WHERE u.status IN (%s)" % ",".join("?" * len(raw))
            params = raw
    rows = db.query(f"{_USER_SELECT} {where} ORDER BY u.id LIMIT ?", (*params, limit)) or []
    ids = [int(r["id"]) for r in rows]
    accounts = _load_accounts(db, ids, pii)
    gifts = _load_last_gifts(db, ids)
    today = _now().date()
    total = db.query_one(f"SELECT COUNT(*) AS c FROM vodum_users u {where}", tuple(params))
    return {
        "total": int(total["c"]) if total else len(rows),
        "users": [_user_dict(r, today, accounts.get(int(r["id"]), []), gifts.get(int(r["id"])), pii) for r in rows],
    }


@isolated_read_operation
def _build_user(db, user_id, pii):
    row = db.query_one(f"{_USER_SELECT} WHERE u.id = ?", (user_id,))
    if row is None:
        return None
    accounts = _load_accounts(db, [user_id], pii).get(user_id, [])
    gift_rows = db.query(
        """
        SELECT r.id AS run_id, r.created_at, r.days_added, r.reason, r.target_type
        FROM subscription_gift_run_users gu JOIN subscription_gift_runs r ON r.id = gu.run_id
        WHERE gu.vodum_user_id = ?
        ORDER BY r.created_at DESC, r.id DESC LIMIT 100
        """,
        (user_id,),
    ) or []
    gifts = []
    for g in gift_rows:
        item = _gift(g)
        item["target_type"] = g["target_type"]
        gifts.append(item)
    user = _user_dict(row, _now().date(), accounts, gifts[0] if gifts else None, pii)
    user["accounts"] = [
        {k: v for k, v in a.items()} for a in accounts
    ]
    user["gifts"] = gifts
    return user


@isolated_read_operation
def _build_expiring(db, days, pii):
    today = _now().date()
    rows = db.query(
        f"""{_USER_SELECT}
        WHERE u.expiration_date IS NOT NULL AND TRIM(u.expiration_date) <> ''
          AND (u.status IS NULL OR u.status NOT IN ({",".join("?" * len(NOT_EXPIRING))}))
        ORDER BY u.expiration_date, u.id""",
        NOT_EXPIRING,
    ) or []
    selected = []
    for r in rows:
        expiry = _expiry(r["expiration_date"])
        if expiry is not None and 0 <= (expiry - today).days <= days:
            selected.append((expiry, int(r["id"]), r))
    selected.sort(key=lambda item: (item[0], item[1]))
    ids = [item[1] for item in selected]
    accounts = _load_accounts(db, ids, pii)
    gifts = _load_last_gifts(db, ids)
    return [_user_dict(r, today, accounts.get(uid, []), gifts.get(uid), pii) for _, uid, r in selected]


def _request_state(row) -> str:
    if row["seerr_request_id"] is not None and row["seerr_state"]:
        return str(row["seerr_state"])
    status = str(row["status"] or "")
    if status == "done":
        return "done"
    return status or "queued"


def _request_is_open(row) -> bool:
    return _request_state(row) in OPEN_REQUEST_STATES


@isolated_read_operation
def _build_requests(db, state, pii):
    rows = db.query(
        """
        SELECT j.id, j.kind, j.title, j.source, j.status, j.result, j.is_4k, j.seasons,
               j.external_id, j.seerr_request_id, j.seerr_state, j.seerr_checked_at,
               j.created_at, j.finished_at, a.vodum_user_id,
               u.username, u.firstname, u.lastname, u.email
        FROM portal_media_request_jobs j
        LEFT JOIN portal_accounts a ON a.id = j.account_id
        LEFT JOIN vodum_users u ON u.id = a.vodum_user_id
        ORDER BY j.created_at DESC, j.rowid DESC
        """
    ) or []
    out = []
    for r in rows:
        job_state = _request_state(r)
        is_open = _request_is_open(r)
        if state == "open" and not is_open:
            continue
        if state not in ("open", "all") and job_state != state:
            continue
        try:
            seasons = json.loads(r["seasons"]) if r["seasons"] else None
        except (TypeError, ValueError):
            seasons = None
        item = {
            "id": r["id"],
            "title": r["title"],
            "type": r["kind"],
            "year": None,
            "user_id": r["vodum_user_id"],
            "user": _display_name(r) if r["vodum_user_id"] is not None else None,
            "state": job_state,
            "open": is_open,
            "source": r["source"],
            "job_status": r["status"],
            "requested_at": _iso(r["created_at"]),
            "finished_at": _iso(r["finished_at"]),
            "seerr_id": r["seerr_request_id"],
            "external_id": r["external_id"],
            "is_4k": None if r["is_4k"] is None else bool(r["is_4k"]),
            "seasons": seasons if isinstance(seasons, list) else None,
        }
        if pii:
            item["user_email"] = r["email"]
        out.append(item)
    return out


@isolated_read_operation
def _build_servers(db):
    now = _now()
    since = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    live_since = (now - timedelta(seconds=LIVE_WINDOW_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    servers = db.query(
        """
        SELECT id, name, type, status, last_checked, unavailable_since, server_version
        FROM servers ORDER BY name COLLATE NOCASE, id
        """
    ) or []
    playing = {
        int(r["server_id"]): int(r["c"])
        for r in db.query(
            """
            SELECT server_id, COUNT(*) AS c FROM media_sessions
            WHERE datetime(last_seen_at) >= datetime(?) AND COALESCE(missing_count, 0) = 0
            GROUP BY server_id
            """,
            (live_since,),
        ) or []
    }
    samples: dict[int, list] = {}
    for r in db.query(
        """
        SELECT server_id, checked_at, online FROM arr_monitoring_samples
        WHERE datetime(checked_at) >= datetime(?)
        ORDER BY server_id, checked_at, id
        """,
        (since,),
    ) or []:
        samples.setdefault(int(r["server_id"]), []).append(r)
    result = []
    for s in servers:
        sid = int(s["id"])
        kind = str(s["type"] or "").strip().lower()
        history, source = [], "none"
        if sid in samples:
            source = "arr_monitoring_samples"
            previous = None
            for sample in samples[sid]:
                current = "up" if int(sample["online"] or 0) else "down"
                if current != previous:
                    history.append({"at": _iso(sample["checked_at"]), "status": current})
                    previous = current
        status = _health_state(s["status"])
        result.append({
            "id": sid,
            "name": s["name"],
            "type": kind,
            "status": status,
            "last_checked": _iso(s["last_checked"]),
            "down_since": _iso(s["unavailable_since"]) if status == "down" else None,
            "version": s["server_version"],
            "now_playing": playing.get(sid, 0) if kind in ("plex", "jellyfin") else None,
            "history_days": HISTORY_DAYS,
            "history_source": source,
            "history": history,
        })
    return result


def _task_result(row) -> str:
    if not int(row["enabled"] if row["enabled"] is not None else 1):
        return "disabled"
    status = str(row["status"] or "").strip().lower()
    if status == "error":
        return "error"
    if status in ("running", "queued"):
        return status
    return "ok" if row["last_run"] else "never"


@isolated_read_operation
def _build_tasks(db):
    rows = db.query(
        """
        SELECT id, name, description, schedule, schedule_mode, interval_seconds, enabled,
               status, last_run, next_run, last_error, retry_count, max_retries, next_retry_at
        FROM tasks ORDER BY name COLLATE NOCASE
        """
    ) or []
    out = []
    for r in rows:
        result = _task_result(r)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "enabled": bool(r["enabled"] if r["enabled"] is not None else 1),
            "status": r["status"],
            "result": result,
            "last_run": _iso(r["last_run"]),
            "next_run": _iso(r["next_run"]),
            "schedule": r["schedule"] if str(r["schedule_mode"] or "cron") == "cron" else None,
            "interval_seconds": r["interval_seconds"],
            "retry_count": r["retry_count"],
            "last_error": _scrub(r["last_error"]) if result == "error" else None,
        })
    return out


@isolated_read_operation
def _build_stats(db):
    by_status = {slug: 0 for slug in STATUS_SLUGS}
    for r in db.query("SELECT status, COUNT(*) AS c FROM vodum_users GROUP BY status") or []:
        by_status[_status(r["status"])] += int(r["c"])
    by_plan: dict[str, int] = {}
    for r in db.query(
        """
        SELECT COALESCE(t.name, '') AS name, COUNT(*) AS c
        FROM vodum_users u LEFT JOIN subscription_templates t ON t.id = u.subscription_template_id
        GROUP BY t.name
        """
    ) or []:
        by_plan[r["name"] or "(none)"] = by_plan.get(r["name"] or "(none)", 0) + int(r["c"])
    requests_by_state: dict[str, int] = {}
    open_total = 0
    jobs = db.query(
        "SELECT status, seerr_request_id, seerr_state FROM portal_media_request_jobs"
    ) or []
    for r in jobs:
        key = _request_state(r)
        requests_by_state[key] = requests_by_state.get(key, 0) + 1
        open_total += 1 if _request_is_open(r) else 0
    today = _now().date()
    expiring = {7: 0, 30: 0}
    for r in db.query(
        "SELECT expiration_date FROM vodum_users WHERE expiration_date IS NOT NULL AND COALESCE(status, '') NOT IN (?, ?)",
        NOT_EXPIRING,
    ) or []:
        expiry = _expiry(r["expiration_date"])
        if expiry is not None:
            left = (expiry - today).days
            for span in expiring:
                if 0 <= left <= span:
                    expiring[span] += 1
    return {
        "users_total": sum(by_status.values()),
        "users_by_status": by_status,
        "users_by_plan": dict(sorted(by_plan.items())),
        "expiring_7d": expiring[7],
        "expiring_30d": expiring[30],
        "requests_total": len(jobs),
        "requests_open": open_total,
        "requests_by_state": dict(sorted(requests_by_state.items())),
    }


def _latest_backup(directory: str | None) -> tuple[str | None, int]:
    """Newest ``backup_*.zip`` mtime and the count; the files are only stat()ed."""
    latest, count = None, 0
    if not directory:
        return None, 0
    try:
        for entry in os.scandir(directory):
            if entry.is_file() and entry.name.startswith("backup_") and entry.name.endswith(".zip"):
                count += 1
                mtime = entry.stat().st_mtime
                latest = mtime if latest is None or mtime > latest else latest
    except OSError:
        return None, 0
    return (_iso(datetime.fromtimestamp(latest, tz=timezone.utc)) if latest is not None else None), count


@isolated_read_operation
def _read_portal_enabled(db):
    row = db.query_one("SELECT portal_enabled FROM settings WHERE id = 1")
    return bool(row and int(row["portal_enabled"] or 0) == 1)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@dashboard_api.get("/health")
def health():
    db_path = str(current_app.config.get("DATABASE") or "")
    try:
        db_size = Path(db_path).stat().st_size
    except OSError:
        db_size = None
    backup_at, backup_count = _latest_backup(current_app.config.get("BACKUP_DIR"))
    return _json({
        "status": "ok",
        "version": load_app_version(fallback=None),
        "uptime_seconds": int(time.monotonic() - _STARTED),
        "db_size_bytes": db_size,
        "last_backup_at": backup_at,
        "backup_count": backup_count,
        "portal_enabled": _read_portal_enabled(get_db()),
    })


@dashboard_api.get("/users")
def users():
    status_filter = None
    if request.args.get("status"):
        status_filter = _normalize_status_filter(request.args["status"])
        if status_filter is None:
            return _bad_request("unknown status; use one of: " + ", ".join(STATUS_SLUGS))
    limit = _int_arg("limit", DEFAULT_USER_LIMIT, 1, MAX_USER_LIMIT)
    data = _build_users(get_db(), status_filter, limit, _wants_pii())
    return _json({"count": len(data["users"]), "total": data["total"], "users": data["users"]})


@dashboard_api.get("/users/<int:user_id>")
def user_detail(user_id: int):
    user = _build_user(get_db(), user_id, _wants_pii())
    if user is None:
        abort(404)
    return _json({"user": user})


@dashboard_api.get("/expiring")
def expiring():
    days = _int_arg("days", 30, 0, MAX_EXPIRING_DAYS)
    rows = _build_expiring(get_db(), days, _wants_pii())
    return _json({"days": days, "count": len(rows), "users": rows})


@dashboard_api.get("/requests")
def media_requests():
    state = (request.args.get("state") or "open").strip().lower()
    rows = _build_requests(get_db(), state, _wants_pii())
    return _json({"state": state, "count": len(rows), "requests": rows})


@dashboard_api.get("/servers")
def servers():
    rows = _build_servers(get_db())
    return _json({"count": len(rows), "servers": rows})


@dashboard_api.get("/tasks")
def tasks():
    rows = _build_tasks(get_db())
    return _json({"count": len(rows), "tasks": rows})


@dashboard_api.get("/stats")
def stats():
    return _json(_build_stats(get_db()))


# ------------------------------------------------------------------
# The only write route: gift days to one member
# ------------------------------------------------------------------
_gift_lock = threading.Lock()
_gift_replays: dict[str, tuple[float, tuple, dict]] = {}


def _gift_error(message: str, http_status: int, **extra):
    response = jsonify({"error": message, **extra})
    response.status_code = http_status
    return response


@dashboard_api.post("/users/<int:user_id>/gift")
def gift_user(user_id: int):
    """Gift days through the same code path as Subscriptions > Gifts (one user)."""
    body = request.get_json(silent=True) if request.is_json else None
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return _gift_error("body must be a JSON object", 400)

    days = body.get("days", GIFT_DEFAULT_DAYS)
    if type(days) is not int or days not in GIFT_DAYS_ALLOWED:
        return _gift_error("days must be one of: " + ", ".join(map(str, GIFT_DAYS_ALLOWED)), 400)
    note = body.get("note")
    if note is not None:
        if not isinstance(note, str):
            return _gift_error("note must be a string", 400)
        note = "".join(ch for ch in note if ch.isprintable()).strip()
        if len(note) > GIFT_NOTE_MAX:
            return _gift_error(f"note must be at most {GIFT_NOTE_MAX} characters", 400)
    request_id = (request.headers.get("X-Request-Id") or "").strip()
    if request_id and (len(request_id) > 128 or not request_id.isprintable()):
        return _gift_error("X-Request-Id must be printable and at most 128 characters", 400)

    fingerprint = (user_id, days, note or "")
    with _gift_lock:
        mono = time.monotonic()
        for key in [k for k, v in _gift_replays.items() if mono - v[0] > GIFT_IDEMPOTENCY_SECONDS]:
            del _gift_replays[key]
        if request_id and request_id in _gift_replays:
            _, cached_fingerprint, cached_payload = _gift_replays[request_id]
            if cached_fingerprint != fingerprint:
                return _gift_error("X-Request-Id was already used with different parameters", 409)
            response = jsonify(cached_payload)
            response.headers["Idempotent-Replay"] = "true"
            return response

        db = get_db()
        row = db.query_one(
            """
            SELECT u.id, u.status, u.expiration_date,
                   EXISTS(SELECT 1 FROM media_users mu WHERE mu.vodum_user_id = u.id) AS has_account
            FROM vodum_users u WHERE u.id = ?
            """,
            (user_id,),
        )
        if row is None:
            abort(404)
        slug = _status(row["status"])
        if str(row["status"] or "").strip().lower() not in GIFT_ELIGIBLE_STATUSES:
            return _gift_error("user status does not allow a gift", 409, status=slug)
        if not row["has_account"]:
            return _gift_error("user has no media account", 409, status=slug)

        from api.subscriptions import apply_gift_run  # same path as the Gifts page

        reason = GIFT_REASON_PREFIX + (f": {note}" if note else "")
        run_id, _updated, results = apply_gift_run(
            db, [{"id": user_id, "expiration_date": row["expiration_date"]}],
            "user", None, days, reason,
        )
        if not run_id or not results or not results[0][3]:
            log.error("dashboard gift failed for user %s", user_id)
            return _gift_error("gift failed", 500)
        _, old_exp, new_exp, _ok = results[0]
        after = db.query_one("SELECT status FROM vodum_users WHERE id = ?", (user_id,))
        new_slug = _status(after["status"] if after else None)
        payload = {
            "generated_at": _iso(_now()),
            "user_id": user_id,
            "gift_id": int(run_id),
            "days_added": days,
            "old_expiration_date": old_exp,
            "new_expiration_date": new_exp,
            "status": new_slug,
            "status_label": STATUS_LABELS[new_slug],
        }
        if request_id:
            _gift_replays[request_id] = (mono, fingerprint, payload)
        log.info("dashboard gift: user=%s days=%s run=%s", user_id, days, run_id)
        return jsonify(payload)
