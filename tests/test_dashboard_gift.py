"""Tests for POST /api/dashboard/v1/users/<id>/gift (the dashboard API's only write route)."""
import sqlite3

import pytest

from api import dashboard
from test_dashboard_api import (  # noqa: F401  (fixtures and helpers)
    ADMIN_HOST, KEY, PORTAL_HOST, client, env, get,
)


def post(client, user_id, body=None, key=KEY, host=ADMIN_HOST, request_id=None, raw=None):
    headers = {"Host": host}
    if key is not None:
        headers["X-Api-Key"] = key
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    kwargs = {"json": body} if body is not None else {}
    if raw is not None:
        kwargs = {"data": raw, "content_type": "application/json"}
    return client.post(f"/api/dashboard/v1/users/{user_id}/gift", headers=headers, **kwargs)


def row(env, user_id):
    conn = sqlite3.connect(env.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM vodum_users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def count(env, table):
    conn = sqlite3.connect(env.config["DATABASE"])
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_default_is_365_days_and_returns_dates_and_id(env, client):
    response = post(client, 10, {})
    assert response.status_code == 200
    data = response.get_json()
    assert data["old_expiration_date"] == "2026-11-23"
    assert data["new_expiration_date"] == "2027-11-23"
    assert data["days_added"] == 365 and data["user_id"] == 10
    assert row(env, 10)["expiration_date"] == "2027-11-23"
    assert data["gift_id"] == count(env, "subscription_gift_runs")


def test_without_body_uses_default(client):
    assert post(client, 10).get_json()["days_added"] == 365


def test_180_days(client):
    data = post(client, 11, {"days": 180}).get_json()
    assert (data["old_expiration_date"], data["new_expiration_date"]) == ("2026-12-23", "2027-06-21")


def test_matches_the_gifts_page_code_path(env, client):
    """An API gift and a Gifts-page gift (apply_gift_run) leave identical state and history."""
    from api.subscriptions import apply_gift_run
    from db_manager import DBManager

    db = DBManager(env.config["DATABASE"])
    ui = db.query_one("SELECT id, expiration_date FROM vodum_users WHERE id = 15")
    apply_gift_run(db, [ui], "user", None, 365, "manual gift")
    assert post(client, 14, {"days": 365, "note": "thanks"}).status_code == 200
    api, hand = row(env, 14), row(env, 15)
    for column in ("status", "last_status", "expiration_date"):
        assert api[column] == hand[column], column
    assert api["expiration_date"] == "2027-11-26"
    conn = sqlite3.connect(env.config["DATABASE"])
    runs = conn.execute("SELECT target_type, target_server_id, days_added, users_updated, reason "
                        "FROM subscription_gift_runs WHERE id > 2 ORDER BY id").fetchall()
    assert runs == [("user", None, 365, 1, "manual gift"), ("user", None, 365, 1, "Dashboard: thanks")]
    members = conn.execute("SELECT vodum_user_id FROM subscription_gift_run_users WHERE run_id > 2 ORDER BY run_id").fetchall()
    conn.close()
    assert members == [(15,), (14,)]


def test_shows_in_history_with_source_dashboard(client):
    gift_id = post(client, 10, {"days": 365, "note": "birthday"}).get_json()["gift_id"]
    user = get(client, "/users/10").get_json()["user"]
    gift = user["gifts"][0]
    assert gift["run_id"] == gift_id and gift["source"] == "Dashboard"
    assert gift["reason"] == "Dashboard: birthday" and gift["days_added"] == 365
    assert user["last_gift"]["run_id"] == gift_id


@pytest.mark.parametrize("body", [
    {"days": 0}, {"days": 30}, {"days": 182}, {"days": 366}, {"days": 730}, {"days": "365"},
    {"days": True}, {"days": 365.0}, {"days": None}, {"note": 5}, {"note": "x" * 201}, [1],
])
def test_invalid_values_are_400(env, client, body):
    assert post(client, 10, body).status_code == 400
    assert row(env, 10)["expiration_date"] == "2026-11-23"
    assert count(env, "subscription_gift_runs") == 2  # only the fixture's runs


def test_days_error_names_both_allowed_values(client):
    assert "180, 365" in post(client, 10, {"days": 7}).get_json()["error"]


def test_note_limit_boundary(client):
    assert post(client, 10, {"note": "x" * 200}).status_code == 200


@pytest.mark.parametrize("user_id,status", [
    (6, "removed"), (7, "suspended"), (4, "expired"), (5, "invited"), (8, "unknown")])
def test_conflict_for_statuses_without_gift_right(env, client, user_id, status):
    before = row(env, user_id)["expiration_date"]
    response = post(client, user_id, {"days": 365})
    assert response.status_code == 409 and response.get_json()["status"] == status
    assert row(env, user_id)["expiration_date"] == before
    assert count(env, "subscription_gift_runs") == 2


def test_conflict_without_media_account(client):
    assert post(client, 3).status_code == 409


def test_unknown_user_is_404(client):
    assert post(client, 9999).status_code == 404


@pytest.mark.parametrize("user_id", [10, 6, 9999])
def test_key_and_host_cases_are_404_and_change_nothing(env, client, user_id):
    assert post(client, user_id, key=None).status_code == 404
    assert post(client, user_id, key="wrong").status_code == 404
    assert post(client, user_id, key="").status_code == 404
    assert post(client, user_id, host=PORTAL_HOST).status_code == 404
    assert row(env, 10)["expiration_date"] == "2026-11-23"
    assert count(env, "subscription_gift_runs") == 2


def test_disabled_without_env_key(client, monkeypatch):
    monkeypatch.delenv("VODUM_DASHBOARD_API_KEY")
    assert post(client, 10, key="anything").status_code == 404


def test_route_is_post_only_and_other_writes_stay_404(client):
    headers = {"X-Api-Key": KEY, "Host": ADMIN_HOST}
    url = "/api/dashboard/v1/users/10/gift"
    # In the full app the CSRF guard answers 403 to any other state-changing request; never 405 or 2xx.
    assert client.get(url, headers=headers).status_code == 404
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(url, headers=headers).status_code in (403, 404)
    for other in ("/api/dashboard/v1/users/10", "/api/dashboard/v1/users/10/gift/x", "/api/dashboard/v1/health"):
        assert client.post(other, headers=headers).status_code in (403, 404)


def test_idempotent_with_request_id(env, client):
    first = post(client, 10, {"days": 365}, request_id="req-1")
    second = post(client, 10, {"days": 365}, request_id="req-1")
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json()
    assert second.headers["Idempotent-Replay"] == "true" and "Idempotent-Replay" not in first.headers
    assert row(env, 10)["expiration_date"] == "2027-11-23"  # not doubled
    assert count(env, "subscription_gift_runs") == 3


def test_request_ids_are_independent_and_optional(env, client):
    post(client, 10, request_id="a")
    post(client, 10, request_id="b")
    post(client, 11)
    post(client, 11)
    assert count(env, "subscription_gift_runs") == 2 + 4
    assert row(env, 10)["expiration_date"] == "2028-11-22"


def test_request_id_reused_with_other_parameters_is_409(env, client):
    assert post(client, 10, {"days": 365}, request_id="r").status_code == 200
    assert post(client, 10, {"days": 180}, request_id="r").status_code == 409
    assert post(client, 11, {"days": 365}, request_id="r").status_code == 409
    assert count(env, "subscription_gift_runs") == 3


def test_request_id_expires_after_ten_minutes(env, client):
    post(client, 10, request_id="old")
    stamp, fingerprint, payload = dashboard._gift_replays["old"]
    dashboard._gift_replays["old"] = (stamp - 601, fingerprint, payload)
    assert "Idempotent-Replay" not in post(client, 10, request_id="old").headers
    assert row(env, 10)["expiration_date"] == "2028-11-22"


def test_request_id_within_window_still_replays(client):
    post(client, 10, request_id="fresh")
    stamp, fingerprint, payload = dashboard._gift_replays["fresh"]
    dashboard._gift_replays["fresh"] = (stamp - 590, fingerprint, payload)
    assert post(client, 10, request_id="fresh").headers["Idempotent-Replay"] == "true"


def test_failed_request_is_not_replayed(client):
    assert post(client, 6, request_id="x").status_code == 409
    assert "x" not in dashboard._gift_replays


def test_invalid_request_id_is_400(client):
    assert post(client, 10, request_id="x" * 129).status_code == 400


def test_needs_no_csrf_token_but_other_posts_still_do(client):
    assert post(client, 10).status_code == 200  # the fixture runs the CSRF guard before the access guard
    assert client.post("/somewhere-else", headers={"Host": ADMIN_HOST}).status_code == 403
