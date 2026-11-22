"""Tests for the read-only dashboard API (fork addition; tests/ is git-ignored upstream).

The fixture builds a real database from tables.sql and runs the real global
access guard, so the portal-host and method rules are exercised end to end.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from api import dashboard
from core.global_access_guard import register_global_access_guard
from core.portal_media_request_jobs import JOB_SCHEMA, JOB_INDEXES

ROOT = Path(__file__).resolve().parents[1]
KEY = "test-key-not-a-secret"
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
ADMIN_HOST = "vodum.test"
PORTAL_HOST = "portal.test"
MAIL = "member1@example.invalid"


def _build_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "tables.sql").read_text(encoding="utf-8"))
    conn.execute("DROP TABLE IF EXISTS portal_media_request_jobs")
    conn.execute(JOB_SCHEMA)
    for statement in JOB_INDEXES:
        conn.execute(statement)
    conn.execute("""CREATE TABLE admin_auth_identities (
        id INTEGER PRIMARY KEY, admin_account_id INTEGER, provider TEXT, is_active INTEGER)""")
    conn.execute("INSERT OR IGNORE INTO settings(id) VALUES (1)")
    conn.execute("UPDATE settings SET auth_enabled=0, portal_enabled=1, portal_public_url=? WHERE id=1",
                 (f"https://{PORTAL_HOST}",))

    conn.executemany("INSERT INTO subscription_templates(id,name,duration_days,is_lifetime) VALUES (?,?,?,?)",
                     [(1, "Monthly", 30, 0), (2, "Forever", 0, 1)])
    # (id, username, status, expiration_date, plan)
    users = [
        (1, "alice", "active", "2027-01-01", 1),
        (2, "bob", "pre_expired", "2026-12-10", 1),
        (3, "carol", "reminder", "2026-11-26", 1),
        (4, "dave", "expired", "2026-11-20", 1),
        (5, "erin", "invited", "2026-11-24", 1),
        (6, "frank", "unfriended", "2026-11-25", 1),
        (7, "grace", "suspended", "2026-12-01", 1),
        (8, "heidi", "unknown", "2026-11-30", None),
        (9, "ivan", "active", None, 2),
        (10, "judy", "active", "2026-11-23", 1),   # expires today
        (11, "kim", "active", "2026-12-23", 1),    # exactly 30 days
        (12, "lee", "active", "2026-12-24", 1),    # 31 days
        (13, "mia", "expired", "2026-11-22", 1),   # yesterday
    ]
    for uid, name, status, exp, plan in users:
        conn.execute(
            "INSERT INTO vodum_users(id,username,email,status,expiration_date,subscription_template_id) VALUES (?,?,?,?,?,?)",
            (uid, name, MAIL if uid == 1 else f"user{uid}@example.invalid", status, exp, plan))
    conn.execute("UPDATE vodum_users SET username=NULL, firstname='No', lastname='Name' WHERE id=13")

    conn.execute("INSERT INTO servers(id,name,server_identifier,type,status,last_checked,token,server_version) "
                 "VALUES (1,'Plex One','id1','plex','up','2026-11-23 11:59:00','SECRET-TOKEN','1.2.3')")
    conn.execute("INSERT INTO servers(id,name,server_identifier,type,status,last_checked,unavailable_since,token) "
                 "VALUES (2,'Plex Two','id2','plex','down','2026-11-23 11:58:00','2026-11-23 08:00:00','SECRET-TOKEN')")
    conn.execute("INSERT INTO servers(id,name,server_identifier,type,status) VALUES (3,'Seerr','id3','seerr','up')")
    conn.executemany("INSERT INTO libraries(id,server_id,section_id,name,type) VALUES (?,?,?,?,?)",
                     [(1, 1, "1", "Movies", "movie"), (2, 1, "2", "Shows", "show"), (3, 2, "1", "Anime", "show")])
    conn.executemany("INSERT INTO media_users(id,server_id,vodum_user_id,username,email,type,role) VALUES (?,?,?,?,?,?,?)",
                     [(1, 1, 1, "alice-plex", MAIL, "plex", "friend"),
                      (2, 2, 1, "alice-plex", MAIL, "plex", "friend"),
                      (3, 1, 2, "bob-plex", None, "plex", "friend")])
    conn.executemany("INSERT INTO media_user_libraries(media_user_id,library_id) VALUES (?,?)",
                     [(1, 1), (1, 2), (2, 3), (3, 1)])
    conn.executemany("INSERT INTO media_sessions(server_id,provider,session_key,last_seen_at,missing_count) VALUES (?,?,?,?,?)",
                     [(1, "plex", "a", "2026-11-23 11:59:30", 0),
                      (1, "plex", "b", "2026-11-23 11:59:50", 0),
                      (1, "plex", "stale", "2026-11-23 10:00:00", 0),
                      (2, "plex", "c", "2026-11-23 11:59:30", 1)])
    conn.executemany("INSERT INTO arr_monitoring_samples(server_id,checked_at,online) VALUES (?,?,?)",
                     [(3, "2026-11-10 00:00:00", 1),   # outside the 7-day window
                      (3, "2026-11-20 00:00:00", 1), (3, "2026-11-20 01:00:00", 1),
                      (3, "2026-11-21 00:00:00", 0), (3, "2026-11-22 00:00:00", 1)])

    conn.executemany("INSERT INTO subscription_gift_runs(id,created_at,target_type,days_added,reason) VALUES (?,?,?,?,?)",
                     [(1, "2026-10-01 10:00:00", "all", 30, "autumn"),
                      (2, "2026-11-01 10:00:00", "server", 14, "outage token=abc123 mail me@example.invalid")])
    conn.executemany("INSERT INTO subscription_gift_run_users(run_id,vodum_user_id) VALUES (?,?)",
                     [(1, 1), (2, 1), (1, 2)])

    conn.execute("INSERT INTO portal_accounts(id,vodum_user_id,status) VALUES (1,1,'active')")
    conn.execute("INSERT INTO portal_accounts(id,vodum_user_id,status) VALUES (2,2,'active')")
    jobs = [  # id, account, kind, ext, title, source, status, seerr_request_id, seerr_state, created
        ("j1", 1, "movie", 11, "Open pending", "seerr", "done", 101, "pending_approval", "2026-11-22 10:00:00"),
        ("j2", 1, "series", 12, "Approved show", "seerr", "done", 102, "approved", "2026-11-21 10:00:00"),
        ("j3", 2, "movie", 13, "Available movie", "seerr", "done", 103, "available", "2026-11-20 10:00:00"),
        ("j4", 2, "movie", 14, "Declined movie", "seerr", "done", 104, "declined", "2026-11-19 10:00:00"),
        ("j5", 2, "movie", 15, "Queued", "arr", "queued", None, None, "2026-11-23 09:00:00"),
        ("j6", 1, "movie", 16, "Arr done", "arr", "done", None, None, "2026-11-18 10:00:00"),
    ]
    for j in jobs:
        conn.execute("INSERT INTO portal_media_request_jobs(id,account_id,kind,external_id,title,source,status,"
                     "seerr_request_id,seerr_state,created_at,seasons) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (*j, "[1, 2]" if j[0] == "j2" else None))

    conn.execute("DELETE FROM tasks")
    conn.executemany("INSERT INTO tasks(id,name,schedule,enabled,status,last_run,next_run,last_error) VALUES (?,?,?,?,?,?,?,?)",
                     [(1, "ok_task", "0 * * * *", 1, "idle", "2026-11-23 11:00:00", "2026-11-23 12:00:00", None),
                      (2, "bad_task", "0 * * * *", 1, "error", "2026-11-23 11:00:00", None,
                       "boom https://x/y?X-Plex-Token=abcdef&a=1 password: hunter2 a@b.example"),
                      (3, "off_task", "0 * * * *", 0, "idle", None, None, None),
                      (4, "new_task", "0 * * * *", 1, "idle", None, None, None),
                      (5, "run_task", "0 * * * *", 1, "running", "2026-11-23 11:00:00", None, None)])
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "database.db"
    backups = tmp_path / "backups"
    backups.mkdir()
    old, new = backups / "backup_20261101_000000.zip", backups / "backup_20261122_030000.zip"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (1_795_400_000, 1_795_400_000))  # 2026-11-22T..Z
    (backups / "notes.txt").write_text("ignored")
    _build_db(db_path)

    monkeypatch.setenv("VODUM_DASHBOARD_API_KEY", KEY)
    monkeypatch.setenv("VODUM_ADMIN_PUBLIC_URL", f"https://{ADMIN_HOST}")
    monkeypatch.setenv("VODUM_IP_FILTER", "0")
    monkeypatch.setattr(dashboard, "_now", lambda: NOW)
    app = Flask(__name__)
    app.config.update(DATABASE=str(db_path), BACKUP_DIR=str(backups), SECRET_KEY="t")
    app.register_blueprint(dashboard.dashboard_api)
    register_global_access_guard(app)
    return app


@pytest.fixture()
def client(env):
    return env.test_client()


def get(client, path, key=KEY, host=ADMIN_HOST, **kwargs):
    headers = {"Host": host}
    if key is not None:
        headers["X-Api-Key"] = key
    return client.get("/api/dashboard/v1" + path, headers=headers, **kwargs)


def by_name(payload, key="users"):
    return {u["display_name"]: u for u in payload[key]}


# ------------------------------------------------------------------ auth
ENDPOINTS = ["/health", "/users", "/users/1", "/expiring", "/requests", "/servers", "/tasks", "/stats"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_missing_or_wrong_key_is_404(client, path):
    assert get(client, path, key=None).status_code == 404
    assert get(client, path, key="wrong").status_code == 404
    assert get(client, path, key="").status_code == 404
    assert get(client, path).status_code == 200


def test_unset_or_empty_env_key_disables_api(client, monkeypatch):
    monkeypatch.setenv("VODUM_DASHBOARD_API_KEY", "")
    assert get(client, "/health", key="").status_code == 404
    assert get(client, "/health", key=KEY).status_code == 404
    monkeypatch.delenv("VODUM_DASHBOARD_API_KEY")
    assert get(client, "/health").status_code == 404


@pytest.mark.parametrize("path", ENDPOINTS)
def test_portal_host_is_404_even_with_valid_key(client, path):
    assert get(client, path, host=PORTAL_HOST).status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_write_methods_are_404_not_405(client, method):
    response = getattr(client, method)("/api/dashboard/v1/users", headers={"X-Api-Key": KEY, "Host": ADMIN_HOST})
    assert response.status_code == 404


def test_internal_host_name_works(client):
    assert get(client, "/health", host="vodum:5000").status_code == 200


def test_responses_are_not_cached(client):
    assert get(client, "/health").headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ health
def test_health(client):
    data = get(client, "/health").get_json()
    assert data["status"] == "ok"
    assert data["generated_at"] == "2026-11-23T12:00:00Z"
    assert data["portal_enabled"] is True
    assert data["db_size_bytes"] > 0
    assert data["backup_count"] == 2
    assert data["last_backup_at"].endswith("Z") and data["last_backup_at"].startswith("2026-")
    assert data["uptime_seconds"] >= 0
    assert "version" in data


def test_health_without_backup_dir(env):
    env.config["BACKUP_DIR"] = str(Path(env.config["BACKUP_DIR"]) / "missing")
    data = get(env.test_client(), "/health").get_json()
    assert data["last_backup_at"] is None and data["backup_count"] == 0


# ------------------------------------------------------------------ users and statuses
def test_all_status_cases(client):
    users = by_name(get(client, "/users").get_json())
    assert users["alice"]["status"] == "active"
    assert users["bob"]["status"] == "expiring_soon" and users["bob"]["status_label"] == "Expiring soon"
    assert users["carol"]["status"] == "reminder"
    assert users["dave"]["status"] == "expired"
    assert users["erin"]["status"] == "invited"
    assert users["frank"]["status"] == "removed" and users["frank"]["status_label"] == "Removed"
    assert users["grace"]["status"] == "suspended"
    assert users["heidi"]["status"] == "unknown"


@pytest.mark.parametrize("query,expected", [
    ("active", {"alice", "ivan", "judy", "kim", "lee"}),
    ("expiring_soon", {"bob"}), ("Expiring soon", {"bob"}), ("pre_expired", {"bob"}),
    ("reminder", {"carol"}), ("Expired", {"dave", "No Name"}), ("invited", {"erin"}),
    ("removed", {"frank"}), ("unfriended", {"frank"}), ("suspended", {"grace"}), ("unknown", {"heidi"}),
])
def test_status_filter(client, query, expected):
    data = get(client, "/users", query_string={"status": query}).get_json()
    assert {u["display_name"] for u in data["users"]} == expected
    assert data["count"] == data["total"] == len(expected)


def test_status_filter_rejects_unknown_value(client):
    assert get(client, "/users", query_string={"status": "nope"}).status_code == 400


def test_limit(client):
    data = get(client, "/users", query_string={"limit": 3}).get_json()
    assert data["count"] == 3 and data["total"] == 13
    assert get(client, "/users", query_string={"limit": 0}).status_code == 400
    assert get(client, "/users", query_string={"limit": "x"}).status_code == 400
    assert get(client, "/users", query_string={"limit": 999999}).status_code == 400


def test_user_fields_plan_expiry_gift_and_accounts(client):
    users = by_name(get(client, "/users").get_json())
    alice = users["alice"]
    assert alice["plan"] == "Monthly" and alice["plan_is_lifetime"] is False
    assert alice["expiration_date"] == "2027-01-01" and alice["days_left"] == 39
    assert alice["last_gift"]["run_id"] == 2 and alice["last_gift"]["days_added"] == 14
    assert alice["last_gift"]["at"] == "2026-11-01T10:00:00Z"
    assert alice["account_count"] == 2
    assert {(a["server"], tuple(a["libraries"])) for a in alice["accounts"]} == {
        ("Plex One", ("Movies", "Shows")), ("Plex Two", ("Anime",))}
    assert users["ivan"]["plan_is_lifetime"] is True and users["ivan"]["expiration_date"] is None
    assert users["ivan"]["days_left"] is None and users["ivan"]["last_gift"] is None
    assert users["heidi"]["plan"] is None
    assert users["No Name"]["display_name"] == "No Name"
    assert users["dave"]["days_left"] == -3


def test_user_detail_has_gift_history_and_accounts(client):
    data = get(client, "/users/1").get_json()["user"]
    assert [g["run_id"] for g in data["gifts"]] == [2, 1]
    assert data["gifts"][0]["target_type"] == "server"
    assert data["accounts"][0]["username"] == "alice-plex"
    assert get(client, "/users/9999").status_code == 404


def test_gift_reason_is_scrubbed(client):
    reason = get(client, "/users/1").get_json()["user"]["gifts"][0]["reason"]
    assert "abc123" not in reason and "me@example.invalid" not in reason


# ------------------------------------------------------------------ pii
@pytest.mark.parametrize("path", ["/users", "/users/1", "/expiring?days=400", "/requests?state=all"])
def test_no_mail_by_default(client, path):
    body = get(client, path).get_data(as_text=True)
    assert "@example.invalid" not in body and "email" not in body


@pytest.mark.parametrize("flag", ["1", "true"])
def test_pii_adds_mail(client, flag):
    users = by_name(get(client, "/users", query_string={"pii": flag}).get_json())
    assert users["alice"]["email"] == MAIL and users["alice"]["accounts"] is not None
    detail = get(client, "/users/1", query_string={"pii": flag}).get_json()["user"]
    assert detail["email"] == MAIL and detail["accounts"][0]["email"] == MAIL
    rows = get(client, "/requests", query_string={"pii": flag}).get_json()["requests"]
    assert rows[0]["user_email"]


def test_pii_needs_a_valid_key(client):
    response = get(client, "/users", key="wrong", query_string={"pii": "1"})
    assert response.status_code == 404 and MAIL not in response.get_data(as_text=True)


def test_pii_off_values(client):
    assert "email" not in get(client, "/users", query_string={"pii": "0"}).get_data(as_text=True)


def test_no_tokens_anywhere(client):
    for path in ENDPOINTS + ["/requests?state=all", "/expiring?days=400"]:
        body = get(client, path).get_data(as_text=True)
        assert "SECRET-TOKEN" not in body and "hunter2" not in body and "abcdef" not in body


# ------------------------------------------------------------------ expiring
def test_expiring_boundaries(client):
    data = get(client, "/expiring").get_json()
    names = [u["display_name"] for u in data["users"]]
    # judy (today) included, kim (exactly 30 days) included, lee (31) and expired excluded,
    # invited/removed excluded, lifetime (no date) excluded.
    assert names == ["judy", "carol", "heidi", "grace", "bob", "kim"]
    assert [u["days_left"] for u in data["users"]] == [0, 3, 7, 8, 17, 30]
    assert data["days"] == 30 and data["count"] == 6


def test_expiring_days_parameter(client):
    names = lambda d: [u["display_name"] for u in get(client, "/expiring", query_string={"days": d}).get_json()["users"]]
    assert names(0) == ["judy"]
    assert names(3) == ["judy", "carol"]
    assert names(2) == ["judy"]
    assert names(31)[-1] == "lee"
    assert get(client, "/expiring", query_string={"days": -1}).status_code == 400
    assert get(client, "/expiring", query_string={"days": "x"}).status_code == 400


# ------------------------------------------------------------------ requests
def test_requests_default_is_open(client):
    data = get(client, "/requests").get_json()
    assert data["state"] == "open"
    assert [r["title"] for r in data["requests"]] == ["Queued", "Open pending", "Approved show"]
    first = data["requests"][1]
    assert first["seerr_id"] == 101 and first["type"] == "movie" and first["state"] == "pending_approval"
    assert first["user"] == "alice" and first["requested_at"] == "2026-11-22T10:00:00Z"
    assert first["year"] is None and first["external_id"] == 11
    assert data["requests"][2]["seasons"] == [1, 2]
    assert data["requests"][0]["state"] == "queued" and data["requests"][0]["seerr_id"] is None


def test_requests_state_filters(client):
    titles = lambda s: [r["title"] for r in get(client, "/requests", query_string={"state": s}).get_json()["requests"]]
    assert len(titles("all")) == 6
    assert titles("available") == ["Available movie"]
    assert titles("declined") == ["Declined movie"]
    assert titles("done") == ["Arr done"]
    assert titles("approved") == ["Approved show"]
    assert titles("nonsense") == []


# ------------------------------------------------------------------ servers
def test_servers(client):
    servers = {s["name"]: s for s in get(client, "/servers").get_json()["servers"]}
    one, two, seerr = servers["Plex One"], servers["Plex Two"], servers["Seerr"]
    assert one["status"] == "up" and one["now_playing"] == 2 and one["version"] == "1.2.3"
    assert one["last_checked"] == "2026-11-23T11:59:00Z" and one["down_since"] is None
    assert two["status"] == "down" and two["down_since"] == "2026-11-23T08:00:00Z"
    assert two["now_playing"] == 0  # its only session is marked missing
    assert one["history"] == [] and one["history_source"] == "none"
    assert seerr["now_playing"] is None and seerr["history_source"] == "arr_monitoring_samples"
    assert seerr["history"] == [
        {"at": "2026-11-20T00:00:00Z", "status": "up"},
        {"at": "2026-11-21T00:00:00Z", "status": "down"},
        {"at": "2026-11-22T00:00:00Z", "status": "up"},
    ]
    assert set(one) == {"id", "name", "type", "status", "last_checked", "down_since", "version",
                        "now_playing", "history_days", "history_source", "history"}


# ------------------------------------------------------------------ tasks
def test_tasks(client):
    tasks = {t["name"]: t for t in get(client, "/tasks").get_json()["tasks"]}
    assert tasks["ok_task"]["result"] == "ok" and tasks["ok_task"]["last_error"] is None
    assert tasks["ok_task"]["next_run"] == "2026-11-23T12:00:00Z"
    assert tasks["bad_task"]["result"] == "error"
    error = tasks["bad_task"]["last_error"]
    assert "boom" in error and "abcdef" not in error and "hunter2" not in error and "a@b.example" not in error
    assert tasks["off_task"]["result"] == "disabled" and tasks["off_task"]["enabled"] is False
    assert tasks["new_task"]["result"] == "never" and tasks["new_task"]["last_run"] is None
    assert tasks["run_task"]["result"] == "running"


# ------------------------------------------------------------------ stats
def test_stats(client):
    data = get(client, "/stats").get_json()
    assert data["users_total"] == 13
    assert data["users_by_status"] == {
        "active": 5, "expiring_soon": 1, "reminder": 1, "expired": 2,
        "invited": 1, "removed": 1, "suspended": 1, "unknown": 1}
    assert data["users_by_plan"] == {"(none)": 1, "Forever": 1, "Monthly": 11}
    assert data["requests_total"] == 6 and data["requests_open"] == 3
    assert data["requests_by_state"] == {
        "approved": 1, "available": 1, "declined": 1, "done": 1, "pending_approval": 1, "queued": 1}
    assert data["expiring_7d"] == 3 and data["expiring_30d"] == 6


# ------------------------------------------------------------------ time handling
def test_iso_helper_normalizes_to_utc():
    assert dashboard._iso("2026-11-23 10:00:00") == "2026-11-23T10:00:00Z"
    assert dashboard._iso("2026-11-23T10:00:00+02:00") == "2026-11-23T08:00:00Z"
    assert dashboard._iso("2026-11-23T10:00:00Z") == "2026-11-23T10:00:00Z"
    assert dashboard._iso("") is None and dashboard._iso(None) is None and dashboard._iso("garbage") is None
