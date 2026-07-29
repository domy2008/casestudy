# Feature: intelliknow-kms, Property 5: Log listings respect filter, order, and limit
"""Property 5: Log listings respect filter, order, and limit.

Validates: Requirements 3.4, 7.7, 10.4

For any contents of the query log or the integration error log and any applied
filters (tool, time range, Intent_Spaces), the returned entries are exactly
those matching all filters, ordered by timestamp descending, capped at the
requested limit (50), and each entry carries all required fields.
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
from app.kb.store import IntegrationErrorLogRepository

# The four Intent_Spaces seeded at bootstrap (General=1, HR=2, Legal=3, Finance=4).
SPACE_IDS = [1, 2, 3, 4]
TOOLS = ["telegram", "teams"]
BASE = datetime(2024, 1, 1, 0, 0, 0)
LIMIT = 50


@contextlib.contextmanager
def fresh_service():
    """Yield an AnalyticsService backed by a throwaway bootstrapped DB."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "x"})
        conn = bootstrap(settings)
        try:
            yield conn, AnalyticsService(conn)
        finally:
            conn.close()


def _ts(minute: int) -> datetime:
    """Build a distinct-format timestamp at a whole-minute offset from BASE."""
    return BASE + timedelta(minutes=minute)


# --- Query log --------------------------------------------------------------

_qlog_record = st.fixed_dictionaries(
    {
        "minute": st.integers(min_value=0, max_value=300),
        "tool": st.sampled_from(TOOLS),
        "space_id": st.sampled_from(SPACE_IDS),
        "query_text": st.text(max_size=40),
        "confidence": st.floats(
            min_value=0, max_value=100, allow_nan=False, allow_infinity=False
        ),
        "status": st.sampled_from(["Success", "Failed"]),
        "latency": st.one_of(st.none(), st.integers(min_value=0, max_value=5000)),
    }
)


@settings(max_examples=100, deadline=None)
@given(
    records=st.lists(_qlog_record, max_size=60),
    start_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=310)),
    end_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=310)),
    tool_filter=st.one_of(st.none(), st.sampled_from(TOOLS)),
    space_filter=st.one_of(
        st.none(), st.lists(st.sampled_from(SPACE_IDS), max_size=4)
    ),
)
def test_query_log_listing_respects_filters_order_and_limit(
    records, start_min, end_min, tool_filter, space_filter
):
    """history() returns exactly the matching entries, ts desc, capped at 50."""
    with fresh_service() as (_conn, service):
        stored = []
        for rec in records:
            entry = QueryLogEntry(
                ts=_ts(rec["minute"]),
                query_text=rec["query_text"],
                detected_space_id=rec["space_id"],
                confidence=rec["confidence"],
                response_status=rec["status"],
                tool=rec["tool"],
                latency_ms=rec["latency"],
            )
            new_id = service.log_query(entry)
            stored.append((new_id, rec))

        start = _ts(start_min) if start_min is not None else None
        end = _ts(end_min) if end_min is not None else None

        def matches(rec) -> bool:
            ts = _ts(rec["minute"])
            if start is not None and ts < start:
                return False
            if end is not None and ts > end:
                return False
            if tool_filter is not None and rec["tool"] != tool_filter:
                return False
            if space_filter is not None and rec["space_id"] not in space_filter:
                return False
            return True

        # An empty space filter matches nothing.
        if space_filter is not None and len(space_filter) == 0:
            expected_pairs = []
        else:
            expected_pairs = [(i, r) for (i, r) in stored if matches(r)]

        # Reference order: ts descending, id descending as the tie-break.
        expected_pairs.sort(key=lambda p: (_ts(p[1]["minute"]), p[0]), reverse=True)
        expected_pairs = expected_pairs[:LIMIT]

        result = service.history(
            start=start, end=end, space_ids=space_filter, tool=tool_filter, limit=LIMIT
        )

        assert len(result) <= LIMIT
        assert [e.id for e in result] == [i for (i, _r) in expected_pairs]

        # Timestamps are non-increasing (descending order holds).
        ts_list = [e.ts for e in result]
        assert ts_list == sorted(ts_list, reverse=True)

        # Every entry carries all required fields with the stored values.
        for entry, (_i, rec) in zip(result, expected_pairs):
            assert entry.id is not None
            assert entry.ts is not None
            assert entry.query_text == rec["query_text"]
            assert entry.detected_space_id == rec["space_id"]
            assert entry.confidence == rec["confidence"]
            assert entry.response_status == rec["status"]
            assert entry.tool == rec["tool"]
            assert entry.latency_ms == rec["latency"]


# --- Integration error log --------------------------------------------------

_err_record = st.fixed_dictionaries(
    {
        "minute": st.integers(min_value=0, max_value=300),
        "tool": st.sampled_from(TOOLS),
        "operation": st.sampled_from(["send", "getMe", "poll", "token"]),
        "detail": st.text(max_size=40),
    }
)


@settings(max_examples=100, deadline=None)
@given(
    records=st.lists(_err_record, max_size=60),
    tool_filter=st.one_of(st.none(), st.sampled_from(TOOLS)),
)
def test_error_log_listing_respects_filter_order_and_limit(records, tool_filter):
    """error_history() returns exactly the matching entries, ts desc, capped at 50."""
    with fresh_service() as (conn, service):
        errors = IntegrationErrorLogRepository(conn)
        stored = []
        for rec in records:
            new_id = errors.insert(
                rec["tool"], rec["operation"], rec["detail"], ts=_ts(rec["minute"])
            )
            stored.append((new_id, rec))

        def matches(rec) -> bool:
            return tool_filter is None or rec["tool"] == tool_filter

        expected_pairs = [(i, r) for (i, r) in stored if matches(r)]
        expected_pairs.sort(key=lambda p: (_ts(p[1]["minute"]), p[0]), reverse=True)
        expected_pairs = expected_pairs[:LIMIT]

        result = service.error_history(tool=tool_filter, limit=LIMIT)

        assert len(result) <= LIMIT
        assert [row["id"] for row in result] == [i for (i, _r) in expected_pairs]

        ts_list = [row["ts"] for row in result]
        assert ts_list == sorted(ts_list, reverse=True)

        for row, (_i, rec) in zip(result, expected_pairs):
            assert row["tool"] == rec["tool"]
            assert row["operation"] == rec["operation"]
            assert row["error_detail"] == rec["detail"]
            assert row["ts"] is not None
