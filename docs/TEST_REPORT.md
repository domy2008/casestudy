# Test Report — IntelliKnow KMS

Generated: 2026-08-02 | Branch: `main` | Commit: `ce654ec`

## 1. Summary

| Metric | Value |
|---|---|
| Total tests collected | 372 |
| Passed | 371 |
| Failed | 0 |
| Skipped | 1 (see §6) |
| Line coverage (`app/` + `admin_ui/`) | 73% |
| Wall-clock runtime | ~37 s |
| Production smoke test (deployed) | Pass (see §5) |
| Live IM credential-swap test (deployed) | Pass, all 3 IMs (see §5a) |

Environment: Python 3.11.14, pytest 8.3.5, hypothesis 6.130.0, macOS (local venv).

This run was taken ahead of a customer delivery. Since the previous report it
adds regression tests for three production fixes:

1. **Async-endpoint DB connections** — `get_connection` opens SQLite with
   `check_same_thread=False` and closes per request, so AI-suggestion and
   other `async` endpoints no longer return HTTP 500.
2. **Telegram poll-loop resilience** — a transient proxy HTTP error (502) or a
   failing handler on one update is retried/skipped instead of silently
   killing the polling task.
3. **First-time Telegram configuration** — the poller now always starts and
   idles while unconfigured, activating the moment an Admin saves a bot token
   (no application restart required).

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
| Unit | `tests/unit/` | 333 | Every module in isolation: loaders, processor, search index, orchestrator, generator, bot adapters (incl. poll-loop resilience and late-configuration regression tests), dispatcher, status monitor, credential store, admin API endpoints (incl. the `get_connection` cross-thread regression test), analytics, monitoring publisher, all 6 Streamlit pages |
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
| Core domain (`app/core/`, `app/db.py`, `app/config.py`) | 89–100% | Models 100%, config 100%, orchestrator 89%, DB bootstrap 89% |
| Knowledge base (`app/kb/`) | 80–93% | Loaders 88%, processor 80%, search 80%, service 93%, store 86% |
| Security (`app/security/`) | 68–92% | Credential store 92%, logfilter 92%; startup 68% (rare fail-closed branches) |
| Analytics & monitoring | 45–92% | Analytics service 92%, monitoring publisher 91%; monitoring API 45% (thin CloudWatch read glue) |
| Admin API (`app/api/admin.py`) | 86% | `chat.py` 53% — SSE streaming glue |
| Bots (`app/bots/`) | 68–100% | Base 100%, monitor 89%, dispatcher 85%, telegram 82%, teams 70%, whatsapp 68% |
| RAG (`app/rag/generator.py`, `app/ai/`) | 59–94% | Generator 94%, prompts 80%; DashScope client 59% (network glue mocked) |
| Admin UI (`admin_ui/`) | 29–78% | Shared components 78%; page scripts lower — Streamlit render flow is exercised via logic-function tests, not full page execution |

Lowest-covered modules (`auth.py` 29%, `Home.py` 45%, `dashscope_client.py`
59%) are known gaps, not failures; their critical behaviors are covered
indirectly by dispatcher, formatting, page-logic, and property tests.

## 5. Production Smoke Test (delivery)

Run against the deployed instance (`ikms-api` / `ikms-ui` systemd services on
the cn-north-1 EC2 host) on 2026-08-01, ahead of customer delivery. No test
data was written; all checks are read-only or idempotent.

| Check | Method | Result |
|---|---|---|
| Backend service up | `systemctl is-active ikms-api` | active |
| Admin UI service up | `systemctl is-active ikms-ui` | active |
| Backend health | `GET :8000/health` | 200 |
| Admin UI reachable | `GET :8501/` | 200 |
| Public portal (TLS) | `GET https://kms.autobuy.top/health` | 200 |
| **AI keyword suggestions (fixed)** | `POST :8000/spaces/2/suggest-keywords` (HR) | 200 — returns keywords from verified-but-misrouted queries (previously 500) |

The keyword-suggestion endpoint — the one that regressed with an HTTP 500 — was
verified end to end on the live backend across every seeded space: spaces with
verified misrouted queries return AI-proposed keywords, and spaces without them
return the expected "verify queries in Analytics first" message.

## 5a. Live IM Credential-Swap Test (delivery)

Run on the deployed instance on 2026-08-02, simulating exactly what a customer
does: replace each IM's credentials via the same Save-credentials API the
Frontend Integration screen uses, verify the running system picks them up
immediately, then restore. No restarts were performed at any point.

| IM | New credentials used live? | Restore recovered live? |
|---|---|---|
| Telegram | Yes — the poll loop and the connectivity test switched to the newly saved token within seconds (observed the new token in `getUpdates`/`getMe` requests) | Yes — polling resumed on its own, 0 residual errors |
| Microsoft Teams | Yes — the connectivity test immediately attempted Bot Framework auth with the new App ID/Password | Yes — token acquired again instantly |
| WhatsApp | Yes — the connectivity test immediately called the Graph API with the new phone-number ID/token | Yes — phone number verified again instantly |

Conclusion: for all three IMs, credential changes (first-time setup or
replacement) take effect **live, without an application restart**. Invalid
credentials fail visibly in the Test button with the platform's real error and
do not crash the poller or webhooks.

Note: the swap used format-valid but non-working credentials (no second real
bot/app was available); the code path is identical for a customer's real
credentials. Final state after the test: all three integrations green with the
original credentials.

## 6. Skipped Test

`tests/unit/test_loaders.py:276` — the PDF **table-extraction** assertion needs
`reportlab` (dev-only, to synthesize a table-bearing PDF fixture) which is not
installed. PDF text extraction and loader dispatch are still verified.
Install `reportlab` to enable it.

## 7. Requirement Traceability

Tests reference requirement IDs from `.kiro/specs/intelliknow-kms/requirements.md`
in their docstrings (e.g. "Req 8.3"), and each property file maps to a numbered
design property. Every functional requirement area in the project specification
(frontend integration, document-driven KB, orchestrator, retrieval/response,
analytics, security) has at least one dedicated unit suite plus at least one
property invariant.
