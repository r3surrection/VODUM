"""Schema for the dashboard API keys (fork addition, see api/dashboard.py).

Keys are stored as SHA-256 hashes only; the clear text is shown once at
creation and never persisted. ``settings.dashboard_api_enabled`` is the
on/off switch of the whole API (default on, so an existing
``VODUM_DASHBOARD_API_KEY`` keeps working after the upgrade).
"""
from __future__ import annotations


DASHBOARD_API_KEYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT DEFAULT NULL,
    revoked_at TEXT DEFAULT NULL
)
"""


def ensure_dashboard_api_schema(conn, cursor, *, table_exists, ensure_column) -> None:
    if not table_exists(cursor, "dashboard_api_keys"):
        cursor.execute(DASHBOARD_API_KEYS_SCHEMA)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_api_keys_hash ON dashboard_api_keys(key_hash)"
    )
    ensure_column(cursor, "settings", "dashboard_api_enabled", "INTEGER NOT NULL DEFAULT 1")
    conn.commit()
