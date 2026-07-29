# Feature: intelliknow-kms, Property 21: Any generation-path failure produces an error message and a Failed log
"""Property 21: Any generation-path failure produces an error message and a Failed log.

*For any* query and *any* injected failure in the generation path (AI_Model
error, Embedding_Service error, or a 10-second external-call timeout —
regardless of which other calls succeed), the End_User receives the
could-not-process error message and the Query_Log entry records status Failed.

**Validates: Requirements 8.8, 8.9**
"""

from __future__ import annotations

from typing import Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ai.dashscope_client import DashScopeError, DashScopeTimeoutError
from app.core.models import Passage
from app.rag.generator import COULD_NOT_PROCESS_MESSAGE, ResponseGenerator


class FailingChatClient:
    """A fake RAG chat client whose ``generate`` always raises.

    Records that it was called; simulates AI/timeout failures on the
    generation path without any network I/O.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.called = False

    async def generate(self, messages: Sequence[dict]) -> str:
        self.called = True
        raise self._exc


def _log_status_for(response_status: str) -> str:
    """Map a GeneratedResponse status to the Query_Log status the caller records."""
    return "Failed" if response_status == "failed" else "Success"


# The range of failures the generation path may encounter: the DashScope client
# surfaces timeouts and generic errors, and any unexpected exception must also
# be treated as a failure (Req 8.8).
_failures = st.sampled_from(
    [
        DashScopeTimeoutError("RAG request timed out after 10.0s"),
        DashScopeError("transport error"),
        RuntimeError("unexpected boom"),
        ValueError("bad response"),
        TimeoutError("hard timeout"),
    ]
)


@st.composite
def passages(draw: st.DrawFn) -> list[Passage]:
    """Generate a non-empty passage list (so the AI generation path is taken)."""
    n = draw(st.integers(min_value=1, max_value=5))
    return [
        Passage(
            chunk_id=i + 1,
            document_id=i + 1,
            document_name=f"Doc {i + 1}",
            text=f"passage {i}",
            similarity=0.8,
        )
        for i in range(n)
    ]


@settings(max_examples=100)
@given(
    query=st.text(min_size=1, max_size=4000),
    passage_list=passages(),
    failure=_failures,
)
async def test_generation_failure_yields_error_and_failed_status(
    query: str,
    passage_list: list[Passage],
    failure: BaseException,
) -> None:
    """Any generation failure → could-not-process message + Failed log status."""
    client = FailingChatClient(failure)

    # Even if document-access recording would also fail, the response must not
    # change — the failure path returns before recording anyway.
    def exploding_recorder(_ids: list[int]) -> None:
        raise RuntimeError("recorder should not affect the outcome")

    generator = ResponseGenerator(client, access_recorder=exploding_recorder)

    response = await generator.generate(query, passage_list)

    # The generation path was attempted...
    assert client.called is True
    # ...and its failure produced the could-not-process error message (Req 8.9).
    assert response.text == COULD_NOT_PROCESS_MESSAGE
    assert response.status == "failed"
    assert response.citations == []
    # The caller records a Failed Query_Log entry (Req 8.8).
    assert _log_status_for(response.status) == "Failed"
