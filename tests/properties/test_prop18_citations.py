# Feature: intelliknow-kms, Property 18: Citations name exactly the source documents
"""Property 18: Citations name exactly the source documents.

*For any* non-empty set of retrieved passages used for generation, the
response's citations are exactly the unique document names of those passages.

**Validates: Requirements 8.2**
"""

from __future__ import annotations

from typing import Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.models import Passage
from app.rag.generator import ResponseGenerator


class FakeChatClient:
    """A fake RAG chat client that records whether ``generate`` was called.

    Never performs network I/O; returns a fixed answer so the citation logic
    can be exercised deterministically.
    """

    def __init__(self, answer: str = "grounded answer") -> None:
        self.answer = answer
        self.called = False
        self.call_count = 0

    async def generate(self, messages: Sequence[dict]) -> str:
        self.called = True
        self.call_count += 1
        return self.answer


# Passages carry a document id paired with its name so the same document always
# maps to a single, consistent name across generated passages.
_doc_ids = st.integers(min_value=1, max_value=8)
_doc_names = st.sampled_from(
    ["HR Policy", "Salary Grid", "Legal Handbook", "Finance FAQ", "Onboarding"]
)


@st.composite
def passages(draw: st.DrawFn) -> list[Passage]:
    """Generate a non-empty list of passages with consistent doc id↔name."""
    # Assign each document id a stable name for this example.
    ids = draw(st.lists(_doc_ids, min_size=1, max_size=6, unique=True))
    id_to_name = {doc_id: draw(_doc_names) for doc_id in ids}
    # Build passages by sampling from the known documents (repeats allowed).
    chosen = draw(st.lists(st.sampled_from(ids), min_size=1, max_size=10))
    result: list[Passage] = []
    for i, doc_id in enumerate(chosen):
        result.append(
            Passage(
                chunk_id=i + 1,
                document_id=doc_id,
                document_name=id_to_name[doc_id],
                text=f"passage {i} of doc {doc_id}",
                similarity=0.9,
            )
        )
    return result


@settings(max_examples=100)
@given(passage_list=passages())
async def test_citations_are_exactly_unique_document_names(
    passage_list: list[Passage],
) -> None:
    """Citations equal the unique document names of the passages used."""
    client = FakeChatClient()
    generator = ResponseGenerator(client)

    response = await generator.generate("any question", passage_list)

    # Expected: unique document names, in first-appearance order.
    expected: list[str] = []
    seen: set[str] = set()
    for passage in passage_list:
        if passage.document_name not in seen:
            seen.add(passage.document_name)
            expected.append(passage.document_name)

    assert response.status == "success"
    # The AI model WAS called for a non-empty passage set.
    assert client.called is True
    # Citations name exactly the source documents — no more, no fewer.
    assert response.citations == expected
    assert set(response.citations) == {p.document_name for p in passage_list}
    assert len(response.citations) == len(set(response.citations))


@settings(max_examples=100)
@given(
    names=st.lists(
        st.text(min_size=1, max_size=20), min_size=1, max_size=8, unique=True
    )
)
async def test_each_distinct_document_appears_once(names: list[str]) -> None:
    """Distinct documents each contribute exactly one citation."""
    client = FakeChatClient()
    generator = ResponseGenerator(client)
    passage_list = [
        Passage(
            chunk_id=i + 1,
            document_id=i + 1,
            document_name=name,
            text="text",
            similarity=0.8,
        )
        for i, name in enumerate(names)
    ]

    response = await generator.generate("q", passage_list)

    assert response.citations == names
