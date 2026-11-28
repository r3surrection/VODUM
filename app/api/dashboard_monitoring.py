"""Read-only monitoring endpoints of the dashboard API (fork addition).

Registered on the ``dashboard_api`` blueprint, so every route here shares its
guard (``X-Api-Key``, 404 for a missing/wrong key and on the portal host) and
the ``Cache-Control: no-store`` header.

Nothing here recomputes statistics from scratch on the request path:
* ``usage-risk`` reads the precomputed ``usage_risk`` snapshot only.
* ``live`` reads the current ``media_sessions`` rows (a handful) and the
  stored server resource samples.
* The history views (``overview``, ``activity``, ``heatmap``, ``top``,
  ``locations``, ``events``) run one bounded, read-only query over the history
  window and keep the result in a short in-process cache (``CACHE_TTL``).
  ``computed_at`` of each block is the time that result was built.

No IP address, mail address, token or server URL is ever part of a response.
"""
from __future__ import annotations

import bisect
import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from flask import Response, abort, jsonify, request

from api.dashboard import (
    _MAIL_RE, _bad_request, _display_name, _int_arg, _iso, _json, _now, _scrub, dashboard_api,
)
from db_manager import isolated_read_operation
from web.helpers import get_db

RANGES = {"7d": 7, "30d": 30, "180d": 180, "365d": 365, "all": None}
DEFAULT_RANGE = "7d"
WEEK_FROM_DAYS = 180          # day buckets up to 30d, ISO-week buckets (KW) from 6 months
LIVE_WINDOW_SECONDS = 300
CACHE_TTL = 300
MAX_ROWS = 500_000
MAX_GEO_IPS = 300
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
RISK_LEVELS = {"low": "niedrig", "medium": "mittel", "high": "hoch"}
TOP_KINDS = ("users", "series", "movies")

_IP_RE = re.compile(
    r"(?:\b\d{1,3}(?:\.\d{1,3}){3}\b)"
    r"|(?:\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b)"
    r"|(?:\b(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{0,4}\b)")
_cache: dict = {}
_cache_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _cached(key, builder):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    value = builder()
    value["computed_at"] = _iso(_now())
    with _cache_lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (now, value)
    return value


def _safe_text(value, limit=200):
    """Text for output: no mail addresses, no IP addresses."""
    if value is None:
        return None
    text = _IP_RE.sub("[ip]", _MAIL_RE.sub("[mail]", str(value)))
    return " ".join(text.split())[:limit] or None


def _person(name, fallback):
    name = str(name or "").strip()
    if "@" in name:                      # Plex user names can be mail addresses
        name = name.split("@", 1)[0]
    return _safe_text(name, 80) or fallback


def _range_arg():
    value = (request.args.get("range") or DEFAULT_RANGE).strip().lower()
    if value not in RANGES:
        abort(_bad_request("range must be one of: " + ", ".join(RANGES)))
    return value


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _last_sunday(year, month):
    day = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _berlin(utc_dt):
    """UTC datetime -> naive Europe/Berlin wall time (EU DST rules, no tz database needed)."""
    year = utc_dt.year
    start = datetime(year, 3, _last_sunday(year, 3).day, 1, tzinfo=timezone.utc)
    end = datetime(year, 10, _last_sunday(year, 10).day, 1, tzinfo=timezone.utc)
    return (utc_dt + timedelta(hours=2 if start <= utc_dt < end else 1)).replace(tzinfo=None)


def _berlin_midnight_utc(day):
    """UTC instant of local midnight of ``day`` in Europe/Berlin."""
    guess = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=1)
    for offset in (1, 2):
        candidate = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=offset)
        if _berlin(candidate) == datetime(day.year, day.month, day.day):
            return candidate
    return guess


def _sql_ts(utc_dt):
    return utc_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _media_kind(media_type, series):
    media_type = str(media_type or "").strip().lower()
    if media_type == "track":
        return "music"
    if str(series or "").strip():
        return "series"
    if media_type in ("movie", "film", "video"):
        return "movie"
    return "other"


