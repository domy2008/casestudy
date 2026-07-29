"""SQLite repositories over the schema defined in :mod:`app.db`.

This module is the single data-access layer for the metadata store. It reuses
the tables, indexes, and connection helpers created by :mod:`app.db` (never
redefining the schema) and exposes small repository classes — one per table
group — that own the SQL for their domain.

Every repository accepts an injectable :class:`sqlite3.Connection`, so callers
wire in the process-wide connection in production and a throwaway temp-DB
connection (via :func:`app.db.bootstrap`) in tests. Rows are returned as plain
``dict`` objects (the underlying connection uses :class:`sqlite3.Row`), keeping
the repositories free of any ORM dependency.

Repository groups:

* :class:`DocumentRepository` - documents CRUD, status transitions, filtered
  listing (name / format / upload-date / space), space reassignment, delete.
* :class:`ChunkRepository` - chunk inserts with ``float32`` embedding BLOBs and
  retrieval by document or by space (for FAISS index rebuilds).
* :class:`IntentSpaceRepository` - Intent_Space + keyword CRUD.
* :class:`SettingsRepository` - key/value settings get/set.
* :class:`IntegrationRepository` - per-tool integration status get/set.
* :class:`QueryLogRepository` - Query_Log insert, filtered listing, verify.
* :class:`DocumentAccessRepository` - access-event insert and count aggregation.
* :class:`IntegrationErrorLogRepository` - error-log insert and recent listing.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import numpy as np

from app.core.models import QueryLogEntry

__all__ = [
    "embedding_to_blob",
    "blob_to_embedding",
    "DocumentRepository",
    "ChunkRepository",
    "IntentSpaceRepository",
    "SettingsRepository",
    "IntegrationRepository",
    "QueryLogRepository",
    "DocumentAccessRepository",
    "IntegrationErrorLogRepository",
]

# Valid document status values (design: documents.status Pending|Processed|Error).
DOCUMENT_STATUSES: frozenset[str] = frozenset({"Pending", "Processed", "Error"})


# ---------------------------------------------------------------------------
# Embedding (de)serialization helpers
# ---------------------------------------------------------------------------


def embedding_to_blob(vector: Sequence[float] | np.ndarray) -> bytes:
    """Serialize an embedding vector to a ``float32`` byte BLOB.

    Args:
        vector: The embedding as a sequence of floats or a numpy array. It is
            coerced to a contiguous ``float32`` array before serialization so
            the on-disk representation is stable regardless of input dtype.

    Returns:
        The raw little-endian ``float32`` bytes suitable for storage in the
        ``chunks.embedding`` BLOB column.
    """
    arr = np.ascontiguousarray(np.asarray(vector, dtype=np.float32))
    return arr.tobytes()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    """Deserialize a ``float32`` byte BLOB back into an embedding array.

    Args:
        blob: The raw bytes previously produced by :func:`embedding_to_blob`.

    Returns:
        A 1-D ``float32`` :class:`numpy.ndarray`. The array is a copy (not a
        read-only view over the input buffer), so callers may mutate it freely.
    """
    return np.frombuffer(blob, dtype=np.float32).copy()


def _to_iso(value: datetime | date | str | None) -> str | None:
    """Normalize a timestamp/date to an ISO-formatted string.

    Args:
        value: A :class:`datetime`, :class:`date`, ISO string, or ``None``.

    Returns:
        The ISO string form, or ``None`` when ``value`` is ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()


