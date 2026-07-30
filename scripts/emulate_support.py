#!/usr/bin/env python3
"""Support helpers for the user-emulation runner.

Holds the idempotent corpus seeding and query-context construction so
``scripts/emulate_usage.py`` stays focused on replaying user questions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.core.models import QueryContext
from app.db import bootstrap
from scripts.seed_demo import seed


def make_ctx(text: str) -> QueryContext:
    """Build a Telegram-style :class:`QueryContext` for ``text``.

    Args:
        text: The emulated user message.

    Returns:
        A query context as the Telegram adapter would produce it.
    """
    return QueryContext(
        query_id=str(uuid.uuid4()),
        tool="telegram",
        conversation_ref={"chat_id": 10001},
        text=text,
        received_at=datetime.now(timezone.utc),
    )


async def seed_missing(settings: Settings, samples_dir: Path, specs) -> bool:
    """Seed the specs whose documents are not already Processed.

    Idempotent: a document whose name already exists with status ``Processed``
    is skipped, so re-runs never duplicate knowledge-base content.

    Args:
        settings: Settings snapshot for this run.
        samples_dir: Directory containing the sample files.
        specs: Candidate :class:`~scripts.seed_demo.SampleSpec` entries.

    Returns:
        True when every newly seeded document is Processed and searchable.
    """
    conn = bootstrap(settings)
    try:
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM documents WHERE status = 'Processed'"
            )
        }
    finally:
        conn.close()

    missing = tuple(s for s in specs if s.filename not in existing)
    for spec in specs:
        if spec.filename in existing:
            print(f"[seed SKIP] {spec.filename} already Processed")
    if not missing:
        return True

    report = await seed(settings, samples_dir=samples_dir, specs=missing)
    for r in report.results:
        mark = "OK" if (r.status == "Processed" and r.searchable) else "FAIL"
        print(f"[seed {mark}] {r.filename} -> {r.space_name} "
              f"(chunks={r.chunk_count}, top_sim={r.top_similarity:.2f})")
    return report.ok
