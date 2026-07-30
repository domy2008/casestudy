# Feature: intelliknow-kms, Integration: ingestion pipeline (task 17.1)
"""End-to-end ingestion: admin-API upload → background processing → searchable.

A document is uploaded through the real ``POST /documents`` endpoint (FastAPI
``TestClient`` executes the scheduled background task on response completion),
processed by the real :class:`DocumentProcessor` (structuring + embeddings via
the fake DashScope seam), persisted to SQLite, indexed in FAISS — and finally
retrieved by a semantically similar query embedding.
"""

from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin
from app.kb.processor import DocumentProcessor

from .conftest import HR_SPACE_ID, embed_text

EXPENSE_DOC_BODY = (
    "Expense reimbursement policy: submit travel expense reports within 30 "
    "days. Meal expenses are capped at 75 USD per day for business travel."
)
EXPENSE_QUERY = "What is the daily cap for meal expenses on business travel?"


class _InlineProcessor:
    """Runs the real ingestion pipeline as the endpoint's background task."""

    def __init__(self, stack) -> None:
        self._stack = stack

    async def process(self, document_id: int) -> None:
        processor = DocumentProcessor(
            self._stack.conn,
            self._stack.ai,
            settings=self._stack.settings,
            search_index=self._stack.search_index,
        )
        await processor.process(document_id)


def _make_app(stack) -> FastAPI:
    """Mount the admin router over the stack's temp DB and real processor."""
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.get_connection] = lambda: stack.conn
    app.dependency_overrides[admin.get_settings_dependency] = lambda: stack.settings
    app.dependency_overrides[admin.get_document_processor] = (
        lambda: _InlineProcessor(stack)
    )
    return app


def test_upload_processes_to_processed_and_becomes_searchable(stack):
    """Upload fixture → status Processed → retrievable from its space index."""
    stack.ai.structured_text = EXPENSE_DOC_BODY
    client = TestClient(_make_app(stack))

    created = client.post(
        "/documents",
        json={
            "name": "expense_policy.txt",
            "format": "txt",
            "size_bytes": len(EXPENSE_DOC_BODY.encode("utf-8")),
            "content_b64": base64.b64encode(
                EXPENSE_DOC_BODY.encode("utf-8")
            ).decode("ascii"),
            "space_id": HR_SPACE_ID,
        },
    )
    assert created.status_code == 200, created.text
    doc_id = created.json()["id"]

    # TestClient runs the scheduled background task before returning, so the
    # document has completed the real pipeline: Pending → Processed (Req 5.1).
    listed = client.get("/documents").json()
    items = listed if isinstance(listed, list) else listed.get("documents", [])
    record = next(item for item in items if item["id"] == doc_id)
    assert record["status"] == "Processed"

    # The stored original file exists under uploads.
    uploads = list(stack.settings.uploads_dir.glob("*"))
    assert uploads, "original upload file was not persisted"

    # Semantic search over the assigned space retrieves the new document
    # with a passage grounded in its content (Req 5.3, 5.6).
    passages = stack.search_index.search(HR_SPACE_ID, embed_text(EXPENSE_QUERY), k=5)
    assert passages, "processed document was not searchable"
    top = passages[0]
    assert top.document_name == "expense_policy.txt"
    assert "75 USD" in top.text


def test_upload_rejection_leaves_no_trace(stack):
    """An unsupported-format upload is rejected with no row and no file (Req 4.2)."""
    client = TestClient(_make_app(stack))

    rejected = client.post(
        "/documents",
        json={
            "name": "malware.exe",
            "format": "exe",
            "size_bytes": 10,
            "content_b64": base64.b64encode(b"0123456789").decode("ascii"),
            "space_id": HR_SPACE_ID,
        },
    )

    assert rejected.status_code == 400
    assert stack.conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 0
    assert not list(stack.settings.uploads_dir.glob("*"))
