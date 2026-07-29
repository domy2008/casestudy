# Feature: intelliknow-kms, Property 23: Export round trip preserves the filtered history
"""Property 23: Export round trip preserves the filtered history.

Validates: Requirements 10.5

For any Query_Log contents and any applied filters, parsing the exported file
yields exactly the set of entries matching the filters, with field values
intact.
"""

from __future__ import annotations

import contextlib
import tempfile
from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.analytics.service import (
    QUERY_LOG_FIELDS,
    AnalyticsService,
    Filters,
    parse_exported_query_log,
)
from app.config import load_settings
from app.core.models import QueryLogEntry
from app.db import bootstrap

SPACE_IDS = [1, 2, 3, 4]
TOOLS = ["telegram", "teams"]
BASE = datetime(2024, 1, 1, 0, 0, 0)


@contextlib.contextmanager
def fresh_service():
    """Yield an AnalyticsService backed by a throwaway bootstrapped DB."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "x"})
        conn = bootstrap(settings)
        try:
            yield AnalyticsService(conn)
        finally:
            conn.close()


def _ts(minute: int) -> datetime:
    """Build a timestamp at a whole-minute offset from BASE."""
    return BASE + timedelta(minutes=minute)


def _expected_row(entry: QueryLogEntry) -> dict[str, str]:
    """Render an entry the way the export serializes it (None -> empty string)."""
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
    return {f: ("" if values[f] is None else str(values[f])) for f in QUERY_LOG_FIELDS}


_record = st.fixed_dictionaries(
    {
        "minute": st.integers(min_value=0, max_value=300),
        "tool": st.sampled_from(TOOLS),
        "space_id": st.sampled_from(SPACE_IDS),
        # Exercise commas, quotes, and newlines to stress CSV round-tripping.
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
    records=st.lists(_record, max_size=40),
    start_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=310)),
    end_min=st.one_of(st.none(), st.integers(min_value=-10, max_value=310)),
    tool_filter=st.one_of(st.none(), st.sampled_from(TOOLS)),
    space_filter=st.one_of(
        st.none(), st.lists(st.sampled_from(SPACE_IDS), max_size=4)
    ),
    limit=st.integers(min_value=1, max_value=50),
)
def test_export_round_trip_preserves_filtered_history(
    records, start_min, end_min, tool_filter, space_filter, limit
):
    """Parsing the export yields exactly the filtered history, values intact."""
    with fresh_service() as service:
        for rec in records:
            service.log_query(
                QueryLogEntry(
                    ts=_ts(rec["minute"]),
                    query_text=rec["query_text"],
                    detected_space_id=rec["space_id"],
                    confidence=rec["confidence"],
                    response_status=rec["status"],
                    tool=rec["tool"],
                    latency_ms=rec["latency"],
                )
            )

        filters = Filters(
            start=_ts(start_min) if start_min is not None else None,
            end=_ts(end_min) if end_min is not None else None,
            space_ids=space_filter,
            tool=tool_filter,
            limit=limit,
        )

        # The filtered history is the reference set of entries.
        expected_entries = service.history(
            filters.start, filters.end, filters.space_ids, filters.tool, filters.limit
        )
        expected_rows = [_expected_row(e) for e in expected_entries]

        data = service.export_csv(filters)
        parsed_rows = parse_exported_query_log(data)

        assert parsed_rows == expected_rows
