"""Unit tests for the documents / spaces / settings-analytics-dashboard admin API.

These cover the endpoints added in tasks 13.2 / 13.3 / 13.7 in
``app/api/admin.py`` using FastAPI's :class:`~fastapi.testclient.TestClient`
with dependency overrides pointing at a freshly bootstrapped temporary
database (via :func:`app.db.bootstrap`) and small fakes where an external seam
(the background processor, the analytics service) needs to be controlled.

Scenarios exercised:

* General_Space deletion rejection (Req 6.7);
* dashboard summary content and per-section partial-failure tolerance
  (Req 9.5/9.7);
* upload rejection paths — unsupported format and oversize (Req 4.2/4.3) — plus
  the accepted path scheduling background processing (Req 5.1);
* document list filtering passthrough (Req 4.6);
* analytics export failure leaving data unchanged (Req 10.8);
* empty query-history handling (Req 10.7).
"""

from __future__ import annotations

import base64
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin
from app.config import load_settings
from app.db import bootstrap
from app.kb.store import (
    DocumentRepository,
    IntegrationRepository,
    IntentSpaceRepository,
    QueryLogRepository,
)
from app.core.models import QueryLogEntry


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_env(tmp_path):
    """A bootstrapped temp DB connection plus its Settings snapshot."""
    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "CREDENTIAL_MASTER_KEY": "unused"}
    )
    bootstrap(settings).close()
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn, settings
    conn.close()


class FakeProcessor:
    """Records which document ids were scheduled for processing."""

    def __init__(self) -> None:
        self.processed: list[int] = []

    async def process(self, document_id: int) -> None:
        self.processed.append(document_id)


def _make_app(
    conn,
    settings,
    *,
    processor=None,
    analytics=None,
    integration_repo=None,
) -> FastAPI:
    """Build a FastAPI app mounting the admin router with the temp DB injected."""
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.get_connection] = lambda: conn
    app.dependency_overrides[admin.get_settings_dependency] = lambda: settings
    if processor is not None:
        app.dependency_overrides[admin.get_document_processor] = lambda: processor
    if analytics is not None:
        app.dependency_overrides[admin.get_analytics_service] = lambda: analytics
    if integration_repo is not None:
        app.dependency_overrides[admin.get_integration_repo] = lambda: integration_repo
    return app


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Spaces — General_Space deletion rejection (Req 6.7)
# ---------------------------------------------------------------------------


def test_delete_general_space_is_rejected(temp_env):
    conn, settings = temp_env
    general_id = int(IntentSpaceRepository(conn).get_general()["id"])
    client = TestClient(_make_app(conn, settings))

    resp = client.delete(f"/spaces/{general_id}")

    assert resp.status_code == 400
    assert "general" in resp.json()["detail"].lower()
    # The General_Space still exists.
    assert IntentSpaceRepository(conn).get(general_id) is not None


def test_delete_unknown_space_returns_404(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))
    assert client.delete("/spaces/9999").status_code == 404


def test_create_space_then_duplicate_case_insensitive_conflict(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))

    created = client.post("/spaces", json={"name": "Benefits", "keywords": ["pto"]})
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "Benefits"
    assert created.json()["keywords"] == ["pto"]

    dup = client.post("/spaces", json={"name": "benefits"})
    assert dup.status_code == 409


def test_create_space_validation_error(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))
    resp = client.post("/spaces", json={"name": "", "description": "x"})
    assert resp.status_code == 400
    fields = {e["field"] for e in resp.json()["detail"]["errors"]}
    assert "name" in fields


# ---------------------------------------------------------------------------
# Dashboard summary content + per-section partial failure (Req 9.5/9.7)
# ---------------------------------------------------------------------------


