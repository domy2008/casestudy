"""Unit tests for :class:`app.kb.service.DocumentLifecycleService`.

Covers the concrete example paths for upload acceptance/rejection, deletion
(file + chunks removed, index rebuilt), Update re-processing (status reset +
processor invoked), and space reassignment (persisted + both indexes rebuilt).
The FAISS search index is replaced with a lightweight recorder so these tests
exercise the service's coordination logic without touching real index files.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.config import load_settings
from app.db import bootstrap
from app.kb.service import (
    MAX_UPLOAD_BYTES,
    DocumentLifecycleService,
    UploadRejected,
    validate_upload,
)
from app.kb.store import ChunkRepository, DocumentRepository, IntentSpaceRepository


class _RecordingSearch:
    """Stand-in for :class:`SearchIndex` that records coordination calls."""

    def __init__(self) -> None:
        self.rebuilt: list[int] = []
        self.added: list[int] = []

    def rebuild_space(self, space_id: int) -> None:
        self.rebuilt.append(space_id)

    def add_document(self, document_id: int) -> None:
        self.added.append(document_id)


class _RecordingProcessor:
    """Stand-in for :class:`DocumentProcessor` recording ``process`` calls."""

    def __init__(self) -> None:
        self.processed: list[int] = []

    async def process(self, document_id: int) -> None:
        self.processed.append(document_id)


def _make_service(tmp: str):
    """Bootstrap a temp DB and build a service with a recording search index."""
    settings = load_settings({"DATA_DIR": tmp})
    conn = bootstrap(settings)
    search = _RecordingSearch()
    service = DocumentLifecycleService(conn, settings=settings, search_index=search)
    return conn, service, search, settings


def _general_id(conn) -> int:
    return IntentSpaceRepository(conn).get_general()["id"]


# ---------------------------------------------------------------------------
# validate_upload (pure)
# ---------------------------------------------------------------------------


def test_validate_upload_accepts_supported_within_limit() -> None:
    assert validate_upload("report.pdf", MAX_UPLOAD_BYTES) == []
    assert validate_upload("notes.md", 0) == []


def test_validate_upload_flags_bad_format_and_size() -> None:
    errors = validate_upload("archive.zip", MAX_UPLOAD_BYTES + 1)
    fields = {e.field for e in errors}
    assert fields == {"format", "size"}
    combined = " ".join(e.message for e in errors).lower()
    assert "supported" in combined
    assert "50 mb" in combined


# ---------------------------------------------------------------------------
# accept_upload
# ---------------------------------------------------------------------------


def test_accept_upload_creates_pending_document_and_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, service, _search, settings = _make_service(tmp)
        try:
            doc_id = service.accept_upload("handbook.pdf", content=b"hello")
            row = DocumentRepository(conn).get(doc_id)
            assert row["status"] == "Pending"
            assert row["format"] == "pdf"
            assert row["space_id"] == _general_id(conn)
            assert Path(row["file_path"]).read_bytes() == b"hello"
        finally:
            conn.close()


def test_accept_upload_rejects_and_leaves_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, service, _search, settings = _make_service(tmp)
        try:
            with pytest.raises(UploadRejected):
                service.accept_upload(
                    "big.pdf", content=b"x", declared_size=MAX_UPLOAD_BYTES + 1
                )
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            uploads = settings.uploads_dir
            stored = list(uploads.glob("*")) if uploads.exists() else []
            assert stored == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_file_chunks_and_rebuilds_index() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, service, search, _settings = _make_service(tmp)
        try:
            space_id = _general_id(conn)
            doc_id = service.accept_upload("doc.txt", content=b"body")
            file_path = Path(DocumentRepository(conn).get(doc_id)["file_path"])

            chunks = ChunkRepository(conn)
            chunks.insert(doc_id, 0, "chunk text", np.ones(4, dtype=np.float32))
            assert len(chunks.fetch_by_document(doc_id)) == 1

            service.delete(doc_id)

            assert DocumentRepository(conn).get(doc_id) is None
            assert chunks.fetch_by_document(doc_id) == []
            assert not file_path.exists()
            assert search.rebuilt == [space_id]
        finally:
            conn.close()


def test_delete_unknown_document_is_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, service, search, _settings = _make_service(tmp)
        try:
            service.delete(9999)
            assert search.rebuilt == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_resets_status_and_invokes_processor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings({"DATA_DIR": tmp})
        conn = bootstrap(settings)
        processor = _RecordingProcessor()
        service = DocumentLifecycleService(
            conn,
            settings=settings,
            search_index=_RecordingSearch(),
            processor=processor,
        )
        try:
            doc_id = service.accept_upload("doc.md", content=b"# title")
            DocumentRepository(conn).set_status(doc_id, "Error", error_message="boom")

            await service.update(doc_id)

            assert DocumentRepository(conn).get(doc_id)["status"] == "Pending"
            assert processor.processed == [doc_id]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# reassign_space
# ---------------------------------------------------------------------------


def test_reassign_space_persists_and_rebuilds_both_indexes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, service, search, _settings = _make_service(tmp)
        try:
            spaces = IntentSpaceRepository(conn)
            old_space = _general_id(conn)
            new_space = spaces.get_by_name("HR")["id"]

            doc_id = service.accept_upload("doc.txt", content=b"body")
            assert DocumentRepository(conn).get(doc_id)["space_id"] == old_space

            service.reassign_space(doc_id, new_space)

            assert DocumentRepository(conn).get(doc_id)["space_id"] == new_space
            assert search.rebuilt == [old_space, new_space]
        finally:
            conn.close()


def test_reassign_same_space_is_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, service, search, _settings = _make_service(tmp)
        try:
            old_space = _general_id(conn)
            doc_id = service.accept_upload("doc.txt", content=b"body")
            service.reassign_space(doc_id, old_space)
            assert search.rebuilt == []
        finally:
            conn.close()