def _window(range_key, today, first_day):
    """(first local day, last local day, previous first, previous last) of a range."""
    days = RANGES[range_key]
    if days is None:
        return (first_day or today), today, None, None
    start = today - timedelta(days=days - 1)
    return start, today, start - timedelta(days=days), start - timedelta(days=1)


def _buckets(range_key, start, end):
    """Ordered bucket list [{key,label,start,end,partial}] covering start..end (local days)."""
    weekly = (RANGES[range_key] or 10 ** 6) >= WEEK_FROM_DAYS
    today = _berlin(_now()).date()
    out, cursor = [], start
    if weekly:
        cursor = start - timedelta(days=start.weekday())
    while cursor <= end:
        if weekly:
            last = cursor + timedelta(days=6)
            iso = cursor.isocalendar()
            first_partial = RANGES[range_key] is not None and cursor < start
            out.append({"key": f"{iso[0]}-W{iso[1]:02d}", "label": f"KW {iso[1]}", "start": cursor, "end": last,
                        "partial": first_partial or last >= today})
            cursor = last + timedelta(days=1)
        else:
            out.append({"key": cursor.isoformat(), "label": cursor.isoformat(), "start": cursor, "end": cursor,
                        "partial": cursor >= today})
            cursor += timedelta(days=1)
    return out


def _bucket_index(buckets):
    index = {}
    for position, bucket in enumerate(buckets):
        day = bucket["start"]
        while day <= bucket["end"]:
            index[day] = position
            day += timedelta(days=1)
    return index


# ------------------------------------------------------------------ data
@isolated_read_operation
def _load_plays(db, start_utc, end_utc):
    """Distinct plays that started inside [start_utc, end_utc)."""
    rows = db.query(
        """
        SELECT h.id, h.server_id, s.name AS server_name, h.media_user_id, h.media_key, h.media_type,
               h.title, h.grandparent_title, h.started_at, h.stopped_at, h.watch_ms, h.duration_ms, h.ip,
               h.was_transcode,
               COALESCE(CASE WHEN vu.id IS NOT NULL THEN 'u' || vu.id END,
                        CASE WHEN mu.id IS NOT NULL THEN 'a' || mu.id END,
                        'x:' || COALESCE(h.external_user_id, '')) AS user_key,
               COALESCE(NULLIF(TRIM(vu.username), ''), NULLIF(TRIM(mu.username), ''),
                        NULLIF(TRIM(h.external_user_id), '')) AS user_name
        FROM media_session_history h
        JOIN servers s ON s.id = h.server_id
        LEFT JOIN media_users mu ON mu.id = h.media_user_id
        LEFT JOIN vodum_users vu ON vu.id = mu.vodum_user_id
        WHERE datetime(h.started_at) >= datetime(?) AND datetime(h.started_at) < datetime(?)
        ORDER BY h.started_at
        LIMIT ?
        """,
        (_sql_ts(start_utc), _sql_ts(end_utc), MAX_ROWS),
    )
    plays = {}
    for row in rows or []:
        started = _parse(row["started_at"])
        if started is None:
            continue
        stopped = _parse(row["stopped_at"]) or started
        watch = int(row["watch_ms"] or 0)
        duration = int(row["duration_ms"] or 0)
        if duration > 0:
            watch = min(watch, duration)
        key = (row["server_id"], row["media_user_id"], row["media_key"], started.strftime("%Y-%m-%d %H:%M"))
        play = {
            "id": row["id"], "server": row["server_name"], "user_key": row["user_key"],
            "user": _person(row["user_name"], "unbekannt"), "title": row["title"],
            "series": (row["grandparent_title"] or "").strip() or None,
            "kind": _media_kind(row["media_type"], row["grandparent_title"]),
            "started": started, "stopped": max(stopped, started), "watch_ms": watch,
            "ip": row["ip"], "media_key": row["media_key"], "server_id": row["server_id"],
        }
        if key not in plays or play["watch_ms"] > plays[key]["watch_ms"]:
            plays[key] = play
    return list(plays.values())


