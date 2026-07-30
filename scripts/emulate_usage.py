#!/usr/bin/env python3
"""Emulate End_User usage over the bilingual (zh + en) sample corpus.

End-to-end user test:

1. Seeds the Chinese Markdown documents and the English DOCX/XLSX/TXT/PDF
   documents into their Intent_Spaces (idempotent: documents already
   Processed under the same name are not re-ingested).
2. Wires the production query pipeline (DashScope + FAISS + RAG generator +
   analytics) exactly like ``app.main`` does.
3. Replays bilingual user questions as a Telegram user, checks routing and
   grounding, and exports a customer-facing markdown Q&A report.

Run it with::

    export DATA_DIR=/tmp/intelliknow-zh-demo
    export DASHSCOPE_API_KEY=sk-...
    python scripts/emulate_usage.py [report.md]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import load_settings  # noqa: E402
from app.core.orchestrator import Orchestrator  # noqa: E402
from app.db import bootstrap  # noqa: E402
from app.kb.search import SearchIndex  # noqa: E402
from app.rag.generator import ResponseGenerator  # noqa: E402
from scripts.emulate_support import make_ctx, seed_missing  # noqa: E402
from scripts.sample_corpus import (  # noqa: E402
    DEFAULT_REPORT_PATH,
    EN_SAMPLES,
    EN_SAMPLES_DIR,
    REFUSAL_MARKERS,
    USER_QUESTIONS,
    ZH_SAMPLES,
    ZH_SAMPLES_DIR,
)
from scripts.seed_demo import _build_default_client  # noqa: E402
from scripts.zh_report import QARecord, write_report  # noqa: E402


async def run() -> int:
    """Seed the corpus, replay the user questions, and export the report.

    Returns:
        Process exit code: 0 when seeding succeeded and every question routed
        to its expected space with the expected answer/no-match outcome.
    """
    settings = load_settings()
    zh_ok = await seed_missing(settings, ZH_SAMPLES_DIR, ZH_SAMPLES)
    en_ok = await seed_missing(settings, EN_SAMPLES_DIR, EN_SAMPLES)
    if not (zh_ok and en_ok):
        print("Seeding failed; aborting user emulation.")
        return 1

    conn = bootstrap(settings)
    client = _build_default_client(settings)
    try:
        from app.analytics.service import AnalyticsService

        orchestrator = Orchestrator(
            conn=conn,
            ai_client=client,
            search_index=SearchIndex(conn, settings),
            generator=ResponseGenerator(client),
            analytics=AnalyticsService(conn),
        )
        spaces = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id, name FROM intent_spaces")
        }

        failures = 0
        records: list[QARecord] = []
        for question, expected_space, expect_answer in USER_QUESTIONS:
            classification = await orchestrator.classify(question)
            started = datetime.now(timezone.utc)
            response = await orchestrator.handle_query(make_ctx(question))
            elapsed_ms = int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            )
            routed = spaces.get(classification.space_id, "?")
            route_ok = expected_space is None or routed == expected_space
            if expect_answer:
                answer_ok = response.status == "success"
            else:
                answer_ok = response.status == "no_match" or any(
                    marker in response.text.lower() for marker in REFUSAL_MARKERS
                )
            ok = route_ok and answer_ok
            failures += 0 if ok else 1
            print(f"\n[{'OK' if ok else 'FAIL'}] 用户: {question}")
            print(f"  路由: {routed} (期望 {expected_space or '任意'}, "
                  f"置信度 {classification.confidence:.0f})")
            print(f"  状态: {response.status} | 引用: {response.citations}")
            print(f"  回答: {response.text[:160]}")
            records.append(QARecord(
                question=question,
                space=routed,
                confidence=classification.confidence,
                answer=response.text,
                citations=list(response.citations),
                latency_ms=elapsed_ms,
                status=response.status,
            ))

        report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPORT_PATH
        print(f"\n问答报告已导出: {write_report(records, report_path)}")
        print("用户测试通过。" if failures == 0 else f"{failures} 个问题未达预期。")
        return 0 if failures == 0 else 1
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
