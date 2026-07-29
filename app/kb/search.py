"""FAISS-backed semantic search index manager (one index per Intent_Space).

This module implements the design's "FAISS Index Layout" and the ``SearchIndex``
interface. Embeddings persisted in SQLite (``chunks.embedding``) are the source
of truth; each Intent_Space's FAISS index is a *derived* artifact rebuilt from
SQLite whenever documents change, are deleted, or are reassigned. This makes
deletion (Req 4.8) and space reassignment (Req 6.3) trivially correct: throw
the index away and rebuild it from the authoritative rows.

Index layout (design "FAISS Index Layout"):

* One file per Intent_Space at ``{settings.faiss_dir}/space_{space_id}.index``.
* Type ``faiss.IndexIDMap2(faiss.IndexFlatIP(dim))`` — vectors are L2-normalized
  before add/search so inner product equals cosine similarity.
* The FAISS vector id is the ``chunks.id`` value, giving a direct join back to
  SQLite for mapping search hits to their chunk / document / text / name.
* Only chunks of ``Processed`` documents are ever indexed (Req 5.7).
* An empty or missing index file yields an empty result set (Req 5.9).

A ``MIN_SIMILARITY`` retrieval filter (default ``0.30``) drops below-threshold
hits so that a query with no sufficiently similar passages retrieves nothing,
which drives the no-match answer path (supports Req 8.3).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from app.config import Settings, get_settings
from app.core.models import Passage
from app.kb.store import ChunkRepository, DocumentRepository

__all__ = ["SearchIndex", "MIN_SIMILARITY"]

# Minimum cosine similarity for a retrieved passage to be kept. Hits below this
# threshold are discarded so an off-topic query returns nothing and the
# response generator takes the no-match path (supports Req 8.3).
MIN_SIMILARITY: float = 0.30


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Return an L2-normalized, contiguous ``float32`` copy of ``matrix``.

    Normalizing both stored and query vectors makes the inner product computed
    by ``IndexFlatIP`` equal to cosine similarity. Zero-norm rows are left as
    zeros (rather than producing NaNs) so degenerate embeddings are handled
    gracefully.

    Args:
        matrix: A 2-D array of shape ``(n, dim)``.

    Returns:
        A contiguous ``float32`` array of the same shape with each row scaled
        to unit L2 norm (zero rows unchanged).
    """
    mat = np.ascontiguousarray(matrix, dtype=np.float32)
    if mat.ndim != 2:
        mat = mat.reshape(1, -1) if mat.ndim == 1 else mat.reshape(mat.shape[0], -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return np.ascontiguousarray(mat / norms, dtype=np.float32)


class SearchIndex:
    """Per-Intent_Space FAISS index manager over the SQLite chunk store.

    One flat inner-product index (wrapped in an id map) is maintained per
    Intent_Space. Vectors are L2-normalized so scores are cosine similarities,
    and FAISS vector ids are ``chunks.id`` values for a direct join back to
    SQLite.

    The manager is stateless beyond its injected connection and settings: every
    operation reads or writes the on-disk index file for the relevant space, so
    it is safe to construct freely (including per request/test).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings | None = None,
        *,
        min_similarity: float = MIN_SIMILARITY,
    ) -> None:
        """Store the injected connection and resolve index paths.

        Args:
            conn: An open connection over the bootstrapped schema. Used to read
                chunk embeddings (the source of truth) and document metadata.
            settings: Optional settings snapshot; defaults to process settings.
                Its ``faiss_dir`` determines where index files live.
            min_similarity: Retrieval threshold; hits with cosine similarity
                below this value are dropped from search results.
        """
        self._conn = conn
        self._settings = settings or get_settings()
        self._chunks = ChunkRepository(conn)
        self._documents = DocumentRepository(conn)
        self.min_similarity = float(min_similarity)
        # Ensure the FAISS directory exists so writes never fail on a fresh volume.
        self._settings.faiss_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def index_path(self, space_id: int) -> Path:
        """Return the on-disk index file path for an Intent_Space.

        Args:
            space_id: The Intent_Space id.

        Returns:
            ``{faiss_dir}/space_{space_id}.index``.
        """
        return self._settings.faiss_dir / f"space_{space_id}.index"

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, space_id: int, vector: np.ndarray, k: int) -> list[Passage]:
        """Return the top-``k`` passages for a query vector within one space.

        Searches only the given Intent_Space's index (Req 5.6). Vector ids are
        mapped back to chunk/document/text/name via the SQLite store, restricted
        to chunks of ``Processed`` documents in that space (Req 5.7). Hits below
        :attr:`min_similarity` are dropped (supports Req 8.3). An empty or
        missing index file yields an empty result (Req 5.9).

        Args:
            space_id: The Intent_Space whose index to search.
            vector: The query embedding (1-D, length ``dim``). It is
                L2-normalized before search so scores are cosine similarities.
            k: Maximum number of passages to return. Values ``<= 0`` yield an
                empty result.

        Returns:
            A list of :class:`~app.core.models.Passage` ordered by
            non-increasing cosine similarity, of length at most ``k``.
        """
        if k <= 0:
            return []

        path = self.index_path(space_id)
        if not path.exists():
            return []

        index = faiss.read_index(str(path))
        if index.ntotal == 0:
            return []

        # Build the id -> chunk lookup from the authoritative store. Restricting
        # to Processed chunks of THIS space means any stale vector id in the
        # index (e.g. from a not-yet-rebuilt change) is simply skipped, so only
        # Processed documents ever surface in results (Req 5.7).
        chunk_by_id = {
            row["id"]: row
            for row in self._chunks.fetch_for_space(space_id, processed_only=True)
        }
        if not chunk_by_id:
            return []

        query = _l2_normalize(np.asarray(vector, dtype=np.float32).reshape(1, -1))
        n = min(k, index.ntotal)
        distances, ids = index.search(query, n)

        passages: list[Passage] = []
        for sim, chunk_id in zip(distances[0], ids[0]):
            cid = int(chunk_id)
            if cid == -1:
                continue  # FAISS pads with -1 when fewer than n results exist.
            if float(sim) < self.min_similarity:
                continue  # Below-threshold hit — drop it (supports Req 8.3).
            row = chunk_by_id.get(cid)
            if row is None:
                continue  # Vector id no longer maps to a Processed chunk.
            passages.append(
                Passage(
                    chunk_id=cid,
                    document_id=int(row["document_id"]),
                    document_name=row["document_name"],
                    text=row["text"],
                    # FAISS returns descending inner product; clamp to [0, 1]
                    # so tiny floating-point overshoots don't exceed 1.0.
                    similarity=float(min(1.0, max(0.0, sim))),
                )
            )
        return passages

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_document(self, document_id: int) -> None:
        """Add a Processed document's chunk vectors to its space index.

        Loads (or creates) the index for the document's Intent_Space and adds
        every chunk vector under its ``chunks.id`` vector id. Documents that are
        not ``Processed`` are ignored, since only Processed documents are ever
        indexed (Req 5.7). Documents with no chunks are a no-op.

        Args:
            document_id: The document whose chunks to index.
        """
        doc = self._documents.get(document_id)
        if doc is None or doc["status"] != "Processed":
            return

        chunks = self._chunks.fetch_by_document(document_id)
        if not chunks:
            return

        space_id = int(doc["space_id"])
        vectors, ids = self._vectors_and_ids(chunks)

        path = self.index_path(space_id)
        if path.exists():
            index = faiss.read_index(str(path))
        else:
            index = self._new_index(vectors.shape[1])
        index.add_with_ids(vectors, ids)
        self._write_atomic(index, path)

    def rebuild_space(self, space_id: int) -> None:
        """Atomically rebuild a space's index from SQLite chunk embeddings.

        The index is rebuilt from scratch using every chunk of every
        ``Processed`` document in the space (the authoritative rows in SQLite).
        The new index is written to a temporary file and then ``os.replace``-d
        over the live file so readers never observe a partial index. Used after
        document deletion (Req 4.8) and space reassignment (Req 6.3).

        If the space has no Processed chunks, any existing index file is
        removed so subsequent searches return an empty result (Req 5.9).

        Args:
            space_id: The Intent_Space whose index to rebuild.
        """
        chunks = self._chunks.fetch_for_space(space_id, processed_only=True)
        path = self.index_path(space_id)

        if not chunks:
            # Nothing to index: drop the file so search returns empty (Req 5.9).
            if path.exists():
                os.remove(path)
            return

        vectors, ids = self._vectors_and_ids(chunks)
        index = self._new_index(vectors.shape[1])
        index.add_with_ids(vectors, ids)
        self._write_atomic(index, path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _new_index(dim: int) -> faiss.Index:
        """Create an empty ``IndexIDMap2(IndexFlatIP(dim))``.

        Args:
            dim: Embedding dimensionality.

        Returns:
            A fresh FAISS index that maps external ids to normalized vectors and
            scores by inner product (cosine similarity on unit vectors).
        """
        return faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

    @staticmethod
    def _vectors_and_ids(
        chunks: list[dict[str, Any]],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Build normalized vectors and their int64 chunk ids for FAISS.

        Args:
            chunks: Chunk dicts (each with an ``embedding`` array and ``id``),
                as returned by the chunk repository.

        Returns:
            A tuple ``(vectors, ids)`` where ``vectors`` is a contiguous
            ``float32`` ``(n, dim)`` array of L2-normalized embeddings and
            ``ids`` is an ``int64`` ``(n,)`` array of chunk ids.
        """
        matrix = np.vstack([np.asarray(c["embedding"], dtype=np.float32) for c in chunks])
        vectors = _l2_normalize(matrix)
        ids = np.asarray([int(c["id"]) for c in chunks], dtype=np.int64)
        return vectors, ids

    def _write_atomic(self, index: faiss.Index, path: Path) -> None:
        """Write an index to ``path`` atomically (temp file then ``os.replace``).

        Args:
            index: The FAISS index to persist.
            path: The destination index file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        faiss.write_index(index, str(tmp))
        os.replace(tmp, path)
