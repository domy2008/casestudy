# Feature: intelliknow-kms, Property 3: Message gate forwards exactly the valid messages
"""Property 3: Message gate forwards exactly the valid messages.

For any incoming Frontend_Tool message (text or non-text, length 0 to beyond
4,000), the message is forwarded to the Orchestrator if and only if it contains
text of 1 to 4,000 characters; otherwise a rejection message is delivered to the
originating conversation and nothing reaches the Orchestrator.

Validates: Requirements 2.1, 2.2
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.bots.base import (
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    REJECTION_MESSAGE,
    evaluate_inbound,
)

# Boundary-weighted text lengths: 0 (empty), 1 (min valid), 4000 (max valid),
# 4001 (first invalid over-limit), plus a spread of interior/exterior lengths.
_BOUNDARY_LENGTHS = st.sampled_from([0, 1, 2, 3999, 4000, 4001, 4002])
_RANDOM_LENGTHS = st.integers(min_value=0, max_value=MAX_QUERY_LENGTH + 50)

# A length drawn with heavy weight on the boundary values.
_length = st.one_of(
    _BOUNDARY_LENGTHS,
    _BOUNDARY_LENGTHS,  # doubled weight on boundaries
    _RANDOM_LENGTHS,
)


def _text_of_length(n: int) -> str:
    """Build a text payload of exactly ``n`` characters."""
    return "a" * n


# Inbound payloads: either extracted text of a chosen length, or None to model
# a non-text message (image, sticker, etc.).
_inbound = st.one_of(
    st.none(),
    _length.map(_text_of_length),
)


@settings(max_examples=200)
@given(text=_inbound)
def test_gate_forwards_iff_text_in_valid_range(text: str | None) -> None:
    """Forward iff text length in [1, 4000]; otherwise reject and forward nothing."""
    decision = evaluate_inbound(text)

    is_valid = isinstance(text, str) and MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH

    assert decision.forward is is_valid

    if is_valid:
        # Forwarded: the exact query text is carried, no rejection is produced.
        assert decision.query_text == text
        assert decision.rejection_message is None
    else:
        # Rejected: a rejection message is delivered and nothing is forwarded.
        assert decision.query_text is None
        assert decision.rejection_message == REJECTION_MESSAGE


@settings(max_examples=100)
@given(text=st.text(min_size=MIN_QUERY_LENGTH, max_size=MAX_QUERY_LENGTH))
def test_arbitrary_in_range_text_is_forwarded_unchanged(text: str) -> None:
    """Any text within the valid length range is forwarded verbatim."""
    decision = evaluate_inbound(text)
    assert decision.forward is True
    assert decision.query_text == text
    assert decision.rejection_message is None
