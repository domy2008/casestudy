# Feature: intelliknow-kms, Property 20: Telegram formatting respects the length limit and preserves citations
"""Property 20: Telegram formatting respects the length limit and preserves citations.

*For any* generated response of arbitrary body length and *any* citation list,
the Telegram-formatted message is at most 4,096 characters; whenever truncation
occurs, the output still contains every citation and a truncation indicator.

Validates: Requirements 8.5, 8.7

Documented last-resort behavior (see :func:`app.bots.telegram.format_telegram_message`):
the body is always truncated first so citations survive whenever the citation
footer fits within the limit. In the extreme, degenerate case where the footer
alone (plus the truncation indicator) cannot fit within 4,096 characters, the
footer itself is truncated as a last resort to honor the hard length cap, and
not all citations can be preserved. This test asserts full-citation
preservation exactly when the footer fits, and the hard length cap always.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.bots.telegram import (
    TELEGRAM_MAX_MESSAGE_CHARS,
    TRUNCATION_INDICATOR,
    build_sources_footer,
    format_telegram_message,
)
from app.core.models import GeneratedResponse

# Bodies range from empty to comfortably beyond the 4,096-char limit so both the
# no-truncation and truncation branches are exercised.
_bodies = st.text(min_size=0, max_size=8000)

# Citation lists of arbitrary document names, including empty lists and empty
# strings, matching "any citation list".
_citations = st.lists(st.text(min_size=0, max_size=60), min_size=0, max_size=12)


@settings(max_examples=200)
@given(body=_bodies, citations=_citations)
# A long body with real citations: forces body-only truncation with the footer
# preserved.
@example(body="x" * 5000, citations=["Employee Handbook.pdf", "Policy.docx"])
# A citation footer so large it alone exceeds the limit: exercises the
# documented last-resort branch where the cap still holds.
@example(body="short", citations=["c" * 500 for _ in range(20)])
# Truncation with no citations: indicator present, length capped, nothing to
# preserve.
@example(body="y" * 6000, citations=[])
def test_telegram_formatting_respects_limit_and_preserves_citations(
    body: str, citations: list[str]
) -> None:
    """Formatted output never exceeds the cap and preserves citations on truncation."""
    response = GeneratedResponse(text=body, citations=citations, status="success")
    result = format_telegram_message(response)

    # Invariant 1: the hard length cap always holds (Req 8.5).
    assert len(result) <= TELEGRAM_MAX_MESSAGE_CHARS

    footer = build_sources_footer(citations)
    naive_full = body + footer
    truncated = len(naive_full) > TELEGRAM_MAX_MESSAGE_CHARS

    if not truncated:
        # Within the limit: the message is body + footer verbatim, so every
        # citation is trivially present and no indicator is added.
        assert result == naive_full
        for citation in citations:
            assert citation in result
        return

    # Truncation occurred: a truncation indicator must be present (Req 8.7).
    assert TRUNCATION_INDICATOR in result

    footer_fits = len(footer) + len(TRUNCATION_INDICATOR) <= TELEGRAM_MAX_MESSAGE_CHARS
    if footer_fits:
        # Body-first truncation: every citation survives (Req 8.7).
        for citation in citations:
            assert citation in result
    # else: documented last-resort branch - only the hard cap (asserted above)
    # is guaranteed; full citation preservation is impossible when the footer
    # alone overflows the limit.
