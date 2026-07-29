#!/usr/bin/env python3
"""Demo seed script — ingest the sample documents and verify they answer queries.

This script wires the *existing* IntelliKnow KMS building blocks together to
produce a working, testable demo knowledge base:

* :func:`app.db.bootstrap` — create/seed the SQLite schema and default
  Intent_Spaces (General/HR/Legal/Finance) under a configurable ``DATA_DIR``.
* :class:`app.kb.service.DocumentLifecycleService` — validate + accept each
  sample upload, associating it with its Intent_Space (HR or Finance).
* :class:`app.kb.processor.DocumentProcessor` — run the parse → structure →
  chunk → embed → index pipeline for each accepted document.
* :class:`app.kb.search.SearchIndex` — confirm each document is retrievable by
  embedding a representative query and searching its space.

The DashScope key is resolved from the environment / Credential_Store
(``DASHSCOPE_API_KEY``, with the Credential_Store winning per Req 11.4) and the
persistent volume is the configurable ``DATA_DIR`` (defaults to ``/data``).

Network safety: **no** network call is made at import time. The DashScope
client is constructed only when :func:`seed` runs, and callers (e.g. tests)
may inject a fake ``client`` so the whole flow runs offline. Run it with::

    export DATA_DIR=/tmp/intelliknow-demo
    export DASHSCOPE_API_KEY=sk-...
    python scripts/seed_demo.py

See ``samples/README.md`` for how the seeded knowledge base maps to the
Telegram and Teams integrations for a testable query flow.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Ensure the project root is importable when run as a plain script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import Settings, load_settings  # noqa: E402
from app.db import bootstrap  # noqa: E402
from app.kb.processor import DocumentProcessor  # noqa: E402
from app.kb.search import SearchIndex  # noqa: E402
from app.kb.service import DocumentLifecycleService  # noqa: E402
from app.kb.store import ChunkRepository, DocumentRepository, IntentSpaceRepository  # noqa: E402

logger = logging.getLogger("seed_demo")

#: Directory holding the sample documents (``<project>/samples``).
SAMPLES_DIR: Path = _PROJECT_ROOT / "samples"


@dataclass(frozen=True)
class SampleSpec:
    """A sample document to seed and the query used to prove it is searchable.

    Attributes:
        filename: File name under the samples directory.
        space_name: Intent_Space to associate the document with (must be one of
            the seeded spaces, e.g. ``"HR"`` / ``"Finance"``).
        verify_query: A representative question whose answer lives in the
            document; used to confirm the document becomes retrievable.
    """

    filename: str
    space_name: str
    verify_query: str


#: The demo corpus: an HR policy (with a salary grid table) and a Finance policy.
SAMPLES: tuple[SampleSpec, ...] = (
    SampleSpec(
        filename="hr_salary_policy.md",
        space_name="HR",
        verify_query="What is the salary band and annual leave for a senior professional?",
    ),
    SampleSpec(
        filename="finance_expense_policy.md",
        space_name="Finance",
        verify_query="What is the daily meal allowance and reimbursement limit for travel?",
    ),
)


@dataclass
class SeedResult:
    """Outcome of seeding a single sample document.

    Attributes:
        filename: The sample file name.
        space_name: The Intent_Space it was associated with.
        document_id: The created document id.
        status: The document status after processing (``Processed`` on success).
        chunk_count: Number of chunks persisted for the document.
        searchable: Whether the verification query retrieved the document.
        top_similarity: Cosine similarity of the best matching passage (or 0.0).
    """

    filename: str
    space_name: str
    document_id: int
    status: str
    chunk_count: int
    searchable: bool
    top_similarity: float = 0.0


@dataclass
class SeedReport:
    """Aggregate report for a seed run.

    Attributes:
        results: Per-document results.
    """

    results: list[SeedResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every document is Processed and searchable."""
        return bool(self.results) and all(
            r.status == "Processed" and r.searchable for r in self.results
        )


def _build_default_client(settings: Settings):
    """Construct the real DashScope client, resolving the key lazily.

    The Credential_Store is the primary API-key source (Req 11.4); it is only
    consulted when a ``CREDENTIAL_MASTER_KEY`` is configured, otherwise the
    client falls back to ``settings.dashscope_api_key`` (``DASHSCOPE_API_KEY``).
    Constructing the client makes **no** network call — the key is resolved and
    used only when a chat/embed method is invoked.

    Args:
        settings: The settings snapshot for this run.

    Returns:
        A ready-to-use :class:`app.ai.dashscope_client.DashScopeClient`.
    """
    # Imported here (not at module top) purely to keep import-time light.
    from app.ai.dashscope_client import DashScopeClient

    credential_store = None
    if settings.credential_master_key:
        try:
            from app.security.credentials import CredentialStore

            credential_store = CredentialStore(settings)
        except Exception:  # noqa: BLE001 - credential store is best-effort here
            logger.warning(
                "Credential_Store unavailable; using DASHSCOPE_API_KEY from settings"
            )
            credential_store = None

    return DashScopeClient(settings=settings, credential_store=credential_store)


