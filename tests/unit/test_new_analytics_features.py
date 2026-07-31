"""Unit tests for knowledge-gap analytics and End_User feedback.

Covers the AnalyticsService additions (``knowledge_gaps``,
``record_feedback``, ``feedback_summary``), the ``feedback`` column
migration for pre-existing databases, and the two new analytics endpoints
(``GET /analytics/gaps``, ``GET /analytics/feedback``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analytics.service import AnalyticsService
from app.api import admin
from app.config import load_settings
from app.core.models import QueryLogEntry
from app.db import bootstrap, create_schema


@pytest.fixture()
def conn(tmp_path):
    """A bootstrapped temp DB connection usable across TestClient threads."""
    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "CREDENTIAL_MASTER_KEY": "unused"}
    )
    bootstrap(settings).close()
    connection = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


def _log(service: AnalyticsService, text: str, tool: str = "telegram") -> int:
    """Insert one Success query-log row and return its id."""
    return service.log_query(
        QueryLogEntry(
            ts=datetime.now(),
            query_text=text,
            detected_space_id=1,
            confidence=90.0,
            response_status="Success",
            tool=tool,
        )
    )


def _ground(conn: sqlite3.Connection, query_log_id: int) -> None:
    """Mark a query as grounded by writing one document_access row."""
    conn.execute(
        "INSERT INTO document_access (query_log_id, document_id, ts) "
        "VALUES (?, 1, datetime('now'))",
        (query_log_id,),
    )
    conn.commit()


def test_knowledge_gaps_groups_unanswered_queries(conn):
    """Gaps are Success queries without access rows, grouped and ranked."""
    service = AnalyticsService(conn)
    # Asked twice, never answered → the top gap.
    _log(service, "报销流程是什么？")
    _log(service, "报销流程是什么？")
    # Answered (grounded) → not a gap.
    _ground(conn, _log(service, "年假有几天？"))
    # Integration-test traffic → excluded.
    _log(service, "Integration test query", tool="telegram-test")

    gaps = service.knowledge_gaps()

    assert [g["query_text"] for g in gaps] == ["报销流程是什么？"]
    assert gaps[0]["count"] == 2
    assert gaps[0]["space_name"]


def test_feedback_record_and_summary(conn):
    """Verdicts persist, invalid input is rejected, summary aggregates."""
    service = AnalyticsService(conn)
    qid_up = _log(service, "q1")
    qid_down = _log(service, "q2")

    assert service.record_feedback(qid_up, "up") is True
    assert service.record_feedback(qid_down, "down") is True
    assert service.record_feedback(qid_up, "sideways") is False
    assert service.record_feedback(999999, "up") is False

    summary = service.feedback_summary()
    assert summary == {"up": 1, "down": 1, "satisfaction_pct": 50.0}


def test_feedback_summary_empty_is_na(conn):
    """No feedback recorded → counts zero and a null satisfaction rate."""
    assert AnalyticsService(conn).feedback_summary() == {
        "up": 0,
        "down": 0,
        "satisfaction_pct": None,
    }


def test_feedback_column_migration(tmp_path):
    """A pre-feature DB (no feedback column) is migrated on create_schema."""
    db = tmp_path / "old.db"
    old = sqlite3.connect(str(db))
    old.execute(
        "CREATE TABLE query_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
        "query_text TEXT NOT NULL, detected_space_id INTEGER NOT NULL, "
        "confidence REAL NOT NULL, response_status TEXT NOT NULL, "
        "latency_ms INTEGER, tool TEXT NOT NULL, verified_space_id INTEGER)"
    )
    old.commit()

    create_schema(old)

    columns = {row[1] for row in old.execute("PRAGMA table_info(query_log)")}
    assert "feedback" in columns
    old.close()


def test_gaps_and_feedback_endpoints(conn):
    """The new analytics endpoints expose gaps and the feedback summary."""
    service = AnalyticsService(conn)
    _log(service, "unanswered question")
    service.record_feedback(_log(service, "voted"), "up")

    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.get_connection] = lambda: conn
    client = TestClient(app)

    gaps = client.get("/analytics/gaps").json()
    assert any(g["query_text"] == "unanswered question" for g in gaps)

    feedback = client.get("/analytics/feedback").json()
    assert feedback["up"] == 1 and feedback["satisfaction_pct"] == 100.0
