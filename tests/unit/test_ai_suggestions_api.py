"""Unit tests for the AI suggestion endpoints.

Covers ``POST /spaces/{id}/suggest-keywords`` (keyword suggestions from
misrouted queries) and ``POST /documents/suggest-space`` (Intent_Space
suggestion for an about-to-be-uploaded document), with the AI client faked on
``app.state`` so no network is touched.
"""

from __future__ import annotations

import base64
import sqlite3
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analytics.service import AnalyticsService
from app.api import admin
from app.config import load_settings
from app.core.models import QueryLogEntry
from app.db import bootstrap


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


class FakeAI:
    """AI client fake returning canned JSON (or raising)."""

    def __init__(self, reply: str = "{}", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list = []

    async def chat_completion(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.reply

    async def classify(self, messages, **kwargs) -> str:
        return await self.chat_completion(messages, **kwargs)


def _client(conn, ai) -> TestClient:
    """A TestClient over the admin router with the fake AI on app.state."""
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.get_connection] = lambda: conn
    app.state.dashscope_client = ai
    return TestClient(app)


def _misroute(conn, text: str, detected: int, verified: int) -> None:
    """Log a query detected into one space but verified into another."""
    service = AnalyticsService(conn)
    qid = service.log_query(
        QueryLogEntry(
            ts=datetime.now(),
            query_text=text,
            detected_space_id=detected,
            confidence=90.0,
            response_status="Success",
            tool="telegram",
        )
    )
    service.verify_query(qid, verified)


def test_suggest_keywords_from_misrouted_queries(conn):
    """Misrouted queries feed the prompt; new keywords are filtered/capped."""
    hr = conn.execute(
        "SELECT id FROM intent_spaces WHERE name = 'HR'"
    ).fetchone()["id"]
    general = conn.execute(
        "SELECT id FROM intent_spaces WHERE is_general = 1"
    ).fetchone()["id"]
    _misroute(conn, "工资什么时候发？", detected=general, verified=hr)

    ai = FakeAI(reply='{"keywords": ["工资", "  发薪日 ", "", 42, "工资"]}')
    body = _client(conn, ai).post(f"/spaces/{hr}/suggest-keywords").json()

    # De-duplicated, trimmed, non-strings dropped.
    assert body["keywords"] == ["工资", "发薪日"]
    assert body["based_on"] == 1
    # The misrouted query text was fed to the model.
    assert "工资什么时候发" in str(ai.calls[0])


def test_suggest_keywords_no_misrouted_queries(conn):
    """Without misrouted queries the endpoint explains instead of calling AI."""
    hr = conn.execute(
        "SELECT id FROM intent_spaces WHERE name = 'HR'"
    ).fetchone()["id"]
    ai = FakeAI()
    body = _client(conn, ai).post(f"/spaces/{hr}/suggest-keywords").json()

    assert body["keywords"] == [] and body["based_on"] == 0
    assert "misrouted" in body["message"].lower() or "verify" in body["message"].lower()
    assert ai.calls == []


def test_suggest_keywords_ai_failure_is_graceful(conn):
    """An AI failure yields an empty suggestion with a message, not a 500."""
    hr = conn.execute(
        "SELECT id FROM intent_spaces WHERE name = 'HR'"
    ).fetchone()["id"]
    general = conn.execute(
        "SELECT id FROM intent_spaces WHERE is_general = 1"
    ).fetchone()["id"]
    _misroute(conn, "salary question", detected=general, verified=hr)

    resp = _client(conn, FakeAI(error=RuntimeError("down"))).post(
        f"/spaces/{hr}/suggest-keywords"
    )
    assert resp.status_code == 200
    assert resp.json()["keywords"] == []


def test_suggest_space_for_text_document(conn):
    """A text file is loaded, classified, and the suggestion returned."""
    hr = conn.execute(
        "SELECT id FROM intent_spaces WHERE name = 'HR'"
    ).fetchone()["id"]
    ai = FakeAI(reply=f'{{"space_id": {hr}, "confidence": 88}}')
    content = "Annual leave policy: employees receive 20 days.".encode()

    body = _client(conn, ai).post(
        "/documents/suggest-space",
        json={
            "name": "leave_policy.txt",
            "content_b64": base64.b64encode(content).decode(),
        },
    ).json()

    assert body["suggestion"]["space_id"] == hr
    assert body["suggestion"]["space_name"] == "HR"
    assert body["suggestion"]["confidence"] == 88.0


def test_suggest_space_invalid_inputs(conn):
    """Bad base64 → 400; AI failure → null suggestion (manual default kept)."""
    client = _client(conn, FakeAI(error=RuntimeError("down")))

    assert (
        client.post(
            "/documents/suggest-space",
            json={"name": "a.txt", "content_b64": "!!!not-base64!!!"},
        ).status_code
        == 400
    )

    ok = client.post(
        "/documents/suggest-space",
        json={
            "name": "a.txt",
            "content_b64": base64.b64encode(b"some content here").decode(),
        },
    )
    assert ok.status_code == 200 and ok.json() == {"suggestion": None}
