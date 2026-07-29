"""Unit tests for the document processor pipeline (:mod:`app.kb.processor`).

These cover subtask 6.4's error paths and the successful Update re-processing
path, all with DashScope mocked via an injected fake client so no network
calls are made:

* parse failure and parse-deadline timeout → Status ``Error`` + error log,
* embedding failure (parse OK) → Status stays ``Pending`` + error log, with a
  subsequent Update retry succeeding to ``Processed``,
* unsupported format → Status ``Error`` + error log,
* a successful Update re-parses and re-embeds a Pending document to
  ``Processed``.

Validates: Requirements 5.4, 5.5, 5.8, 4.9, 4.10.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.config import load_settings
from app.core.models import ExtractedContent
from app.db import bootstrap
from app.kb import processor as processor_mod
from app.kb.loaders import DocumentParseError, UnsupportedFormatError
from app.kb.processor import DocumentProcessor, chunk_structured_text
from app.kb.store import (
    ChunkRepository,
    DocumentRepository,
    IntegrationErrorLogRepository,
    IntentSpaceRepository,
)

EMBED_DIM = 8


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeDashScopeClient:
    """In-memory stand-in for :class:`DashScopeClient` (no network).

    ``structured_text`` is returned from :meth:`chat_completion`; :meth:`embed`
    returns a deterministic unit-ish vector per input. Either method can be
    forced to raise by setting ``chat_error`` / ``embed_error``.
    """

    def __init__(
        self,
        *,
        structured_text: str = "Structured body text.",
        chat_error: Exception | None = None,
        embed_error: Exception | None = None,
    ) -> None:
        self.structured_text = structured_text
        self.chat_error = chat_error
        self.embed_error = embed_error
        self.chat_calls = 0
        self.embed_calls = 0

    async def chat_completion(self, messages, **kwargs) -> str:
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return self.structured_text

    async def embed(self, texts, **kwargs):
        self.embed_calls += 1
        if self.embed_error is not None:
            raise self.embed_error
        items = [texts] if isinstance(texts, str) else list(texts)
        vectors = []
        for i, _ in enumerate(items):
            vec = np.zeros(EMBED_DIM, dtype=np.float32)
            vec[i % EMBED_DIM] = 1.0
            vectors.append(vec)
        return vectors


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    """Settings pointing every persistent path at a temp directory."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return load_settings({"DATA_DIR": str(tmp_path)})


@pytest.fixture()
def conn(settings):
    """A bootstrapped SQLite connection over the temp data dir."""
    connection = bootstrap(settings)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def general_space_id(conn):
    """Return the seeded General_Space id."""
    return IntentSpaceRepository(conn).get_general()["id"]


def _make_document(
    conn, settings, general_space_id, *, name="doc.txt", fmt="txt", body="hello world"
) -> int:
    """Create a Pending document row with a real file under uploads."""
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    path = settings.uploads_dir / name
    path.write_text(body, encoding="utf-8")
    return DocumentRepository(conn).create(
        name=name,
        format=fmt,
        size_bytes=len(body.encode("utf-8")),
        space_id=general_space_id,
        file_path=str(path),
        status="Pending",
    )


# ---------------------------------------------------------------------------
# Chunking (tables never split)
# ---------------------------------------------------------------------------


def test_chunk_keeps_markdown_table_whole() -> None:
    """A markdown table is emitted as a single, unsplit chunk (Req 5.2)."""
    table = "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    structured = f"Intro paragraph.\n\n{table}\n\nClosing paragraph."
    chunks = chunk_structured_text(structured, target_tokens=5, overlap_tokens=1)
    table_chunks = [c for c in chunks if c.strip().startswith("|")]
    assert table in table_chunks


# ---------------------------------------------------------------------------
# Success + Update re-processing (Req 4.9)
# ---------------------------------------------------------------------------


async def test_successful_processing_marks_processed_and_indexes(
    conn, settings, general_space_id
) -> None:
    """A clean run persists chunks, marks Processed, and indexes vectors."""
    doc_id = _make_document(conn, settings, general_space_id)
    client = FakeDashScopeClient(structured_text="Some clean structured content here.")
    proc = DocumentProcessor(conn, client, settings=settings)

    await proc.process(doc_id)

    doc = DocumentRepository(conn).get(doc_id)
    assert doc["status"] == "Processed"
    assert doc["error_message"] is None
    chunks = ChunkRepository(conn).fetch_by_document(doc_id)
    assert len(chunks) >= 1
    # The document's vectors were added to its space index.
    assert proc._search.index_path(general_space_id).exists()


async def test_update_reparses_and_reembeds_to_processed(
    conn, settings, general_space_id
) -> None:
    """After an embed failure, a later successful run re-processes to Processed.

    Mirrors the Update retry path (Req 5.5, 4.9): the same document is
    processed twice — first the embedding fails (stays Pending), then it
    succeeds and reaches Processed with a single set of chunks.
    """
    doc_id = _make_document(conn, settings, general_space_id)
    docs = DocumentRepository(conn)

    # First attempt: embedding fails → stays Pending.
    failing = FakeDashScopeClient(embed_error=RuntimeError("embed down"))
    proc = DocumentProcessor(conn, failing, settings=settings)
    await proc.process(doc_id)
    assert docs.get(doc_id)["status"] == "Pending"
    assert ChunkRepository(conn).fetch_by_document(doc_id) == []

    # Update retry: embedding works → Processed with exactly one chunk set.
    ok = FakeDashScopeClient(structured_text="Recovered content.")
    proc2 = DocumentProcessor(conn, ok, settings=settings)
    await proc2.process(doc_id)

    doc = docs.get(doc_id)
    assert doc["status"] == "Processed"
    assert doc["error_message"] is None
    assert len(ChunkRepository(conn).fetch_by_document(doc_id)) >= 1


