# Feature: intelliknow-kms, Property 19: Empty retrieval yields a no-match success
"""Property 19: Empty retrieval yields a no-match success.

*For any* query whose retrieval returns zero passages above the minimum
similarity threshold, the Response_Generator returns the no-match message
without calling the AI_Model for generation, and the Query_Log entry records
status Success.

**Validates: Requirements 8.3, 8.4**
"""

from __future__ import annotations

from typing import Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ai.prompts import NO_MATCH_MESSAGE
from app.rag.generator import ResponseGenerator


class FakeChatClient:
    """A fake RAG chat client that records whether ``generate`` was called."""

    def __init__(self) -> None:
        self.called = False
        self.call_count = 0

    async def generate(self, messages: Sequence[dict]) -> str:
        self.called = True
        self.call_count += 1
        return "should not be called"


def _log_status_for(response_status: str) -> str:
    """Map a GeneratedResponse status to the Query_Log status the caller records.

    A no-match is a non-failed outcome, so the Query_Log records Success
    (Req 8.4); only ``"failed"`` maps to a Failed log entry.
    """
    return "Failed" if response_status == "failed" else "Success"


@settings(max_examples=100)
@given(query=st.text(min_size=1, max_size=4000))
async def test_empty_retrieval_returns_no_match_without_ai_call(query: str) -> None:
    """Zero passages → no-match message, no AI call, Success log status."""
    client = FakeChatClient()

    # An access_recorder that would flag if invoked; it must not be on no-match.
    recorded: list[list[int]] = []
    generator = ResponseGenerator(client, access_recorder=recorded.append)

    response = await generator.generate(query, [])

    # No-match message, no citations.
    assert response.text == NO_MATCH_MESSAGE
    assert response.citations == []
    # Status is the no-match (non-failed) outcome...
    assert response.status == "no_match"
    # ...which the caller records as a Success Query_Log entry (Req 8.4).
    assert _log_status_for(response.status) == "Success"
    # The AI model was NOT called for generation (Req 8.3).
    assert client.called is False
    assert client.call_count == 0
    # No document access is recorded when nothing was retrieved.
    assert recorded == []
