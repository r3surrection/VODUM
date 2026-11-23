"""Tests for the dashboard API keys: storage, authentication and the admin page.

Reuses the database fixture of test_dashboard_api (built from tables.sql, so the
``dashboard_api_keys`` table and ``settings.dashboard_api_enabled`` come from
there; the bootstrap function is exercised separately on an old-style database).
"""
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from flask import Flask, g

from api import dashboard
from core import dashboard_api_keys as api_keys
from core.auth_principal import SESSION_PRINCIPAL_KEY, admin_principal
from core.csrf_security import csrf_request_guard
from core.db_bootstrap_dashboard_api import ensure_dashboard_api_schema
from core.global_access_guard import register_global_access_guard
from db_manager import DBManager
from routes import dashboard_api_admin
from test_dashboard_api import (  # noqa: F401  (fixtures and helpers)
    ADMIN_HOST, KEY, NOW, client, env, get,
)

ROOT = Path(__file__).resolve().parents[1]


def manager(env) -> DBManager:
    return DBManager(env.config["DATABASE"])


def raw_rows(env):
    conn = sqlite3.connect(env.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM dashboard_api_keys ORDER BY id")]
    finally:
        conn.close()


# ------------------------------------------------------------------ storage
def test_create_stores_hash_and_prefix_only(env):
    row, raw = api_keys.create_key(manager(env), "  Plex   dashboard ", now=NOW)
    assert len(raw) >= 40 and raw[:6] == row["key_prefix"]
    assert row["name"] == "Plex dashboard" and row["status"] == "active"
    stored = raw_rows(env)
    assert len(stored) == 1
    assert stored[0]["key_hash"] == api_keys.hash_key(raw) and len(stored[0]["key_hash"]) == 64
    assert raw not in str(stored[0])
    assert stored[0]["created_at"] == "2026-11-23 12:00:00"
    assert stored[0]["last_used_at"] is None and stored[0]["revoked_at"] is None


def test_create_requires_a_name(env):
    with pytest.raises(ValueError):
        api_keys.create_key(manager(env), "   ")


def test_list_never_exposes_the_hash(env):
    db = manager(env)
    _, raw = api_keys.create_key(db, "one", now=NOW)
    api_keys.create_key(db, "two", now=NOW + timedelta(seconds=1))
    listed = api_keys.list_keys(db)
    assert [k["name"] for k in listed] == ["two", "one"]  # newest first
    for item in listed:
        assert "key_hash" not in item and raw not in str(item)
        assert set(item) == {"id", "name", "key_prefix", "created_at", "last_used_at", "revoked_at", "status"}


def test_revoke_marks_once(env):
    db = manager(env)
    row, _ = api_keys.create_key(db, "x", now=NOW)
    assert api_keys.revoke_key(db, row["id"], now=NOW) is True
    assert api_keys.revoke_key(db, row["id"], now=NOW) is False  # already revoked
    assert api_keys.revoke_key(db, 9999, now=NOW) is False
    listed = api_keys.list_keys(db)
    assert listed[0]["status"] == "revoked" and listed[0]["revoked_at"] == "2026-11-23 12:00:00"


def test_bootstrap_adds_table_and_switch_to_old_database(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, portal_enabled INTEGER)")
    conn.execute("INSERT INTO settings(id) VALUES (1)")
    cursor = conn.cursor()

    def table_exists(cur, table):
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None

    def ensure_column(cur, table, column, definition):
        cur.execute(f"PRAGMA table_info({table})")
        if not any(r[1] == column for r in cur.fetchall()):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    for _ in range(2):  # idempotent
        ensure_dashboard_api_schema(conn, cursor, table_exists=table_exists, ensure_column=ensure_column)
    assert table_exists(cursor, "dashboard_api_keys")
    assert conn.execute("SELECT dashboard_api_enabled FROM settings WHERE id=1").fetchone()[0] == 1
    conn.execute("INSERT INTO dashboard_api_keys(name,key_hash,key_prefix,created_at) VALUES ('a','h','p','t')")
    with pytest.raises(sqlite3.IntegrityError):  # hash is unique
        conn.execute("INSERT INTO dashboard_api_keys(name,key_hash,key_prefix,created_at) VALUES ('b','h','p','t')")
    conn.close()


# ------------------------------------------------------------------ API access
def test_db_key_grants_access_and_records_last_use(env, client):
    _, raw = api_keys.create_key(manager(env), "dash", now=NOW - timedelta(days=1))
    assert get(client, "/health", key=raw).status_code == 200
    assert get(client, "/users", key=raw).status_code == 200
    assert raw_rows(env)[0]["last_used_at"] == "2026-11-23 12:00:00"


def test_last_used_is_written_at_most_once_per_minute(env, client, monkeypatch):
    _, raw = api_keys.create_key(manager(env), "dash", now=NOW - timedelta(days=1))
    get(client, "/health", key=raw)
    monkeypatch.setattr(dashboard, "_now", lambda: NOW + timedelta(seconds=59))
    assert get(client, "/health", key=raw).status_code == 200
    assert raw_rows(env)[0]["last_used_at"] == "2026-11-23 12:00:00"
    monkeypatch.setattr(dashboard, "_now", lambda: NOW + timedelta(seconds=60))
    assert get(client, "/health", key=raw).status_code == 200
    assert raw_rows(env)[0]["last_used_at"] == "2026-11-23 12:01:00"


def test_revoked_key_is_404(env, client):
    db = manager(env)
    row, raw = api_keys.create_key(db, "dash", now=NOW)
    assert get(client, "/health", key=raw).status_code == 200
    api_keys.revoke_key(db, row["id"], now=NOW)
    assert get(client, "/health", key=raw).status_code == 404
    assert get(client, "/users/1", key=raw).status_code == 404


def test_wrong_prefix_or_hash_is_404(env, client):
    _, raw = api_keys.create_key(manager(env), "dash", now=NOW)
    assert get(client, "/health", key=raw[:-1]).status_code == 404
    assert get(client, "/health", key=raw + "x").status_code == 404
    assert get(client, "/health", key=api_keys.hash_key(raw)).status_code == 404  # the stored hash is no key


def test_env_key_keeps_working_next_to_db_keys(env, client):
    _, raw = api_keys.create_key(manager(env), "dash", now=NOW)
    assert get(client, "/health", key=KEY).status_code == 200
    assert get(client, "/health", key=raw).status_code == 200
    assert raw_rows(env)[0]["last_used_at"] == "2026-11-23 12:00:00"  # only the DB key is tracked


def test_db_key_works_without_env_variable(env, client, monkeypatch):
    _, raw = api_keys.create_key(manager(env), "dash", now=NOW)
    monkeypatch.delenv("VODUM_DASHBOARD_API_KEY")
    assert get(client, "/health", key=KEY).status_code == 404
    assert get(client, "/health", key=raw).status_code == 200


def test_disabled_api_is_404_for_every_key(env, client):
    db = manager(env)
    _, raw = api_keys.create_key(db, "dash", now=NOW)
    api_keys.set_api_enabled(db, False)
    assert get(client, "/health", key=KEY).status_code == 404
    assert get(client, "/health", key=raw).status_code == 404
    assert get(client, "/stats", key=raw).status_code == 404
    assert raw_rows(env)[0]["last_used_at"] is None
    api_keys.set_api_enabled(db, True)
    assert get(client, "/health", key=raw).status_code == 200


def test_gift_route_accepts_db_key(env, client):
    _, raw = api_keys.create_key(manager(env), "dash", now=NOW)
    response = client.post("/api/dashboard/v1/users/10/gift", json={"days": 180},
                           headers={"X-Api-Key": raw, "Host": ADMIN_HOST})
    assert response.status_code == 200 and response.get_json()["days_added"] == 180


# ------------------------------------------------------------------ admin page
STUB_ENDPOINTS = (
    "dashboard", "users_list", "monitoring_page", "servers_list", "subscriptions",
    "communications_page", "collections_page", "migrations_page", "backup_page",
    "settings_page", "portal_settings_page", "logs_page", "tasks_page", "about_page",
    "logout", "login", "set_language",
)


@pytest.fixture()
def admin_app(env):
    """The real admin routes and templates with a minimal shell around them."""
    app = Flask("admin-test", template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    app.config.update(DATABASE=env.config["DATABASE"], SECRET_KEY="t")
    for name in STUB_ENDPOINTS:
        app.add_url_rule(f"/_stub/{name}", endpoint=name, view_func=(lambda: "")) if name != "set_language" \
            else app.add_url_rule("/set_language/<lang>", endpoint=name, view_func=(lambda lang: ""))
    app.add_url_rule("/_stub/user/<int:user_id>", endpoint="user_detail", view_func=lambda user_id: "")

    @app.context_processor
    def _globals():
        return {"t": lambda key, **kw: f"[{key}]", "csrf_token": "csrf-test", "app_brand_name": "VODUM",
                "active_lang": "en", "settings": {}}

    @app.before_request
    def _csrf():
        from flask import session
        session["_csrf_token"] = "csrf-test"
        return csrf_request_guard()

    dashboard_api_admin.register(app)
    return app


@pytest.fixture()
def admin(admin_app):
    client = admin_app.test_client()
    with client.session_transaction() as session:
        session[SESSION_PRINCIPAL_KEY] = admin_principal("admin@example.invalid")
    return client


def test_page_requires_admin(admin_app):
    anonymous = admin_app.test_client()
    response = anonymous.get("/settings/dashboard-api")
    assert response.status_code == 302 and "/_stub/login" in response.headers["Location"]


def test_page_lists_env_row_and_keys_without_clear_text(env, admin):
    db = manager(env)
    _, raw = api_keys.create_key(db, "Plex dashboard", now=NOW)
    revoked, raw2 = api_keys.create_key(db, "Old one", now=NOW - timedelta(days=2))
    api_keys.revoke_key(db, revoked["id"], now=NOW)
    html = admin.get("/settings/dashboard-api").get_data(as_text=True)
    assert "[dashboard_api_env_key]" in html           # environment variable row
    assert "Plex dashboard" in html and raw[:6] + "&hellip;" in html
    assert raw not in html and raw2 not in html and api_keys.hash_key(raw) not in html
    assert "[dashboard_api_revoked]" in html
    assert html.count("[dashboard_api_revoke]") == 1   # only the active key has a revoke button
    assert "[enabled]" in html and "[dashboard_api_disable]" in html


def test_page_without_env_variable_has_no_env_row(admin, monkeypatch):
    monkeypatch.delenv("VODUM_DASHBOARD_API_KEY")
    html = admin.get("/settings/dashboard-api").get_data(as_text=True)
    assert "[dashboard_api_env_key]" not in html and "[dashboard_api_no_keys]" in html


def test_create_shows_the_key_exactly_once(env, admin, client):
    response = admin.post("/settings/dashboard-api/keys", data={"name": "New dash", "_csrf_token": "csrf-test"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "[dashboard_api_key_created_title]" in html
    rows = raw_rows(env)
    assert len(rows) == 1 and rows[0]["name"] == "New dash"
    start = html.index('id="dashboard-api-new-key"')
    shown = html[start:].split(">", 1)[1].split("<", 1)[0].strip()
    assert api_keys.hash_key(shown) == rows[0]["key_hash"]
    assert get(client, "/health", key=shown).status_code == 200
    # A later page view only has the prefix.
    again = admin.get("/settings/dashboard-api").get_data(as_text=True)
    assert shown not in again and shown[:6] + "&hellip;" in again


def test_create_without_name_is_rejected(env, admin):
    response = admin.post("/settings/dashboard-api/keys", data={"name": "  ", "_csrf_token": "csrf-test"})
    assert response.status_code == 302 and raw_rows(env) == []


def test_create_without_csrf_token_is_403(env, admin):
    assert admin.post("/settings/dashboard-api/keys", data={"name": "x"}).status_code == 403
    assert raw_rows(env) == []


def test_revoke_from_page(env, admin, client):
    row, raw = api_keys.create_key(manager(env), "dash", now=NOW)
    response = admin.post(f"/settings/dashboard-api/keys/{row['id']}/revoke", data={"_csrf_token": "csrf-test"})
    assert response.status_code == 302
    assert raw_rows(env)[0]["revoked_at"] is not None
    assert get(client, "/health", key=raw).status_code == 404
    assert admin.post("/settings/dashboard-api/keys/9999/revoke", data={"_csrf_token": "csrf-test"}).status_code == 302


def test_toggle_from_page(env, admin, client):
    assert admin.post("/settings/dashboard-api/toggle", data={"enabled": "0", "_csrf_token": "csrf-test"}).status_code == 302
    assert get(client, "/health").status_code == 404
    html = admin.get("/settings/dashboard-api").get_data(as_text=True)
    assert "[dashboard_api_disabled_notice]" in html and "[dashboard_api_enable]" in html
    assert admin.post("/settings/dashboard-api/toggle", data={"enabled": "1", "_csrf_token": "csrf-test"}).status_code == 302
    assert get(client, "/health").status_code == 200


def test_page_names_endpoints_and_header(admin):
    html = admin.get("/settings/dashboard-api").get_data(as_text=True)
    assert "X-Api-Key" in html
    assert "GET /api/dashboard/v1/health" in html and "POST /api/dashboard/v1/users/&lt;id&gt;/gift" in html