@isolated_read_operation
def _first_day(db):
    row = db.query_one("SELECT MIN(started_at) AS first FROM media_session_history")
    parsed = _parse(row["first"]) if row else None
    return _berlin(parsed).date() if parsed else None


def _period_plays(range_key):
    """(plays in the range, plays in the previous period or None, window dict)."""
    db = get_db()
    today = _berlin(_now()).date()
    first = _first_day(db) if RANGES[range_key] is None else None
    start, end, prev_start, prev_end = _window(range_key, today, first)
    start_utc = _berlin_midnight_utc(start)
    end_utc = _berlin_midnight_utc(end + timedelta(days=1))
    current = _load_plays(db, start_utc, end_utc)
    previous = None
    if prev_start is not None:
        previous = _load_plays(db, _berlin_midnight_utc(prev_start), start_utc)
    return current, previous, {"start": start, "end": end, "prev_start": prev_start, "prev_end": prev_end}


def _peak_concurrent(plays, buckets_of=None):
    """Highest number of overlapping plays (overall, or per bucket via ``buckets_of``)."""
    events = []
    for play in plays:
        end = max(play["stopped"], play["started"] + timedelta(milliseconds=play["watch_ms"]))
        events.append((play["started"], 1, play))
        events.append((end, -1, play))
    events.sort(key=lambda item: (item[0], item[1]))
    running, peak, per_bucket = 0, 0, {}
    for moment, delta, play in events:
        running += delta
        if delta == 1:
            peak = max(peak, running)
            if buckets_of is not None:
                slot = buckets_of(moment)
                per_bucket[slot] = max(per_bucket.get(slot, 0), running)
    return peak, per_bucket


def _metrics(plays, buckets_of=None, n_buckets=0):
    users = {p["user_key"] for p in plays}
    watch_ms = sum(p["watch_ms"] for p in plays)
    peak, per_bucket = _peak_concurrent(plays, buckets_of)
    series = {name: [0] * n_buckets for name in ("active_users", "sessions", "watch_hours", "avg_session_minutes",
                                                  "peak_concurrent")}
    if buckets_of is not None:
        grouped = {}
        for play in plays:
            grouped.setdefault(buckets_of(play["started"]), []).append(play)
        for slot, items in grouped.items():
            ms = sum(p["watch_ms"] for p in items)
            series["active_users"][slot] = len({p["user_key"] for p in items})
            series["sessions"][slot] = len(items)
            series["watch_hours"][slot] = round(ms / 3_600_000, 1)
            series["avg_session_minutes"][slot] = round(ms / len(items) / 60_000, 1)
            series["peak_concurrent"][slot] = per_bucket.get(slot, 0)
    return {
        "active_users": len(users), "sessions": len(plays), "watch_hours": round(watch_ms / 3_600_000, 1),
        "avg_session_minutes": round(watch_ms / len(plays) / 60_000, 1) if plays else 0.0,
        "peak_concurrent": peak,
    }, series


def _change_pct(current, previous):
    if previous is None or not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _share(value, leader):
    return round(value / leader * 100) if leader else 0