def _resolve_space_id(spaces: IntentSpaceRepository, name: str) -> int:
    """Return the id of a seeded Intent_Space by name.

    Args:
        spaces: The Intent_Space repository.
        name: The space name (e.g. ``"HR"``).

    Returns:
        The Intent_Space id.

    Raises:
        RuntimeError: If the space is missing (database not seeded).
    """
    row = spaces.get_by_name(name)
    if row is None:
        raise RuntimeError(
            f"Intent_Space {name!r} is missing; the database was not seeded."
        )
    return int(row["id"])


async def _seed_one(
    spec: SampleSpec,
    *,
    conn: sqlite3.Connection,
    settings: Settings,
    client,
    samples_dir: Path,
    search: SearchIndex,
) -> SeedResult:
    """Ingest one sample document and verify it is searchable.

    Args:
        spec: The sample to seed.
        conn: Open, bootstrapped SQLite connection.
        settings: Settings snapshot for this run.
        client: DashScope client seam (real or injected fake).
        samples_dir: Directory containing the sample files.
        search: Shared search index manager.

    Returns:
        The :class:`SeedResult` for this document.
    """
    path = samples_dir / spec.filename
    content = path.read_bytes()

    spaces = IntentSpaceRepository(conn)
    space_id = _resolve_space_id(spaces, spec.space_name)

    # 1) Validate + accept the upload, associating it with its Intent_Space.
    lifecycle = DocumentLifecycleService(conn, settings=settings, search_index=search)
    document_id = lifecycle.accept_upload(
        spec.filename, content, space_id=space_id
    )

    # 2) Run the ingestion pipeline (parse → structure → chunk → embed → index).
    processor = DocumentProcessor(conn, client, settings=settings, search_index=search)
    await processor.process(document_id)

    documents = DocumentRepository(conn)
    doc = documents.get(document_id)
    status = str(doc["status"]) if doc else "Missing"
    chunk_count = len(ChunkRepository(conn).fetch_by_document(document_id))

    # 3) Verify searchable: embed the verification query and search the space.
    searchable = False
    top_similarity = 0.0
    if status == "Processed":
        query_vectors = await client.embed(spec.verify_query)
        passages = search.search(space_id, query_vectors[0], k=5)
        hits = [p for p in passages if p.document_id == document_id]
        searchable = bool(hits)
        if hits:
            top_similarity = max(p.similarity for p in hits)

    return SeedResult(
        filename=spec.filename,
        space_name=spec.space_name,
        document_id=document_id,
        status=status,
        chunk_count=chunk_count,
        searchable=searchable,
        top_similarity=top_similarity,
    )


async def seed(
    settings: Settings | None = None,
    *,
    client=None,
    samples_dir: Path | None = None,
    specs: tuple[SampleSpec, ...] = SAMPLES,
) -> SeedReport:
    """Seed the demo corpus and verify every document becomes searchable.

    Args:
        settings: Settings snapshot; defaults to :func:`app.config.load_settings`
            (which reads ``DATA_DIR`` / ``DASHSCOPE_API_KEY`` from the env).
        client: Optional DashScope client seam. When omitted a real client is
            constructed (no network call happens until embed/chat is invoked);
            tests inject a fake so the flow runs entirely offline.
        samples_dir: Directory containing the sample files; defaults to
            ``<project>/samples``.
        specs: The sample specifications to seed.

    Returns:
        A :class:`SeedReport` summarizing each document's outcome.
    """
    settings = settings or load_settings()
    samples_dir = samples_dir or SAMPLES_DIR

    conn = bootstrap(settings)
    try:
        active_client = client if client is not None else _build_default_client(settings)
        search = SearchIndex(conn, settings)

        report = SeedReport()
        for spec in specs:
            result = await _seed_one(
                spec,
                conn=conn,
                settings=settings,
                client=active_client,
                samples_dir=samples_dir,
                search=search,
            )
            report.results.append(result)

        # Close a client we own (an injected client is the caller's to manage).
        if client is None:
            aclose = getattr(active_client, "aclose", None)
            if aclose is not None:
                await aclose()

        return report
    finally:
        conn.close()


def _format_report(report: SeedReport) -> str:
    """Render a human-readable summary of a seed run.

    Args:
        report: The completed seed report.

    Returns:
        A multi-line string suitable for printing to the console.
    """
    lines = ["", "IntelliKnow KMS — demo seed report", "=" * 40]
    for r in report.results:
        mark = "OK" if (r.status == "Processed" and r.searchable) else "FAIL"
        lines.append(
            f"[{mark}] {r.filename} -> space={r.space_name} "
            f"(doc #{r.document_id}, status={r.status}, chunks={r.chunk_count}, "
            f"searchable={r.searchable}, top_similarity={r.top_similarity:.2f})"
        )
    lines.append("=" * 40)
    lines.append("All documents seeded and searchable." if report.ok
                 else "One or more documents failed to seed/search.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """CLI entry point: seed the demo and print a report.

    Returns:
        Process exit code (0 when every document is Processed and searchable).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = asyncio.run(seed())
    print(_format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
