# Feature: intelliknow-kms, Property 8: Deletion removes every trace of a document
"""Property 8: Deletion removes every trace of a document.

For any corpus of Processed documents across Intent_Spaces, after a document is
deleted (its rows removed and the affected space index rebuilt from SQLite):

* no semantic search in ANY space returns a passage from the deleted document,
* its chunks and embeddings are absent from the store, and
* documents that were not deleted remain fully searchable.

Uses a temporary ``DATA_DIR`` with the real SQLite bootstrap, the real store,
and real FAISS — no mocks.

**Validates: Requirements 4.8**
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


@st.composite
def corpus(draw):
    """Generate 1..6 Processed documents, each in one of two spaces with chunks."""
    n_docs = draw(st.integers(min_value=1, max_value=6))
    docs = []
    for _ in range(n_docs):
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
        docs.append({"space_offset": space_offset, "vectors": vectors})
    return docs


@given(
    docs=corpus(),
    delete_index=st.integers(min_value=0, max_value=5),
    queries=st.lists(
        st.lists(
            st.floats(min_value=-5.0, max_value=5.0, allow_nan=False, allow_infinity=False),
            min_size=DIM,
            max_size=DIM,
        ),
        min_size=1,
        max_size=3,
    ),
)
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_deletion_removes_every_trace(tmp_path_factory, docs, delete_index, queries):
    """After deletion, no search finds the document and its chunks are gone."""
    data_dir = tmp_path_factory.mktemp("kms")
    settings = load_settings({"DATA_DIR": str(data_dir), "CREDENTIAL_MASTER_KEY": "x"})
    conn = bootstrap(settings)
    try:
        spaces = IntentSpaceRepository(conn)
        docs_repo = DocumentRepository(conn)
        chunks_repo = ChunkRepository(conn)

        space_ids = [spaces.create(f"Space{i}", description="") for i in range(2)]
        index = SearchIndex(conn, settings)

        doc_ids: list[int] = []
        for i, spec in enumerate(docs):
            space_id = space_ids[spec["space_offset"]]
            doc_id = docs_repo.create(
                name=f"doc{i}",
                format="txt",
                size_bytes=1,
                space_id=space_id,
                file_path="/tmp/x.txt",
                status="Processed",
            )
            chunks_repo.insert_many(
                doc_id,
                [(seq, f"chunk {seq}", vec) for seq, vec in enumerate(spec["vectors"])],
            )
            doc_ids.append(doc_id)

        # Build every space index once from SQLite.
        for space_id in space_ids:
            index.rebuild_space(space_id)

        # Choose a real document to delete.
        victim_id = doc_ids[delete_index % len(doc_ids)]
        victim_space = int(docs_repo.get(victim_id)["space_id"])
        victim_chunk_ids = {c["id"] for c in chunks_repo.fetch_by_document(victim_id)}

        # Delete (cascades chunks) then rebuild the affected space index (Req 4.8).
        docs_repo.delete(victim_id)
        index.rebuild_space(victim_space)

        # (a) Chunks and embeddings absent from the store.
        assert chunks_repo.fetch_by_document(victim_id) == []
        assert docs_repo.get(victim_id) is None

        # (b) No search in ANY space returns a deleted chunk.
        for query in queries:
            q = np.asarray(query, dtype=np.float32)
            for space_id in space_ids:
                results = index.search(space_id, q, k=DIM * 8)
                assert all(p.chunk_id not in victim_chunk_ids for p in results)
                assert all(p.document_id != victim_id for p in results)

        # (c) A surviving document in the victim's space is still searchable
        # when queried with one of its own chunk vectors.
        survivors = [
            d for d in doc_ids
            if d != victim_id and int(docs_repo.get(d)["space_id"]) == victim_space
        ]
        if survivors:
            surv_chunks = chunks_repo.fetch_by_document(survivors[0])
            # Query with a chunk vector that has non-zero norm so its cosine
            # self-similarity is 1.0 (a zero vector normalizes to zero and would
            # fall below the retrieval threshold, which is a valid drop).
            probe = next(
                (
                    np.asarray(c["embedding"], dtype=np.float32)
                    for c in surv_chunks
                    if np.linalg.norm(np.asarray(c["embedding"], dtype=np.float32)) > 0
                ),
                None,
            )
            if probe is not None:
                # Use a large k so ties in similarity can't push the exact match
                # out of the returned window.
                hits = index.search(victim_space, probe, k=1000)
                assert any(p.document_id == survivors[0] for p in hits)
    finally:
        conn.close()
