# Sample Knowledge Documents & Demo Seed

This directory contains realistic, self-contained sample documents used to
demonstrate IntelliKnow KMS end-to-end, plus a description of how to run the
demo seed script.

## Sample documents

| File | Intent Space | Highlights |
| ---- | ------------ | ---------- |
| [`hr_salary_policy.md`](./hr_salary_policy.md) | **HR** | Embedded **salary grade table** (G1–G6) plus annual/sick/parental leave policy text. |
| [`finance_expense_policy.md`](./finance_expense_policy.md) | **Finance** | Meal-allowance and approval-threshold **tables** plus reimbursement rules. |

Both documents contain at least one GitHub-markdown table so the ingestion
pipeline's "tables are kept whole" behaviour can be demonstrated (the salary
grid and the expense tables are never split across chunks).

## Chinese (zh) corpus

The [`zh/`](./zh) directory holds a larger Chinese corpus — 16 documents
covering all four seeded Intent_Spaces (HR / Legal / Finance / General) in
Markdown, DOCX, and XLSX formats, each with at least one table. The companion
[`zh/测试问答手册.md`](./zh/测试问答手册.md) lists 40+ test questions with
expected answers and source documents per space.

## English (en) corpus

The [`en/`](./en) directory holds one English document per Intent_Space, each
in a **different supported format** so every loader is exercised:

| File | Space | Format |
| ---- | ----- | ------ |
| `hr_employee_handbook.docx` | HR | DOCX (with annual-leave table) |
| `finance_travel_rates.xlsx` | Finance | XLSX (lodging + meal sheets) |
| `general_it_support_faq.txt` | General | Plain text |
| `legal_contract_approval_policy.pdf` | Legal | PDF |

Facts mirror the Chinese corpus (same fictional company), so zh and en answers
stay consistent. Regenerate the files with `python scripts/make_en_samples.py`.

## Loading the corpus into a deployed instance

The emulation seeds a local `DATA_DIR`. To populate the KB behind a **running**
bot, upload through the admin REST API so the live app ingests and indexes each
document itself. `scripts/upload_corpus.py` batch-uploads every sample document
(mapping the filename prefix to its Intent_Space and skipping docs already
Processed):

```bash
# The deployed admin API is tunnel-only (see deploy/DEPLOYMENT.md). Open the
# tunnel first, then upload through it:
ssh -i ~/.ssh/intelliknow-demo.pem -N -L 8000:localhost:8000 ec2-user@<instance> &
python scripts/upload_corpus.py --base-url http://localhost:8000
```

Documentation files (`README.md`, `测试问答手册.md`, the Q&A report) are
excluded automatically. Use `--force` to re-upload existing documents.

## Emulating real user usage

To seed the zh + en corpus (idempotent) and replay bilingual user questions
through classify → route → retrieve → generate → log, exporting a
customer-facing markdown Q&A report to `samples/用户测试问答报告.md`:

```bash
export DATA_DIR=/tmp/intelliknow-zh-demo
export DASHSCOPE_API_KEY=sk-...
python scripts/emulate_usage.py [optional/report/path.md]
```

## Running the demo seed

The seed script lives at [`../scripts/seed_demo.py`](../scripts/seed_demo.py).
It ingests both sample documents into a fresh knowledge base, associates them
with the seeded **HR** and **Finance** intent spaces, and verifies each
becomes searchable.

```bash
# From the project root, with the project virtualenv active:
export DATA_DIR=/tmp/intelliknow-demo          # any writable, isolated directory
export DASHSCOPE_API_KEY=sk-...                # your DashScope API key

python scripts/seed_demo.py
```

Configuration:

- **`DATA_DIR`** — root of the persistent volume the demo writes to (SQLite DB,
  FAISS indexes, uploaded originals). Point it at a throwaway directory to keep
  the demo isolated from production `/data`.
- **`DASHSCOPE_API_KEY`** — resolved from the environment or, if a
  `CREDENTIAL_MASTER_KEY` is configured and the `dashscope` credential has been
  saved, from the encrypted Credential Store (the Credential Store wins).

The script prints a per-document report (chunks created, status, and a sample
search hit) and exits non-zero if any document fails to become searchable.

## How the demo maps to the two frontend integrations

Once seeded, the same knowledge base backs both configured frontends:

- **Telegram** (long-polling through the outbound proxy): a user messages the
  bot *"What is the salary band for a G3 senior professional?"*. The
  orchestrator classifies the query into the **HR** space, retrieves the salary
  grid chunk, and returns a cited answer.
- **Microsoft Teams** (Bot Framework inbound webhook): a user asks *"What's the
  daily meal allowance for international travel?"*. The query classifies into
  the **Finance** space, retrieves the meal-allowance table, and returns a cited
  answer formatted with Teams markdown.

Because both integrations share the orchestrator and knowledge base, seeding
once makes both testable. Suggested demo queries:

| Ask via Telegram (HR) | Ask via Teams (Finance) |
| --------------------- | ----------------------- |
| "How many annual leave days after 6 years?" | "What is the hotel cap for international travel?" |
| "What is the G5 salary midpoint?" | "Who approves an expense of 3,000 USD?" |
