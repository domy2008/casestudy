# IntelliKnow KMS

A Gen AI-powered Knowledge Management System. End users ask questions from **Telegram**, **Microsoft Teams**, or **WhatsApp** — or right in the web console's **Test Chat** — and receive concise, **cited** answers generated with Retrieval-Augmented Generation (RAG) over an admin-managed document knowledge base.

Live demo: **https://kms.autobuy.top**

| Portal access | |
|---|---|
| Account | `kmsadmin` |
| Password | `IntelliKnow2026!` |

> The browser prompts for these credentials once; they protect the demo admin console (dashboard, knowledge base, integrations, analytics, and Test Chat).

---

## Part 1 · Features

IntelliKnow KMS addresses three enterprise pain points: fragmented information, inefficient knowledge retrieval, and siloed communication channels.

### Multi-frontend IM integration
- Ask questions from the tools your teams already use: **Telegram** (long-poll via outbound proxy), **Microsoft Teams** (Bot Framework webhook), and **WhatsApp** (Meta Cloud API).
- Answers are formatted natively per tool — Teams gets Markdown bullets, Telegram/WhatsApp get plain text with a `Sources:` footer — within each platform's message limits.
- Credentials are configured in the admin console, validated, and stored **Fernet-encrypted**; each integration shows a live Connected/Error/Disconnected status, an end-to-end **Test** button, and its 50 most recent error-log entries.

### Document-driven knowledge base
- Upload **PDF, DOCX, XLSX, Markdown, or plain text** (up to 50 MB); documents move Pending → Processed automatically.
- AI-powered parsing and structuring: embedded tables (e.g. salary grids) are preserved as Markdown tables so structured data stays searchable.
- Semantic search over FAISS vector indexes, one index per knowledge domain; re-upload, reassign, or delete documents any time.

### Query orchestrator (intent classification)
- Queries are classified into admin-defined **Intent Spaces** (e.g. HR, Legal, Finance, General) and routed to the matching knowledge domain.
- Configurable **confidence threshold**: low-confidence classifications fall back to the General space instead of guessing.
- Admins can verify each query's correct space in Analytics, feeding a per-space classification accuracy rate.

### Grounded, cited answers
- RAG responses answer **only from retrieved passages** and cite the source document names; a clear "no match" message is returned when the knowledge base has nothing relevant.
- Answers reply in the user's language (Chinese question → Chinese answer), typically within **2–4 seconds** (fast model for classification, quality model for generation).
- The web **Test Chat streams answers token by token**, so text starts appearing in about a second.

### Admin console (six screens)
- **Dashboard** — system summary cards: document counts by status, 24-hour query activity, integration health.
- **Frontend Integration** — credential entry (masked on re-read), connectivity tests, error logs.
- **KB Management** — upload zone, document table (name/date/format/status), search/filter, space assignment.
- **Intent Configuration** — intent space cards, keyword editor, confidence threshold setting.
- **Analytics** — query history with intent + confidence, top documents/spaces, per-space accuracy, CSV export.
- **Test Chat** — chat with the knowledge base directly in the browser, no IM client needed.

### Operations & security
- Every query is logged (timestamp, intent, confidence, latency, response status, originating tool) and exportable as CSV.
- CloudWatch metrics, logs, and alarms for latency p95, error rate, and per-tool integration health.
- Secrets never appear in source, logs, or API responses (values are masked to the last ≤4 characters).

---

## Part 2 · User Guide

How to run IntelliKnow KMS day to day, from first login to a working IM bot.

### 2.1 Open the admin console

1. Open **https://kms.autobuy.top** in a browser (HTTPS, works on desktop and mobile).
2. Sign in at the browser's login dialog (HTTP basic auth, realm "IntelliKnow Admin") with the admin credentials provided by your operator. The Teams/WhatsApp webhook endpoints and `/health` remain open, as the platforms cannot authenticate.
3. Use the left sidebar to switch between the six screens; the Dashboard loads first.

### 2.2 Configure the AI credential (once)

The system uses Aliyun DashScope (Tongyi Qwen) for parsing, classification, and answer generation. The key is configured by the operator, either way:

- **Host env file** (used by the demo deployment): set `DASHSCOPE_API_KEY` in `/opt/intelliknow/.env` and restart the backend; or
- **Encrypted credential store** (takes precedence, updatable without restart): `PUT /integrations/dashscope/credentials` on the admin API with `{"api_key": "sk-..."}`. It is validated, Fernet-encrypted at rest, and always returned masked.

### 2.3 Connect an IM chat bot

Each tool takes a few minutes. All three follow the same pattern: create the bot on the platform, paste its credentials into **Frontend Integration**, then press **Test**.

**Telegram**
1. In Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the bot token.
2. Admin console → Frontend Integration → **Telegram** tab → paste the token → Save.
3. Press **Test** — the card should turn **Connected**.
4. End users just open the bot in Telegram and send questions.

