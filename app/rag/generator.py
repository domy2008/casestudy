"""RAG response generation: grounded answers, citations, and no-match handling.

The :class:`ResponseGenerator` is the final stage of the query pipeline. Given
an End_User query and the passages retrieved for the routed Intent_Space, it
produces a :class:`~app.core.models.GeneratedResponse` with three possible
outcomes (Req 8.1-8.4, 8.8, 8.9):

* **No match** — when retrieval yields *zero* passages, the generator returns a
  fixed no-match message with status ``"no_match"`` and does **not** call the
  AI model at all (Req 8.3, 8.4). A no-match is a *successful* (non-failed)
  outcome, so downstream the Query_Log records status Success.
* **Success** — otherwise it grounds the answer strictly in the passages via
  the Qwen-Max RAG prompt (:func:`app.ai.prompts.build_rag_messages`) and
  attaches citations equal to the unique source document names of the passages
  used (Req 8.1, 8.2). It also records a ``document_access`` event per used
  document so the analytics "most-accessed documents" metric works (Req 10.2).
* **Failed** — any AI/timeout failure on the generation path (the RAG call
  raising, including the client's 10s timeout, Req 8.8) yields a
  could-not-process error message with status ``"failed"`` (Req 8.9),
  regardless of whether other calls succeeded.

Design seams:

* The :class:`~app.ai.dashscope_client.DashScopeClient` is injected, so tests
  supply a fake that records whether ``generate()`` was called and never makes
  real network calls.
* Recording document access requires the ``query_log_id`` of the current query,
  which the Orchestrator owns (it writes the Query_Log entry before/around
  generation). Rather than reach back into the store, the generator invokes an
  optional injected ``access_recorder`` callback with the list of unique
  document ids that were used, letting the caller persist ``document_access``
  rows via :class:`~app.kb.store.DocumentAccessRepository`. A failing recorder
  never affects the returned response.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol, Sequence, runtime_checkable

from app.ai.prompts import NO_MATCH_MESSAGE, build_rag_messages
from app.core.models import GeneratedResponse, Passage

logger = logging.getLogger(__name__)

__all__ = [
    "COULD_NOT_PROCESS_MESSAGE",
    "ResponseGenerator",
    "RagChatClient",
]

#: User-facing message delivered when the generation path fails (AI error or
#: the client's 10s RAG timeout). Kept distinct from the no-match message so an
#: End_User can tell "nothing found" apart from "something went wrong"
#: (Req 8.9).
COULD_NOT_PROCESS_MESSAGE = (
    "Sorry, I couldn't process your request right now. Please try again in a "
    "few moments."
)


@runtime_checkable
class RagChatClient(Protocol):
    """Minimal seam for the chat client consumed by the generator.

    Matches :meth:`app.ai.dashscope_client.DashScopeClient.generate`: given a
    list of OpenAI-style chat messages it returns the generated answer text and
    enforces the RAG timeout budget internally (Req 8.8). Declaring the
    dependency as this protocol lets the real client be injected in production
    and a trivial fake be injected in tests.
    """

    async def generate(self, messages: Sequence[dict]) -> str:
        """Return the generated answer for ``messages``."""
        ...


class ResponseGenerator:
    """Assemble grounded RAG answers with citations and no-match handling.

    Args:
        client: The chat client seam used for RAG generation (typically a
            :class:`~app.ai.dashscope_client.DashScopeClient`). Its
            ``generate`` method must enforce the 10s RAG timeout (Req 8.8).
        access_recorder: Optional callback invoked, on a successful answer
            only, with the list of unique document ids whose passages were used
            in the response. The caller uses it to write ``document_access``
            rows (Req 10.2). Any exception it raises is swallowed so recording
            failures never alter the delivered response.
    """

    def __init__(
        self,
        client: RagChatClient,
        *,
        access_recorder: Callable[[list[int]], None] | None = None,
    ) -> None:
        self._client = client
        self._access_recorder = access_recorder

    async def generate(
        self, query: str, passages: Sequence[Passage]
    ) -> GeneratedResponse:
        """Generate a response for ``query`` grounded in ``passages``.

        Args:
            query: The End_User query text.
            passages: The passages retrieved for the routed Intent_Space. When
                empty, the no-match path is taken and the AI model is not
                called (Req 8.3, 8.4).

        Returns:
            A :class:`~app.core.models.GeneratedResponse`:

            * ``status="no_match"`` with :data:`~app.ai.prompts.NO_MATCH_MESSAGE`
              and no citations when ``passages`` is empty (a non-failed, i.e.
              Success, outcome).
            * ``status="success"`` with the grounded answer and citations equal
              to the unique source document names of ``passages`` on success.
            * ``status="failed"`` with :data:`COULD_NOT_PROCESS_MESSAGE` and no
              citations if the generation call raises or times out (Req 8.8,
              8.9), regardless of any other call's outcome.
        """
        # No-match path: zero passages → fixed message, no AI call (Req 8.3/8.4).
        if not passages:
            return GeneratedResponse(
                text=NO_MATCH_MESSAGE, citations=[], status="no_match"
            )

        messages = build_rag_messages(query, list(passages))
        try:
            answer = await self._client.generate(messages)
        except Exception:  # noqa: BLE001 - any AI/timeout failure → Failed (Req 8.8/8.9)
            logger.warning("RAG generation failed; returning error response")
            return GeneratedResponse(
                text=COULD_NOT_PROCESS_MESSAGE, citations=[], status="failed"
            )

        citations = self._unique_document_names(passages)
        self._record_access(passages)
        return GeneratedResponse(text=answer, citations=citations, status="success")

    @staticmethod
    def _unique_document_names(passages: Sequence[Passage]) -> list[str]:
        """Return the unique source document names, first-appearance order.

        These are exactly the citations for the response — the set of source
        documents whose passages grounded the answer (Req 8.2, Property 18).

        Args:
            passages: The passages used for generation.

        Returns:
            The de-duplicated document names, ordered by first occurrence.
        """
        seen: set[str] = set()
        names: list[str] = []
        for passage in passages:
            if passage.document_name not in seen:
                seen.add(passage.document_name)
                names.append(passage.document_name)
        return names

    def _record_access(self, passages: Sequence[Passage]) -> None:
        """Record a document_access event per unique used document (Req 10.2).

        Invokes the injected ``access_recorder`` with the unique document ids
        of the used passages. Any failure is logged and swallowed so it never
        affects the delivered response.

        Args:
            passages: The passages used for generation.
        """
        if self._access_recorder is None:
            return
        seen: set[int] = set()
        document_ids: list[int] = []
        for passage in passages:
            if passage.document_id not in seen:
                seen.add(passage.document_id)
                document_ids.append(passage.document_id)
        try:
            self._access_recorder(document_ids)
        except Exception:  # noqa: BLE001 - recording must never break the response
            logger.warning("recording document_access failed; response unaffected")
