"""Unit test for the demo seed script (``scripts/seed_demo.py``).

Runs the full seed flow — accept upload → process → verify searchable — for the
real sample documents against a throwaway ``DATA_DIR``, with DashScope replaced
by an offline fake so **no** network call is made. This proves the two sample
documents (HR salary policy + Finance expense policy) are ingested, associated
with their intent spaces, and become retrievable.

Validates: Requirements 15.5, 15.6, 15.7.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pytest

from app.config import load_settings
from app.kb.store import DocumentRepository, IntentSpaceRepository

# --- Load the seed script as a module from its file path -------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _PROJECT_ROOT / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo", _SEED_PATH)
assert _spec is not None and _spec.loader is not None
seed_demo = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses can resolve the module's namespace.
sys.modules["seed_demo"] = seed_demo
_spec.loader.exec_module(seed_demo)


# --- Offline DashScope fake ------------------------------------------------

# Topic term sets: a chunk/query is embedded onto a topic axis if it mentions
# any of these terms, so a matching query retrieves the matching document with
# cosine similarity 1.0 (well above the retrieval threshold) — no network.
_HR_TERMS = {
    "salary", "leave", "grade", "compensation", "parental", "sick", "hr",
}
_FIN_TERMS = {
    "expense", "reimburse", "reimbursement", "meal", "allowance", "travel",
    "mileage", "finance",
}


def _topic_vector(text: str) -> np.ndarray:
    """Return a 3-axis topic embedding: [HR, Finance, other]."""
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    vec = np.zeros(3, dtype=np.float32)
    if tokens & _HR_TERMS:
        vec[0] = 1.0
    if tokens & _FIN_TERMS:
        vec[1] = 1.0
    if not vec.any():
        vec[2] = 1.0
    return vec


class FakeDashScopeClient:
    """Offline stand-in for the DashScope client (no network).

    ``chat_completion`` returns an empty string so the processor falls back to
    a deterministic render of the *real* extracted document content, and
    ``embed`` produces a topic vector so matching queries retrieve their
    document.
    """

    def __init__(self) -> None:
        self.embed_calls = 0
        self.chat_calls = 0

    async def chat_completion(self, messages, **kwargs) -> str:
        self.chat_calls += 1
        return ""  # -> processor uses the real extracted content as fallback

    async def embed(self, texts, **kwargs):
        self.embed_calls += 1
        items = [texts] if isinstance(texts, str) else list(texts)
        return [_topic_vector(t) for t in items]


# --- Test ------------------------------------------------------------------


async def test_seed_demo_ingests_and_makes_documents_searchable(tmp_path) -> None:
    """Both samples are Processed, associated correctly, and searchable offline."""
    settings = load_settings({"DATA_DIR": str(tmp_path)})
    client = FakeDashScopeClient()

    report = await seed_demo.seed(settings, client=client)

    # Every sample document seeded and became searchable.
    assert report.ok
    assert {r.filename for r in report.results} == {
        "hr_salary_policy.md",
        "finance_expense_policy.md",
    }
    for result in report.results:
        assert result.status == "Processed"
        assert result.chunk_count >= 1
        assert result.searchable
        assert result.top_similarity > 0.0

    # The documents landed in the expected intent spaces (HR and Finance).
    conn = seed_demo.bootstrap(settings)
    try:
        spaces = IntentSpaceRepository(conn)
        documents = DocumentRepository(conn)
        by_space = {
            row["name"]: documents.list(space_id=row["id"])
            for row in spaces.list()
        }
        hr_names = {d["name"] for d in by_space["HR"]}
        finance_names = {d["name"] for d in by_space["Finance"]}
        assert "hr_salary_policy.md" in hr_names
        assert "finance_expense_policy.md" in finance_names
    finally:
        conn.close()

    # The fake was exercised (offline): structuring + embedding + verify search.
    assert client.chat_calls >= 2
    assert client.embed_calls >= 2


async def test_seed_report_formatting_is_readable() -> None:
    """The report renderer produces a non-empty human-readable summary."""
    report = seed_demo.SeedReport(
        results=[
            seed_demo.SeedResult(
                filename="hr_salary_policy.md",
                space_name="HR",
                document_id=1,
                status="Processed",
                chunk_count=3,
                searchable=True,
                top_similarity=1.0,
            )
        ]
    )
    text = seed_demo._format_report(report)
    assert "hr_salary_policy.md" in text
    assert "HR" in text
    assert report.ok
