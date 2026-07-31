"""Query orchestration: classify -> route -> retrieve -> generate -> log.

The :class:`Orchestrator` is the heart of the query path. Given an inbound
:class:`~app.core.models.QueryContext` it:

1. Runs **intent classification** and **query embedding concurrently**
   (``asyncio.gather``), since the embedding does not depend on the
   classification result and launching both in parallel saves latency
   (design "End-to-End Query Flow", Req 7.1).
2. Reads the Confidence_Threshold **from the settings table per query**, so an
   Admin update applies to every subsequent query immediately (Req 7.4).
3. **Routes** the classification through the pure :meth:`Orchestrator.route`
   function: a confident, model-proposed Intent_Space wins, otherwise the query
   falls back to the General_Space (Req 7.2, 7.3, 7.8).
4. **Searches** the routed Intent_Space's FAISS index for the top ``k`` passages
   (``k = 5``) using the query embedding (Req 8.1). If the embedding failed
   there is nothing to search with, so retrieval yields no passages.
5. **Generates** the grounded answer via the injected
   :class:`~app.rag.generator.ResponseGenerator` (Req 8.1-8.4).
6. Writes **exactly one** Query_Log entry per query via the injected
   :class:`~app.analytics.service.AnalyticsService` — carrying the query text,
   the detected Intent_Space, the confidence, a timestamp, the originating
   tool, the response status, and the processing latency (Req 7.6, 10.1) — and
   records a ``document_access`` row per document actually used in the answer
   (Req 10.2).

Design seams (all injected, so tests never touch the network):

* ``ai_client`` — the DashScope seam exposing ``classify(messages)`` and
  ``embed(texts)`` (see :class:`app.ai.dashscope_client.DashScopeClient`).
* ``search_index`` — the FAISS seam exposing ``search(space_id, vector, k)``
  (see :class:`app.kb.search.SearchIndex`).
* ``generator`` — a :class:`~app.rag.generator.ResponseGenerator`.
* ``analytics`` — an :class:`~app.analytics.service.AnalyticsService`.
* ``conn`` — the SQLite connection used to build the settings, Intent_Space,
  and document-access repositories.

Resilience: classification and embedding failures are contained
(classification failure => ``raw_space_id=None``/confidence ``0`` and routing to
General per Req 7.8; embedding failure => empty retrieval), and Query_Log /
document-access writes are best-effort so they never break the response path
(the analytics service already swallows its own persistence errors, Req 10.6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

import numpy as np

from app.ai.prompts import IntentSpaceSpec, build_classification_messages
from app.core.models import (
    Classification,
    GeneratedResponse,
    Passage,
    QueryContext,
    QueryLogEntry,
)

logger = logging.getLogger(__name__)

__all__ = ["Orchestrator", "DEFAULT_CONFIDENCE_THRESHOLD", "SEARCH_TOP_K"]

#: Number of passages retrieved from the routed Intent_Space (design budget).
SEARCH_TOP_K = 5

#: Fallback Confidence_Threshold used when the settings table has no value.
DEFAULT_CONFIDENCE_THRESHOLD = 70.0

#: The settings key under which the Confidence_Threshold is stored.
_THRESHOLD_KEY = "confidence_threshold"


@runtime_checkable
class ClassifyEmbedClient(Protocol):
    """Minimal AI seam consumed by the Orchestrator.

    Matches :class:`app.ai.dashscope_client.DashScopeClient`: ``classify``
    returns JSON text for a classification prompt and ``embed`` returns one
    vector per input text.
    """

    async def classify(self, messages: Sequence[dict[str, Any]]) -> str:
        """Return the classification model's JSON response text."""
        ...

    async def embed(self, texts: Any) -> list[np.ndarray]:
        """Return one embedding vector per input text."""
        ...


@runtime_checkable
class SearchSeam(Protocol):
    """Minimal search seam consumed by the Orchestrator.

    Matches :meth:`app.kb.search.SearchIndex.search`.
    """

    def search(self, space_id: int, vector: np.ndarray, k: int) -> list[Passage]:
        """Return the top-``k`` passages for ``vector`` within ``space_id``."""
        ...


