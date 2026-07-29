"""Unit tests for the Orchestrator classification/routing/query pipeline.

Covers the nominal path (a confident classification routes to the matched
Intent_Space and yields a grounded answer), the error path (an AI classification
failure routes to the General_Space with confidence 0 and still writes exactly
one Query_Log entry), and the classification-response JSON parsing.

All external calls (classification/embedding AI and RAG generation) are faked,
so no real network I/O happens.
"""

from __future__ import annotations

import contextlib
import tempfile
import uuid
from datetime import datetime
from typing import Any, Sequence

import numpy as np

from app.analytics.service import AnalyticsService
from app.config import load_settings
from app.core.models import Classification, Passage, QueryContext
from app.core.orchestrator import Orchestrator
from app.db import bootstrap
from app.kb.store import IntentSpaceRepository, SettingsRepository
from app.rag.generator import COULD_NOT_PROCESS_MESSAGE, ResponseGenerator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAI:
    """Configurable classification/embedding AI seam."""

    def __init__(self, classify_json: str = '{"space_id": 2, "confidence": 95}') -> None:
        self.classify_json = classify_json
        self.classify_raises = False
        self.embed_raises = False
        self.vector = np.ones(4, dtype=np.float32)
        self.classify_calls = 0
        self.embed_calls = 0

    async def classify(self, messages: Sequence[dict[str, Any]]) -> str:
        self.classify_calls += 1
        if self.classify_raises:
            raise RuntimeError("classify boom")
        return self.classify_json

    async def embed(self, texts):
        self.embed_calls += 1
        if self.embed_raises:
            raise RuntimeError("embed boom")
        return [self.vector]


class FakeSearch:
    """Search seam recording the routed space it was asked about."""

    def __init__(self) -> None:
        self.passages: list[Passage] = []
        self.searched_space_id: int | None = None

    def search(self, space_id: int, vector, k: int) -> list[Passage]:
        self.searched_space_id = space_id
        return list(self.passages)


class FakeRag:
    """RAG chat seam for the real ResponseGenerator."""

    def __init__(self) -> None:
        self.raises = False
        self.answer = "grounded answer"

    async def generate(self, messages: Sequence[dict]) -> str:
        if self.raises:
            raise RuntimeError("rag boom")
        return self.answer


@contextlib.contextmanager
def build():
    """Yield a bootstrapped DB, an Orchestrator, and its fakes.

    Seeds an extra non-General Intent_Space (id 2, "HR" already seeded) so the
    classifier can route to a concrete matched space.
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings_obj = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "x"})
        conn = bootstrap(settings_obj)
        try:
            spaces = IntentSpaceRepository(conn)
            spaces.set_keywords(2, ["payroll", "leave"])  # HR keywords
            ai = FakeAI()
            search = FakeSearch()
            rag = FakeRag()
            orch = Orchestrator(
                conn=conn,
                ai_client=ai,
                search_index=search,
                generator=ResponseGenerator(rag),
                analytics=AnalyticsService(conn),
            )
            yield conn, orch, ai, search, rag, spaces
        finally:
            conn.close()


def _ctx(text: str = "how much leave do I have?") -> QueryContext:
    return QueryContext(
        query_id=str(uuid.uuid4()),
        tool="telegram",
        conversation_ref={"chat_id": 1},
        text=text,
        received_at=datetime(2024, 1, 1, 12, 0, 0),
    )


def _general_id(spaces: IntentSpaceRepository) -> int:
    return int(spaces.get_general()["id"])


# ---------------------------------------------------------------------------
# Nominal path
# ---------------------------------------------------------------------------


async def test_nominal_confident_classification_routes_to_matched_space():
    """A confident classification above threshold routes to the matched space."""
    with build() as (conn, orch, ai, search, rag, spaces):
        # Model proposes space 2 (HR) with confidence 95 >= default threshold 70.
        search.passages = [
            Passage(1, 10, "HR Handbook", "You get 20 days of leave.", 0.9)
        ]

        response = await orch.handle_query(_ctx())

        # Classification/embedding both ran concurrently (both called once).
        assert ai.classify_calls == 1
        assert ai.embed_calls == 1
        # Routed to the matched space (2), not General.
        assert search.searched_space_id == 2
        # Grounded, successful answer with citations from the used document.
        assert response.status == "success"
        assert response.text == "grounded answer"
        assert response.citations == ["HR Handbook"]

        # Exactly one Query_Log entry, detected space 2, confidence 95, Success.
        rows = conn.execute("SELECT * FROM query_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["detected_space_id"] == 2
        assert rows[0]["confidence"] == 95.0
        assert rows[0]["response_status"] == "Success"

        # A document_access row was recorded for the used document (Req 10.2).
        access = conn.execute(
            "SELECT document_id FROM document_access"
        ).fetchall()
        assert [r["document_id"] for r in access] == [10]


async def test_below_threshold_confidence_falls_back_to_general():
    """A proposed space below the threshold routes to the General_Space (Req 7.3)."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_json = '{"space_id": 2, "confidence": 40}'  # below 70

        await orch.handle_query(_ctx())

        assert search.searched_space_id == _general_id(spaces)
        row = conn.execute("SELECT * FROM query_log").fetchone()
        assert row["detected_space_id"] == _general_id(spaces)
        assert row["confidence"] == 40.0


