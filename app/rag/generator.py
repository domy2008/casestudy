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
import re
from typing import AsyncIterator, Callable, Protocol, Sequence, runtime_checkable

from app.ai.prompts import (
    NO_MATCH_MESSAGE,
    build_rag_messages,
    rewrite_passage_references,
)
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

#: Matches an *incomplete* "[Passage N" label at the end of a text chunk —
#: any prefix of the full label the model may still be in the middle of
#: emitting (e.g. "[", "[Pass", "[Passage 1"). Used to hold back the tail of
#: a streamed delta until the label is complete enough to rewrite.
_PARTIAL_PASSAGE_REF = re.compile(
    r"\[\s*(?:p(?:a(?:s(?:s(?:a(?:g(?:e(?:\s+\d*\s*)?)?)?)?)?)?)?)?$",
    re.IGNORECASE,
)


def _rewrite_stream_chunk(
    text: str, passages: Sequence[Passage]
) -> tuple[str, str]:
    """Rewrite passage references in ``text``, holding back a partial label.

    Args:
        text: The buffered stream text to process.
        passages: The passages sent to the model, in prompt order.

    Returns:
        ``(emit, pending)`` where ``emit`` is safe to deliver now (complete
        ``[Passage N]`` labels rewritten to document names) and ``pending`` is
        a trailing chunk that may be the start of a label still being streamed.
    """
    rewritten = rewrite_passage_references(text, passages)
    partial = _PARTIAL_PASSAGE_REF.search(rewritten)
    if partial:
        return rewritten[: partial.start()], rewritten[partial.start() :]
    return rewritten, ""


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

        # The prompt forbids "[Passage N]" citations, but models occasionally
        # echo the internal labels anyway; rewrite them to document names so
        # End_Users never see references to passages they cannot read.
        answer = rewrite_passage_references(answer, passages)

        citations = self._unique_document_names(passages)
        self._record_access(passages)
        return GeneratedResponse(text=answer, citations=citations, status="success")

    async def generate_stream(
        self, query: str, passages: Sequence[Passage]
    ) -> AsyncIterator[tuple[str, str | GeneratedResponse]]:
        """Stream a response for ``query``, yielding deltas then a final result.

        Event protocol: zero or more ``("delta", text_chunk)`` events followed
        by exactly one terminal ``("done", GeneratedResponse)`` event. The
        no-match, success, and failed outcomes mirror :meth:`generate` exactly
        (same messages, statuses, citations, and access recording); only the
        delivery is incremental. A failure mid-stream still emits a delta with
        the could-not-process message so a partially rendered answer is
        visibly terminated, then the failed terminal event.

        Args:
            query: The End_User query text.
            passages: The passages retrieved for the routed Intent_Space.

        Yields:
            ``("delta", str)`` events, then one ``("done", GeneratedResponse)``.
        """
        if not passages:
            yield ("delta", NO_MATCH_MESSAGE)
            yield (
                "done",
                GeneratedResponse(
                    text=NO_MATCH_MESSAGE, citations=[], status="no_match"
                ),
            )
            return

        messages = build_rag_messages(query, list(passages))
        parts: list[str] = []
        # Hold-back buffer so a "[Passage N]" label split across deltas can
        # still be rewritten to a document name before the End_User sees it.
        pending = ""
        try:
            async for delta in self._client.generate_stream(messages):
                parts.append(delta)
                emit, pending = _rewrite_stream_chunk(pending + delta, passages)
                if emit:
                    yield ("delta", emit)
        except Exception:  # noqa: BLE001 - any AI/timeout failure → Failed (Req 8.8/8.9)
            logger.warning("streaming RAG generation failed; returning error response")
            if pending:
                yield ("delta", pending)
            notice = ("\n\n" if parts else "") + COULD_NOT_PROCESS_MESSAGE
            yield ("delta", notice)
            yield (
                "done",
                GeneratedResponse(
                    text=COULD_NOT_PROCESS_MESSAGE, citations=[], status="failed"
                ),
            )
            return

        if pending:
            yield ("delta", pending)

        citations = self._unique_document_names(passages)
        self._record_access(passages)
        yield (
            "done",
            GeneratedResponse(
                text=rewrite_passage_references("".join(parts), passages),
                citations=citations,
                status="success",
            ),
        )

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
