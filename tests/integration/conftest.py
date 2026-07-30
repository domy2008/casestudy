"""Shared fixtures for the end-to-end integration suite (task 17.1).

The integration tests wire the *real* modules together — SQLite (bootstrapped
in a temp dir), FAISS search index, document processor, orchestrator, response
generator, dispatcher, and frontend adapters — and mock only the true external
boundaries:

* **DashScope** is faked at the single client seam (:class:`FakeDashScope`),
  covering all four call sites: ``classify`` / ``embed`` (orchestrator),
  ``generate`` (RAG), and ``chat_completion`` (document structuring).
* **Telegram / Teams HTTP** is intercepted with ``respx`` inside each test.

Embeddings are deterministic hashed bag-of-words vectors, so a query sharing
vocabulary with an ingested chunk genuinely scores high cosine similarity —
retrieval is exercised for real rather than stubbed.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

import numpy as np
import pytest

from app.analytics.service import AnalyticsService
from app.config import Settings, load_settings
from app.core.models import QueryContext
from app.core.orchestrator import Orchestrator
from app.db import bootstrap
from app.kb.processor import DocumentProcessor
from app.kb.search import SearchIndex
from app.kb.store import DocumentRepository
from app.rag.generator import ResponseGenerator

#: Dimension of the deterministic test embeddings.
EMBED_DIM = 64

#: Seeded Intent_Space id for HR (General=1, HR=2, Legal=3, Finance=4).
HR_SPACE_ID = 2


def embed_text(text: str) -> np.ndarray:
    """Produce a deterministic hashed bag-of-words embedding for ``text``.

    Texts sharing vocabulary produce vectors with high cosine similarity, so
    real FAISS retrieval works end to end without a live embedding service.

    Args:
        text: The text to embed.

    Returns:
        A float32 vector of :data:`EMBED_DIM` dimensions (never all-zero).
    """
    vector = np.zeros(EMBED_DIM, dtype=np.float32)
    for token in text.lower().split():
        word = "".join(ch for ch in token if ch.isalnum())
        if word:
            vector[hash(word) % EMBED_DIM] += 1.0
    if not vector.any():
        vector[0] = 1.0
    return vector


class FakeDashScope:
    """Configurable stand-in for the DashScope client seam (no network).

    Attributes:
        classify_json: Raw classifier reply returned by :meth:`classify`.
        answer: Grounded answer returned by :meth:`generate`.
        structured_text: Structured document text returned by
            :meth:`chat_completion` (the ingestion structuring step).
        classify_error / generate_error / embed_error: When set, the matching
            method raises instead of returning.
    """

    def __init__(self) -> None:
        self.classify_json = '{"space_id": 2, "confidence": 95}'
        self.answer = "Employees receive 20 days of annual leave per year."
        self.structured_text = "Employees receive 20 days of annual leave per year."
        self.classify_error: Exception | None = None
        self.generate_error: Exception | None = None
        self.embed_error: Exception | None = None

    async def classify(self, messages) -> str:
        """Return the configured classification JSON (or raise)."""
        if self.classify_error is not None:
            raise self.classify_error
        return self.classify_json

    async def generate(self, messages) -> str:
        """Return the configured grounded answer (or raise)."""
        if self.generate_error is not None:
            raise self.generate_error
        return self.answer

    async def chat_completion(self, messages, **kwargs) -> str:
        """Return the configured structured document text."""
        return self.structured_text

    async def embed(self, texts, **kwargs):
        """Embed one or many texts with :func:`embed_text`."""
        if self.embed_error is not None:
            raise self.embed_error
        items = [texts] if isinstance(texts, str) else list(texts)
        return [embed_text(t) for t in items]


class Stack:
    """The assembled real modules over a temp data dir, sharing one fake AI."""

    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        self.settings = settings
        self.conn = conn
        self.ai = FakeDashScope()
        self.search_index = SearchIndex(conn, settings)
        self.analytics = AnalyticsService(conn)
        self.orchestrator = Orchestrator(
            conn=conn,
            ai_client=self.ai,
            search_index=self.search_index,
            generator=ResponseGenerator(self.ai),
            analytics=self.analytics,
        )

    async def ingest(
        self, *, name: str, body: str, space_id: int = HR_SPACE_ID
    ) -> int:
        """Run the real ingestion pipeline over an on-disk text document.

        Args:
            name: The document file name (``.txt``).
            body: The raw file content; also used as the structured text so
                the embedded chunk carries the document's real vocabulary.
            space_id: Target Intent_Space id.

        Returns:
            The processed document's id (status ``Processed``).
        """
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.uploads_dir / name
        path.write_text(body, encoding="utf-8")
        doc_id = DocumentRepository(self.conn).create(
            name=name,
            format="txt",
            size_bytes=len(body.encode("utf-8")),
            space_id=space_id,
            file_path=str(path),
            status="Pending",
        )
        self.ai.structured_text = body
        processor = DocumentProcessor(
            self.conn, self.ai, settings=self.settings, search_index=self.search_index
        )
        await processor.process(doc_id)
        status = DocumentRepository(self.conn).get(doc_id)["status"]
        assert status == "Processed", f"corpus ingestion failed: {status}"
        return doc_id


def make_query_context(text: str, tool: str = "telegram") -> QueryContext:
    """Build a :class:`QueryContext` for driving the dispatcher in tests."""
    return QueryContext(
        query_id=str(uuid.uuid4()),
        tool=tool,
        conversation_ref={"chat_id": 42},
        text=text,
        received_at=datetime.now(),
    )


@pytest.fixture()
def stack(tmp_path):
    """A fully wired :class:`Stack` over a bootstrapped temp database."""
    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "CREDENTIAL_MASTER_KEY": "test-only"}
    )
    bootstrap(settings).close()
    # check_same_thread=False: FastAPI's TestClient serves requests on a
    # worker thread, so the shared connection must cross thread boundaries.
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    instance = Stack(settings, conn)
    try:
        yield instance
    finally:
        conn.close()
