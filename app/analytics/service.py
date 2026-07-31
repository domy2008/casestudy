"""Analytics service: Query_Log writes, metrics, accuracy, and CSV export.

The :class:`AnalyticsService` sits on top of the SQLite repositories defined in
:mod:`app.kb.store` and implements the Analytics_Module behaviors from the
design:

* :meth:`AnalyticsService.log_query` - persist a Query_Log entry, *never*
  raising so a persistence failure can never alter or delay the delivered
  response (Req 10.6 / 2.8).
* :meth:`AnalyticsService.history` - query-log listing filtered by time range
  and Intent_Spaces (and optionally tool), ordered by timestamp descending and
  capped at a limit (Req 10.4, 7.7).
* :meth:`AnalyticsService.error_history` - integration-error-log listing with
  the same order/limit semantics (Req 3.4).
* :meth:`AnalyticsService.top_documents` / :meth:`AnalyticsService.top_spaces` -
  usage metrics: the most accessed documents and the most common Intent_Spaces
  within a time range, ranked by count descending (Req 10.2).
* :meth:`AnalyticsService.accuracy_by_space` - per-Intent_Space classification
  accuracy over Admin-verified queries, rounded to a whole percent, ``None``
  (N/A) when a space has no verified queries (Req 10.3, 6.4).
* :meth:`AnalyticsService.export_csv` - a CSV export of the filtered query log
  plus the metrics; on failure it raises :class:`ExportError` and, being purely
  read-only, leaves stored data unchanged (Req 10.8, 10.5).
* :meth:`AnalyticsService.verify_query` - record an Admin's verified
  Intent_Space for a query, feeding the accuracy computation.

The module-level :func:`parse_exported_query_log` reverses the query-log
section of an export so the round-trip can be validated (Req 10.5).
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.core.models import QueryLogEntry
from app.kb.store import (
    DocumentAccessRepository,
    DocumentRepository,
    IntegrationErrorLogRepository,
    IntentSpaceRepository,
    QueryLogRepository,
)

__all__ = [
    "AnalyticsService",
    "Filters",
    "ExportError",
    "parse_exported_query_log",
    "QUERY_LOG_FIELDS",
]

logger = logging.getLogger(__name__)

# Column order for the query-log section of an export. Kept explicit so the
# writer and the round-trip parser agree exactly (Req 10.5).
QUERY_LOG_FIELDS: tuple[str, ...] = (
    "id",
    "ts",
    "query_text",
    "detected_space_id",
    "confidence",
    "response_status",
    "latency_ms",
    "tool",
    "verified_space_id",
)

# Section markers used inside an export file.
_QUERY_LOG_HEADER = "# Query Log"
_TOP_DOCS_HEADER = "# Top Documents"
_TOP_SPACES_HEADER = "# Top Spaces"
_ACCURACY_HEADER = "# Accuracy By Space"


class ExportError(RuntimeError):
    """Raised when producing an analytics export fails.

    Because export is a read-only operation, raising this leaves the stored
    query history unchanged (Req 10.8).
    """


@dataclass
class Filters:
    """The set of filters applied to a history listing or export.

    Attributes:
        start: Inclusive lower bound on the query timestamp, or ``None``.
        end: Inclusive upper bound on the query timestamp, or ``None``.
        space_ids: Restrict to these detected Intent_Space ids. An empty
            sequence matches nothing; ``None`` applies no space filter.
        tool: Restrict to a single Frontend_Tool, or ``None`` for all tools.
        limit: Maximum number of query-log rows to include (default 50).
    """

    start: datetime | str | None = None
    end: datetime | str | None = None
    space_ids: Sequence[int] | None = None
    tool: str | None = None
    limit: int = 50


def _row_to_entry(row: dict[str, Any]) -> QueryLogEntry:
    """Convert a ``query_log`` row dict into a :class:`QueryLogEntry`.

    Args:
        row: A row dict as returned by :class:`QueryLogRepository`.

    Returns:
        The equivalent :class:`QueryLogEntry` (``ts`` kept as its stored ISO
        string form, matching how the repository persists it).
    """
    return QueryLogEntry(
        id=row["id"],
        ts=row["ts"],
        query_text=row["query_text"],
        detected_space_id=row["detected_space_id"],
        confidence=row["confidence"],
        response_status=row["response_status"],
        latency_ms=row["latency_ms"],
        tool=row["tool"],
        verified_space_id=row["verified_space_id"],
    )


class AnalyticsService:
    """Query-log persistence, usage metrics, accuracy, and export.

    The service owns a single SQLite connection and constructs the repositories
    it needs from it, so it is the one seam callers wire up for analytics.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Build the service over an open, bootstrapped connection.

        Args:
            conn: An open :class:`sqlite3.Connection` over the bootstrapped
                schema (see :func:`app.db.bootstrap`).
        """
        self._conn = conn
        self._qlog = QueryLogRepository(conn)
        self._access = DocumentAccessRepository(conn)
        self._docs = DocumentRepository(conn)
        self._spaces = IntentSpaceRepository(conn)
        self._errors = IntegrationErrorLogRepository(conn)

    # -- Logging -----------------------------------------------------------

    def log_query(self, entry: QueryLogEntry) -> int | None:
        """Persist a Query_Log entry, swallowing any persistence failure.

        This method NEVER raises: if writing the entry fails, the error is
        logged and ``None`` is returned so the response path is completely
        unaffected (Req 10.6 / 2.8).

        Args:
            entry: The :class:`QueryLogEntry` to persist.

        Returns:
            The new query-log id, or ``None`` if persistence failed.
        """
        try:
            return self._qlog.insert(entry)
        except Exception:  # noqa: BLE001 - resilience is the whole point here.
            logger.exception("failed to persist Query_Log entry; swallowing")
            return None

    # -- Listings ----------------------------------------------------------

    def history(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        space_ids: Sequence[int] | None = None,
        tool: str | None = None,
        limit: int = 50,
    ) -> list[QueryLogEntry]:
        """Return query-log entries matching all filters, newest first.

        Entries are exactly those matching every applied filter, ordered by
        timestamp descending, and capped at ``limit`` (Req 10.4, 7.7).

        Args:
            start: Inclusive lower bound on the timestamp, or ``None``.
            end: Inclusive upper bound on the timestamp, or ``None``.
            space_ids: Restrict to these detected Intent_Space ids. An empty
                sequence matches nothing; ``None`` applies no space filter.
            tool: Restrict to a single Frontend_Tool, or ``None`` for all.
            limit: Maximum number of rows to return (default 50).

        Returns:
            The matching entries as :class:`QueryLogEntry` objects.
        """
        rows = self._qlog.list(
            start=start,
            end=end,
            space_ids=space_ids,
            tool=tool,
            limit=limit,
        )
        return [_row_to_entry(r) for r in rows]

    def error_history(
        self, tool: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most recent integration-error entries, newest first.

        Entries are exactly those matching the tool filter, ordered by
        timestamp descending, and capped at ``limit`` (Req 3.4).

        Args:
            tool: Restrict to a single Frontend_Tool, or ``None`` for all.
            limit: Maximum number of entries to return (default 50).

        Returns:
            Matching error-log rows as dicts.
        """
        return self._errors.list_recent(tool=tool, limit=limit)

    # -- Usage metrics -----------------------------------------------------

    def top_documents(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        n: int = 10,
    ) -> list[tuple[str, int]]:
        """Return the most accessed documents in a range, count descending.

        Ranking is by access count within the (inclusive) time range, then by
        document id ascending as a stable tie-break, capped at ``n`` (Req 10.2).

        Args:
            start: Inclusive lower bound on the access timestamp, or ``None``.
            end: Inclusive upper bound on the access timestamp, or ``None``.
            n: Maximum number of documents to return (default 10).

        Returns:
            A list of ``(document_name, access_count)`` tuples.
        """
        counts = self._access.counts(start=start, end=end, limit=n)
        result: list[tuple[str, int]] = []
        for row in counts:
            doc = self._docs.get(row["document_id"])
            name = doc["name"] if doc is not None else f"#{row['document_id']}"
            result.append((name, int(row["access_count"])))
        return result

    def top_spaces(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        n: int = 10,
    ) -> list[tuple[str, int]]:
        """Return the most common Intent_Spaces in a range, count descending.

        Ranking is by query count within the (inclusive) time range, then by
        detected Intent_Space id ascending as a stable tie-break, capped at
        ``n`` (Req 10.2).

        Args:
            start: Inclusive lower bound on the query timestamp, or ``None``.
            end: Inclusive upper bound on the query timestamp, or ``None``.
            n: Maximum number of spaces to return (default 10).

        Returns:
            A list of ``(space_name, query_count)`` tuples.
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
            "SELECT detected_space_id, COUNT(*) AS query_count FROM query_log"
            + where
            + " GROUP BY detected_space_id"
            + " ORDER BY query_count DESC, detected_space_id ASC LIMIT ?"
        )
        params.append(n)
        rows = self._conn.execute(sql, params).fetchall()
        result: list[tuple[str, int]] = []
        for row in rows:
            space = self._spaces.get(row["detected_space_id"])
            name = space["name"] if space is not None else f"#{row['detected_space_id']}"
            result.append((name, int(row["query_count"])))
        return result

    # -- Accuracy ----------------------------------------------------------

    def accuracy_by_space(self) -> dict[int, float | None]:
        """Compute per-Intent_Space classification accuracy over verified queries.

        For each Intent_Space, accuracy is the percentage of Admin-verified
        queries detected into that space whose detected Intent_Space matches
        the Admin-verified Intent_Space, rounded to the nearest whole percent.
        A space with no verified queries has accuracy ``None`` (displayed as
        "N/A") (Req 10.3, 6.4).

        Returns:
            A mapping from Intent_Space id to its accuracy percentage as a
            float, or ``None`` when the space has no verified queries.
        """
        denom: dict[int, int] = {}
        match: dict[int, int] = {}
        rows = self._conn.execute(
            "SELECT detected_space_id, verified_space_id FROM query_log "
            "WHERE verified_space_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            detected = row["detected_space_id"]
            verified = row["verified_space_id"]
            denom[detected] = denom.get(detected, 0) + 1
            if detected == verified:
                match[detected] = match.get(detected, 0) + 1

        result: dict[int, float | None] = {}
        for space in self._spaces.list():
            sid = space["id"]
            total = denom.get(sid, 0)
            if total == 0:
                result[sid] = None
            else:
                result[sid] = float(round(100 * match.get(sid, 0) / total))
        return result

    # -- Knowledge gaps ------------------------------------------------------

    def knowledge_gaps(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        n: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the top unanswered questions ("knowledge gaps"), count desc.

        A no-match answer is logged as Success but never writes a
        ``document_access`` row (only grounded answers do), so unanswered
        queries are exactly the Success entries with no access rows. Identical
        query texts are grouped so a frequently asked unanswered question
        ranks by how often it was asked. Integration-test traffic (tools
        ending in ``-test``) is excluded.

        Args:
            start: Inclusive lower bound on the query timestamp, or ``None``.
            end: Inclusive upper bound on the query timestamp, or ``None``.
            n: Maximum number of grouped questions to return (default 10).

        Returns:
            Dicts with ``query_text``, ``space_name`` (of the most recent
            occurrence), ``count``, and ``last_ts``, ordered by count then
            recency descending.
        """
        clauses = [
            "q.response_status = 'Success'",
            "q.tool NOT LIKE '%-test'",
            "NOT EXISTS (SELECT 1 FROM document_access a WHERE a.query_log_id = q.id)",
        ]
        params: list[Any] = []
        if start is not None:
            clauses.append("q.ts >= ?")
            params.append(_to_iso(start))
        if end is not None:
            clauses.append("q.ts <= ?")
            params.append(_to_iso(end))
        sql = (
            "SELECT q.query_text, COUNT(*) AS cnt, MAX(q.ts) AS last_ts, "
            "(SELECT detected_space_id FROM query_log q2 "
            " WHERE q2.query_text = q.query_text ORDER BY q2.ts DESC LIMIT 1) "
            " AS space_id "
            "FROM query_log q WHERE " + " AND ".join(clauses) + " "
            "GROUP BY q.query_text ORDER BY cnt DESC, last_ts DESC LIMIT ?"
        )
        params.append(n)
        rows = self._conn.execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            space = self._spaces.get(row["space_id"])
            result.append(
                {
                    "query_text": row["query_text"],
                    "space_name": space["name"] if space else f"#{row['space_id']}",
                    "count": int(row["cnt"]),
                    "last_ts": row["last_ts"],
                }
            )
        return result

    # -- End-user feedback ---------------------------------------------------

    def record_feedback(self, query_log_id: int, verdict: str) -> bool:
        """Record an End_User 👍/👎 verdict for a query, never raising.

        Args:
            query_log_id: The Query_Log row the feedback refers to.
            verdict: ``"up"`` or ``"down"``; anything else is rejected.

        Returns:
            ``True`` when a row was updated, ``False`` for an invalid verdict,
            an unknown id, or a persistence failure (which is swallowed so
            feedback can never break message handling).
        """
        if verdict not in ("up", "down"):
            return False
        try:
            cursor = self._conn.execute(
                "UPDATE query_log SET feedback = ? WHERE id = ?",
                (verdict, query_log_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0
        except Exception:  # noqa: BLE001 - feedback is strictly best-effort
            logger.exception("failed to record feedback; swallowing")
            return False

    def feedback_summary(self) -> dict[str, Any]:
        """Aggregate End_User feedback into counts and a satisfaction rate.

        Returns:
            ``{"up": int, "down": int, "satisfaction_pct": float | None}``
            where the percentage is 👍 over all verdicts rounded to a whole
            percent, or ``None`` when no feedback has been recorded.
        """
        row = self._conn.execute(
            "SELECT SUM(feedback = 'up') AS up, SUM(feedback = 'down') AS down "
            "FROM query_log WHERE feedback IS NOT NULL"
        ).fetchone()
        up = int(row["up"] or 0)
        down = int(row["down"] or 0)
        total = up + down
        pct = float(round(100 * up / total)) if total else None
        return {"up": up, "down": down, "satisfaction_pct": pct}

    # -- Verification ------------------------------------------------------

    def verify_query(self, query_log_id: int, verified_space_id: int) -> None:
        """Record an Admin's verified Intent_Space for a query (Req 10.3).

        Args:
            query_log_id: The query-log entry to annotate.
            verified_space_id: The Admin-confirmed correct Intent_Space id.
        """
        self._qlog.set_verified_space_id(query_log_id, verified_space_id)

    # -- Export ------------------------------------------------------------

    def export_csv(self, filters: Filters) -> bytes:
        """Export the filtered query log plus metrics as CSV bytes.

        The export contains four sections: the filtered query log (with a row
        count so it round-trips exactly), the top documents, the top spaces,
        and the per-space accuracy. Because the operation is read-only, any
        failure raises :class:`ExportError` and leaves stored data untouched
        (Req 10.5, 10.8).

        Args:
            filters: The :class:`Filters` describing the query-log slice and
                the metric time range.

        Returns:
            The UTF-8 encoded CSV export.

        Raises:
            ExportError: If building the export fails for any reason.
        """
        try:
            entries = self.history(
                filters.start,
                filters.end,
                filters.space_ids,
                filters.tool,
                filters.limit,
            )
            buf = io.StringIO()
            writer = csv.writer(buf)

            # Section 1: query log, prefixed with its row count so the parser
            # can read exactly N records even when fields contain newlines.
            buf.write(f"{_QUERY_LOG_HEADER}\n")
            writer.writerow(["count", len(entries)])
            writer.writerow(list(QUERY_LOG_FIELDS))
            for entry in entries:
                writer.writerow(_entry_to_export_row(entry))

            # Section 2: top documents.
            buf.write(f"{_TOP_DOCS_HEADER}\n")
            writer.writerow(["document_name", "access_count"])
            for name, count in self.top_documents(filters.start, filters.end):
                writer.writerow([name, count])

            # Section 3: top spaces.
            buf.write(f"{_TOP_SPACES_HEADER}\n")
            writer.writerow(["space_name", "query_count"])
            for name, count in self.top_spaces(filters.start, filters.end):
                writer.writerow([name, count])

            # Section 4: accuracy by space.
            buf.write(f"{_ACCURACY_HEADER}\n")
            writer.writerow(["space_id", "accuracy_percent"])
            for sid, acc in self.accuracy_by_space().items():
                writer.writerow([sid, "N/A" if acc is None else acc])

            return buf.getvalue().encode("utf-8")
        except Exception as exc:  # noqa: BLE001 - surface as an export error.
            raise ExportError(str(exc)) from exc


def _entry_to_export_row(entry: QueryLogEntry) -> list[str]:
    """Render a :class:`QueryLogEntry` as a query-log CSV row.

    ``None`` values become empty strings so they round-trip cleanly.

    Args:
        entry: The entry to render.

    Returns:
        The cell values in :data:`QUERY_LOG_FIELDS` order.
    """
    values = {
        "id": entry.id,
        "ts": entry.ts,
        "query_text": entry.query_text,
        "detected_space_id": entry.detected_space_id,
        "confidence": entry.confidence,
        "response_status": entry.response_status,
        "latency_ms": entry.latency_ms,
        "tool": entry.tool,
        "verified_space_id": entry.verified_space_id,
    }
    return ["" if values[f] is None else str(values[f]) for f in QUERY_LOG_FIELDS]


def parse_exported_query_log(data: bytes) -> list[dict[str, str]]:
    """Parse the query-log section of an export back into row dicts.

    Reverses :meth:`AnalyticsService.export_csv` for the query-log section so a
    round-trip can be validated: the returned rows are exactly the exported
    query-log entries with their field values intact (Req 10.5).

    Args:
        data: The UTF-8 encoded export bytes.

    Returns:
        A list of dicts keyed by :data:`QUERY_LOG_FIELDS`, in export order.

    Raises:
        ValueError: If the query-log section cannot be located.
    """
    # Parse with csv.reader over the whole buffer rather than splitting on
    # newlines: csv only breaks rows on true CSV terminators, so control
    # characters and quoted newlines inside a field survive intact.
    reader = csv.reader(io.StringIO(data.decode("utf-8")))

    for row in reader:
        if row == [_QUERY_LOG_HEADER]:
            break
    else:
        raise ValueError("query-log section not found in export")

    # The row after the header is "count,<N>"; the next row is the CSV header;
    # exactly N data rows follow.
    count = int(next(reader)[1])
    next(reader)  # discard the field-name header row.

    rows: list[dict[str, str]] = []
    for _ in range(count):
        cells = next(reader)
        rows.append(dict(zip(QUERY_LOG_FIELDS, cells)))
    return rows


def _to_iso(value: datetime | str) -> str:
    """Normalize a timestamp to the ISO string form used by the store.

    Args:
        value: A :class:`datetime` or an already-ISO string.

    Returns:
        The ISO string (space-separated for datetimes).
    """
    if isinstance(value, str):
        return value
    return value.isoformat(sep=" ")