# ------------------------------------------------------------------ overview
@dashboard_api.get("/monitoring/overview")
def monitoring_overview():
    range_key = _range_arg()

    def build():
        current, previous, win = _period_plays(range_key)
        buckets = _buckets(range_key, win["start"], win["end"])
        index = _bucket_index(buckets)
        to_slot = lambda moment: index.get(_berlin(moment).date(), len(buckets) - 1)  # noqa: E731
        now_metrics, spark = _metrics(current, to_slot, len(buckets))
        prev_metrics = _metrics(previous)[0] if previous is not None else None
        metrics = {}
        for name, value in now_metrics.items():
            before = prev_metrics[name] if prev_metrics else None
            metrics[name] = {"value": value, "previous": before, "change_pct": _change_pct(value, before),
                             "sparkline": spark[name]}
        return {
            "range": range_key, "granularity": "week" if buckets and buckets[0]["key"][4:6] == "-W" else "day",
            "partial": any(b["partial"] for b in buckets),
            "period": {"from": win["start"].isoformat(), "to": win["end"].isoformat(),
                       "previous_from": win["prev_start"].isoformat() if win["prev_start"] else None,
                       "previous_to": win["prev_end"].isoformat() if win["prev_end"] else None},
            "sparkline_labels": [b["label"] for b in buckets],
            "metrics": metrics,
        }

    return _json(_cached(("overview", range_key), build))


# ------------------------------------------------------------------ activity
@dashboard_api.get("/monitoring/activity")
def monitoring_activity():
    range_key = _range_arg()

    def build():
        current, _previous, win = _period_plays(range_key)
        buckets = _buckets(range_key, win["start"], win["end"])
        index = _bucket_index(buckets)
        rows = [{"key": b["key"], "label": b["label"], "start": b["start"].isoformat(),
                 "partial": b["partial"], "sessions": 0, "watch_hours": 0.0, "by_server": {}, "by_media_type": {}}
                for b in buckets]
        for play in current:
            row = rows[index.get(_berlin(play["started"]).date(), len(rows) - 1)]
            hours = play["watch_ms"] / 3_600_000
            row["sessions"] += 1
            row["watch_hours"] += hours
            for group, name in (("by_server", play["server"]), ("by_media_type", play["kind"])):
                cell = row[group].setdefault(name, {"sessions": 0, "watch_hours": 0.0})
                cell["sessions"] += 1
                cell["watch_hours"] += hours
        for row in rows:
            row["watch_hours"] = round(row["watch_hours"], 2)
            for group in ("by_server", "by_media_type"):
                row[group] = [{"name": name, "sessions": cell["sessions"], "watch_hours": round(cell["watch_hours"], 2)}
                              for name, cell in sorted(row[group].items())]
        return {"range": range_key, "granularity": "week" if rows and "-W" in rows[0]["key"] else "day",
                "partial": any(r["partial"] for r in rows), "buckets": rows,
                "servers": sorted({p["server"] for p in current}),
                "media_types": sorted({p["kind"] for p in current})}

    return _json(_cached(("activity", range_key), build))


