# Feature: intelliknow-kms, Property 9: Semantic search invariant
"""Property 9: Semantic search invariant.

For any corpus of documents with mixed statuses and Intent_Space assignments,
and any query vector, a semantic search of a space ``S`` with limit ``k``:

* returns at most ``k`` passages,
* returns passages only from ``Processed`` documents associated with ``S``,
* orders them by non-increasing cosine similarity, and
* returns an empty result when ``S`` has no Processed documents.

The test uses a temporary ``DATA_DIR`` with the real SQLite bootstrap, the real
chunk store, and real FAISS — no mocks — so the invariant is checked against the
actual retrieval path.

**Validates: Requirements 5.6, 5.7, 5.9, 8.1**
"""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.config import load_settings
from app.db import bootstrap
from app.kb.search import SearchIndex
from app.kb.store import ChunkRepository, DocumentRepository, IntentSpaceRepository

DIM = 8
STATUSES = ["Pending", "Processed", "Error"]


@st.composite
def corpus(draw):
    """Generate a corpus of documents (status + space) each with chunk vectors.

    Returns a list of document specs, where each spec is a dict with ``status``,
    ``space_offset`` (0 or 1, mapped to one of two non-general spaces), and a
    list of ``float32`` embedding vectors (one per chunk).
    """
    n_docs = draw(st.integers(min_value=0, max_value=6))
    docs = []
    for _ in range(n_docs):
        status = draw(st.sampled_from(STATUSES))
        space_offset = draw(st.integers(min_value=0, max_value=1))
        n_chunks = draw(st.integers(min_value=1, max_value=4))
        vectors = draw(
            st.lists(
                st.lists(
                    st.floats(
                        min_value=-5.0,
                        max_value=5.0,
                        allow_nan=False,
                        allow_infinity=False,
                    ),
                    min_size=DIM,
                    max_size=DIM,
                ),
                min_size=n_chunks,
                max_size=n_chunks,
            )
        )
        docs.append({"status": status, "space_offset": space_offset, "vectors": vectors})
    return docs


@given(
    docs=corpus(),
    query=st.lists(
        st.floats(min_value=-5.0, max_value=5.0, allow_nan=False, allow_infinity=False),
        min_size=DIM,
        max_size=DIM,
    ),
    k=st.integers(min_value=0, max_value=8),
    target_offset=st.integers(min_value=0, max_value=1),
)
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_semantic_search_invariant(tmp_path_factory, docs, query, k, target_offset):
    """Search of a space returns ≤k Processed-only passages ordered by score."""
    data_dir = tmp_path_factory.mktemp("kms")
    settings = load_settings({"DATA_DIR": str(data_dir), "CREDENTIAL_MASTER_KEY": "x"})
    conn = bootstrap(settings)
    try:
        spaces = IntentSpaceRepository(conn)
        docs_repo = DocumentRepository(conn)
        chunks_repo = ChunkRepository(conn)

        # Two candidate spaces to exercise scoping; the target is one of them.
        space_ids = [
            spaces.create(f"Space{i}", description="") for i in range(2)
        ]
        target_space = space_ids[target_offset]

        index = SearchIndex(conn, settings)

        # Track which chunk ids legitimately belong to the target space's
        # Processed documents — the only ids allowed to appear in results.
        allowed_chunk_ids: set[int] = set()

        for spec in docs:
            space_id = space_ids[spec["space_offset"]]
            doc_id = docs_repo.create(
                name=f"doc{len(allowed_chunk_ids)}",
                format="txt",
                size_bytes=1,
                space_id=space_id,
                file_path="/tmp/x.txt",
                status=spec["status"],
            )
            ids = chunks_repo.insert_many(
                doc_id,
                [(seq, f"chunk {seq}", vec) for seq, vec in enumerate(spec["vectors"])],
            )
            if spec["status"] == "Processed" and space_id == target_space:
                allowed_chunk_ids.update(ids)

        # Build the target space index from SQLite (rebuild = source of truth).
        index.rebuild_space(target_space)

        results = index.search(target_space, np.asarray(query, dtype=np.float32), k)

        # (1) At most k passages.
        assert len(results) <= max(k, 0)

        # (2) Only Processed chunks of the target space.
        for p in results:
            assert p.chunk_id in allowed_chunk_ids

        # (3) Ordered by non-increasing similarity.
        sims = [p.similarity for p in results]
        assert all(a >= b - 1e-6 for a, b in zip(sims, sims[1:]))

        # (3b) Every returned similarity is at or above the retrieval threshold.
        assert all(p.similarity >= index.min_similarity - 1e-6 for p in results)

        # (4) No Processed docs in the space => empty result (Req 5.9).
        if not allowed_chunk_ids:
            assert results == []
    finally:
        conn.close()
