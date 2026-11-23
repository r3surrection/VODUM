"""Admin page "Dashboard API": on/off switch and API keys (fork addition).

Keys are shown in clear text exactly once, in the response of the creating
POST; afterwards only their first characters are displayed. Nothing here
writes a key to the logs.
"""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from api.dashboard import API_PREFIX
from core import dashboard_api_keys as api_keys
from core.auth_principal import admin_required
from web.helpers import add_log, get_db

ENDPOINTS = ("health", "users", "users/<id>", "expiring", "requests", "servers", "tasks", "stats")
WRITE_ENDPOINTS = ("users/<id>/gift",)


def _page_context(db, **extra) -> dict:
    return {
        "active_page": "dashboard_api",
        "api_enabled": api_keys.api_enabled(db),
        "env_key_configured": api_keys.env_key_configured(),
        "keys": api_keys.list_keys(db),
        "api_prefix": API_PREFIX,
        "endpoints": ENDPOINTS,
        "write_endpoints": WRITE_ENDPOINTS,
        "key_prefix_length": api_keys.PREFIX_LENGTH,
        "name_max_length": api_keys.NAME_MAX_LENGTH,
        "new_key": None,
        "new_key_name": None,
        **extra,
    }


def register(app):
    @app.get("/settings/dashboard-api")
    @admin_required
    def dashboard_api_settings_page():
        return render_template("settings/dashboard_api.html", **_page_context(get_db()))

    @app.post("/settings/dashboard-api/toggle")
    @admin_required
    def dashboard_api_settings_toggle():
        db = get_db()
        enabled = str(request.form.get("enabled") or "").strip() == "1"
        api_keys.set_api_enabled(db, enabled)
        add_log("info", "settings", "Dashboard API " + ("enabled" if enabled else "disabled"))
        flash("dashboard_api_enabled_saved" if enabled else "dashboard_api_disabled_saved", "success")
        return redirect(url_for("dashboard_api_settings_page"))

    @app.post("/settings/dashboard-api/keys")
    @admin_required
    def dashboard_api_key_create():
        db = get_db()
        name = api_keys.normalize_name(request.form.get("name"))
        if not name:
            flash("dashboard_api_key_name_required", "error")
            return redirect(url_for("dashboard_api_settings_page"))
        row, raw_key = api_keys.create_key(db, name)
        add_log("info", "settings", f"Dashboard API key created: #{row['id']} {row['name']}")
        # The clear-text key is rendered directly (no redirect), so it never
        # passes through the session cookie and is shown exactly once.
        return render_template(
            "settings/dashboard_api.html",
            **_page_context(db, new_key=raw_key, new_key_name=row["name"]),
        )

    @app.post("/settings/dashboard-api/keys/<int:key_id>/revoke")
    @admin_required
    def dashboard_api_key_revoke(key_id: int):
        db = get_db()
        if api_keys.revoke_key(db, key_id):
            add_log("info", "settings", f"Dashboard API key revoked: #{key_id}")
            flash("dashboard_api_key_revoked", "success")
        else:
            flash("dashboard_api_key_not_found", "error")
        return redirect(url_for("dashboard_api_settings_page"))
