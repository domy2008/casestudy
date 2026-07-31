# Test Report — IntelliKnow KMS

Generated: 2026-07-31 | Branch: `main`

## 1. Summary

| Metric | Value |
|---|---|
| Total tests collected | 337 |
| Passed | 336 |
| Failed | 0 |
| Skipped | 1 (see §5) |
| Line coverage (`app/` + `admin_ui/`) | 71% (4536 statements, 1301 missed) |
| Wall-clock runtime | ~65 s |

Environment: Python 3.11.14, pytest 8.3.5, hypothesis 6.130.0, macOS (local venv).

## 2. How to Run

```bash
# Full suite
.venv/bin/python -m pytest tests/ -q

# With coverage (requires pytest-cov)
.venv/bin/python -m pytest tests/ -q --cov=app --cov=admin_ui --cov-report=term
```

No network access or real credentials are required: every external dependency
(DashScope AI API, Telegram/Teams/WhatsApp APIs, CloudWatch) is replaced by an
injected fake or mocked HTTP transport.

## 3. Suite Structure

| Suite | Location | Tests | Focus |
|---|---|---|---|
| Unit | `tests/unit/` | 298 | Every module in isolation: loaders, processor, search index, orchestrator, generator, bot adapters, dispatcher, status monitor, credential store, admin API endpoints, analytics, monitoring publisher, all 6 Streamlit pages |
| Integration | `tests/integration/` | 6 | End-to-end ingestion pipeline (upload → parse → structure → index), full query flow (classify → retrieve → generate → log), Teams webhook round-trip |
| Property-based | `tests/properties/` | 33 | 25 invariants (Hypothesis), one file per property |

### Property invariants (P1–P25)

Credential validation and persistence (P1–P2), message length gate (P3),
logging resilience (P4), log listings (P5), upload validation (P6), document
list filtering (P7), document deletion completeness (P8), semantic search
relevance ordering (P9), space config validation (P10), space name uniqueness
(P11), space deletion reassignment (P12), classification accuracy math (P13),
confidence-threshold routing to General (P14), threshold configuration (P15),
classification keywords injection (P16), query logging completeness (P17),
citations equal unique source documents (P18), no-match path never calls the
AI (P19), Telegram 4096-char formatting (P20), generation failure always
yields Failed status (P21), usage metrics (P22), CSV export round-trip (P23),
credential masking reveals ≤4 chars (P24), log redaction of secrets (P25).

## 4. Coverage by Area

| Area | Coverage | Notes |
|---|---|---|
| Core domain (`app/core/`, `app/db.py`, `app/config.py`) | 82–100% | Models and DB bootstrap fully covered |
| Knowledge base (`app/kb/`) | 80–93% | Loaders 88%, processor 80%, search 93%, store 93% |
| Security (`app/security/`) | 68–96% | Credential store 92%; logfilter 68% (rare redaction branches) |
| Analytics & monitoring | 91–94% | |
| Admin API (`app/api/admin.py`) | 83% | `chat.py` 53% — SSE streaming glue |
| Bots (`app/bots/`) | 33–100% | Telegram 85%, Teams 67%, WhatsApp 33% (send/webhook paths mocked at a higher level) |
| RAG (`app/rag/generator.py`, `app/ai/`) | 52–70% | Streaming path partially exercised |
| Admin UI (`admin_ui/`) | 24–82% | Shared components 82%; page scripts lower — Streamlit render flow is exercised via logic-function tests, not full page execution |

Lowest-covered modules (`whatsapp.py` 33%, `generator.py` streaming 52%,
`Home.py` 24%) are known gaps, not failures; their critical behaviors are
covered indirectly by dispatcher, formatting, and property tests.

## 5. Skipped Test

`tests/unit/test_loaders.py:276` — the PDF **table-extraction** assertion needs
`reportlab` (dev-only, to synthesize a table-bearing PDF fixture) which is not
installed. PDF text extraction and loader dispatch are still verified.
Install `reportlab` to enable it.

## 6. Requirement Traceability

Tests reference requirement IDs from `.kiro/specs/intelliknow-kms/requirements.md`
in their docstrings (e.g. "Req 8.3"), and each property file maps to a numbered
design property. Every functional requirement area in the project specification
(frontend integration, document-driven KB, orchestrator, retrieval/response,
analytics, security) has at least one dedicated unit suite plus at least one
property invariant.
