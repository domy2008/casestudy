"""Document ingestion pipeline (parse → structure → chunk → embed → index).

This module implements the design's "Document Ingestion Pipeline". A document
starts life as a ``Pending`` row (created by the upload service) with its
original file saved under ``/data/uploads``. :class:`DocumentProcessor` then
takes it through the full pipeline:

``Pending`` → deterministic loader extraction (:func:`app.kb.loaders.load_document`)
→ Qwen-Max structuring (:func:`app.ai.prompts.build_structuring_messages` +
:meth:`DashScopeClient.chat_completion`, rendering tables as GitHub-markdown)
→ chunking (~800 tokens, 100-token overlap, markdown tables never split)
→ batched embeddings (:meth:`DashScopeClient.embed`)
→ persist chunks + vectors (:class:`app.kb.store.ChunkRepository`)
→ ``Processed`` + add to the space FAISS index
(:meth:`app.kb.search.SearchIndex.add_document`).

Failure handling follows the requirements exactly:

* **Parse failure or the 10-minute (600s) parse deadline expiring** → Status
  ``Error`` + an error-log entry with the failure reason (Req 5.4, 4.10).
* **Unsupported format** (:class:`~app.kb.loaders.UnsupportedFormatError`) →
  Status ``Error`` + an error-log entry naming the unsupported format
  (Req 5.8).
* **Embedding failure after a successful parse** → Status stays ``Pending`` +
  an error-log entry, so the Admin can retry via the Update action (Req 5.5).

The DashScope client is injected (the single AI mocking seam), so tests supply
a fake and never make network calls. Background scheduling — starting
processing within 5 seconds of upload (Req 5.1) — is wired in ``main.py``;
this module simply exposes the awaitable :meth:`DocumentProcessor.process`
coroutine plus a convenience :meth:`DocumentProcessor.schedule` helper.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

import numpy as np

from app.ai.prompts import build_structuring_messages
from app.config import Settings, get_settings
from app.core.models import ExtractedContent
from app.kb.loaders import (
    DocumentParseError,
    UnsupportedFormatError,
    load_document,
)
from app.kb.search import SearchIndex
from app.kb.store import (
    ChunkRepository,
    DocumentRepository,
    IntegrationErrorLogRepository,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DocumentProcessor",
    "chunk_structured_text",
    "PARSE_DEADLINE_S",
    "STRUCTURING_TIMEOUT_S",
    "CHUNK_TARGET_TOKENS",
    "CHUNK_OVERLAP_TOKENS",
    "EMBED_BATCH_SIZE",
    "ERROR_LOG_TOOL",
]

#: Hard deadline for the parse + structure + chunk stage (Req 5.4: 10 minutes).
PARSE_DEADLINE_S: float = 600.0

#: Per-call timeout for the AI structuring step (kept well under the deadline).
STRUCTURING_TIMEOUT_S: float = 120.0

#: Approximate chunk size and overlap, measured in whitespace tokens.
CHUNK_TARGET_TOKENS: int = 800
CHUNK_OVERLAP_TOKENS: int = 100

#: Number of chunk texts embedded per batched DashScope request.
EMBED_BATCH_SIZE: int = 16

#: ``tool`` value used for document-processing entries in the shared
#: ``integration_error_log`` table (the design reuses that log for pipeline
#: failure reasons, Req 5.4/5.5/5.8).
ERROR_LOG_TOOL: str = "knowledge_base"


@runtime_checkable
class ChatEmbedClient(Protocol):
    """Minimal AI-client seam consumed by the document processor.

    Declaring only the two methods the pipeline needs lets the real
    :class:`app.ai.dashscope_client.DashScopeClient` be injected in production
    while tests supply a trivial fake that makes no network calls.
    """

    async def chat_completion(
        self, messages: Sequence[dict[str, Any]], **kwargs: Any
    ) -> str:
        """Return the assistant message content for a chat prompt."""
        ...

    async def embed(
        self, texts: str | Iterable[str], **kwargs: Any
    ) -> list[np.ndarray]:
        """Return one embedding vector per input text, in input order."""
        ...


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _split_into_blocks(text: str) -> list[tuple[str, str]]:
    """Split structured markdown into ``("text" | "table", content)`` blocks.

    Contiguous lines that (after stripping) begin with ``|`` are grouped into a
    single ``table`` block so a markdown table is never broken up; every other
    run of lines forms a ``text`` block.

    Args:
        text: The structured markdown produced by the AI structuring step.

    Returns:
        An ordered list of ``(kind, content)`` blocks covering the whole input.
    """
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []
    buf_is_table = False

    def flush() -> None:
        nonlocal buf
        if buf:
            blocks.append(("table" if buf_is_table else "text", "\n".join(buf)))
            buf = []

    for line in text.splitlines():
        is_table_line = line.strip().startswith("|")
        if buf and is_table_line != buf_is_table:
            flush()
        buf_is_table = is_table_line
        buf.append(line)
    flush()
    return blocks


def _window_words(
    words: list[str], target_tokens: int, overlap_tokens: int
) -> list[list[str]]:
    """Slice a word list into overlapping windows of at most ``target_tokens``.

    Args:
        words: The tokenized text run.
        target_tokens: Maximum tokens (words) per window.
        overlap_tokens: Tokens shared between consecutive windows.

    Returns:
        A list of word windows; a single window when the run fits in one chunk.
    """
    if not words:
        return []
    if len(words) <= target_tokens:
        return [words]
    step = max(1, target_tokens - max(0, overlap_tokens))
    windows: list[list[str]] = []
    i = 0
    n = len(words)
    while i < n:
        windows.append(words[i : i + target_tokens])
        if i + target_tokens >= n:
            break
        i += step
    return windows


def chunk_structured_text(
    structured: str,
    *,
    target_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    """Chunk structured markdown into ~``target_tokens`` windows with overlap.

    Text is packed into overlapping windows of roughly ``target_tokens``
    whitespace tokens (with ``overlap_tokens`` of overlap between consecutive
    windows). Markdown tables emitted by the structuring step are detected and
    kept intact as their own single chunk, so a table is never split across
    chunks (design "Document Ingestion Pipeline"; Req 5.2).

    Args:
        structured: The cleaned markdown document to chunk.
        target_tokens: Approximate maximum tokens per text chunk.
        overlap_tokens: Token overlap carried between consecutive text chunks.

    Returns:
        The list of chunk texts in document order (empty when the input has no
        content).
    """
    chunks: list[str] = []
    text_run: list[str] = []

    def flush_text_run() -> None:
        nonlocal text_run
        if text_run:
            joined = "\n".join(text_run).strip()
            if joined:
                for window in _window_words(joined.split(), target_tokens, overlap_tokens):
                    chunk = " ".join(window).strip()
                    if chunk:
                        chunks.append(chunk)
            text_run = []

    for kind, content in _split_into_blocks(structured):
        if kind == "table":
            # A table breaks the surrounding text run and stands alone so it is
            # never split, regardless of its size (Req 5.2).
            flush_text_run()
            table = content.strip()
            if table:
                chunks.append(table)
        else:
            text_run.append(content)
    flush_text_run()

    return chunks


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class DocumentProcessor:
    """Runs the parse → structure → chunk → embed → index ingestion pipeline.

    One processor instance owns the repositories and search index it needs;
    construct it with the process-wide SQLite connection and the injected
    DashScope client seam. :meth:`process` is idempotent and safe to run for
    both first-time processing and Update re-processing (Req 4.9): any existing
    chunks for the document are cleared before new ones are written.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: ChatEmbedClient,
        *,
        settings: Settings | None = None,
        search_index: SearchIndex | None = None,
        parse_deadline_s: float = PARSE_DEADLINE_S,
        target_tokens: int = CHUNK_TARGET_TOKENS,
        overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
        embed_batch_size: int = EMBED_BATCH_SIZE,
    ) -> None:
        """Store dependencies and pipeline tuning knobs.

        Args:
            conn: Open connection over the bootstrapped schema.
            client: Injected DashScope client seam (chat + embed). Tests pass a
                fake so no network calls are made.
            settings: Optional settings snapshot; defaults to process settings.
            search_index: Optional :class:`SearchIndex`; one is created over
                ``conn`` when omitted.
            parse_deadline_s: Hard deadline for the parse/structure/chunk stage
                (Req 5.4). Defaults to 600 seconds (10 minutes).
            target_tokens: Approximate chunk size in tokens.
            overlap_tokens: Token overlap between consecutive text chunks.
            embed_batch_size: Number of chunks embedded per batched request.
        """
        self._conn = conn
        self._client = client
        self._settings = settings or get_settings()
        self._documents = DocumentRepository(conn)
        self._chunks = ChunkRepository(conn)
        self._errors = IntegrationErrorLogRepository(conn)
        self._search = search_index or SearchIndex(conn, self._settings)
        self._parse_deadline_s = float(parse_deadline_s)
        self._target_tokens = int(target_tokens)
        self._overlap_tokens = int(overlap_tokens)
        self._embed_batch_size = max(1, int(embed_batch_size))

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def schedule(self, document_id: int) -> asyncio.Task[None]:
        """Schedule :meth:`process` on the running event loop.

        Convenience for callers (e.g. the upload endpoint) that want fire-and-
        forget background processing so it starts within 5 seconds of upload
        (Req 5.1). The actual wiring lives in ``main.py``.

        Args:
            document_id: The document to process.

        Returns:
            The created :class:`asyncio.Task`.
        """
        return asyncio.ensure_future(self.process(document_id))

    async def process(self, document_id: int) -> None:
        """Run the full ingestion pipeline for one document.

        Stages, in order:

        1. Load the ``documents`` row; return quietly if it no longer exists.
        2. Under an :func:`asyncio.timeout` parse deadline (Req 5.4): extract
           content with the format loader, AI-structure it (tables → markdown),
           and chunk it. An unsupported format (Req 5.8), a parse error, or the
           deadline expiring sets Status ``Error`` and records an error log.
        3. Embed the chunks in batches. An embedding failure keeps Status
           ``Pending`` and records an error log so the Admin can retry via
           Update (Req 5.5).
        4. Persist chunks + vectors, set Status ``Processed``, and add the
           document's vectors to its Intent_Space FAISS index (Req 5.3).

        Args:
            document_id: The document to process.
        """
        doc = self._documents.get(document_id)
        if doc is None:
            logger.warning("process() called for unknown document id=%s", document_id)
            return

        file_path = Path(doc["file_path"])

        # Stage 1-2: parse + structure + chunk, bounded by the parse deadline.
        try:
            async with asyncio.timeout(self._parse_deadline_s):
                content = await asyncio.to_thread(load_document, file_path)
                structured = await self._structure(content)
                chunk_texts = chunk_structured_text(
                    structured,
                    target_tokens=self._target_tokens,
                    overlap_tokens=self._overlap_tokens,
                )
        except UnsupportedFormatError as exc:
            # Unsupported format → Status Error + error log (Req 5.8).
            self._mark_error(document_id, "unsupported_format", str(exc))
            return
        except (DocumentParseError, TimeoutError) as exc:
            # Parse failure or deadline expiry → Status Error + error log
            # (Req 5.4). asyncio.timeout raises TimeoutError on expiry.
            reason = (
                f"parsing exceeded the {int(self._parse_deadline_s)}s deadline"
                if isinstance(exc, TimeoutError)
                else str(exc)
            )
            self._mark_error(document_id, "parse", reason)
            return
        except Exception as exc:  # noqa: BLE001 - any parse-stage failure → Error
            self._mark_error(document_id, "parse", str(exc))
            return

        # Stage 3: batched embeddings. Failure here keeps the document Pending
        # so the Admin can retry embedding via Update (Req 5.5).
        try:
            embeddings = await self._embed(chunk_texts)
        except Exception as exc:  # noqa: BLE001 - embed-only failure → stay Pending
            self._mark_embed_failure(document_id, str(exc))
            return

        # Stage 4: persist + index + mark Processed (Req 5.3).
        self._persist_chunks(document_id, chunk_texts, embeddings)
        self._documents.set_status(document_id, "Processed", error_message=None)
        self._search.add_document(document_id)

    # ------------------------------------------------------------------
    # Pipeline stages
    # ------------------------------------------------------------------

    async def _structure(self, content: ExtractedContent) -> str:
        """AI-structure extracted content into clean markdown (Req 5.2).

        Args:
            content: The loader-extracted text and tables.

        Returns:
            The structured markdown text. Falls back to a deterministic
            rendering of the raw extraction if the model returns nothing.
        """
        messages = build_structuring_messages(content)
        structured = await self._client.chat_completion(
            messages, timeout=STRUCTURING_TIMEOUT_S
        )
        structured = (structured or "").strip()
        if not structured:
            # Defensive fallback so an empty model reply doesn't drop content.
            structured = _fallback_render(content)
        return structured

    async def _embed(self, chunk_texts: Sequence[str]) -> list[np.ndarray]:
        """Embed chunk texts in batches, preserving input order.

        Args:
            chunk_texts: The chunk texts to embed.

        Returns:
            One embedding vector per chunk, in the same order as ``chunk_texts``
            (empty when there are no chunks).

        Raises:
            Exception: Propagates any client error so the caller can keep the
                document Pending for retry (Req 5.5).
        """
        if not chunk_texts:
            return []
        vectors: list[np.ndarray] = []
        for start in range(0, len(chunk_texts), self._embed_batch_size):
            batch = list(chunk_texts[start : start + self._embed_batch_size])
            vectors.extend(await self._client.embed(batch))
        if len(vectors) != len(chunk_texts):
            raise ValueError(
                "embedding count mismatch: "
                f"{len(vectors)} vectors for {len(chunk_texts)} chunks"
            )
        return vectors

    def _persist_chunks(
        self,
        document_id: int,
        chunk_texts: Sequence[str],
        embeddings: Sequence[np.ndarray],
    ) -> None:
        """Replace the document's chunks with the newly produced ones.

        Existing chunks are deleted first so Update re-processing (Req 4.9)
        never leaves stale rows; new chunks are then inserted with their
        embedding BLOBs via the chunk repository.

        Args:
            document_id: The owning document id.
            chunk_texts: The chunk texts in order.
            embeddings: The matching embedding vectors in order.
        """
        # Clear any prior chunks (idempotent Update re-processing).
        self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        self._conn.commit()
        if not chunk_texts:
            return
        self._chunks.insert_many(
            document_id,
            (
                (seq, text, embedding)
                for seq, (text, embedding) in enumerate(zip(chunk_texts, embeddings))
            ),
        )

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _mark_error(self, document_id: int, operation: str, reason: str) -> None:
        """Set Status ``Error`` and record an error-log entry (Req 5.4/5.8).

        Args:
            document_id: The failing document.
            operation: Pipeline stage token (``parse``/``unsupported_format``).
            reason: Human-readable failure reason (stored on the document and
                in the error log for the Admin).
        """
        self._documents.set_status(document_id, "Error", error_message=reason)
        self._record_error(operation, document_id, reason)

    def _mark_embed_failure(self, document_id: int, reason: str) -> None:
        """Keep Status ``Pending`` and record an embed error (Req 5.5).

        The document is left Pending (not Error) so the Admin can retry
        embedding via the Update action; the reason is stored on the document
        and in the error log.

        Args:
            document_id: The document whose embedding failed.
            reason: Human-readable failure reason.
        """
        self._documents.set_status(document_id, "Pending", error_message=reason)
        self._record_error("embed", document_id, reason)

    def _record_error(self, operation: str, document_id: int, reason: str) -> None:
        """Append a pipeline failure to the shared integration error log.

        Logging failures are swallowed so they never mask the underlying
        processing outcome.

        Args:
            operation: The pipeline stage that failed.
            document_id: The document involved (embedded in the detail).
            reason: The failure reason.
        """
        try:
            self._errors.insert(
                ERROR_LOG_TOOL,
                operation,
                f"document {document_id}: {reason}",
            )
        except Exception:  # noqa: BLE001 - never let error logging break the flow
            logger.exception("failed to record processing error for doc %s", document_id)


def _fallback_render(content: ExtractedContent) -> str:
    """Deterministically render extracted content as markdown.

    Used only when the AI structuring step returns an empty result, so a
    document's content is never silently dropped. Tables are rendered as
    GitHub-flavored markdown so they remain single, unsplit chunks.

    Args:
        content: The loader-extracted text and tables.

    Returns:
        A markdown string combining the body text and any tables.
    """
    parts: list[str] = []
    if content.text.strip():
        parts.append(content.text.strip())
    for table in content.tables:
        rendered = _render_markdown_table(table)
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts)


def _render_markdown_table(table: list[list[str]]) -> str:
    """Render a table grid as a GitHub-flavored markdown table.

    Args:
        table: The table as a list of rows of cell strings.

    Returns:
        The markdown table text, or an empty string for an empty grid.
    """
    if not table:
        return ""
    width = max(len(row) for row in table)
    norm = [list(row) + [""] * (width - len(row)) for row in table]
    header = norm[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for row in norm[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
