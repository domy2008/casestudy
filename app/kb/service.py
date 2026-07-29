"""Document lifecycle service (upload validation, delete, update, reassign).

This module sits above the data layer (:mod:`app.kb.store`), the FAISS index
manager (:mod:`app.kb.search`), and the ingestion pipeline
(:mod:`app.kb.processor`), orchestrating the Admin-facing document lifecycle:

* **Upload acceptance** (:meth:`DocumentLifecycleService.accept_upload`) — the
  gate in front of ingestion. An upload is accepted **iff** its format is one
  of :data:`~app.kb.loaders.SUPPORTED_EXTENSIONS` (PDF/DOCX/XLSX/TXT/MD) *and*
  its size is at most :data:`MAX_UPLOAD_BYTES` (50 MB). On acceptance the
  original bytes are saved to ``{uploads_dir}/{uuid}{ext}`` and a ``Pending``
  document row is created (Req 4.1). On rejection nothing is persisted — no
  document row and no stored file — and an :class:`UploadRejected` error is
  raised naming the supported formats (Req 4.2) or the maximum size (Req 4.3).
* **Delete** (:meth:`DocumentLifecycleService.delete`) — removes the document
  content, chunks, and embeddings, deletes the stored original, and rebuilds
  the space's FAISS index from SQLite so no trace remains (Req 4.8).
* **Update** (:meth:`DocumentLifecycleService.trigger_update`) — marks the
  document ``Pending`` and re-runs the ingestion pipeline
  (:meth:`app.kb.processor.DocumentProcessor.process`) so it is re-parsed and
  re-embedded (Req 4.9).
* **Reassign** (:meth:`DocumentLifecycleService.assign_space`) — persists a new
  Intent_Space association and rebuilds both the old and new space indexes so
  the document's passages move with it (Req 5.6).

The size of an upload is decided *before* any large write: callers may pass a
``declared_size`` so the validation gate can reject an over-limit upload
without ever touching disk (and so tests need not synthesize 50 MB of bytes).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from pathlib import Path

from app.config import Settings, get_settings
from app.core.models import FieldError
from app.kb.loaders import SUPPORTED_EXTENSIONS
from app.kb.processor import DocumentProcessor
from app.kb.search import SearchIndex
from app.kb.store import DocumentRepository, IntentSpaceRepository

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_UPLOAD_BYTES",
    "UploadRejected",
    "DocumentNotFound",
    "validate_upload",
    "DocumentLifecycleService",
]

#: Maximum accepted upload size in bytes (50 MB, Req 4.1/4.3).
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024


def _supported_formats_label() -> str:
    """Return a human-readable list of supported formats for error messages.

    Returns:
        The supported extensions rendered as uppercase, dotless, comma-separated
        tokens (e.g. ``"PDF, DOCX, XLSX, TXT, MD"``).
    """
    return ", ".join(
        sorted(ext.lstrip(".").upper() for ext in SUPPORTED_EXTENSIONS)
    )


def validate_upload(filename: str, size: int) -> list[FieldError]:
    """Validate an upload's format and size, returning one error per problem.

    Pure function (no I/O): an upload is valid — an empty list — **iff** its
    file extension is one of :data:`~app.kb.loaders.SUPPORTED_EXTENSIONS` *and*
    its size is at most :data:`MAX_UPLOAD_BYTES`. Otherwise a
    :class:`~app.core.models.FieldError` is returned for the ``format`` field
    (naming the supported formats, Req 4.2) and/or the ``size`` field (naming
    the 50 MB limit, Req 4.3). Running the check before any write is what lets
    a rejected upload persist nothing.

    Args:
        filename: The upload file name; its extension determines the format.
        size: The upload size in bytes.

    Returns:
        A list of field errors; empty when the upload is acceptable.
    """
    errors: list[FieldError] = []
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        errors.append(
            FieldError(
                field="format",
                message=(
                    f"Unsupported document format {ext or '(none)'!r}. "
                    f"Supported formats are: {_supported_formats_label()}."
                ),
            )
        )
    if size > MAX_UPLOAD_BYTES:
        errors.append(
            FieldError(
                field="size",
                message=(
                    f"File size {size} bytes exceeds the maximum allowed size "
                    f"of 50 MB ({MAX_UPLOAD_BYTES} bytes)."
                ),
            )
        )
    return errors


class UploadRejected(Exception):
    """Raised when an upload fails the format or size gate.

    The message names the supported formats (bad format, Req 4.2) or the maximum
    allowed size (oversize, Req 4.3) so the Admin_UI can surface a clear reason.
    No document row or stored file is created when this is raised. The offending
    :class:`~app.core.models.FieldError` objects are available on ``errors``.
    """

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__("; ".join(e.message for e in errors))


class DocumentNotFound(Exception):
    """Raised when a lifecycle operation targets a non-existent document."""

    def __init__(self, document_id: int) -> None:
        self.document_id = document_id
        super().__init__(f"document {document_id} does not exist")


class DocumentLifecycleService:
    """Coordinates upload validation and the document lifecycle.

    One service instance owns the repositories and the FAISS index manager it
    needs; construct it with the process-wide SQLite connection. A
    :class:`~app.kb.processor.DocumentProcessor` may be injected to enable the
    Update re-processing path (:meth:`trigger_update`); it is optional so upload
    validation, delete, and reassignment can be used without wiring the AI seam.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        search_index: SearchIndex | None = None,
        processor: DocumentProcessor | None = None,
    ) -> None:
        """Store dependencies and resolve the uploads directory.

        Args:
            conn: Open connection over the bootstrapped schema.
            settings: Optional settings snapshot; defaults to process settings.
                Its ``uploads_dir`` is where originals are stored.
            search_index: Optional :class:`SearchIndex`; one is created over
                ``conn`` when omitted.
            processor: Optional ingestion pipeline used by :meth:`trigger_update`
                to re-parse and re-embed a document (Req 4.9).
        """
        self._conn = conn
        self._settings = settings or get_settings()
        self._documents = DocumentRepository(conn)
        self._spaces = IntentSpaceRepository(conn)
        self._search = search_index or SearchIndex(conn, self._settings)
        self._processor = processor

    # ------------------------------------------------------------------
    # Upload acceptance (Req 4.1, 4.2, 4.3)
    # ------------------------------------------------------------------

    def accept_upload(
        self,
        filename: str,
        content: bytes,
        *,
        declared_size: int | None = None,
        space_id: int | None = None,
    ) -> int:
        """Validate an upload and, if accepted, store it as a Pending document.

        The upload is accepted **iff** the file extension is one of
        :data:`~app.kb.loaders.SUPPORTED_EXTENSIONS` *and* its size is at most
        :data:`MAX_UPLOAD_BYTES`. Validation happens before any write, so a
        rejected upload leaves no document row and no stored file (Req 4.2/4.3).

        Args:
            filename: The original file name, used to derive the format and
                recorded as the document name.
            content: The raw file bytes. Stored verbatim on acceptance.
            declared_size: The upload's size in bytes for the size check. When
                omitted, ``len(content)`` is used. Supplying it lets callers
                reject an over-limit upload without materializing the bytes.
            space_id: Optional Intent_Space to associate; defaults to the
                General_Space.

        Returns:
            The id of the newly created ``Pending`` document row.

        Raises:
            UploadRejected: If the format is unsupported (message names the
                supported formats) or the size exceeds the maximum (message
                names the 50 MB limit).
        """
        ext = Path(filename).suffix.lower()
        size = declared_size if declared_size is not None else len(content)

        # --- Validate BEFORE any write so rejects persist nothing. ---
        errors = validate_upload(filename, size)
        if errors:
            raise UploadRejected(errors)

        # --- Accepted: store the original, then create the Pending row. ---
        target_space_id = space_id if space_id is not None else self._general_space_id()

        uploads_dir = self._settings.uploads_dir
        uploads_dir.mkdir(parents=True, exist_ok=True)
        stored_path = uploads_dir / f"{uuid.uuid4().hex}{ext}"
        stored_path.write_bytes(content)

        try:
            document_id = self._documents.create(
                name=filename,
                format=ext.lstrip("."),
                size_bytes=size,
                space_id=target_space_id,
                file_path=str(stored_path),
                status="Pending",
            )
        except Exception:
            # Roll back the stored file so a failed insert leaves no orphan.
            self._safe_remove(stored_path)
            raise

        return document_id

    # ------------------------------------------------------------------
    # Delete (Req 4.8)
    # ------------------------------------------------------------------

    def delete(self, document_id: int) -> None:
        """Delete a document and every trace of it, then rebuild its index.

        Removes the document row (its chunks and embeddings cascade away via the
        schema foreign key), deletes the stored original file, and rebuilds the
        document's Intent_Space FAISS index from the authoritative SQLite rows so
        none of its passages can be retrieved afterward (Req 4.8).

        Deleting a document that does not exist is a no-op (nothing to remove,
        no index to rebuild).

        Args:
            document_id: The document to delete.
        """
        doc = self._documents.get(document_id)
        if doc is None:
            return

        space_id = int(doc["space_id"])
        file_path = doc.get("file_path")

        self._documents.delete(document_id)
        if file_path:
            self._safe_remove(Path(file_path))
        self._search.rebuild_space(space_id)

    # ------------------------------------------------------------------
    # Update re-processing (Req 4.9)
    # ------------------------------------------------------------------

    async def update(self, document_id: int) -> None:
        """Mark a document for re-processing and re-run the ingestion pipeline.

        Sets the document Status to ``Pending`` (clearing any prior error) and
        delegates to :meth:`app.kb.processor.DocumentProcessor.process`, which
        re-parses, re-structures, re-chunks, re-embeds, and — on success — sets
        the document back to ``Processed`` while refreshing its index (Req 4.9).

        Args:
            document_id: The document to re-process.

        Raises:
            DocumentNotFound: If no such document exists.
            RuntimeError: If the service was constructed without a processor.
        """
        doc = self._documents.get(document_id)
        if doc is None:
            raise DocumentNotFound(document_id)
        if self._processor is None:
            raise RuntimeError(
                "update requires a DocumentProcessor; construct the service "
                "with processor=... to enable re-processing."
            )

        self._documents.set_status(document_id, "Pending", error_message=None)
        await self._processor.process(document_id)

    #: Task-language alias for :meth:`update` (trigger re-processing, Req 4.9).
    trigger_update = update

    # ------------------------------------------------------------------
    # Space reassignment (Req 5.6)
    # ------------------------------------------------------------------

    def reassign_space(self, document_id: int, space_id: int) -> None:
        """Reassign a document to a new Intent_Space and rebuild both indexes.

        Persists the new association (Req 5.6) and rebuilds both the old and new
        space FAISS indexes from SQLite (old first, then new) so the document's
        passages leave the old space's index and appear in the new one.
        Reassigning to the same space is a no-op: the association is unchanged
        and no index is rebuilt.

        Args:
            document_id: The document to reassign.
            space_id: The destination Intent_Space id.

        Raises:
            DocumentNotFound: If no such document exists.
            ValueError: If the destination Intent_Space does not exist.
        """
        doc = self._documents.get(document_id)
        if doc is None:
            raise DocumentNotFound(document_id)
        if self._spaces.get(space_id) is None:
            raise ValueError(f"Intent_Space {space_id} does not exist")

        old_space_id = int(doc["space_id"])
        if old_space_id == space_id:
            return  # No change — nothing to persist or rebuild.

        self._documents.set_space(document_id, space_id)
        # Rebuild the affected indexes: old first, then new.
        self._search.rebuild_space(old_space_id)
        self._search.rebuild_space(space_id)

    #: Task-language alias for :meth:`reassign_space` (Req 5.6).
    assign_space = reassign_space

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _general_space_id(self) -> int:
        """Return the General_Space id, the default association for uploads.

        Returns:
            The id of the space flagged ``is_general``.

        Raises:
            RuntimeError: If the General_Space is missing (unseeded database).
        """
        general = self._spaces.get_general()
        if general is None:
            raise RuntimeError(
                "General_Space is not present; the database was not seeded."
            )
        return int(general["id"])

    @staticmethod
    def _safe_remove(path: Path) -> None:
        """Remove a file if it exists, swallowing any filesystem error.

        Args:
            path: The file to remove.
        """
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove stored file %s", path, exc_info=True)