def _now_iso() -> str:
    """Return the current time as an ISO string (space-separated)."""
    return datetime.now().isoformat(sep=" ")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a single :class:`sqlite3.Row` to a dict, preserving ``None``."""
    return dict(row) if row is not None else None


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert an iterable of :class:`sqlite3.Row` to a list of dicts."""
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class DocumentRepository:
    """Repository for the ``documents`` table.

    Covers creation, single/filtered retrieval, status transitions
    (Pending/Processed/Error), Intent_Space reassociation, and deletion. The
    ``chunks`` rows of a deleted document are removed automatically by the
    ``ON DELETE CASCADE`` foreign key declared in the schema.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def create(
        self,
        *,
        name: str,
        format: str,
        size_bytes: int,
        space_id: int,
        file_path: str,
        status: str = "Pending",
        error_message: str | None = None,
        uploaded_at: datetime | str | None = None,
        updated_at: datetime | str | None = None,
    ) -> int:
        """Insert a new document row and return its id.

        Args:
            name: Original document name.
            format: Lowercase format token (``pdf``/``docx``/``xlsx``/``txt``/``md``).
            size_bytes: File size in bytes.
            space_id: Associated Intent_Space id (defaults to General upstream).
            file_path: Location of the stored original under ``/data/uploads``.
            status: Initial status; defaults to ``Pending``.
            error_message: Optional error detail (for Error status).
            uploaded_at: Upload timestamp; defaults to now. Accepts a datetime
                or an ISO string, letting tests set arbitrary dates.
            updated_at: Last-updated timestamp; defaults to ``uploaded_at``.

        Returns:
            The autogenerated document id.
        """
        uploaded = _to_iso(uploaded_at) or _now_iso()
        updated = _to_iso(updated_at) or uploaded
        cur = self._conn.execute(
            """
            INSERT INTO documents
                (name, format, size_bytes, status, space_id, file_path,
                 error_message, uploaded_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, format, size_bytes, status, space_id, file_path,
             error_message, uploaded, updated),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get(self, document_id: int) -> dict[str, Any] | None:
        """Return the document row as a dict, or ``None`` if not found.

        Args:
            document_id: The document id to fetch.
        """
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return _row_to_dict(row)

    def list(
        self,
        *,
        name: str | None = None,
        format: str | None = None,
        space_id: int | None = None,
        uploaded_on: datetime | date | str | None = None,
        uploaded_from: datetime | date | str | None = None,
        uploaded_to: datetime | date | str | None = None,
    ) -> list[dict[str, Any]]:
        """List documents matching all supplied filters, newest first.

        All filters are combined with AND; a filter left as ``None`` is not
        applied. Only documents matching every applied filter are returned
        (Req 4.6). Results are ordered by ``uploaded_at`` descending, then id
        descending, for a stable newest-first listing.

        Args:
            name: Case-insensitive substring match against the document name.
            format: Exact match against the stored format token.
            space_id: Exact match against the associated Intent_Space id.
            uploaded_on: Match documents whose upload *date* equals this day
                (the time-of-day portion is ignored). Accepts a date, datetime,
                or ``YYYY-MM-DD`` string.
            uploaded_from: Inclusive lower bound on the upload date.
            uploaded_to: Inclusive upper bound on the upload date.

        Returns:
            A list of document rows as dicts (possibly empty).
        """
        clauses: list[str] = []
        params: list[Any] = []

        if name is not None:
            # Case-insensitive substring search on the name.
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(name)}%")
        if format is not None:
            clauses.append("format = ?")
            params.append(format)
        if space_id is not None:
            clauses.append("space_id = ?")
            params.append(space_id)
        if uploaded_on is not None:
            clauses.append("date(uploaded_at) = date(?)")
            params.append(_to_iso(uploaded_on))
        if uploaded_from is not None:
            clauses.append("date(uploaded_at) >= date(?)")
            params.append(_to_iso(uploaded_from))
        if uploaded_to is not None:
            clauses.append("date(uploaded_at) <= date(?)")
            params.append(_to_iso(uploaded_to))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM documents"
            + where
            + " ORDER BY uploaded_at DESC, id DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)

    def set_status(
        self,
        document_id: int,
        status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        """Transition a document to a new status (Pending/Processed/Error).

        Args:
            document_id: The document to update.
            status: New status; must be one of :data:`DOCUMENT_STATUSES`.
            error_message: Error detail to store (typically with ``Error``);
                passed through as-is, so ``None`` clears any prior message.

        Raises:
            ValueError: If ``status`` is not a recognized status value.
        """
        if status not in DOCUMENT_STATUSES:
            raise ValueError(f"invalid document status: {status!r}")
        self._conn.execute(
            "UPDATE documents SET status = ?, error_message = ?, updated_at = ? "
            "WHERE id = ?",
            (status, error_message, _now_iso(), document_id),
        )
        self._conn.commit()

    def set_space(self, document_id: int, space_id: int) -> None:
        """Reassociate a document with a different Intent_Space (Req 5.6).

        Args:
            document_id: The document to reassign.
            space_id: The new Intent_Space id.
        """
        self._conn.execute(
            "UPDATE documents SET space_id = ?, updated_at = ? WHERE id = ?",
            (space_id, _now_iso(), document_id),
        )
        self._conn.commit()

    # Backwards-friendly alias matching the design's "update space association".
    update_space = set_space

    def reassign_space_documents(self, from_space_id: int, to_space_id: int) -> int:
        """Move every document from one space to another (space-deletion path).

        Used when an Intent_Space is deleted and its documents are reassigned to
        the General_Space (Req 6.3).

        Args:
            from_space_id: The space being vacated.
            to_space_id: The destination space (typically General).

        Returns:
            The number of documents reassigned.
        """
        cur = self._conn.execute(
            "UPDATE documents SET space_id = ?, updated_at = ? WHERE space_id = ?",
            (to_space_id, _now_iso(), from_space_id),
        )
        self._conn.commit()
        return cur.rowcount

    def delete(self, document_id: int) -> None:
        """Delete a document (and its chunks via cascade).

        Args:
            document_id: The document to remove.
        """
        self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        self._conn.commit()


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally.

    Args:
        term: The raw user search term.

    Returns:
        The term with ``\\``, ``%``, and ``_`` escaped for use with an
        ``ESCAPE '\\'`` LIKE clause.
    """
    return (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


class ChunkRepository:
    """Repository for the ``chunks`` table (text + embedding BLOBs)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def insert(
        self,
        document_id: int,
        seq: int,
        text: str,
        embedding: Sequence[float] | np.ndarray,
    ) -> int:
        """Insert a single chunk with its embedding and return the chunk id.

        Args:
            document_id: Owning document id.
            seq: Zero-based ordinal of the chunk within the document.
            text: The chunk text.
            embedding: The chunk embedding; serialized to a ``float32`` BLOB.

        Returns:
            The autogenerated chunk id (also usable as the FAISS vector id).
        """
        cur = self._conn.execute(
            "INSERT INTO chunks (document_id, seq, text, embedding) "
            "VALUES (?, ?, ?, ?)",
            (document_id, seq, text, embedding_to_blob(embedding)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def insert_many(
        self,
        document_id: int,
        chunks: Iterable[tuple[int, str, Sequence[float] | np.ndarray]],
    ) -> list[int]:
        """Insert multiple chunks for a document in one transaction.

        Args:
            document_id: Owning document id.
            chunks: Iterable of ``(seq, text, embedding)`` tuples.

        Returns:
            The list of inserted chunk ids in insertion order.
        """
        ids: list[int] = []
        for seq, text, embedding in chunks:
            cur = self._conn.execute(
                "INSERT INTO chunks (document_id, seq, text, embedding) "
                "VALUES (?, ?, ?, ?)",
                (document_id, seq, text, embedding_to_blob(embedding)),
            )
            ids.append(int(cur.lastrowid))
        self._conn.commit()
        return ids

    def fetch_by_document(self, document_id: int) -> list[dict[str, Any]]:
        """Return all chunks for a document, ordered by sequence.

        Args:
            document_id: The document whose chunks to fetch.

        Returns:
            A list of dicts with keys ``id``, ``document_id``, ``seq``,
            ``text``, and ``embedding`` (a ``float32`` :class:`numpy.ndarray`).
        """
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY seq ASC",
            (document_id,),
        ).fetchall()
        return [self._decode_row(r) for r in rows]

    def fetch_for_space(
        self, space_id: int, *, processed_only: bool = True
    ) -> list[dict[str, Any]]:
        """Return all chunks for a space, for rebuilding its FAISS index.

        Args:
            space_id: The Intent_Space whose chunks to gather.
            processed_only: When ``True`` (default), include only chunks of
                documents with status ``Processed`` — matching the rule that
                only Processed documents are ever indexed (Req 5.7).

        Returns:
            A list of chunk dicts (see :meth:`fetch_by_document`) each also
            carrying ``document_name`` for convenient citation joins.
        """
        sql = (
            "SELECT c.id, c.document_id, c.seq, c.text, c.embedding, "
            "       d.name AS document_name "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.space_id = ?"
        )
        params: list[Any] = [space_id]
        if processed_only:
            sql += " AND d.status = 'Processed'"
        sql += " ORDER BY c.id ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._decode_row(r) for r in rows]

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a chunk row to a dict, decoding the embedding BLOB."""
        d = dict(row)
        d["embedding"] = blob_to_embedding(d["embedding"])
        return d


# ---------------------------------------------------------------------------
# Intent spaces + keywords
# ---------------------------------------------------------------------------


class IntentSpaceRepository:
    """Repository for ``intent_spaces`` and their ``space_keywords``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def create(
        self,
        name: str,
        *,
        description: str = "",
        is_general: bool = False,
        is_default: bool = False,
        created_at: datetime | str | None = None,
    ) -> int:
        """Create a new Intent_Space and return its id.

        Args:
            name: Space name (case-insensitively unique per the schema).
            description: Optional description (≤500 chars, app-enforced).
            is_general: Whether this is the undeletable General_Space.
            is_default: Whether this is a seeded default (HR/Legal/Finance).
            created_at: Creation timestamp; defaults to now.

        Returns:
            The new space id.
        """
        cur = self._conn.execute(
            "INSERT INTO intent_spaces (name, description, is_general, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, description, int(is_general), int(is_default),
             _to_iso(created_at) or _now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get(self, space_id: int) -> dict[str, Any] | None:
        """Return a space row by id, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT * FROM intent_spaces WHERE id = ?", (space_id,)
        ).fetchone()
        return _row_to_dict(row)

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        """Return a space row by case-insensitive name, or ``None``.

        Args:
            name: The space name to look up (matched via ``COLLATE NOCASE``).
        """
        row = self._conn.execute(
            "SELECT * FROM intent_spaces WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return _row_to_dict(row)

    def get_general(self) -> dict[str, Any] | None:
        """Return the General_Space row (``is_general = 1``), or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM intent_spaces WHERE is_general = 1 LIMIT 1"
        ).fetchone()
        return _row_to_dict(row)

    def list(self) -> list[dict[str, Any]]:
        """Return all Intent_Spaces ordered by id ascending."""
        rows = self._conn.execute(
            "SELECT * FROM intent_spaces ORDER BY id ASC"
        ).fetchall()
        return _rows_to_dicts(rows)

    def update(
        self,
        space_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Update a space's name and/or description.

        Args:
            space_id: The space to update.
            name: New name, or ``None`` to leave unchanged.
            description: New description, or ``None`` to leave unchanged.
        """
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return
        params.append(space_id)
        self._conn.execute(
            f"UPDATE intent_spaces SET {', '.join(sets)} WHERE id = ?", params
        )
        self._conn.commit()

    def delete(self, space_id: int) -> None:
        """Delete a space (its keywords cascade away).

        Callers are responsible for reassigning associated documents first
        (Req 6.3) and for refusing to delete the General_Space (Req 6.7).

        Args:
            space_id: The space to delete.
        """
        self._conn.execute("DELETE FROM intent_spaces WHERE id = ?", (space_id,))
        self._conn.commit()

    def get_keywords(self, space_id: int) -> list[str]:
        """Return the keywords defined for a space, in insertion order.

        Args:
            space_id: The space whose keywords to fetch.
        """
        rows = self._conn.execute(
            "SELECT keyword FROM space_keywords WHERE space_id = ? ORDER BY id ASC",
            (space_id,),
        ).fetchall()
        return [r["keyword"] for r in rows]

    def set_keywords(self, space_id: int, keywords: Sequence[str]) -> None:
        """Replace all keywords for a space with the supplied list.

        The existing keyword set is deleted and the new list inserted in one
        transaction, so the operation is an atomic full replacement.

        Args:
            space_id: The space whose keywords to set.
            keywords: The new keyword list (order preserved on read-back).
        """
        self._conn.execute(
            "DELETE FROM space_keywords WHERE space_id = ?", (space_id,)
        )
        self._conn.executemany(
            "INSERT INTO space_keywords (space_id, keyword) VALUES (?, ?)",
            [(space_id, kw) for kw in keywords],
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsRepository:
    """Repository for the key/value ``settings`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return a setting value by key, or ``default`` if absent.

        Args:
            key: The setting key (e.g. ``confidence_threshold``).
            default: Value returned when the key is not present.
        """
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def set(self, key: str, value: str) -> None:
        """Insert or update a setting value (upsert).

        Args:
            key: The setting key.
            value: The value to store (stored as text).
        """
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Integrations status
# ---------------------------------------------------------------------------


class IntegrationRepository:
    """Repository for per-tool ``integrations`` status rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def get(self, tool: str) -> dict[str, Any] | None:
        """Return the integration row for a tool, or ``None`` if absent.

        Args:
            tool: The Frontend_Tool key (``telegram``/``teams``).
        """
        row = self._conn.execute(
            "SELECT * FROM integrations WHERE tool = ?", (tool,)
        ).fetchone()
        return _row_to_dict(row)

    def list(self) -> list[dict[str, Any]]:
        """Return all integration status rows ordered by tool name."""
        rows = self._conn.execute(
            "SELECT * FROM integrations ORDER BY tool ASC"
        ).fetchall()
        return _rows_to_dicts(rows)

    def set_status(
        self,
        tool: str,
        status: str,
        *,
        active: bool | None = None,
        last_check_ts: datetime | str | None = None,
    ) -> None:
        """Upsert a tool's status (and optionally its active flag / check ts).

        Args:
            tool: The Frontend_Tool key.
            status: New status (``Connected``/``Error``/``Disconnected``).
            active: Optional active flag; left unchanged on update when ``None``
                (defaults to ``0`` on first insert).
            last_check_ts: Timestamp of this check; defaults to now.
        """
        ts = _to_iso(last_check_ts) or _now_iso()
        existing = self.get(tool)
        if existing is None:
            self._conn.execute(
                "INSERT INTO integrations (tool, status, active, last_check_ts) "
                "VALUES (?, ?, ?, ?)",
                (tool, status, int(active) if active is not None else 0, ts),
            )
        else:
            new_active = existing["active"] if active is None else int(active)
            self._conn.execute(
                "UPDATE integrations SET status = ?, active = ?, last_check_ts = ? "
                "WHERE tool = ?",
                (status, new_active, ts, tool),
            )
        self._conn.commit()

    def set_active(self, tool: str, active: bool) -> None:
        """Set only the active flag for a tool, upserting the row if needed.

        Args:
            tool: The Frontend_Tool key.
            active: Whether the integration is active/configured.
        """
        existing = self.get(tool)
        if existing is None:
            self._conn.execute(
                "INSERT INTO integrations (tool, status, active) VALUES (?, ?, ?)",
                (tool, "Disconnected", int(active)),
            )
        else:
            self._conn.execute(
                "UPDATE integrations SET active = ? WHERE tool = ?",
                (int(active), tool),
            )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Query log
# ---------------------------------------------------------------------------


class QueryLogRepository:
    """Repository for the ``query_log`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def insert(self, entry: QueryLogEntry) -> int:
        """Insert a Query_Log entry and return its id.

        Args:
            entry: The :class:`~app.core.models.QueryLogEntry` to persist. Its
                ``id`` field is ignored on insert.

        Returns:
            The autogenerated query-log id.
        """
        cur = self._conn.execute(
            """
            INSERT INTO query_log
                (ts, query_text, detected_space_id, confidence,
                 response_status, latency_ms, tool, verified_space_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _to_iso(entry.ts),
                entry.query_text,
                entry.detected_space_id,
                entry.confidence,
                entry.response_status,
                entry.latency_ms,
                entry.tool,
                entry.verified_space_id,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get(self, query_log_id: int) -> dict[str, Any] | None:
        """Return a query-log row by id, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT * FROM query_log WHERE id = ?", (query_log_id,)
        ).fetchone()
        return _row_to_dict(row)

    def list(
        self,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        space_ids: Sequence[int] | None = None,
        tool: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List query-log entries matching all filters, newest first.

        Args:
            start: Inclusive lower bound on ``ts``.
            end: Inclusive upper bound on ``ts``.
            space_ids: Restrict to these detected Intent_Space ids. An empty
                sequence matches nothing; ``None`` applies no space filter.
            tool: Restrict to a single Frontend_Tool.
            limit: Maximum number of rows to return (default 50).

        Returns:
            Matching rows ordered by ``ts`` descending (id descending as a
            tie-breaker), capped at ``limit``.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if start is not None:
            clauses.append("ts >= ?")
            params.append(_to_iso(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(_to_iso(end))
        if tool is not None:
            clauses.append("tool = ?")
            params.append(tool)
        if space_ids is not None:
            if len(space_ids) == 0:
                return []
            placeholders = ",".join("?" for _ in space_ids)
            clauses.append(f"detected_space_id IN ({placeholders})")
            params.extend(space_ids)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM query_log"
            + where
            + " ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)

    def set_verified_space_id(self, query_log_id: int, verified_space_id: int) -> None:
        """Record an Admin's verified Intent_Space for a query (Req 10.3).

        Args:
            query_log_id: The query-log entry to annotate.
            verified_space_id: The Admin-confirmed correct Intent_Space id.
        """
        self._conn.execute(
            "UPDATE query_log SET verified_space_id = ? WHERE id = ?",
            (verified_space_id, query_log_id),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Document access
# ---------------------------------------------------------------------------


class DocumentAccessRepository:
    """Repository for the ``document_access`` table (usage counts)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def insert(
        self,
        query_log_id: int,
        document_id: int,
        *,
        ts: datetime | str | None = None,
    ) -> int:
        """Record that a document was used to answer a query.

        Args:
            query_log_id: The originating query-log entry.
            document_id: The document whose passage was used.
            ts: Access timestamp; defaults to now.

        Returns:
            The autogenerated access-row id.
        """
        cur = self._conn.execute(
            "INSERT INTO document_access (query_log_id, document_id, ts) "
            "VALUES (?, ?, ?)",
            (query_log_id, document_id, _to_iso(ts) or _now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def counts(
        self,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate access counts per document within an optional window.

        Args:
            start: Inclusive lower bound on the access ``ts``.
            end: Inclusive upper bound on the access ``ts``.
            limit: Maximum number of documents to return (highest counts
                first); ``None`` returns all.

        Returns:
            A list of dicts with ``document_id`` and ``access_count``, ordered
            by count descending then document id ascending.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(_to_iso(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(_to_iso(end))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT document_id, COUNT(*) AS access_count FROM document_access"
            + where
            + " GROUP BY document_id ORDER BY access_count DESC, document_id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Integration error log
# ---------------------------------------------------------------------------


class IntegrationErrorLogRepository:
    """Repository for the ``integration_error_log`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Store the injected connection.

        Args:
            conn: An open connection over the bootstrapped schema.
        """
        self._conn = conn

    def insert(
        self,
        tool: str,
        operation: str,
        error_detail: str,
        *,
        ts: datetime | str | None = None,
    ) -> int:
        """Record an integration error entry.

        Args:
            tool: The Frontend_Tool involved.
            operation: The operation being attempted (e.g. ``send``, ``getMe``).
            error_detail: Human-readable failure reason.
            ts: Timestamp; defaults to now.

        Returns:
            The autogenerated error-log id.
        """
        cur = self._conn.execute(
            "INSERT INTO integration_error_log (ts, tool, operation, error_detail) "
            "VALUES (?, ?, ?, ?)",
            (_to_iso(ts) or _now_iso(), tool, operation, error_detail),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_recent(
        self, *, tool: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most recent error-log entries, newest first.

        Args:
            tool: Restrict to a single Frontend_Tool, or ``None`` for all.
            limit: Maximum number of entries to return (default 50).

        Returns:
            Matching rows ordered by ``ts`` descending (id descending as a
            tie-breaker), capped at ``limit``.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if tool is not None:
            clauses.append("tool = ?")
            params.append(tool)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM integration_error_log"
            + where
            + " ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)
