"""Tests for the monitoring endpoints of the dashboard API (fork addition).

Deterministic invented data only: made-up users, cities and documentation IP ranges.
"""
import json
import re
import sqlite3
from datetime import date

import pytest

import test_dashboard_api as base
from test_dashboard_api import ADMIN_HOST, KEY, PORTAL_HOST, env, get  # noqa: F401
from api import dashboard_monitoring as mon
from core.monitoring import ip_location
from core.monitoring.precomputed_tabs import normalize, view_key

BERLIN = ((52.5234, 13.4112), "Berlin, DE", "Berlin", "Berlin", "DE")
MUNICH = ((48.1371, 11.5754), "Munich, DE", "Munich", "Bavaria", "DE")
GEO = {"203.0.113.5": BERLIN, "198.51.100.7": MUNICH}
MAILISH = "stream.fan@example.invalid"

ENDPOINTS = [
    "/monitoring/overview", "/monitoring/live", "/monitoring/activity", "/monitoring/heatmap",
    "/monitoring/top?kind=users", "/monitoring/top?kind=series", "/monitoring/top?kind=movies",
    "/monitoring/locations", "/monitoring/usage-risk", "/monitoring/events",
]

IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b|::")
MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _seed(path):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO media_users(id,server_id,vodum_user_id,username,type,role) "
                 "VALUES (90,1,NULL,?, 'plex','friend')", (MAILISH,))
    hist = [  # server, media_user, key, type, title, series, started, stopped, duration, watch, ip
        (1, 1, "m1", "movie", "Moon Harbor", None, "2026-11-20 20:00:00", "2026-11-20 22:00:00", 7200000, 7200000, "203.0.113.5"),
        (1, 3, "e1", "episode", "Pilot", "Night Show", "2026-11-20 20:10:00", "2026-11-20 21:10:00", 3600000, 3600000, "198.51.100.7"),
        (2, 2, "e2", "episode", "Second", "Night Show", "2026-11-21 19:00:00", "2026-11-21 19:30:00", 3600000, 1800000, "203.0.113.5"),
        (1, 1, "m2", "movie", "Dune Two", None, "2026-11-22 20:00:00", "2026-11-22 21:00:00", 3600000, 3600000, "203.0.113.5"),
        (1, 90, "m3", "movie", "Moon Harbor", None, "2026-11-23 09:00:00", "2026-11-23 09:30:00", 7200000, 1800000, "2001:db8:1:2:3:4:5:6"),
        (1, 3, "e0", "episode", "Earlier", "Night Show", "2026-11-12 18:00:00", "2026-11-12 19:00:00", 3600000, 3600000, "198.51.100.7"),
    ]
    for server, user, key, mtype, title, series, start, stop, dur, watch, ip in hist:
        conn.execute(
            "INSERT INTO media_session_history(server_id,provider,session_key,media_key,media_user_id,media_type,title,"
            "grandparent_title,started_at,stopped_at,duration_ms,watch_ms,ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (server, "plex", f"k-{key}", key, user, mtype, title, series, start, stop, dur, watch, ip))
    raw_a = {"VideoOrTrack": {"parentIndex": 1, "index": 10},
             "Computed": {"videoDecision": "transcode", "audioDecision": "directplay", "is_transcode": True}}
    conn.execute("UPDATE media_sessions SET media_user_id=1, media_type='episode', title='Pilot', "
                 "grandparent_title='Night Show', state='playing', progress_ms=600000, duration_ms=2400000, "
                 "is_transcode=1, client_product='Plex Web', device='Browser', ip='203.0.113.5', raw_json=? "
                 "WHERE session_key='a'", (json.dumps(raw_a),))
    conn.execute("UPDATE media_sessions SET media_user_id=3, media_type='movie', title='Moon Harbor', state='paused', "
                 "progress_ms=1000, duration_ms=0, is_transcode=0, client_product='App', device='TV', "
                 "ip='198.51.100.7' WHERE session_key='b'")
    events = [("start", "2026-11-23 10:00:00"), ("pause", "2026-11-23 10:30:00"),
              ("resume", "2026-11-23 10:31:00"), ("stop", "2026-11-23 11:00:00")]
    for kind, ts in events:
        conn.execute("INSERT INTO media_events(server_id,provider,event_type,ts,session_key,media_user_id,media_key,"
                     "media_type,title) VALUES (1,'plex',?,?, 'ev1',1,'m9','movie','Moon Harbor')", (kind, ts))
    conn.execute("INSERT INTO media_events(server_id,provider,event_type,ts,session_key,media_user_id,media_key,"
                 "media_type,title) VALUES (1,'plex','start','2026-11-23 11:30:00','ev2',3,'m8','movie','Dune Two')")
    options = normalize("usage_risk")
    payload = {"usage_risk_report": {"enabled": True, "rows": [{
        "username": "alice", "email": base.MAIL, "risk_level": "high", "risk_score": 80,
        "main_reason": "Many places", "reasons": ["Seen from 203.0.113.5 and 198.51.100.7", f"Mail {base.MAIL}"],
        "evidence": {"ips": ["203.0.113.5", "198.51.100.7", "2001:db8:1:2:3:4:5:6"],
                     "fixed_devices": ["tv"], "mobile_devices": ["phone"], "browser_devices": []},
    }, {"username": "bob", "risk_level": "low", "risk_score": 5, "reasons": [], "evidence": {}}]}}
    conn.execute("INSERT OR REPLACE INTO monitoring_precomputed(cache_key,kind,options_json,payload_json,computed_at) "
                 "VALUES (?,?,?,?,?)", (view_key("usage_risk", options), "tab_usage_risk", json.dumps(options),
                                       json.dumps(payload), "2026-11-23 11:55:00"))
    conn.commit()
    conn.close()


@pytest.fixture()
def client(env, monkeypatch):
    _seed(env.config["DATABASE"])
    mon._cache.clear()
    monkeypatch.setattr(ip_location, "load_ip_coordinates",
                        lambda ips: {ip: GEO[ip] for ip in ips if ip in GEO})
    monkeypatch.setattr(mon, "_now", base.dashboard._now)
    return env.test_client()


def data(client, path):
    response = get(client, path)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    return response.get_json()


# ------------------------------------------------------------------ access
@pytest.mark.parametrize("path", ENDPOINTS + ["/monitoring/artwork/h1"])
def test_key_and_host_guard(client, path):
    assert get(client, path, key=None).status_code == 404
    assert get(client, path, key="wrong").status_code == 404
    assert get(client, path, host=PORTAL_HOST).status_code == 404


def test_invalid_arguments_are_400(client):
    assert get(client, "/monitoring/overview?range=2d").status_code == 400
    assert get(client, "/monitoring/top?kind=cats").status_code == 400
    assert get(client, "/monitoring/top?limit=0").status_code == 400
    assert get(client, "/monitoring/events?limit=500").status_code == 400


# ------------------------------------------------------------------ privacy
@pytest.mark.parametrize("path", ENDPOINTS)
def test_no_ip_mail_or_secret_anywhere(client, path):
    text = get(client, path).get_data(as_text=True)
    assert not IPV4.search(text)
    assert not IPV6.search(text)
    assert not MAIL.search(text)
    assert "SECRET-TOKEN" not in text and "example.invalid" not in text


# ------------------------------------------------------------------ overview
def test_overview_metrics_and_previous_period(client):
    body = data(client, "/monitoring/overview?range=7d")
    assert body["computed_at"] and body["partial"] is True and body["granularity"] == "day"
    m = body["metrics"]
    assert m["sessions"] == {"value": 5, "previous": 1, "change_pct": 400.0, "sparkline": m["sessions"]["sparkline"]}
    assert len(m["sessions"]["sparkline"]) == 7 and sum(m["sessions"]["sparkline"]) == 5
    assert m["active_users"]["value"] == 3 and m["active_users"]["previous"] == 1
    assert m["watch_hours"]["value"] == 5.0 and m["watch_hours"]["change_pct"] == 400.0
    assert m["peak_concurrent"]["value"] == 2
    assert set(m) == {"active_users", "sessions", "watch_hours", "avg_session_minutes", "peak_concurrent"}


def test_change_pct_is_null_without_previous_data(client):
    month = data(client, "/monitoring/overview?range=30d")["metrics"]["sessions"]
    assert month["value"] == 6 and month["previous"] == 0 and month["change_pct"] is None
    everything = data(client, "/monitoring/overview?range=all")["metrics"]["sessions"]
    assert everything["previous"] is None and everything["change_pct"] is None


# ------------------------------------------------------------------ activity
def test_activity_days_split_and_partial(client):
    body = data(client, "/monitoring/activity?range=7d")
    days = {b["key"]: b for b in body["buckets"]}
    assert len(days) == 7 and days["2026-11-23"]["partial"] is True and days["2026-11-22"]["partial"] is False
    friday = days["2026-11-20"]
    assert friday["sessions"] == 2 and friday["watch_hours"] == 3.0
    assert {s["name"] for s in friday["by_server"]} == {"Plex One"}
    assert {t["name"] for t in friday["by_media_type"]} == {"movie", "series"}
    assert {s["name"] for s in days["2026-11-21"]["by_server"]} == {"Plex Two"}


def test_activity_uses_week_buckets_with_kw_label_from_six_months(client):
    body = data(client, "/monitoring/activity?range=365d")
    assert body["granularity"] == "week"
    last = body["buckets"][-1]
    assert last["label"] == f"KW {date(2026, 11, 23).isocalendar()[1]}" and last["partial"] is True
    assert body["buckets"][0]["partial"] is False      # 365d starts on a Monday
    half = data(client, "/monitoring/activity?range=180d")
    assert half["granularity"] == "week" and half["buckets"][0]["partial"] is True   # starts mid-week
    assert sum(b["sessions"] for b in body["buckets"]) == 6
    assert data(client, "/monitoring/activity?range=30d")["granularity"] == "day"


# ------------------------------------------------------------------ heatmap
def test_heatmap_levels_and_peak_label(client):
    body = data(client, "/monitoring/heatmap?range=7d")
    assert len(body["cells"]) == 168 and body["timezone"] == "Europe/Berlin"
    assert body["peak_label"] == "Meist: Fr 21 Uhr"
    assert body["max_cell"] == {"weekday": 4, "hour": 21, "count": 2}
    cells = {(c["weekday"], c["hour"]): c for c in body["cells"]}
    assert cells[(4, 21)]["level"] == 4 and cells[(0, 3)]["level"] == 0
    assert {c["level"] for c in body["cells"]} <= {0, 1, 2, 3, 4}
    assert all(c["level"] >= 1 for c in body["cells"] if c["count"])


# ------------------------------------------------------------------ top
def test_top_users_series_movies(client):
    users = data(client, "/monitoring/top?range=7d&kind=users")["items"]
    assert [u["name"] for u in users] == ["alice", "bob", "stream.fan"]
    assert users[0]["rank"] == 1 and users[0]["share_pct"] == 100 and users[0]["plays"] == 3
    assert users[0]["hours"] == 3.5 and 0 < users[2]["share_pct"] < users[1]["share_pct"] < 100
    series = data(client, "/monitoring/top?range=7d&kind=series")["items"]
    assert [(s["name"], s["plays"], s["users"]) for s in series] == [("Night Show", 2, 2)]
    assert series[0]["image_id"].startswith("h")
    movies = data(client, "/monitoring/top?range=7d&kind=movies&limit=1")["items"]
    assert [m["name"] for m in movies] == ["Moon Harbor"] and movies[0]["plays"] == 2 and movies[0]["users"] == 2


# ------------------------------------------------------------------ locations
def test_locations_city_level_only(client):
    body = data(client, "/monitoring/locations?range=7d")
    cities = {c["city"]: c for c in body["locations"]}
    assert set(cities) == {"Berlin", "Munich"}
    berlin = cities["Berlin"]
    assert (berlin["lat"], berlin["lon"], berlin["country"]) == (52.52, 13.41, "DE")
    assert berlin["sessions"] == 3 and berlin["users"] == 1 and berlin["live"] is True
    assert cities["Munich"]["live"] is True and set(berlin) == {
        "city", "country", "lat", "lon", "users", "sessions", "live"}


# ------------------------------------------------------------------ usage risk
def test_usage_risk_levels_reasons_and_counts_only(client):
    body = data(client, "/monitoring/usage-risk")
    assert body["ready"] is True
    first = body["accounts"][0]
    assert (first["name"], first["level"], first["score"]) == ("alice", "hoch", 80)
    assert (first["ips"], first["devices"]) == (3, 2) and isinstance(first["places"], int)
    assert any("[ip]" in r for r in first["reasons"]) and any("[mail]" in r for r in first["reasons"])
    assert body["accounts"][1]["level"] == "niedrig"


def test_usage_risk_not_ready_for_unknown_period(client):
    body = data(client, "/monitoring/usage-risk?range=7d")
    assert body["ready"] is False and body["accounts"] == []


# ------------------------------------------------------------------ live
def test_live_sessions_and_totals(client):
    body = data(client, "/monitoring/live")
    assert body["totals"] == {"live": 2, "direct": 1, "transcode": 1}
    series = next(s for s in body["sessions"] if s["media_type"] == "series")
    assert (series["se"], series["series"], series["episode_title"]) == ("S01E10", "Night Show", "Pilot")
    assert series["play_method"] == "transcode" and series["transcode_reason"] == "video transcode"
    assert (series["progress_pct"], series["position_s"], series["duration_s"], series["remaining_s"]) == (25, 600, 2400, 1800)
    assert (series["city"], series["country"], series["user"]) == ("Berlin", "DE", "alice")
    assert series["image_id"].startswith("s") and series["server"] == "Plex One"
    movie = next(s for s in body["sessions"] if s["media_type"] == "movie")
    assert movie["progress_pct"] is None and movie["transcode_reason"] is None and movie["state"] == "paused"
    server = body["servers"][0]
    assert server["server"] == "Plex One" and server["live"] == 2 and "cpu_pct" in server and "ram_pct" in server


# ------------------------------------------------------------------ events
def test_events_are_summarised_per_session(client):
    body = data(client, "/monitoring/events?limit=50")
    assert len(body["events"]) == 2
    done = next(e for e in body["events"] if e["title"] == "Moon Harbor")
    assert done["start"] == "2026-11-23T10:00:00Z" and done["end"] == "2026-11-23T11:00:00Z"
    assert done["pause"] == "2026-11-23T10:30:00Z" and done["pause_count"] == 1 and done["status"] == "ended"
    assert (done["user"], done["server"]) == ("alice", "Plex One")
    running = next(e for e in body["events"] if e["title"] == "Dune Two")
    assert running["status"] == "playing" and running["end"] is None
    assert body["events"][0]["title"] == "Dune Two"
    assert len(data(client, "/monitoring/events?limit=1")["events"]) == 1


# ------------------------------------------------------------------ artwork
def test_artwork_validates_id_and_proxies(client, monkeypatch):
    from core.monitoring import artwork_proxy
    assert get(client, "/monitoring/artwork/nonsense").status_code == 404
    assert get(client, "/monitoring/artwork/h99999").status_code == 404
    conn = sqlite3.connect(client.application.config["DATABASE"])
    conn.execute("UPDATE media_session_history SET poster_ref_json=? WHERE media_key='m1'",
                 (json.dumps({"provider": "plex", "path": "/library/metadata/1/thumb/2"}),))
    conn.commit()
    row_id = conn.execute("SELECT id FROM media_session_history WHERE media_key='m1'").fetchone()[0]
    conn.close()
    calls = []

    def fake(server, query):
        calls.append(query)
        return {"kind": "content", "content": b"JPEG", "content_type": "image/jpeg"}
    monkeypatch.setattr(artwork_proxy, "fetch_monitoring_artwork", fake)
    response = get(client, f"/monitoring/artwork/h{row_id}?w=160")
    assert response.status_code == 200 and response.data == b"JPEG"
    assert response.headers["Content-Type"] == "image/jpeg" and calls == [{"path": "/library/metadata/1/thumb/2"}]
    assert get(client, f"/monitoring/artwork/h{row_id}?w=5").status_code == 400


def test_berlin_time_helper_handles_dst():
    from datetime import datetime, timezone
    assert mon._berlin(datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)).hour == 21
    assert mon._berlin(datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)).hour == 22
    assert mon._berlin(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)).hour == 1
    assert mon._berlin(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)).hour == 3