**Microsoft Teams**
1. Register a bot in the Azure Bot portal; note the **App ID** and **App Password**.
2. Set the bot's messaging endpoint to `https://kms.autobuy.top/webhooks/teams`.
3. Admin console → Frontend Integration → **Teams** tab → enter App ID + Password → Save → **Test**.
4. End users chat with the bot inside Teams.

**WhatsApp**
1. Create a Meta app (Business type) at developers.facebook.com, add the **WhatsApp** product; note the **Phone Number ID** and generate a long-lived **access token**.
2. Admin console → Frontend Integration → **WhatsApp** tab → enter Access Token, Phone Number ID, and a self-chosen Verify Token (≥8 chars) → Save **first**.
3. In the Meta app → WhatsApp → Configuration → Webhook: callback URL `https://kms.autobuy.top/webhooks/whatsapp`, the same verify token, subscribe the **messages** field.
4. Press **Test**; end users message the WhatsApp number.

If a test fails, the card shows the reason and the screen lists the recent error-log entries for that tool.

### 2.4 Build the knowledge base

1. Go to **KB Management**.
2. Drag files into the upload zone (PDF, DOCX, XLSX, MD, TXT; ≤50 MB each) and pick which Intent Space each document belongs to (defaults to General).
3. Watch the status move **Pending → Processed** (parsing, AI structuring, chunking, and embedding happen automatically). An **Error** status shows the reason and can be retried with **Update**.
4. Use search/filter to manage documents; **Delete** or **Reassign** rebuilds the affected search index automatically.

### 2.5 Organize knowledge domains (Intent Spaces)

1. Go to **Intent Configuration**.
2. Create a space per domain — e.g. HR, Legal, Finance — with a short description and hint keywords (these improve classification).
3. Set the **confidence threshold** (default 70): queries classified below it are routed to General rather than a wrong domain.

### 2.6 Test before rolling out

1. Open **Test Chat** and ask real questions (e.g. 报销流程是什么？ / "How many days of annual leave?").
2. The answer streams in live, ends with its **Sources** and a status line (✅ success / 🔍 no match / ⚠️ failed, plus latency).
3. Test Chat runs the exact same pipeline as the IM bots and its queries appear in Analytics, so it is a faithful preview of the end-user experience.

### 2.7 Monitor and improve

1. **Analytics** shows the query history (text, detected intent, confidence, status), the most-accessed documents, the most common spaces, and per-space accuracy.
2. Mark the correct space for misclassified queries in the **verification** control — accuracy rates update from your verdicts, and adding keywords to spaces improves future routing.
3. Export everything as CSV for offline reporting.

---

## Tech Stack

