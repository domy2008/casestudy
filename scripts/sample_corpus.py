#!/usr/bin/env python3
"""Corpus and question definitions for the user-emulation test.

Declares which sample documents get seeded into which Intent_Space, and the
bilingual user questions replayed against the pipeline. Shared by
``scripts/emulate_usage.py`` so the runner stays small.
"""

from __future__ import annotations

from pathlib import Path

from scripts.seed_demo import SampleSpec

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Directories holding the Chinese and English sample documents.
ZH_SAMPLES_DIR: Path = _PROJECT_ROOT / "samples" / "zh"
EN_SAMPLES_DIR: Path = _PROJECT_ROOT / "samples" / "en"

#: Default destination of the customer-facing markdown Q&A report.
DEFAULT_REPORT_PATH: Path = _PROJECT_ROOT / "samples" / "用户测试问答报告.md"

#: Chinese documents (Markdown), one per seeded Intent_Space.
ZH_SAMPLES: tuple[SampleSpec, ...] = (
    SampleSpec("hr_招聘与内部推荐制度.md", "HR", "内推奖金是多少？"),
    SampleSpec("finance_发票与税务管理指引.md", "Finance", "开票税率是多少？"),
    SampleSpec("legal_数据合规与个人信息保护制度.md", "Legal", "数据泄露怎么上报？"),
    SampleSpec("general_办公安全与应急指南.md", "General", "疏散集合点在哪里？"),
)

#: English documents, one per Intent_Space and one per file format
#: (DOCX / XLSX / TXT / PDF) so every loader is exercised end to end.
EN_SAMPLES: tuple[SampleSpec, ...] = (
    SampleSpec("hr_employee_handbook.docx", "HR", "How many annual leave days?"),
    SampleSpec("finance_travel_rates.xlsx", "Finance", "What is the hotel rate?"),
    SampleSpec("general_it_support_faq.txt", "General", "How long can a VPN session last?"),
    SampleSpec(
        "legal_contract_approval_policy.pdf", "Legal", "Who approves a large contract?"
    ),
)

#: Emulated user questions: (question, expected space, expect an answer?).
#: ``expected_space=None`` skips the routing check; ``expect_answer=False``
#: means the response must not fabricate facts (a fixed no-match reply, or a
#: grounded refusal stating the corpus has no such information).
USER_QUESTIONS: tuple[tuple[str, str | None, bool], ...] = (
    # Chinese users (answers live in the zh Markdown corpus).
    ("内推一个P4的候选人奖金多少？", "HR", True),
    ("软件开发服务开票税率是多少？", "Finance", True),
    ("发现数据泄露要在多久内上报？", "Legal", True),
    ("公司有AED吗？在哪里？", "General", True),
    # English users (answers live in the en DOCX/XLSX/TXT/PDF corpus).
    ("How many annual leave days do I get after 6 years of service?", "HR", True),
    ("What is the hotel rate for a P4 employee in Beijing?", "Finance", True),
    ("Who approves a contract worth 800,000 CNY?", "Legal", True),
    ("How long can a VPN session last?", "General", True),
    # No-answer probes: the system must refuse instead of fabricating.
    ("公司股票代码是多少？", None, False),
    ("What is the company's stock ticker symbol?", None, False),
)

#: Phrases that signal a grounded refusal (the model declined to fabricate).
REFUSAL_MARKERS: tuple[str, ...] = (
    "未在", "未找到", "没有找到", "没有提到", "未提及", "未能", "无法", "不包含",
    "not mention", "no information", "not found", "could not find",
    "couldn't find", "cannot find",
    "do not contain", "does not contain", "unable to", "not provide",
)