# ------------------------------------------------------------------ heatmap
@dashboard_api.get("/monitoring/heatmap")
def monitoring_heatmap():
    range_key = _range_arg()

    def build():
        current, _previous, _win = _period_plays(range_key)
        grid = [[0] * 24 for _ in range(7)]
        for play in current:
            local = _berlin(play["started"])
            grid[local.weekday()][local.hour] += 1
        nonzero = sorted(v for row in grid for v in row if v)
        cells, best = [], None
        for weekday in range(7):
            for hour in range(24):
                count = grid[weekday][hour]
                if count:
                    # quantile level 1..4 among the non-zero cells
                    level = 1 + min(3, bisect.bisect_left(nonzero, count) * 4 // len(nonzero))
                else:
                    level = 0
                cells.append({"weekday": weekday, "weekday_label": WEEKDAYS[weekday], "hour": hour,
                              "count": count, "level": level})
                if count and (best is None or count > best["count"]):
                    best = {"weekday": weekday, "hour": hour, "count": count}
        return {"range": range_key, "timezone": "Europe/Berlin", "weekdays": list(WEEKDAYS), "cells": cells,
                "max_cell": best,
                "peak_label": f"Meist: {WEEKDAYS[best['weekday']]} {best['hour']} Uhr" if best else None}

    return _json(_cached(("heatmap", range_key), build))


# ------------------------------------------------------------------ top
@dashboard_api.get("/monitoring/top")
def monitoring_top():
    range_key = _range_arg()
    kind = (request.args.get("kind") or "users").strip().lower()
    if kind not in TOP_KINDS:
        return _bad_request("kind must be one of: " + ", ".join(TOP_KINDS))
    limit = _int_arg("limit", 10, 1, 50)

    def build():
        current, _previous, _win = _period_plays(range_key)
        groups = {}
        for play in current:
            if kind == "users":
                name, key = play["user"], play["user_key"]
            elif kind == "series":
                if play["kind"] != "series":
                    continue
                name = key = play["series"]
            else:
                if play["kind"] != "movie":
                    continue
                name = key = play["title"] or "?"
            group = groups.setdefault(key, {"name": _safe_text(name) or "?", "plays": 0, "ms": 0, "users": set(),
                                            "image": None, "last": None})
            group["plays"] += 1
            group["ms"] += play["watch_ms"]
            group["users"].add(play["user_key"])
            if kind != "users" and (group["last"] is None or play["started"] > group["last"]):
                group["last"], group["image"] = play["started"], f"h{play['id']}"
        ranked = sorted(groups.values(), key=lambda g: (-g["ms"], -g["plays"], g["name"]))[:limit]
        leader = ranked[0]["ms"] if ranked else 0
        items = [{"rank": position, "name": g["name"], "plays": g["plays"], "hours": round(g["ms"] / 3_600_000, 1),
                  "users": len(g["users"]), "share_pct": _share(g["ms"], leader), "image_id": g["image"]}
                 for position, g in enumerate(ranked, 1)]
        return {"range": range_key, "kind": kind, "limit": limit, "items": items}

    return _json(_cached(("top", range_key, kind, limit), build))


# ------------------------------------------------------------------ geo
def _geo_for(ips):
    from core.monitoring.ip_location import load_ip_coordinates, parse_stream_ip
    clean = {}
    for ip in ips:
        parsed = parse_stream_ip(ip) if ip else None
        if parsed is not None:
            clean[str(parsed)] = None
    return load_ip_coordinates(list(clean)) if clean else {}


def _city(geo):
    """(city, country code, lat, lon) of a geo tuple or None; coordinates rounded to 2 decimals."""
    if not geo:
        return None
    (latitude, longitude), _label, city, _region, code = geo
    if not city or not code:
        return None
    return str(city), str(code), round(float(latitude), 2), round(float(longitude), 2)


@isolated_read_operation
def _live_rows(db, cutoff):
    return [dict(r) for r in db.query(
        """
        SELECT ms.id, ms.server_id, s.name AS server_name, ms.provider, ms.media_type, ms.title,
               ms.grandparent_title, ms.state, ms.progress_ms, ms.duration_ms, ms.is_transcode,
               ms.client_name, ms.client_product, ms.device, ms.ip, ms.raw_json, ms.media_user_id,
               ms.external_user_id,
               COALESCE(NULLIF(TRIM(vu.username), ''), NULLIF(TRIM(mu.username), ''),
                        NULLIF(TRIM(ms.external_user_id), '')) AS user_name
        FROM media_sessions ms
        JOIN servers s ON s.id = ms.server_id
        LEFT JOIN media_users mu ON mu.id = ms.media_user_id
        LEFT JOIN vodum_users vu ON vu.id = mu.vodum_user_id
        WHERE datetime(ms.last_seen_at) >= datetime(?) AND COALESCE(ms.missing_count, 0) = 0
          AND LOWER(COALESCE(ms.state, '')) <> 'stopped'
        ORDER BY ms.last_seen_at DESC, ms.id DESC
        """, (_sql_ts(cutoff),))]


@isolated_read_operation
def _resources(db, server_ids):
    from core.monitoring.resource_stats import load_server_resource_stats
    return load_server_resource_stats(db, server_ids)


def _raw(value):
    try:
        data = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _season_episode(row, raw):
    if row["provider"] == "plex":
        attrs = raw.get("VideoOrTrack") or {}
        season, episode = attrs.get("parentIndex"), attrs.get("index")
    else:
        item = raw.get("NowPlayingItem") or {}
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
    try:
        season, episode = int(season), int(episode)
    except (TypeError, ValueError):
        return None
    return f"S{season:02d}E{episode:02d}"


def _transcode_reason(row, raw):
    if not row["is_transcode"]:
        return None
    if row["provider"] == "jellyfin":
        reasons = (raw.get("TranscodingInfo") or {}).get("TranscodeReasons")
        if isinstance(reasons, list) and reasons:
            return _safe_text(", ".join(str(r) for r in reasons), 120)
        if isinstance(reasons, str) and reasons:
            return _safe_text(reasons, 120)
    computed = raw.get("Computed") or {}
    parts = [name for name, key in (("video", "videoDecision"), ("audio", "audioDecision"))
             if str(computed.get(key) or "").lower() == "transcode"]
    return " + ".join(parts) + " transcode" if parts else "transcode"


@dashboard_api.get("/monitoring/live")
def monitoring_live():
    now = _now()
    db = get_db()
    rows = _live_rows(db, now - timedelta(seconds=LIVE_WINDOW_SECONDS))
    geo = _geo_for(r["ip"] for r in rows)
    from core.monitoring.ip_location import parse_stream_ip
    sessions, per_server = [], {}
    for row in rows:
        raw = _raw(row["raw_json"])
        parsed = parse_stream_ip(row["ip"]) if row["ip"] else None
        place = _city(geo.get(str(parsed))) if parsed is not None else None
        duration = int(row["duration_ms"] or 0)
        position = int(row["progress_ms"] or 0)
        kind = _media_kind(row["media_type"], row["grandparent_title"])
        series = (row["grandparent_title"] or "").strip() or None
        sessions.append({
            "server": row["server_name"], "user": _person(row["user_name"], "unbekannt"), "media_type": kind,
            "title": _safe_text(series if kind == "series" and series else row["title"]),
            "series": _safe_text(series) if kind == "series" else None,
            "se": _season_episode(row, raw) if kind == "series" else None,
            "episode_title": _safe_text(row["title"]) if kind == "series" else None,
            "state": (row["state"] or "unknown").lower(),
            "play_method": "transcode" if row["is_transcode"] else "direct",
            "transcode_reason": _transcode_reason(row, raw),
            "client": _safe_text(row["client_product"] or row["client_name"], 80),
            "device": _safe_text(row["device"], 80),
            "city": place[0] if place else None, "country": place[1] if place else None,
            "progress_pct": min(100, max(0, round(100 * position / duration))) if duration else None,
            "position_s": position // 1000, "duration_s": duration // 1000,
            "remaining_s": max(0, (duration - position) // 1000) if duration else None,
            "image_id": f"s{row['id']}",
        })
        entry = per_server.setdefault(row["server_id"], {"server": row["server_name"], "live": 0, "direct": 0,
                                                         "transcode": 0})
        entry["live"] += 1
        entry["transcode" if row["is_transcode"] else "direct"] += 1
    resources = _resources(db, list(per_server))
    servers = []
    for server_id, entry in per_server.items():
        stats = resources.get(server_id) or {}
        entry["cpu_pct"] = stats.get("server_cpu_pct")
        entry["ram_pct"] = stats.get("server_ram_pct")
        servers.append(entry)
    servers.sort(key=lambda e: e["server"])
    return _json({
        "computed_at": _iso(now),
        "totals": {"live": len(sessions), "direct": sum(1 for s in sessions if s["play_method"] == "direct"),
                   "transcode": sum(1 for s in sessions if s["play_method"] == "transcode")},
        "servers": servers, "sessions": sessions,
    })


# ------------------------------------------------------------------ locations
@dashboard_api.get("/monitoring/locations")
def monitoring_locations():
    range_key = _range_arg()
    now = _now()
    live_ips = {r["ip"] for r in _live_rows(get_db(), now - timedelta(seconds=LIVE_WINDOW_SECONDS)) if r["ip"]}

    def build():
        current, _previous, _win = _period_plays(range_key)
        counts = {}
        for play in current:
            if play["ip"]:
                counts[play["ip"]] = counts.get(play["ip"], 0) + 1
        top_ips = [ip for ip, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_GEO_IPS]]
        geo = _geo_for(top_ips)
        from core.monitoring.ip_location import parse_stream_ip

        def place_of(ip):
            parsed = parse_stream_ip(ip) if ip else None
            return _city(geo.get(str(parsed))) if parsed is not None else None
        cities = {}
        for play in current:
            place = place_of(play["ip"])
            if place is None:
                continue
            cell = cities.setdefault(place[:2], {"city": place[0], "country": place[1], "lat": place[2],
                                                 "lon": place[3], "users": set(), "sessions": 0, "live": False})
            cell["users"].add(play["user_key"])
            cell["sessions"] += 1
        return {"range": range_key, "cities": cities}

    base = _cached(("locations", range_key), build)
    live_places = {p[:2] for p in (_city(g) for g in _geo_for(live_ips).values()) if p}
    items = [{"city": c["city"], "country": c["country"], "lat": c["lat"], "lon": c["lon"],
              "users": len(c["users"]), "sessions": c["sessions"],
              "live": (c["city"], c["country"]) in live_places}
             for c in base["cities"].values()]
    items.sort(key=lambda c: (-c["sessions"], c["city"]))
    return _json({"range": range_key, "computed_at": base["computed_at"], "locations": items})


# ------------------------------------------------------------------ usage risk
@dashboard_api.get("/monitoring/usage-risk")
def monitoring_usage_risk():
    range_key = (request.args.get("range") or "30d").strip().lower()
    if range_key not in RANGES:
        return _bad_request("range must be one of: " + ", ".join(RANGES))
    period = {"7d": 7, "30d": 30, "180d": 180, "365d": 365, "all": 365}[range_key]
    from core.monitoring.precomputed_tabs import load_tab_snapshot
    payload, meta = load_tab_snapshot(get_db(), "usage_risk", {"period_days": period})
    report = (payload or {}).get("usage_risk_report") or {}
    accounts = []
    for row in report.get("rows") or []:
        evidence = row.get("evidence") or {}
        ips = evidence.get("ips") or []
        devices = sum(len(evidence.get(k) or []) for k in ("fixed_devices", "mobile_devices", "browser_devices"))
        networks = {".".join(str(ip).split(".")[:3]) if "." in str(ip) else str(ip).rsplit(":", 2)[0] for ip in ips}
        reasons = row.get("reasons") or ([row["main_reason"]] if row.get("main_reason") else [])
        accounts.append({
            "name": _person(row.get("username"), f"user-{row.get('vodum_user_id')}"),
            "level": RISK_LEVELS.get(str(row.get("risk_level") or "").lower(), "niedrig"),
            "score": row.get("risk_score"),
            "reasons": [t for t in (_safe_text(r, 200) for r in reasons) if t],
            "places": len(networks), "devices": devices, "ips": len(ips),
        })
    accounts.sort(key=lambda a: (-(a["score"] or 0), a["name"]))
    return _json({"range": range_key, "period_days": period, "ready": bool(meta["ready"]),
                  "stale": bool(meta["stale"]), "computed_at": _iso(meta["computed_at"]),
                  "accounts": accounts})


# ------------------------------------------------------------------ events
@isolated_read_operation
def _load_events(db, since, limit_rows):
    return [dict(r) for r in db.query(
        """
        SELECT e.id, e.server_id, s.name AS server_name, e.event_type, e.ts, e.session_key, e.media_key,
               e.title,
               COALESCE(NULLIF(TRIM(vu.username), ''), NULLIF(TRIM(mu.username), ''),
                        NULLIF(TRIM(e.external_user_id), '')) AS user_name
        FROM media_events e
        JOIN servers s ON s.id = e.server_id
        LEFT JOIN media_users mu ON mu.id = e.media_user_id
        LEFT JOIN vodum_users vu ON vu.id = mu.vodum_user_id
        WHERE datetime(e.ts) >= datetime(?) AND e.session_key IS NOT NULL
        ORDER BY e.ts DESC, e.id DESC LIMIT ?
        """, (_sql_ts(since), limit_rows))]


@dashboard_api.get("/monitoring/events")
def monitoring_events():
    limit = _int_arg("limit", 50, 1, 200)

    def build():
        rows = _load_events(get_db(), _now() - timedelta(days=7), 2000)
        sessions = {}
        for row in reversed(rows):          # oldest first
            key = (row["server_id"], row["session_key"], row["media_key"])
            item = sessions.setdefault(key, {
                "server": row["server_name"], "user": _person(row["user_name"], "unbekannt"),
                "title": _safe_text(row["title"]), "start": None, "pause": None, "pause_count": 0, "end": None,
                "last": None, "status": "playing"})
            stamp, kind = _iso(row["ts"]), str(row["event_type"] or "").lower()
            item["last"] = stamp
            if kind == "start" and item["start"] is None:
                item["start"] = stamp
            elif kind == "pause":
                item["pause"], item["status"] = stamp, "paused"
                item["pause_count"] += 1
            elif kind in ("resume", "start", "update", "heartbeat", "state_change"):
                item["status"] = "playing" if item["end"] is None else item["status"]
            elif kind == "stop":
                item["end"], item["status"] = stamp, "ended"
            if item["start"] is None and item["status"] != "ended":
                item["start"] = stamp
        ordered = sorted(sessions.values(), key=lambda s: s["last"] or "", reverse=True)
        return {"events": ordered}

    base = _cached(("events",), build)
    return _json({"computed_at": base["computed_at"], "events": base["events"][:limit]})


# ------------------------------------------------------------------ artwork
@isolated_read_operation
def _artwork_row(db, table, row_id):
    return db.query_one(
        f"""SELECT t.id, t.server_id, t.provider, t.media_key, t.media_type, t.raw_json, t.poster_ref_json,
                   LOWER(TRIM(s.type)) AS type, s.url, s.local_url, s.public_url, s.token, s.settings_json
            FROM {table} t JOIN servers s ON s.id = t.server_id WHERE t.id = ?""", (row_id,))


@dashboard_api.get("/monitoring/artwork/<image_id>")
def monitoring_artwork(image_id):
    match = re.fullmatch(r"([hs])(\d{1,12})", image_id or "")
    if not match:
        abort(404)
    width = _int_arg("w", 160, 32, 1000)
    row = _artwork_row(get_db(), "media_session_history" if match.group(1) == "h" else "media_sessions",
                       int(match.group(2)))
    if not row:
        abort(404)
    from core.monitoring.artwork import _load_ref_json, _is_valid_ref, extract_artwork_refs
    from core.monitoring.artwork_proxy import ArtworkProxyError, fetch_monitoring_artwork
    row = dict(row)
    ref = _load_ref_json(row.get("poster_ref_json"))
    if not _is_valid_ref(ref):
        ref = extract_artwork_refs(row).get("poster_ref") or {}
    if str(ref.get("provider") or "").lower() == "plex" and ref.get("path"):
        query = {"path": str(ref["path"])}
    elif ref.get("item_id"):
        query = {"item_id": str(ref["item_id"]), "image_type": str(ref.get("image_type") or "Primary"),
                 "w": str(width), "q": "80"}
    else:
        abort(404)
    try:
        result = fetch_monitoring_artwork(row, query)
    except ArtworkProxyError as exc:
        abort(exc.status_code)
    if result["kind"] == "content":
        return Response(result["content"], mimetype=result["content_type"])
    from flask import send_file
    return send_file(result["path"], mimetype=result["content_type"], conditional=True)