| Concern | Technology |
|---|---|
| Language | Python 3.11 |
| Backend API + bot integration | FastAPI (uvicorn), httpx (async HTTP, proxy support) |
| Admin UI | Streamlit (multi-page app) |
| Metadata + query log | SQLite (WAL mode) |
| Vector search | FAISS (`IndexFlatIP` / `IndexIDMap2`, cosine via normalized vectors) |
| LLM | DashScope: Qwen-Turbo (classification), Qwen-Plus (RAG generation), Qwen-Max (document structuring) |
| Embeddings | DashScope `text-embedding-v3` |
| Document extraction | pypdf + pdfplumber (PDF/tables), python-docx (DOCX), openpyxl (XLSX), plain text/Markdown |
| Credential encryption | cryptography (Fernet) |
| Monitoring | boto3 → AWS CloudWatch metrics/logs + SNS alarms |
| Testing | pytest, pytest-asyncio, Hypothesis (property-based), respx |
| Deployment | Single AWS China EC2 instance behind nginx (TLS via Let's Encrypt) |

Exact pinned versions are in [`requirements.txt`](requirements.txt).

## Setup (self-hosting)

These steps take a clean environment to a running system using Docker Compose. For the full AWS China deployment guide (IAM, security groups, TLS, backups, verification), see [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md).

### Prerequisites

- Docker with the Compose plugin (`docker compose`).
- For production: a single AWS China EC2 instance (Amazon Linux 2023, `t3.medium`+), an EBS volume, an IAM instance profile allowing `cloudwatch:PutMetricData` and `logs:*`, and an HTTPS forward proxy reachable from the instance for Telegram/WhatsApp traffic.

### 1. Clone the repository

```bash
git clone <repository-url> intelliknow-kms
cd intelliknow-kms
```

### 2. Create the host-only secrets file

Secrets live only on the host in a file that is **never committed**. Copy the template and fill it in:

```bash
sudo install -d -m 700 /opt/intelliknow
sudo cp .env.example /opt/intelliknow/.env
sudo chmod 600 /opt/intelliknow/.env
sudo vi /opt/intelliknow/.env
```

Variables (see [`.env.example`](.env.example) and [`app/config.py`](app/config.py)):

| Variable | Required | Purpose |
|---|---|---|
| `CREDENTIAL_MASTER_KEY` | Yes | Fernet key encrypting the credential store. Generate once and keep it safe. |
| `TELEGRAM_PROXY_URL` | For Telegram | HTTPS forward proxy used only by the Telegram client (Telegram is unreachable from AWS China). |
| `WHATSAPP_PROXY_URL` | For WhatsApp | HTTPS forward proxy used only by the WhatsApp client (Meta Graph API is unreachable from AWS China). |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | For CloudWatch | `cn-north-1` (Beijing) or `cn-northwest-1` (Ningxia). |
| `DASHSCOPE_API_KEY` | Optional | Dev/local fallback; the credential store configured in the Admin UI is the primary source. |

Generate the master key:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

On a fresh EC2 host, `sudo bash deploy/ec2_bootstrap.sh` installs Docker, creates the `/data` layout, and writes a placeholder `.env`.

### 3. Create the persistent data volume

Everything stateful (SQLite DB, FAISS indexes, uploads, encrypted credentials, log buffer) lives under `/opt/intelliknow/data`:

```bash
sudo install -d -m 700 /opt/intelliknow/data/faiss /opt/intelliknow/data/uploads \
                       /opt/intelliknow/data/credentials /opt/intelliknow/data/logbuffer
```

### 4. Build and start the stack

```bash
docker compose up -d --build
docker compose ps        # app, admin-ui, cloudwatch-agent should all be Up
```

| Service | Port | Role |
|---|---|---|
| `app` | 8000 | FastAPI backend: bots, orchestrator, KB, RAG, analytics, admin REST API, chat/SSE endpoints |
| `admin-ui` | 8501 | Streamlit admin console |
| `cloudwatch-agent` | — | Ships container logs + host metrics to CloudWatch |

Restrict port `8501` to admin IPs via the security group, and terminate TLS in front of both ports (nginx or ALB) for production.

### 5. Configure credentials and verify

Follow the **User Guide** above: enter DashScope + IM credentials in Frontend Integration, upload a document, and confirm a cited answer in Test Chat and in each connected IM tool.

### 6. Provision CloudWatch alarms (once per environment)

```bash
python3 -m pip install --user boto3
python3 deploy/setup_cloudwatch_alarms.py \
    --region cn-north-1 \
    --alarm-email ops@example.com \
    --latency-threshold-ms 3000 \
    --error-rate-threshold-pct 5
```

## Testing

One command runs the complete suite (unit + property-based + integration):

```bash
make test          # or: pytest
```

First-time local environment:

```bash
make venv && make install && make test
```

### Demo seed

```bash
python scripts/seed_demo.py
```

## Project Structure

```
intelliknow-kms/
├── app/                      # FastAPI backend
│   ├── main.py               # App assembly, startup/shutdown, background tasks
│   ├── config.py             # Env-derived settings + persistent-storage paths
│   ├── db.py                 # SQLite bootstrap / schema
│   ├── bots/                 # Frontend integration: telegram.py, teams.py, whatsapp.py, dispatcher.py, monitor.py
│   ├── core/                 # Orchestrator + core data models
│   ├── kb/                   # Loaders, document processor, SQLite store, FAISS search
│   ├── ai/                   # DashScope client + prompt templates
│   ├── rag/                  # RAG assembly, citations, no-match handling, streaming
│   ├── security/             # Fernet credential store, log redaction, startup checks
│   ├── analytics/            # Query log, metrics, CSV export
│   ├── monitoring/           # CloudWatch publisher + local buffering
│   └── api/                  # Admin REST API + web chat (SSE) endpoints
├── admin_ui/                 # Streamlit multi-page app (6 screens)
│   ├── Home.py               # Dashboard
│   └── pages/                # Frontend Integration, KB Management, Intent Config, Analytics, Test Chat
├── deploy/                   # Deployment assets (guide, bootstrap, alarms, CDK)
├── scripts/seed_demo.py      # Demo data seeding
├── samples/                  # Bilingual sample corpus for demos
├── tests/                    # unit/ · properties/ · integration/
├── docs/                     # Architecture and design docs
├── docker-compose.yml        # app + admin-ui + cloudwatch-agent
├── Dockerfile.app            # Backend image
├── Dockerfile.admin-ui       # Streamlit UI image
├── Makefile                  # make test / venv / install
└── pyproject.toml            # Packaging + pytest config
```

## Documentation

- [Architecture Overview](docs/ARCHITECTURE.md)
- [High-Level Design (HLD)](docs/HLD.md)
- [Low-Level Design (LLD)](docs/LLD.md)
- [AI Usage Reflection](docs/AI_USAGE_REFLECTION.md)
- [Deployment Guide](deploy/DEPLOYMENT.md)