@runtime_checkable
class GeneratorSeam(Protocol):
    """Minimal generator seam consumed by the Orchestrator.

    Matches :meth:`app.rag.generator.ResponseGenerator.generate`.
    """

    async def generate(
        self, query: str, passages: Sequence[Passage]
    ) -> GeneratedResponse:
        """Return the grounded response for ``query`` and ``passages``."""
        ...


class Orchestrator:
    """Classify, route, retrieve, generate, and log a single query.

    The Orchestrator can be constructed in two shapes:

    * **Routing-only** — ``Orchestrator(general_space_id=1)``. Only
      :meth:`route` (a pure function) is used; no DB or AI seams are needed.
      This is the form the routing property test wires up.
    * **Full pipeline** — inject ``conn``, ``ai_client``, ``search_index``,
      ``generator``, and ``analytics`` so :meth:`handle_query` can run the whole
      classify -> route -> retrieve -> generate -> log path.

    Args:
        conn: SQLite connection used to build the settings, Intent_Space, and
            document-access repositories. Required for :meth:`handle_query`.
        ai_client: The DashScope seam (:class:`ClassifyEmbedClient`).
        search_index: The FAISS seam (:class:`SearchSeam`).
        generator: The response generator (:class:`GeneratorSeam`).
        analytics: The analytics service used to write Query_Log entries.
        general_space_id: The General_Space id used as the routing fallback.
            When omitted it is resolved from ``conn`` (the row with
            ``is_general = 1``); if that cannot be resolved it defaults to ``1``.
        top_k: Number of passages to retrieve (default :data:`SEARCH_TOP_K`).
    """

    def __init__(
        self,
        *,
        conn: Any = None,
        ai_client: ClassifyEmbedClient | None = None,
        search_index: SearchSeam | None = None,
        generator: GeneratorSeam | None = None,
        analytics: Any = None,
        general_space_id: int | None = None,
        top_k: int = SEARCH_TOP_K,
    ) -> None:
        self._conn = conn
        self._ai_client = ai_client
        self._search_index = search_index
        self._generator = generator
        self._analytics = analytics
        self._top_k = top_k

        # Repositories are built lazily from the connection when present so the
        # routing-only construction (no conn) stays dependency-free.
        self._settings_repo = None
        self._space_repo = None
        self._access_repo = None
        if conn is not None:
            # Imported here to avoid a hard dependency for routing-only use.
            from app.kb.store import (
                DocumentAccessRepository,
                IntentSpaceRepository,
                SettingsRepository,
            )

            self._settings_repo = SettingsRepository(conn)
            self._space_repo = IntentSpaceRepository(conn)
            self._access_repo = DocumentAccessRepository(conn)

        self._general_space_id = self._resolve_general_space_id(general_space_id)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _resolve_general_space_id(self, explicit: int | None) -> int:
        """Resolve the General_Space id from the explicit arg or the store.

        Args:
            explicit: The caller-supplied General_Space id, if any.

        Returns:
            The explicit id when given; otherwise the id of the row with
            ``is_general = 1``; falling back to ``1`` if neither is available.
        """
        if explicit is not None:
            return int(explicit)
        if self._space_repo is not None:
            general = self._space_repo.get_general()
            if general is not None:
                return int(general["id"])
        return 1

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    async def classify(self, text: str) -> Classification:
        """Classify ``text`` into an Intent_Space with a confidence score.

        Builds the JSON-mode classification prompt over *all* Intent_Spaces
        (each space's name, description, and Admin keywords are injected from
        the store, Req 7.5) and asks the AI seam for a strict JSON object. The
        response is parsed into a proposed ``raw_space_id`` and a confidence in
        ``[0, 100]``.

        On any AI error/timeout, malformed JSON, or a proposed space id that is
        not a known Intent_Space, the classification is treated as a failure:
        ``raw_space_id=None`` and ``confidence=0.0`` so routing falls back to
        the General_Space (Req 7.8).

        Args:
            text: The End_User query text.

        Returns:
            A :class:`~app.core.models.Classification`. Its ``space_id`` is a
            provisional value (the proposed space, or General on failure);
            :meth:`route` computes the final assigned space.
        """
        spaces, valid_ids = self._load_space_specs()
        messages = build_classification_messages(spaces, text)
        try:
            raw = await self._ai_client.classify(messages)
        except Exception:  # noqa: BLE001 - AI error/timeout => failure (Req 7.8)
            logger.warning("classification AI call failed; routing to General")
            return self._failed_classification()

        parsed = self._parse_classification(raw, valid_ids)
        if parsed is None:
            return self._failed_classification()

        raw_space_id, confidence = parsed
        return Classification(
            space_id=raw_space_id if raw_space_id is not None else self._general_space_id,
            raw_space_id=raw_space_id,
            confidence=confidence,
        )

    def _failed_classification(self) -> Classification:
        """Return the AI-failure classification (General, confidence 0)."""
        return Classification(
            space_id=self._general_space_id, raw_space_id=None, confidence=0.0
        )

    def _load_space_specs(self) -> tuple[list[IntentSpaceSpec], set[int]]:
        """Load every Intent_Space (with keywords) as classification specs.

        Returns:
            A tuple ``(specs, valid_ids)`` where ``specs`` are the
            :class:`~app.ai.prompts.IntentSpaceSpec` values to inject into the
            prompt and ``valid_ids`` is the set of known Intent_Space ids used
            to validate the model's proposed id.
        """
        if self._space_repo is None:
            return [], set()
        specs: list[IntentSpaceSpec] = []
        valid_ids: set[int] = set()
        for space in self._space_repo.list():
            sid = int(space["id"])
            valid_ids.add(sid)
            specs.append(
                IntentSpaceSpec(
                    space_id=sid,
                    name=space["name"],
                    description=space.get("description", "") or "",
                    keywords=tuple(self._space_repo.get_keywords(sid)),
                )
            )
        return specs, valid_ids

    @staticmethod
    def _parse_classification(
        raw: str, valid_ids: set[int]
    ) -> tuple[int | None, float] | None:
        """Parse the classifier's JSON response into ``(space_id, confidence)``.

        Args:
            raw: The raw JSON text returned by the AI seam.
            valid_ids: The set of known Intent_Space ids. A proposed id outside
                this set (when the set is non-empty) is rejected.

        Returns:
            ``(raw_space_id, confidence)`` on success, where ``confidence`` is
            clamped to ``[0, 100]`` and ``raw_space_id`` is ``None`` when the
            model proposed no usable space. Returns ``None`` when the response
            cannot be parsed at all (treated by the caller as an AI failure).
        """
        data = Orchestrator._extract_json_object(raw)
        if data is None:
            return None

        # Confidence: coerce and clamp to [0, 100]; missing/invalid => 0.
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(100.0, confidence))

        # Proposed space id: must be an int and (when we know the space set) a
        # known Intent_Space; otherwise there is no usable proposal.
        raw_space_id: int | None
        space_value = data.get("space_id")
        try:
            raw_space_id = int(space_value)
        except (TypeError, ValueError):
            raw_space_id = None
        if raw_space_id is not None and valid_ids and raw_space_id not in valid_ids:
            raw_space_id = None

        return raw_space_id, confidence

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any] | None:
        """Parse a JSON object from the model output, tolerating extra text.

        The classifier is asked for a strict JSON object, but real models
        occasionally wrap it in prose or a Markdown code fence. This first tries
        a direct parse, then falls back to the substring spanning the first
        ``{`` to the last ``}``.

        Args:
            raw: The raw model response text.

        Returns:
            The decoded JSON object as a dict, or ``None`` if none can be found.
        """
        if not isinstance(raw, str):
            return None
        candidates: list[str] = [raw.strip()]
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(raw[start : end + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                return data
        return None

    # ------------------------------------------------------------------
    # Routing (pure)
    # ------------------------------------------------------------------

    def route(self, classification: Classification, threshold: float) -> int:
        """Return the Intent_Space id a classification routes to (pure).

        The model's proposed space wins if and only if the model actually
        proposed one (``raw_space_id is not None``) and its confidence is at
        least ``threshold``; otherwise the query routes to the General_Space
        (Req 7.2, 7.3, 7.8). This is a total, side-effect-free function and is
        the single place threshold logic lives.

        Args:
            classification: The classification to route.
            threshold: The Confidence_Threshold in ``[0, 100]``.

        Returns:
            The assigned Intent_Space id.
        """
        if (
            classification.raw_space_id is not None
            and classification.confidence >= threshold
        ):
            return classification.raw_space_id
        return self._general_space_id

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    async def handle_query(self, ctx: QueryContext) -> GeneratedResponse:
        """Run the full query pipeline and return the generated response.

        Classification and query embedding run concurrently; the routed space
        is searched for the top-``k`` passages; the response is generated; and
        exactly one Query_Log entry (plus a ``document_access`` row per used
        document) is written. Classification and embedding failures are
        contained so a response is always produced and always logged.

        Args:
            ctx: The validated inbound query context.

        Returns:
            The :class:`~app.core.models.GeneratedResponse` to deliver.
        """
        started = time.monotonic()

        classification, detected_space_id, passages = await self._prepare(ctx)

        # 5: generate the grounded answer (no-match / success / failed).
        response = await self._generator.generate(ctx.text, passages)

        # 6: write EXACTLY ONE Query_Log entry and record document access.
        latency_ms = int((time.monotonic() - started) * 1000)
        self._log_query(
            ctx,
            detected_space_id,
            classification.confidence,
            response,
            passages,
            latency_ms,
        )

        return response

    async def handle_query_stream(self, ctx: QueryContext):
        """Run the full query pipeline, streaming the answer as it generates.

        Identical to :meth:`handle_query` in every pipeline step and guarantee
        (same classification/routing/retrieval, exactly one Query_Log entry,
        same document-access recording) — only the generation is delivered
        incrementally via the generator's streaming path. Consumed by the web
        Test Chat SSE endpoint; the IM adapters keep :meth:`handle_query`.

        Args:
            ctx: The validated inbound query context.

        Yields:
            ``("delta", str)`` events as answer text is generated, then one
            terminal ``("done", GeneratedResponse)`` event after the Query_Log
            entry has been written.
        """
        started = time.monotonic()
        classification, detected_space_id, passages = await self._prepare(ctx)

        response: GeneratedResponse | None = None
        async for kind, payload in self._generator.generate_stream(
            ctx.text, passages
        ):
            if kind == "done":
                response = payload
                break
            yield (kind, payload)

        if response is None:  # defensive: the generator always emits "done"
            response = GeneratedResponse(
                text="", citations=[], status="failed"
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        self._log_query(
            ctx,
            detected_space_id,
            classification.confidence,
            response,
            passages,
            latency_ms,
        )
        yield ("done", response)

    async def _prepare(
        self, ctx: QueryContext
    ) -> tuple[Classification, int, list[Passage]]:
        """Run pipeline steps 1-4: classify, embed, route, and retrieve.

        Shared by :meth:`handle_query` and :meth:`handle_query_stream` so both
        paths apply identical classification, threshold routing, and retrieval
        behavior (including all failure containment).

        Args:
            ctx: The validated inbound query context.

        Returns:
            A ``(classification, detected_space_id, passages)`` triple.
        """
        # 1 + 2: classify and embed concurrently; neither is allowed to abort
        # the pipeline (return_exceptions keeps a failure of one from cancelling
        # the other).
        classification, vector = await asyncio.gather(
            self._safe_classify(ctx.text),
            self._safe_embed(ctx.text),
        )

        # 3: read the threshold per query (Req 7.4) and route (Req 7.2/7.3/7.8).
        threshold = self._read_threshold()
        detected_space_id = self.route(classification, threshold)

        # 4: retrieve — only possible when we have a query vector.
        if vector is not None and self._search_index is not None:
            try:
                passages = self._search_index.search(
                    detected_space_id, vector, self._top_k
                )
            except Exception:  # noqa: BLE001 - a search failure => no passages
                logger.warning("semantic search failed; treating as no match")
                passages = []
        else:
            passages = []

        return classification, detected_space_id, passages

    async def _safe_classify(self, text: str) -> Classification:
        """Classify ``text``, never raising (failures => General/confidence 0).

        Args:
            text: The query text.

        Returns:
            A :class:`~app.core.models.Classification`; on any unexpected error
            the AI-failure classification (Req 7.8).
        """
        try:
            return await self.classify(text)
        except Exception:  # noqa: BLE001 - defensive; classify already contains
            logger.warning("unexpected classification error; routing to General")
            return self._failed_classification()

    async def _safe_embed(self, text: str) -> np.ndarray | None:
        """Embed the query text, returning ``None`` on any failure.

        A ``None`` vector means retrieval is skipped and the query takes the
        no-match path, rather than the whole pipeline failing.

        Args:
            text: The query text.

        Returns:
            The query embedding vector, or ``None`` if embedding failed.
        """
        if self._ai_client is None:
            return None
        try:
            vectors = await self._ai_client.embed(text)
        except Exception:  # noqa: BLE001 - embedding failure => no retrieval
            logger.warning("query embedding failed; retrieval skipped")
            return None
        if not vectors:
            return None
        return vectors[0]

    def _read_threshold(self) -> float:
        """Read the Confidence_Threshold from settings, per query (Req 7.4).

        Returns:
            The stored threshold as a float, or
            :data:`DEFAULT_CONFIDENCE_THRESHOLD` when unset or unparseable.
        """
        if self._settings_repo is None:
            return DEFAULT_CONFIDENCE_THRESHOLD
        raw = self._settings_repo.get(_THRESHOLD_KEY)
        if raw is None:
            return DEFAULT_CONFIDENCE_THRESHOLD
        try:
            return float(raw)
        except (TypeError, ValueError):
            return DEFAULT_CONFIDENCE_THRESHOLD

    def _log_query(
        self,
        ctx: QueryContext,
        detected_space_id: int,
        confidence: float,
        response: GeneratedResponse,
        passages: Sequence[Passage],
        latency_ms: int,
    ) -> None:
        """Persist the single Query_Log entry and any document-access rows.

        The response status is normalized to the Query_Log vocabulary: a
        ``"failed"`` generation is logged as ``"Failed"``; ``"success"`` and
        ``"no_match"`` (a successful non-answer) are both logged as
        ``"Success"`` (Req 7.7, 10.1). On a successful, grounded answer a
        ``document_access`` row is written for each unique document whose
        passage was used (Req 10.2). All persistence is best-effort and never
        raises so it can never affect the delivered response (Req 10.6).

        Args:
            ctx: The originating query context.
            detected_space_id: The routed Intent_Space id.
            confidence: The classification confidence (``0`` on AI failure).
            response: The generated response.
            passages: The passages retrieved for (and used by) the answer.
            latency_ms: End-to-end processing latency in milliseconds.
        """
        response_status = "Failed" if response.status == "failed" else "Success"
        entry = QueryLogEntry(
            ts=datetime.now(),
            query_text=ctx.text,
            detected_space_id=detected_space_id,
            confidence=confidence,
            response_status=response_status,
            tool=ctx.tool,
            latency_ms=latency_ms,
        )
        query_log_id = None
        if self._analytics is not None:
            query_log_id = self._analytics.log_query(entry)

        # Record document access only for a genuinely answered (grounded) query
        # — one row per unique document that grounded the response (Req 10.2).
        if (
            response.status == "success"
            and query_log_id is not None
            and self._access_repo is not None
        ):
            self._record_document_access(query_log_id, passages)

    def _record_document_access(
        self, query_log_id: int, passages: Sequence[Passage]
    ) -> None:
        """Write a ``document_access`` row per unique used document.

        Any failure here is swallowed so it can never affect the already
        delivered response (Req 10.6).

        Args:
            query_log_id: The id of the Query_Log entry just written.
            passages: The passages that grounded the answer.
        """
        seen: set[int] = set()
        try:
            for passage in passages:
                if passage.document_id in seen:
                    continue
                seen.add(passage.document_id)
                self._access_repo.insert(query_log_id, passage.document_id)
        except Exception:  # noqa: BLE001 - recording must never break the response
            logger.warning("recording document_access failed; response unaffected")