async def test_update_replaces_prior_chunks(conn, settings, general_space_id) -> None:
    """Re-processing clears stale chunks rather than accumulating them."""
    doc_id = _make_document(conn, settings, general_space_id)
    client = FakeDashScopeClient(structured_text="one two three four five")
    proc = DocumentProcessor(conn, client, settings=settings)

    await proc.process(doc_id)
    first_count = len(ChunkRepository(conn).fetch_by_document(doc_id))
    await proc.process(doc_id)
    second_count = len(ChunkRepository(conn).fetch_by_document(doc_id))

    assert first_count >= 1
    assert second_count == first_count


# ---------------------------------------------------------------------------
# Parse failure / timeout → Error (Req 5.4, 4.10)
# ---------------------------------------------------------------------------


async def test_parse_failure_sets_error_and_logs(
    conn, settings, general_space_id, monkeypatch
) -> None:
    """A loader parse failure sets Status Error and records an error log."""
    doc_id = _make_document(conn, settings, general_space_id, name="bad.pdf", fmt="pdf")

    def boom(path):
        raise DocumentParseError(Path(path), "corrupt file")

    monkeypatch.setattr(processor_mod, "load_document", boom)

    client = FakeDashScopeClient()
    proc = DocumentProcessor(conn, client, settings=settings)
    await proc.process(doc_id)

    doc = DocumentRepository(conn).get(doc_id)
    assert doc["status"] == "Error"
    assert doc["error_message"]
    assert client.chat_calls == 0  # never got to structuring

    errors = IntegrationErrorLogRepository(conn).list_recent(limit=10)
    assert any(e["operation"] == "parse" for e in errors)


async def test_parse_deadline_timeout_sets_error(
    conn, settings, general_space_id, monkeypatch
) -> None:
    """Exceeding the parse deadline sets Status Error + a timeout error log."""
    doc_id = _make_document(conn, settings, general_space_id)

    def slow_load(path):
        # Simulate a loader that blocks well past the (tiny) deadline.
        import time

        time.sleep(1.0)
        return ExtractedContent(text="never reached", tables=[])

    monkeypatch.setattr(processor_mod, "load_document", slow_load)

    client = FakeDashScopeClient()
    # Deadline far below the loader's sleep so the timeout fires.
    proc = DocumentProcessor(conn, client, settings=settings, parse_deadline_s=0.05)
    await proc.process(doc_id)

    doc = DocumentRepository(conn).get(doc_id)
    assert doc["status"] == "Error"
    assert "deadline" in (doc["error_message"] or "")
    errors = IntegrationErrorLogRepository(conn).list_recent(limit=10)
    assert any(e["operation"] == "parse" for e in errors)


# ---------------------------------------------------------------------------
# Embedding failure → stays Pending (Req 5.5)
# ---------------------------------------------------------------------------


async def test_embed_failure_keeps_pending_and_logs(
    conn, settings, general_space_id
) -> None:
    """Parse OK but embedding fails → Status stays Pending + error log."""
    doc_id = _make_document(conn, settings, general_space_id)
    client = FakeDashScopeClient(embed_error=RuntimeError("embedding service down"))
    proc = DocumentProcessor(conn, client, settings=settings)

    await proc.process(doc_id)

    doc = DocumentRepository(conn).get(doc_id)
    assert doc["status"] == "Pending"
    assert doc["error_message"]
    assert ChunkRepository(conn).fetch_by_document(doc_id) == []
    assert client.chat_calls == 1  # structuring happened (parse succeeded)

    errors = IntegrationErrorLogRepository(conn).list_recent(limit=10)
    assert any(e["operation"] == "embed" for e in errors)


# ---------------------------------------------------------------------------
# Unsupported format → Error (Req 5.8)
# ---------------------------------------------------------------------------


async def test_unsupported_format_sets_error(
    conn, settings, general_space_id, monkeypatch
) -> None:
    """An unsupported format sets Status Error + an unsupported-format log."""
    doc_id = _make_document(conn, settings, general_space_id, name="data.xyz", fmt="xyz")

    def unsupported(path):
        raise UnsupportedFormatError(".xyz")

    monkeypatch.setattr(processor_mod, "load_document", unsupported)

    client = FakeDashScopeClient()
    proc = DocumentProcessor(conn, client, settings=settings)
    await proc.process(doc_id)

    doc = DocumentRepository(conn).get(doc_id)
    assert doc["status"] == "Error"
    assert doc["error_message"]

    errors = IntegrationErrorLogRepository(conn).list_recent(limit=10)
    assert any(e["operation"] == "unsupported_format" for e in errors)
