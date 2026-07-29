"""Unit tests for the SQLite bootstrap: schema, seeding, and idempotency.

These tests point DATA_DIR at a pytest tmp_path so nothing touches the real
``/data`` volume, verifying the data directory is configurable (Req 12.4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app import db
from app.config import load_settings

EXPECTED_TABLES = {
    "intent_spaces",
    "space_keywords",
    "documents",
    "chunks",
    "query_log",
    "document_access",
    "integration_error_log",
    "integrations",
    "settings",
}

EXPECTED_INDEXES = {
    "idx_chunks_doc",
    "idx_qlog_ts",
    "idx_qlog_space",
    "idx_access_doc",
    "idx_errlog_tool_ts",
}


def _settings_for(tmp_path: Path):
    """Build a Settings snapshot rooted at an isolated temp data directory."""
    return load_settings({"DATA_DIR": str(tmp_path / "data")})


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ).fetchall()
    return {r["name"] for r in rows}


def test_bootstrap_creates_all_tables_and_indexes(tmp_path: Path) -> None:
    """Every designed table and index exists after bootstrap."""
    settings = _settings_for(tmp_path)
    conn = db.bootstrap(settings)
    try:
        assert EXPECTED_TABLES <= _names(conn, "table")
        assert EXPECTED_INDEXES <= _names(conn, "index")
        assert settings.db_path.exists()
    finally:
        conn.close()


def test_bootstrap_uses_wal_and_foreign_keys(tmp_path: Path) -> None:
    """Connections are opened in WAL mode with foreign-key enforcement."""
    settings = _settings_for(tmp_path)
    conn = db.bootstrap(settings)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_bootstrap_seeds_spaces_and_settings(tmp_path: Path) -> None:
    """Default Intent_Spaces and settings are seeded with correct flags."""
    settings = _settings_for(tmp_path)
    conn = db.bootstrap(settings)
    try:
        spaces = {
            row["name"]: (row["is_general"], row["is_default"])
            for row in conn.execute(
                "SELECT name, is_general, is_default FROM intent_spaces"
            )
        }
        assert spaces == {
            "General": (1, 0),
            "HR": (0, 1),
            "Legal": (0, 1),
            "Finance": (0, 1),
        }

        values = dict(conn.execute("SELECT key, value FROM settings"))
        assert values["confidence_threshold"] == "70"
        assert values["latency_alarm_ms"] == "3000"
        assert values["error_rate_alarm_pct"] == "5"
    finally:
        conn.close()


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    """A second bootstrap on the same volume adds no duplicate rows."""
    settings = _settings_for(tmp_path)

    conn1 = db.bootstrap(settings)
    conn1.close()

    conn2 = db.bootstrap(settings)
    try:
        space_count = conn2.execute(
            "SELECT COUNT(*) FROM intent_spaces"
        ).fetchone()[0]
        settings_count = conn2.execute(
            "SELECT COUNT(*) FROM settings"
        ).fetchone()[0]
        assert space_count == 4
        assert settings_count == 3
    finally:
        conn2.close()


def test_bootstrap_preserves_admin_modified_settings(tmp_path: Path) -> None:
    """Re-seeding never overwrites an Admin-changed setting value."""
    settings = _settings_for(tmp_path)

    conn1 = db.bootstrap(settings)
    conn1.execute(
        "UPDATE settings SET value = '85' WHERE key = 'confidence_threshold'"
    )
    conn1.commit()
    conn1.close()

    conn2 = db.bootstrap(settings)
    try:
        value = conn2.execute(
            "SELECT value FROM settings WHERE key = 'confidence_threshold'"
        ).fetchone()[0]
        assert value == "85"
    finally:
        conn2.close()
