# Feature: intelliknow-kms, Property 14: Threshold routing is total and correct
"""Property 14: Threshold routing is total and correct.

Validates: Requirements 7.2, 7.3, 7.8

For any Classification — including the AI-failure case (``raw_space_id`` is
``None`` and confidence ``0``) — and any threshold in ``[0, 100]``,
:meth:`Orchestrator.route` returns the model's assigned Intent_Space if and only
if the model actually proposed one (``raw_space_id is not None``) AND its
confidence is at least the threshold; otherwise it returns the General_Space id.
``route`` is a pure, total function, so it is property-tested directly.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.models import Classification
from app.core.orchestrator import Orchestrator

GENERAL_ID = 1
# A pool of non-General Intent_Space ids the model might propose.
PROPOSABLE_IDS = [2, 3, 4, 5]


def _router() -> Orchestrator:
    """An Orchestrator with only the General_Space id wired up for routing.

    ``route`` depends on nothing else, so no DB or AI seams are needed.
    """
    return Orchestrator(general_space_id=GENERAL_ID)


_classifications = st.builds(
    Classification,
    # space_id is provisional and irrelevant to route(); keep it arbitrary.
    space_id=st.sampled_from([GENERAL_ID, *PROPOSABLE_IDS]),
    raw_space_id=st.one_of(st.none(), st.sampled_from(PROPOSABLE_IDS)),
    confidence=st.floats(min_value=0.0, max_value=100.0),
)


@settings(max_examples=200)
@given(
    classification=_classifications,
    threshold=st.floats(min_value=0.0, max_value=100.0),
)
def test_route_is_total_and_correct(classification, threshold):
    """route() returns the assigned space iff assigned and confident, else General."""
    result = _router().route(classification, threshold)

    should_assign = (
        classification.raw_space_id is not None
        and classification.confidence >= threshold
    )
    expected = classification.raw_space_id if should_assign else GENERAL_ID

    assert result == expected
    # Totality: the result is always a valid integer space id.
    assert isinstance(result, int)


@settings(max_examples=100)
@given(threshold=st.floats(min_value=0.0, max_value=100.0))
def test_ai_failure_always_routes_to_general(threshold):
    """An AI-failure classification (raw None, confidence 0) always routes to General (Req 7.8)."""
    failure = Classification(space_id=GENERAL_ID, raw_space_id=None, confidence=0.0)
    assert _router().route(failure, threshold) == GENERAL_ID


@settings(max_examples=100)
@given(
    space_id=st.sampled_from(PROPOSABLE_IDS),
    confidence=st.floats(min_value=0.0, max_value=100.0),
)
def test_confidence_equal_to_threshold_assigns(space_id, confidence):
    """Confidence exactly at the threshold routes to the assigned space (>= boundary)."""
    classification = Classification(
        space_id=space_id, raw_space_id=space_id, confidence=confidence
    )
    # Threshold equals confidence → the >= comparison must assign the space.
    assert _router().route(classification, confidence) == space_id
