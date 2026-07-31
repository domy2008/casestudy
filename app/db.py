"""SQLite schema bootstrap, connection management, and first-startup seeding.

This module owns the physical data layer definition: it creates every table
and index exactly as specified in the design's "SQLite Schema" section, opens
connections in WAL mode with foreign-key enforcement, and seeds the default
Intent_Spaces and settings on first startup. All operations are idempotent so
repeated startups (Req 12.4, surviving restarts/recreation) are safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Schema: tables and indexes (design "SQLite Schema" section)
# ---------------------------------------------------------------------------

# Each statement uses IF NOT EXISTS so bootstrap is safe to run on every start.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS intent_spaces (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL COLLATE NOCASE UNIQUE,
        description   TEXT NOT NULL DEFAULT '',
        is_general    INTEGER NOT NULL DEFAULT 0,
        is_default    INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS space_keywords (
        id            INTEGER PRIMARY KEY,
        space_id      INTEGER NOT NULL REFERENCES intent_spaces(id) ON DELETE CASCADE,
        keyword       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        format        TEXT NOT NULL,
        size_bytes    INTEGER NOT NULL,
        status        TEXT NOT NULL DEFAULT 'Pending',
        space_id      INTEGER NOT NULL REFERENCES intent_spaces(id),
        file_path     TEXT NOT NULL,
        error_message TEXT,
        uploaded_at   TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id            INTEGER PRIMARY KEY,
        document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        seq           INTEGER NOT NULL,
        text          TEXT NOT NULL,
        embedding     BLOB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS query_log (
        id                INTEGER PRIMARY KEY,
        ts                TEXT NOT NULL,
        query_text        TEXT NOT NULL,
        detected_space_id INTEGER NOT NULL REFERENCES intent_spaces(id),
        confidence        REAL NOT NULL,
        response_status   TEXT NOT NULL,
        latency_ms        INTEGER,
        tool              TEXT NOT NULL,
        verified_space_id INTEGER REFERENCES intent_spaces(id),
        feedback          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_access (
        id            INTEGER PRIMARY KEY,
        query_log_id  INTEGER NOT NULL REFERENCES query_log(id),
        document_id   INTEGER NOT NULL,
        ts            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integration_error_log (
        id            INTEGER PRIMARY KEY,
        ts            TEXT NOT NULL,
        tool          TEXT NOT NULL,
        operation     TEXT NOT NULL,
        error_detail  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integrations (
        tool          TEXT PRIMARY KEY,
        status        TEXT NOT NULL DEFAULT 'Disconnected',
        active        INTEGER NOT NULL DEFAULT 0,
        last_check_ts TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key           TEXT PRIMARY KEY,
        value         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc     ON chunks(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_qlog_ts        ON query_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_qlog_space     ON query_log(detected_space_id)",
    "CREATE INDEX IF NOT EXISTS idx_access_doc     ON document_access(document_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_errlog_tool_ts ON integration_error_log(tool, ts)",
)

# ---------------------------------------------------------------------------
# Seed data (design "Seed data at first startup")
# ---------------------------------------------------------------------------

# (name, is_general, is_default) for the four Intent_Spaces present on init.
SEED_SPACES: tuple[tuple[str, int, int], ...] = (
    ("General", 1, 0),
    ("HR", 0, 1),
    ("Legal", 0, 1),
    ("Finance", 0, 1),
)

# Default confidence threshold (Req 7.4) plus the CloudWatch alarm thresholds
# (design: latency p95 > 3000 ms, error rate > 5% over 5 min — Req 13.3–13.5).
SEED_SETTINGS: tuple[tuple[str, str], ...] = (
    ("confidence_threshold", "70"),
    ("latency_alarm_ms", "3000"),
    ("error_rate_alarm_pct", "5"),
)


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    """Open a SQLite connection in WAL mode with foreign keys enforced.

    Args:
        settings: Optional settings snapshot. Defaults to the process-wide
            settings, which points at the persistent ``/data`` volume.

    Returns:
        An open :class:`sqlite3.Connection` with ``Row`` row factory, WAL
        journaling, and ``PRAGMA foreign_keys = ON``.
    """
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index if they do not already exist.

    Args:
        conn: An open SQLite connection.
    """
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    _apply_migrations(conn)
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema, idempotently.

    ``CREATE TABLE IF NOT EXISTS`` does not alter tables that already exist,
    so columns added after first release are applied here via guarded
    ``ALTER TABLE`` statements.

    Args:
        conn: An open SQLite connection with the base schema created.
    """
    # query_log.feedback: End_User 👍/👎 verdicts ('up'/'down'/NULL).
    # Index-based access so this works with or without a Row factory.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(query_log)")
    }
    if "feedback" not in columns:
        conn.execute("ALTER TABLE query_log ADD COLUMN feedback TEXT")


def seed_defaults(conn: sqlite3.Connection) -> None:
    """Insert the default Intent_Spaces and settings, idempotently.

    Uses ``INSERT OR IGNORE`` keyed on the case-insensitive unique space name
    and the settings primary key, so running this repeatedly never duplicates
    rows or overwrites an Admin-modified value.

    Args:
        conn: An open SQLite connection with the schema already created.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO intent_spaces (name, is_general, is_default, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        SEED_SPACES,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        SEED_SETTINGS,
    )
    conn.commit()


def bootstrap(settings: Settings | None = None) -> sqlite3.Connection:
    """Ensure directories, schema, and seed data exist; return a connection.

    This is the single entry point called at application startup. It is fully
    idempotent: on a fresh volume it builds everything; on subsequent starts it
    is a no-op that simply hands back a ready connection (Req 12.4).

    Args:
        settings: Optional settings snapshot. Defaults to process settings.

    Returns:
        An open, ready-to-use :class:`sqlite3.Connection`.
    """
    settings = settings or get_settings()
    settings.ensure_directories()

    conn = connect(settings)
    create_schema(conn)
    seed_defaults(conn)
    return conn