async def test_threshold_read_per_query_applies_immediately():
    """An updated Confidence_Threshold applies to subsequent queries (Req 7.4)."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_json = '{"space_id": 2, "confidence": 60}'

        # Default threshold 70 → 60 is below → General.
        await orch.handle_query(_ctx())
        assert search.searched_space_id == _general_id(spaces)

        # Lower the threshold to 50; now 60 clears it → routes to space 2.
        SettingsRepository(conn).set("confidence_threshold", "50")
        await orch.handle_query(_ctx())
        assert search.searched_space_id == 2


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


async def test_ai_failure_routes_to_general_with_confidence_zero_and_one_log():
    """An AI classification failure → General, confidence 0, one Query_Log entry (Req 7.8)."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_raises = True
        # Even though embedding succeeds and search could return passages, the
        # failure must route to General with confidence 0.
        search.passages = []  # no passages → no_match (still a Success status)

        response = await orch.handle_query(_ctx())

        assert search.searched_space_id == _general_id(spaces)
        assert response.status == "no_match"

        rows = conn.execute("SELECT * FROM query_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["detected_space_id"] == _general_id(spaces)
        assert rows[0]["confidence"] == 0.0
        # A no-match is a successful (non-failed) outcome.
        assert rows[0]["response_status"] == "Success"


async def test_generation_failure_logs_failed_status():
    """A generation failure yields the error message and a Failed log (Req 8.9)."""
    with build() as (conn, orch, ai, search, rag, spaces):
        search.passages = [Passage(1, 10, "Doc", "text", 0.9)]
        rag.raises = True  # RAG generation fails

        response = await orch.handle_query(_ctx())

        assert response.status == "failed"
        assert response.text == COULD_NOT_PROCESS_MESSAGE
        row = conn.execute("SELECT * FROM query_log").fetchone()
        assert row["response_status"] == "Failed"
        # No document_access recorded on a failed generation.
        assert conn.execute("SELECT COUNT(*) AS n FROM document_access").fetchone()["n"] == 0


async def test_embedding_failure_yields_no_match_success():
    """An embedding failure proceeds with no passages → no-match Success."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.embed_raises = True
        search.passages = [Passage(1, 10, "Doc", "text", 0.9)]  # would match if searched

        response = await orch.handle_query(_ctx())

        assert response.status == "no_match"
        row = conn.execute("SELECT * FROM query_log").fetchone()
        assert row["response_status"] == "Success"


# ---------------------------------------------------------------------------
# Classification JSON parsing
# ---------------------------------------------------------------------------


async def test_classify_parses_well_formed_json():
    """classify() parses a clean JSON object into raw space id and confidence."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_json = '{"space_id": 2, "space_name": "HR", "confidence": 88}'
        result = await orch.classify("some hr question")
        assert isinstance(result, Classification)
        assert result.raw_space_id == 2
        assert result.confidence == 88.0


async def test_classify_tolerates_surrounding_prose_and_fences():
    """classify() extracts the JSON object even with surrounding text/fences."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_json = 'Here you go:\n```json\n{"space_id": 2, "confidence": 73}\n```'
        result = await orch.classify("q")
        assert result.raw_space_id == 2
        assert result.confidence == 73.0


async def test_classify_clamps_confidence_and_rejects_unknown_space():
    """Out-of-range confidence is clamped and an unknown space id becomes None."""
    with build() as (conn, orch, ai, search, rag, spaces):
        # space_id 999 does not exist → rejected to None; confidence clamped to 100.
        ai.classify_json = '{"space_id": 999, "confidence": 150}'
        result = await orch.classify("q")
        assert result.raw_space_id is None
        assert result.confidence == 100.0


async def test_classify_on_unparsable_response_is_a_failure_classification():
    """Unparsable classifier output yields a failure classification (Req 7.8)."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_json = "not json at all"
        result = await orch.classify("q")
        assert result.raw_space_id is None
        assert result.confidence == 0.0


async def test_classify_never_raises_on_ai_error():
    """An AI error inside classify() is caught and mapped to a failure result."""
    with build() as (conn, orch, ai, search, rag, spaces):
        ai.classify_raises = True
        result = await orch.classify("q")
        assert result.raw_space_id is None
        assert result.confidence == 0.0
