# Feature: intelliknow-kms, Property 17: Every query yields exactly one complete Query_Log entry
"""Property 17: Every query yields exactly one complete Query_Log entry.

Validates: Requirements 7.6, 10.1

For any query and any pipeline outcome — a grounded success, a no-match
fallback, a generation failure, a classification failure, or an embedding
failure — running :meth:`Orchestrator.handle_query` writes exactly one
Query_Log entry, and that entry is complete: it carries the query text, the
detected Intent_Space, the confidence, a timestamp, the originating tool, and a
response status. AI, search, and generation are fakes, so no real network I/O
happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import uuid
from datetime import datetime
from typing import Any, Sequence

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from app.analytics.service import AnalyticsService
from app.config import load_settings
from app.core.models import Passage, QueryContext
from app.core.orchestrator import Orchestrator
from app.db import bootstrap
from app.rag.generator import ResponseGenerator

# The five outcome families a query can take through the pipeline.
SCENARIOS = ["success", "no_match", "failed", "classify_fail", "embed_fail"]


class FakeAI:
    """Configurable stand-in for the classification/embedding AI seam."""

    def __init__(self) -> None:
        self.classify_json = '{"space_id": 2, "confidence": 90}'
        self.classify_raises = False
        self.embed_raises = False
        self.vector = np.ones(4, dtype=np.float32)

    async def classify(self, messages: Sequence[dict[str, Any]]) -> str:
        if self.classify_raises:
            raise RuntimeError("classify boom")
        return self.classify_json

    async def embed(self, texts):
        if self.embed_raises:
            raise RuntimeError("embed boom")
        return [self.vector]


class FakeSearch:
    """Search seam that returns a pre-set passage list."""

    def __init__(self) -> None:
        self.passages: list[Passage] = []

    def search(self, space_id: int, vector, k: int) -> list[Passage]:
        return list(self.passages)


class FakeRag:
    """RAG chat seam consumed by the real ResponseGenerator."""

    def __init__(self) -> None:
        self.raises = False
        self.answer = "grounded answer"

    async def generate(self, messages: Sequence[dict]) -> str:
        if self.raises:
            raise RuntimeError("rag boom")
        return self.answer


@contextlib.contextmanager
def fresh_orchestrator():
    """Yield an Orchestrator wired to a throwaway DB and fully faked seams."""
    with tempfile.TemporaryDirectory() as tmp:
        settings_obj = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "x"})
        conn = bootstrap(settings_obj)
        try:
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
            yield conn, orch, ai, search, rag
        finally:
            conn.close()


def _passage(doc_id: int) -> Passage:
    """A minimal retrieved passage for a given document id."""
    return Passage(
        chunk_id=doc_id * 10,
        document_id=doc_id,
        document_name=f"doc-{doc_id}",
        text="some supporting text",
        similarity=0.9,
    )


def _configure(scenario: str, ai: FakeAI, search: FakeSearch, rag: FakeRag) -> None:
    """Set up the fakes so the pipeline takes the requested outcome path."""
    # Reset to a benign baseline.
    ai.classify_raises = False
    ai.embed_raises = False
    rag.raises = False
    search.passages = [_passage(1), _passage(1), _passage(2)]

    if scenario == "success":
        pass  # passages present + rag ok → success
    elif scenario == "no_match":
        search.passages = []  # no passages → no_match (no AI generation)
    elif scenario == "failed":
        rag.raises = True  # generation raises → failed
    elif scenario == "classify_fail":
        ai.classify_raises = True  # classification failure → General, confidence 0
    elif scenario == "embed_fail":
        ai.embed_raises = True  # embedding failure → no passages → no_match


def _count_logs(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM query_log").fetchone()["n"]


@settings(max_examples=100, deadline=None)
@given(scenarios=st.lists(st.sampled_from(SCENARIOS), min_size=1, max_size=25))
def test_every_query_yields_exactly_one_complete_log(scenarios):
    """Each processed query adds exactly one complete Query_Log row."""
    with fresh_orchestrator() as (conn, orch, ai, search, rag):
        for i, scenario in enumerate(scenarios):
            _configure(scenario, ai, search, rag)
            before = _count_logs(conn)

            ctx = QueryContext(
                query_id=str(uuid.uuid4()),
                tool="telegram" if i % 2 == 0 else "teams",
                conversation_ref={"chat_id": i},
                text=f"question number {i}",
                received_at=datetime(2024, 1, 1, 0, 0, 0),
            )
            response = asyncio.run(orch.handle_query(ctx))

            after = _count_logs(conn)
            # Exactly one new entry per query, regardless of outcome.
            assert after == before + 1

            row = conn.execute(
                "SELECT * FROM query_log ORDER BY id DESC LIMIT 1"
            ).fetchone()

            # Completeness: every required field is present and well-formed.
            assert row["query_text"] == ctx.text
            assert row["tool"] == ctx.tool
            assert row["ts"] is not None and row["ts"] != ""
            assert isinstance(row["detected_space_id"], int)
            assert row["detected_space_id"] is not None
            assert row["confidence"] is not None
            assert 0.0 <= row["confidence"] <= 100.0
            assert row["response_status"] in {"Success", "Failed"}

            # Status matches the delivered response outcome.
            expected_status = "Failed" if response.status == "failed" else "Success"
            assert row["response_status"] == expected_status

            # A classification failure must be logged with confidence 0 (Req 7.8).
            if scenario == "classify_fail":
                assert row["confidence"] == 0.0

        # The total row count equals the number of queries processed.
        assert _count_logs(conn) == len(scenarios)
