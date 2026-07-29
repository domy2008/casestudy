# Feature: intelliknow-kms, Property 22: Usage metrics equal the reference computation
"""Property 22: Usage metrics equal the reference computation.

Validates: Requirements 10.2

For any set of Query_Log and document-access entries and any selected time
range, the computed top-10 most accessed documents and top-10 Intent_Spaces
equal a straightforward reference computation (count within range, rank
descending, take 10).
"""

from __future__ import annotations

import contextlib
import tempfile
from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.analytics.service import AnalyticsService
from app.config import load_settings
from app.core.models import QueryLogEntry
from app.db import bootstrap
from app.kb.store import DocumentAccessRepository, DocumentRepository

SPACE_IDS = [1, 2, 3, 4]
BASE = datetime(2024, 1, 1, 0, 0, 0)
NUM_DOCS = 12
TOP_N = 10


@contextlib.contextmanager
def fresh_service():
    """Yield (conn, service) backed by a throwaway bootstrapped DB."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "x"})
        conn = bootstrap(settings)
        try:
            yield conn, AnalyticsService(conn)
        finally:
            conn.close()


def _ts(minute: int) -> datetime:
    """Build a timestamp at a whole-minute offset from BASE."""
    return BASE + timedelta(minutes=minute)


def _range(start_min, end_min):
    """Convert optional minute offsets to (start, end) datetimes."""
    start = _ts(start_min) if start_min is not None else None
    end = _ts(end_min) if end_min is not None else None
    return start, end


def _in_range(minute, start_min, end_min) -> bool:
    """Whether a minute offset lies within the inclusive [start, end] window."""
    if start_min is not None and minute < start_min:
        return False
    if end_min is not None and minute > end_min:
        return False
    return True


@settings(max_examples=100, deadline=None)
@given(
    accesses=st.lists(
        st.fixed_dictionaries(
            {
                "doc": st.integers(min_value=0, max_value=NUM_DOCS - 1),
                "minute": st.integers(min_value=0, max_value=200),
            }
        ),
        max_size=80,
    ),
    start_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=210)),
    end_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=210)),
)
def test_top_documents_matches_reference(accesses, start_min, end_min):
    """top_documents() equals count-in-range, rank descending, take 10."""
    with fresh_service() as (conn, service):
        docs = DocumentRepository(conn)
        access_repo = DocumentAccessRepository(conn)

        # Create NUM_DOCS documents with distinct names in the General space.
        doc_ids = [
            docs.create(
                name=f"doc{i}",
                format="txt",
                size_bytes=1,
                space_id=1,
                file_path=f"/tmp/doc{i}.txt",
            )
            for i in range(NUM_DOCS)
        ]
        id_to_name = {doc_ids[i]: f"doc{i}" for i in range(NUM_DOCS)}

        # A single query-log row to satisfy the access FK.
        qid = service.log_query(
            QueryLogEntry(
                ts=BASE,
                query_text="q",
                detected_space_id=1,
                confidence=0.0,
                response_status="Success",
                tool="telegram",
            )
        )

        for a in accesses:
            access_repo.insert(qid, doc_ids[a["doc"]], ts=_ts(a["minute"]))

        # Reference: count accesses per document within range.
        counts: dict[int, int] = {}
        for a in accesses:
            if _in_range(a["minute"], start_min, end_min):
                did = doc_ids[a["doc"]]
                counts[did] = counts.get(did, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
        expected = [(id_to_name[did], cnt) for did, cnt in ranked]

        start, end = _range(start_min, end_min)
        result = service.top_documents(start, end, n=TOP_N)

        assert result == expected


@settings(max_examples=100, deadline=None)
@given(
    queries=st.lists(
        st.fixed_dictionaries(
            {
                "space": st.sampled_from(SPACE_IDS),
                "minute": st.integers(min_value=0, max_value=200),
            }
        ),
        max_size=80,
    ),
    start_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=210)),
    end_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=210)),
)
def test_top_spaces_matches_reference(queries, start_min, end_min):
    """top_spaces() equals count-in-range, rank descending, take 10."""
    with fresh_service() as (conn, service):
        space_names = {sid: s["name"] for s in _list_spaces(conn) for sid in [s["id"]]}

        for q in queries:
            service.log_query(
                QueryLogEntry(
                    ts=_ts(q["minute"]),
                    query_text="q",
                    detected_space_id=q["space"],
                    confidence=0.0,
                    response_status="Success",
                    tool="telegram",
                )
            )

        counts: dict[int, int] = {}
        for q in queries:
            if _in_range(q["minute"], start_min, end_min):
                counts[q["space"]] = counts.get(q["space"], 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
        expected = [(space_names[sid], cnt) for sid, cnt in ranked]

        start, end = _range(start_min, end_min)
        result = service.top_spaces(start, end, n=TOP_N)

        assert result == expected


def _list_spaces(conn):
    """Return all intent_space rows (id + name)."""
    return [dict(r) for r in conn.execute("SELECT id, name FROM intent_spaces")]