def test_dashboard_summary_content(temp_env):
    conn, settings = temp_env
    docs = DocumentRepository(conn)
    docs.create(name="a", format="pdf", size_bytes=1, space_id=1, file_path="/a", status="Pending")
    docs.create(name="b", format="pdf", size_bytes=1, space_id=1, file_path="/b", status="Processed")
    docs.create(name="c", format="pdf", size_bytes=1, space_id=1, file_path="/c", status="Error")

    IntegrationRepository(conn).set_status("telegram", "Connected")

    qlog = QueryLogRepository(conn)
    from datetime import datetime

    now = datetime.now()
    qlog.insert(
        QueryLogEntry(
            ts=now, query_text="q1", detected_space_id=1, confidence=90.0,
            response_status="Success", tool="telegram",
        )
    )
    qlog.insert(
        QueryLogEntry(
            ts=now, query_text="q2", detected_space_id=1, confidence=10.0,
            response_status="Failed", tool="telegram",
        )
    )

    client = TestClient(_make_app(conn, settings))
    resp = client.get("/dashboard/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["integrations"] == {"telegram": "Connected"}
    assert body["documents"] == {"Pending": 1, "Processed": 1, "Error": 1}
    assert body["queries_24h"] == {"total": 2, "success": 1, "failed": 1}


def test_dashboard_summary_partial_failure(temp_env):
    """A failing section reports an error while the others still return data."""
    conn, settings = temp_env
    DocumentRepository(conn).create(
        name="a", format="pdf", size_bytes=1, space_id=1, file_path="/a", status="Pending"
    )

    class BoomIntegrations:
        def list(self):
            raise RuntimeError("integration store offline")

    client = TestClient(
        _make_app(conn, settings, integration_repo=BoomIntegrations())
    )
    resp = client.get("/dashboard/summary")

    assert resp.status_code == 200
    body = resp.json()
    # The broken section carries an inline error marker (Req 9.7).
    assert "error" in body["integrations"]
    assert "offline" in body["integrations"]["error"]
    # The healthy sections still return their data.
    assert body["documents"]["Pending"] == 1
    assert "error" not in body["queries_24h"]


# ---------------------------------------------------------------------------
# Document upload rejection + accepted path (Req 4.2/4.3, 5.1)
# ---------------------------------------------------------------------------


def test_upload_unsupported_format_rejected(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))

    resp = client.post(
        "/documents",
        json={"name": "malware.exe", "content_b64": _b64(b"data")},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["saved"] is False
    # No document row was created.
    assert DocumentRepository(conn).list() == []


def test_upload_oversize_rejected(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))

    resp = client.post(
        "/documents",
        json={
            "name": "big.pdf",
            "size_bytes": 60 * 1024 * 1024,  # 60 MB > 50 MB cap
            "content_b64": _b64(b"small"),
        },
    )

    assert resp.status_code == 400
    assert DocumentRepository(conn).list() == []


def test_upload_accepted_creates_pending_and_schedules(temp_env):
    conn, settings = temp_env
    processor = FakeProcessor()
    client = TestClient(_make_app(conn, settings, processor=processor))

    resp = client.post(
        "/documents",
        json={"name": "note.txt", "content_b64": _b64(b"hello world")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "Pending"
    rows = DocumentRepository(conn).list()
    assert len(rows) == 1
    # Background processing was scheduled for the created document (Req 5.1).
    assert processor.processed == [body["id"]]


def test_upload_invalid_base64_rejected(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))
    resp = client.post(
        "/documents", json={"name": "note.txt", "content_b64": "!!!not base64!!!"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Document list filtering passthrough (Req 4.6)
# ---------------------------------------------------------------------------


def test_document_list_filtering(temp_env):
    conn, settings = temp_env
    docs = DocumentRepository(conn)
    docs.create(name="alpha", format="pdf", size_bytes=1, space_id=1, file_path="/a")
    docs.create(name="beta", format="docx", size_bytes=1, space_id=2, file_path="/b")
    docs.create(name="gamma", format="pdf", size_bytes=1, space_id=2, file_path="/c")

    client = TestClient(_make_app(conn, settings))

    all_docs = client.get("/documents").json()
    assert len(all_docs) == 3

    pdfs = client.get("/documents", params={"format": "pdf"}).json()
    assert {d["name"] for d in pdfs} == {"alpha", "gamma"}

    space2 = client.get("/documents", params={"space_id": 2}).json()
    assert {d["name"] for d in space2} == {"beta", "gamma"}


# ---------------------------------------------------------------------------
# Analytics export failure leaves data unchanged (Req 10.8)
# ---------------------------------------------------------------------------


def test_export_failure_returns_500_and_leaves_data(temp_env):
    conn, settings = temp_env
    # Seed one query so we can confirm history is untouched by the failed export.
    from datetime import datetime

    QueryLogRepository(conn).insert(
        QueryLogEntry(
            ts=datetime.now(), query_text="q", detected_space_id=1, confidence=50.0,
            response_status="Success", tool="telegram",
        )
    )

    from app.analytics.service import ExportError

    class BoomAnalytics:
        def export_csv(self, filters):
            raise ExportError("disk full")

    client = TestClient(_make_app(conn, settings, analytics=BoomAnalytics()))
    resp = client.get("/analytics/export")

    assert resp.status_code == 500
    assert "failed" in resp.json()["detail"].lower()
    # Stored history is unchanged (export is read-only, Req 10.8).
    assert len(QueryLogRepository(conn).list()) == 1


def test_export_success_returns_csv(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))
    resp = client.get("/analytics/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Query Log" in resp.text


# ---------------------------------------------------------------------------
# Empty query-history handling (Req 10.7)
# ---------------------------------------------------------------------------


def test_queries_empty_history(temp_env):
    conn, settings = temp_env
    client = TestClient(_make_app(conn, settings))
    resp = client.get("/queries")
    assert resp.status_code == 200
    assert resp.json() == []


def test_verify_query_records_verification(temp_env):
    conn, settings = temp_env
    from datetime import datetime

    qid = QueryLogRepository(conn).insert(
        QueryLogEntry(
            ts=datetime.now(), query_text="q", detected_space_id=1, confidence=50.0,
            response_status="Success", tool="telegram",
        )
    )
    client = TestClient(_make_app(conn, settings))
    resp = client.post(f"/queries/{qid}/verify", json={"verified_space_id": 2})
    assert resp.status_code == 200
    assert QueryLogRepository(conn).get(qid)["verified_space_id"] == 2
