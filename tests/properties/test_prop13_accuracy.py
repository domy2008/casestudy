# Feature: intelliknow-kms, Property 13: Classification accuracy computation
"""Property 13: Classification accuracy computation.

Validates: Requirements 6.4, 10.3

For any set of Query_Log entries with arbitrary detected and Admin-verified
Intent_Spaces, the computed accuracy rate per space equals the percentage of
that space's verified queries whose detected space matches the verified space,
rounded to the nearest whole percent, and is N/A (None) for a space with no
verified queries.
"""

from __future__ import annotations

import contextlib
import tempfile
from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from app.analytics.service import AnalyticsService
from app.config import load_settings
from app.core.models import QueryLogEntry
from app.db import bootstrap

SPACE_IDS = [1, 2, 3, 4]
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


_record = st.fixed_dictionaries(
    {
        "detected": st.sampled_from(SPACE_IDS),
        # None means the query has not been Admin-verified.
        "verified": st.one_of(st.none(), st.sampled_from(SPACE_IDS)),
    }
)


@settings(max_examples=100, deadline=None)
@given(records=st.lists(_record, max_size=60))
def test_accuracy_matches_reference_computation(records):
    """accuracy_by_space() equals the per-space verified-match percentage."""
    with fresh_service() as service:
        for rec in records:
            service.log_query(
                QueryLogEntry(
                    ts=BASE,
                    query_text="q",
                    detected_space_id=rec["detected"],
                    confidence=50.0,
                    response_status="Success",
                    tool="telegram",
                    verified_space_id=rec["verified"],
                )
            )

        # Reference: group verified queries by detected space.
        denom: dict[int, int] = {}
        match: dict[int, int] = {}
        for rec in records:
            if rec["verified"] is None:
                continue
            d = rec["detected"]
            denom[d] = denom.get(d, 0) + 1
            if d == rec["verified"]:
                match[d] = match.get(d, 0) + 1

        expected: dict[int, float | None] = {}
        for sid in SPACE_IDS:
            total = denom.get(sid, 0)
            expected[sid] = (
                None if total == 0 else float(round(100 * match.get(sid, 0) / total))
            )

        result = service.accuracy_by_space()

        assert result == expected
        # Every seeded space is represented in the output.
        assert set(result.keys()) == set(SPACE_IDS)
        # Non-None accuracies are whole-percent values within range.
        for value in result.values():
            if value is not None:
                assert 0.0 <= value <= 100.0
                assert value == float(round(value))
