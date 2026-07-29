# Feature: intelliknow-kms, Property 12: Space deletion reassigns all documents to General
"""Property-based test for Intent_Space deletion reassignment.

**Property 12: Space deletion reassigns all documents to General**

For any non-General Intent_Space holding any number of documents, deleting the
space reassigns every one of its documents to the General_Space and rebuilds
the indexes so those documents remain searchable in General afterwards.

**Validates: Requirements 6.3**

The test builds a throwaway bootstrapped database and FAISS index directory per
example, seeds a non-General space with Processed documents (each with a chunk
embedding), deletes the space through the real ``DELETE /spaces/{id}`` endpoint,
then verifies every document now belongs to General and is retrievable from
General's rebuilt index.
"""

from __future__ import annotations

import sqlite3
import tempfile

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.api import admin
from app.config import load_settings
from app.db import bootstrap
from app.kb.search import SearchIndex
from app.kb.store import ChunkRepository, DocumentRepository, IntentSpaceRepository

_EMBED_DIM = 4


def _build(tmp: str):
    """Bootstrap a temp DB + settings and return (settings, connection)."""
    settings_obj = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "unused"})
    bootstrap(settings_obj).close()
    conn = sqlite3.connect(str(settings_obj.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return settings_obj, conn


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    num_docs=st.integers(min_value=1, max_value=5),
    suffix=st.text(alphabet="abcXYZ", min_size=0, max_size=6),
)
def test_deleting_space_reassigns_documents_to_general(
    num_docs: int, suffix: str
) -> None:
    """Deleted-space documents move to General and stay searchable there."""
    with tempfile.TemporaryDirectory() as tmp:
        settings_obj, conn = _build(tmp)
        try:
            spaces = IntentSpaceRepository(conn)
            docs = DocumentRepository(conn)
            chunks = ChunkRepository(conn)
            si = SearchIndex(conn, settings_obj)

            general_id = int(spaces.get_general()["id"])
            space_id = spaces.create("z" + suffix + "_space", description="temp")

            embedding = np.ones(_EMBED_DIM, dtype=np.float32)
            doc_ids: list[int] = []
            for i in range(num_docs):
                doc_id = docs.create(
                    name=f"doc-{i}",
                    format="txt",
                    size_bytes=10,
                    space_id=space_id,
                    file_path=f"/tmp/doc-{i}.txt",
                    status="Processed",
                )
                chunks.insert(doc_id, 0, f"content {i}", embedding)
                si.add_document(doc_id)
                doc_ids.append(doc_id)

            app = FastAPI()
            app.include_router(admin.router)
            app.dependency_overrides[admin.get_connection] = lambda: conn
            app.dependency_overrides[admin.get_settings_dependency] = lambda: settings_obj
            client = TestClient(app)

            resp = client.delete(f"/spaces/{space_id}")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["reassigned_to"] == general_id
            assert body["reassigned_count"] == num_docs

            # Every document now belongs to General (Req 6.3).
            for doc_id in doc_ids:
                assert int(docs.get(doc_id)["space_id"]) == general_id
            # The deleted space is gone.
            assert spaces.get(space_id) is None

            # The reassigned documents remain searchable in General.
            passages = si.search(general_id, embedding, k=num_docs + 5)
            found = {p.document_id for p in passages}
            assert set(doc_ids).issubset(found)
        finally:
            conn.close()
