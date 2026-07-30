#!/usr/bin/env python3
"""Customer-facing markdown report writer for the zh user-emulation test.

Renders the questions replayed by ``scripts/emulate_zh_usage.py`` — with the
generated answers, routed Intent_Space, classification confidence, citations,
and latency — into a single Chinese markdown document suitable for sharing
with a customer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["QARecord", "write_report"]


@dataclass
class QARecord:
    """One replayed user question and its full pipeline outcome.

    Attributes:
        question: The user question text.
        space: Name of the Intent_Space the query was routed to.
        confidence: Classification confidence (0-100).
        answer: The generated answer text.
        citations: Source document names cited by the answer.
        latency_ms: End-to-end processing latency in milliseconds.
        status: Generation status (``success`` / ``no_match`` / ``failed``).
    """

    question: str
    space: str
    confidence: float
    answer: str
    citations: list[str] = field(default_factory=list)
    latency_ms: int = 0
    status: str = "success"


def _cell(text: str) -> str:
    """Escape ``text`` for use inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def write_report(records: list[QARecord], path: Path) -> Path:
    """Write the Q&A records as a customer-facing markdown report.

    Args:
        records: The replayed questions with their outcomes, in ask order.
        path: Destination ``.md`` file (parent directories are created).

    Returns:
        The path the report was written to.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    avg_latency = (
        sum(r.latency_ms for r in records) // len(records) if records else 0
    )
    lines = [
        "# IntelliKnow KMS — 知识库用户测试问答报告（中英双语）",
        "",
        f"- 测试时间：{now}",
        f"- 提问渠道：Telegram 机器人（模拟真实用户）",
        f"- 问题数量：{len(records)}，平均端到端延迟：{avg_latency} ms",
        "",
        "每个问题经过完整流水线处理：意图分类 → 空间路由 → 语义检索 → "
        "基于文档生成回答 → 记录日志。回答均以知识库文档为依据并附引用。",
        "",
        "## 问答明细",
        "",
        "| # | 用户问题 | 意图空间 | 置信度 | 回答 | 引用文档 |",
        "|---|---------|---------|-------|------|---------|",
    ]
    for i, r in enumerate(records, start=1):
        citations = "、".join(r.citations) if r.citations else "—"
        lines.append(
            f"| {i} | {_cell(r.question)} | {r.space} | {r.confidence:.0f} "
            f"| {_cell(r.answer)} | {_cell(citations)} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- 置信度为意图分类模型给出的 0–100 分值；低于阈值的问题自动回退到 "
        "General 空间处理。",
        "- 知识库中不存在答案的问题（如最后一条探针问题），系统会明确告知"
        "未找到相关信息，而不会编造答案。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
