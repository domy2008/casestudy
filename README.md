# IntelliKnow KMS

A Gen AI-powered Knowledge Management System delivered as a production-ready MVP. End users ask questions from **Telegram**, **Microsoft Teams**, or **WhatsApp** and receive concise, **cited** answers generated with Retrieval-Augmented Generation (RAG) over an admin-managed document knowledge base. An admin runs the entire system from a five-screen web UI.

- Multi-frontend intake and delivery: Telegram (long-poll via an outbound proxy), Microsoft Teams (Bot Framework webhook), and WhatsApp (Meta Cloud API webhook + proxied Graph API replies).
- Document-driven knowledge base with AI parsing, structuring (including embedded tables), and semantic search.
- An orchestrator that classifies query intent and routes to the right knowledge domain, falling back to a General space below a configurable confidence threshold.
- RAG responses with citations, formatted per frontend tool.
- A Streamlit admin UI: Dashboard, Frontend Integration, KB Management, Intent Configuration, and Analytics.
- CloudWatch metrics, logs, and alarms for latency, error rate, and integration health.

Designed for a single-instance, one-person-workload MVP: lightweight, well-separated Python modules, no message brokers or microservices.

## Tech Stack

| Concern | Technology |
|---|---|
| Language | Python 3.11 |
| Backend API + bot integration | FastAPI (uvicorn), httpx (async HTTP, proxy support) |
| Admin UI | Streamlit (multi-page app) |
| Metadata + query log | SQLite (WAL mode) |
| Vector search | FAISS (`IndexFlatIP` / `IndexIDMap2`, cosine via normalized vectors) |
| LLM (parsing, classification, RAG) | Aliyun Tongyi **Qwen-Max** via the DashScope API |
| Embeddings | DashScope `text-embedding-v3` |
| Document extraction | pypdf + pdfplumber (PDF/tables), python-docx (DOCX), openpyxl (XLSX), plain text/Markdown |
| Credential encryption | cryptography (Fernet) |
| Monitoring | boto3 → AWS CloudWatch metrics/logs + SNS alarms |
| Testing | pytest, pytest-asyncio, Hypothesis (property-based), respx |
| Deployment | Docker Compose on a single AWS China EC2 instance |

Exact pinned versions are in [`requirements.txt`](requirements.txt).

## Setup

These steps take a clean environment to a running system. They mirror the actual `docker-compose.yml`, `Dockerfile.app`, `Dockerfile.admin-ui`, and `deploy/` layout in this repo. For the full AWS China deployment guide (IAM, security groups, TLS for the Teams webhook, backups, verification), see [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md).

### Prerequisites

- Docker with the Compose plugin (`docker compose`).
- For production: a single AWS China EC2 instance (Amazon Linux 2023, `t3.medium`+), an EBS volume, an IAM instance profile allowing `cloudwatch:PutMetricData` and `logs:*` (plus `cloudwatch:PutMetricAlarm` and `sns:*` for the alarm script), and an HTTPS forward proxy reachable from the instance for Telegram traffic.

### 1. Clone the repository

```bash
git clone <repository-url> intelliknow-kms
cd intelliknow-kms
```

### 2. Create the host-only secrets file

Secrets live only on the host in a file that is **never committed** (it is covered by `.gitignore`). The `app` service reads it via `env_file: /opt/intelliknow/.env` in `docker-compose.yml`. Copy the template and fill it in:

```bash
sudo install -d -m 700 /opt/intelliknow
sudo cp .env.example /opt/intelliknow/.env
sudo chmod 600 /opt/intelliknow/.env
sudo vi /opt/intelliknow/.env
```

Variables (see [`.env.example`](.env.example) and [`app/config.py`](app/config.py)):

| Variable | Required | Purpose |
|---|---|---|
| `CREDENTIAL_MASTER_KEY` | Yes | Fernet key encrypting `/data/credentials/credentials.enc`. Generate once (below) and keep it safe — losing it makes the credential store unreadable. |
| `TELEGRAM_PROXY_URL` | Yes (for Telegram) | HTTPS forward proxy used **only** by the Telegram client, since Telegram is unreachable from AWS China. DashScope and Teams traffic goes direct. |
| `WHATSAPP_PROXY_URL` | Yes (for WhatsApp) | HTTPS forward proxy used **only** by the WhatsApp client, since the Meta Graph API (`graph.facebook.com`) is unreachable from AWS China. May reuse the Telegram proxy. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Yes (for CloudWatch) | AWS China region: `cn-north-1` (Beijing) or `cn-northwest-1` (Ningxia). |
| `DASHSCOPE_API_KEY` | Optional | Dev/local fallback API key. At runtime the Credential Store (configured in the Admin UI) is the primary source; this env var is a convenience for local use. |

Generate the master key:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

On a fresh EC2 host, `sudo bash deploy/ec2_bootstrap.sh` installs Docker, enables it at boot, creates the `/data` layout, and writes a placeholder `/opt/intelliknow/.env` for you to fill in.

### 3. Create the persistent data volume

Everything stateful (SQLite DB, FAISS indexes, uploads, encrypted credentials, CloudWatch log buffer) lives under `/opt/intelliknow/data` on the host and is bind-mounted into the `app` container at `/data`. It survives container restart, recreation, and image updates.

```bash
sudo install -d -m 700 /opt/intelliknow/data/faiss /opt/intelliknow/data/uploads \
                       /opt/intelliknow/data/credentials /opt/intelliknow/data/logbuffer
```

### 4. Build and start the stack

```bash
docker compose up -d --build
docker compose ps        # app, admin-ui, cloudwatch-agent should all be Up
```

This starts three services (see `docker-compose.yml`):

| Service | Port | Role |
|---|---|---|
| `app` | 8000 | FastAPI backend: bots, orchestrator, KB, RAG, analytics, admin REST API, Telegram poller, monitoring publisher (Teams webhook + admin API) |
| `admin-ui` | 8501 | Streamlit admin UI; reaches the backend at `http://app:8000` |
| `cloudwatch-agent` | — | Ships container logs + host metrics to CloudWatch |

Every service uses `restart: unless-stopped`, so a crashed container comes back within ~60s and the whole stack returns within ~5 minutes of an OS boot (Docker enabled at boot). Restrict port `8501` to admin IPs via the security group.

### 5. Configure credentials in the Admin UI

Open the Admin UI at `http://<instance>:8501` and go to **Frontend Integration**. Enter and save your Telegram, Teams, WhatsApp, and DashScope credentials there. They are validated, then stored **Fernet-encrypted** under `/data/credentials/` — never in source or version control. Re-reading shows each value masked (last ≤4 characters).

### 6. Provision CloudWatch alarms (once per environment)

```bash
python3 -m pip install --user boto3
python3 deploy/setup_cloudwatch_alarms.py \
    --region cn-north-1 \
    --alarm-email ops@example.com \
    --latency-threshold-ms 3000 \
    --error-rate-threshold-pct 5
```

This creates the `intelliknow-alarms` SNS topic and three alarm groups (query latency p95, error rate, and per-tool integration health). Confirm the SNS email subscription from the message you receive.

### 7. Verify

- `docker compose ps` shows all three services `Up`.
- `curl http://localhost:8000/health` returns `200` from the instance.
- The Admin UI loads at `:8501` and the Dashboard renders its summary cards.
- Saved credentials appear masked on re-read.
- Telegram: message the bot and receive a cited answer (confirms the proxy path).
- Teams: message the bot and receive a cited answer (confirms the inbound webhook).
- WhatsApp: message the test number and receive a cited answer (confirms the webhook + proxied Graph API path).
- Upload a document and watch its status move Pending → Processed, then query it.

The full manual verification checklist (restart recovery, boot recovery, volume persistence, proxy-failure handling, CloudWatch datapoints and alarm firing) is in [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md).

## Integration Guide

### Telegram (long polling via an outbound proxy)

Telegram cannot be reached directly from AWS China, and its webhook mode would additionally require Telegram to reach the instance — equally blocked. The integration therefore uses **long polling**, which is outbound-only, so a single proxy path handles both directions.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the bot token (format `<digits>:<alphanumeric>`).
2. Ensure `TELEGRAM_PROXY_URL` in `/opt/intelliknow/.env` points at an HTTPS forward proxy reachable from the instance and located where Telegram is reachable. Only the Telegram httpx client uses this proxy.
3. In the Admin UI → **Frontend Integration**, save the Telegram bot token.
4. The backend runs a `getUpdates` long-poll loop through the proxy and replies with `sendMessage`. Responses are plain text with a `Sources:` citation footer, hard-capped at Telegram's 4096-character limit (body is truncated before citations are dropped). Sends retry up to 2 more times on failure, and up to 3 times on proxy connectivity errors.

### Microsoft Teams (Bot Framework webhook)

The Bot Connector service is reachable from AWS China, so Teams uses the standard **inbound webhook**.

1. Register a bot in the Azure Bot / Bot Framework portal and obtain the App ID (a GUID) and App Password (client secret).
2. Set the bot's messaging endpoint to `https://<your-host>/webhooks/teams`. Bot Framework requires HTTPS, so terminate TLS in front of port `8000` (ALB + ACM certificate, or nginx + certificate on the host).
3. In the Admin UI → **Frontend Integration**, save the Teams App ID and App Password.
4. The backend receives Bot Framework activities at `POST /webhooks/teams` (validated with the app credentials) and replies via the Bot Connector service URL from the activity. Responses use Teams-native Markdown (bullet lists, bold citations) within Teams limits.

### WhatsApp (Meta Cloud API webhook + proxied replies)

Inbound messages arrive as Meta webhook notifications (WhatsApp has no long-polling mode), while outbound replies call the Graph API — which is unreachable from AWS China, so replies are routed through `WHATSAPP_PROXY_URL`.

1. Create a Meta app (Business type) at developers.facebook.com and add the **WhatsApp** product; note the **Phone Number ID** from API Setup and generate a long-lived **access token** via a Business system user (`whatsapp_business_messaging` + `whatsapp_business_management`).
2. Register your phone as a test recipient (API Setup → To list) and verify the code delivered to your WhatsApp.
3. In the Admin UI → **Frontend Integration**, save the Access Token, Phone Number ID, and a self-chosen Verify Token (≥8 chars). Save these **before** configuring the webhook — verification reads the stored token.
4. In the Meta app → WhatsApp → Configuration → Webhook, set the callback URL to `https://<your-host>/webhooks/whatsapp` with the same verify token, then subscribe the **messages** field.
5. Ensure the WABA is subscribed to your app (`POST /{waba_id}/subscribed_apps`) — the dashboard flow may skip this, in which case webhook verification succeeds but no messages are delivered.
6. Responses are plain text with a `Sources:` citation footer within WhatsApp's 4096-character limit; sends retry on failure and on proxy connectivity errors, mirroring Telegram.

All three integrations show a live connection status (Connected / Error / Disconnected) and an end-to-end **Test** button on the Frontend Integration screen, plus the 50 most recent integration error log entries.

## Testing

The single documented command that runs the complete suite (unit + property-based + integration) in one pytest run:

```bash
make test
```

Equivalent direct invocation:

```bash
pytest
```

Test discovery and options are configured in `pyproject.toml` (`testpaths = ["tests"]`, `-v -ra`, async auto-mode). pytest reports a pass/fail line per test and a total/passed/failed summary. First-time setup of a local environment:

```bash
make venv       # create .venv with Python 3.11
make install    # install pinned dependencies
make test
```

Tests are organized under `tests/unit/`, `tests/properties/` (Hypothesis property-based tests), and `tests/integration/` (end-to-end query flow with the AI model, embeddings, and bot APIs mocked).

### Demo seed

Populate the system with sample intent spaces and documents for a demo:

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
│   ├── rag/                  # RAG assembly, citations, no-match handling
│   ├── security/             # Fernet credential store, log redaction, startup checks
│   ├── analytics/            # Query log, metrics, CSV export
│   ├── monitoring/           # CloudWatch publisher + local buffering
│   └── api/                  # Admin REST API consumed by the Streamlit UI
├── admin_ui/                 # Streamlit multi-page app (5 screens)
│   ├── Home.py               # Dashboard
│   └── pages/                # Frontend Integration, KB Management, Intent Config, Analytics
├── deploy/                   # Deployment assets
│   ├── DEPLOYMENT.md         # Full AWS China deployment guide
│   ├── ec2_bootstrap.sh      # One-time EC2 host bootstrap
│   ├── setup_cloudwatch_alarms.py   # CloudWatch alarms + SNS topic
│   ├── cloudwatch-agent-config.json # Log/metric shipping config
│   └── cdk/                  # Infrastructure-as-code
├── scripts/
│   └── seed_demo.py          # Demo data seeding
├── tests/                    # unit/ · properties/ · integration/
├── docs/                     # Architecture and design docs (see below)
├── docker-compose.yml        # app + admin-ui + cloudwatch-agent
├── Dockerfile.app            # Backend image (Python 3.11 + FastAPI)
├── Dockerfile.admin-ui       # Streamlit UI image
├── Makefile                  # `make test`, `make venv`, `make install`
├── requirements.txt          # Pinned dependencies
└── pyproject.toml            # Packaging + pytest config
```

## Documentation

- [Architecture Overview](docs/ARCHITECTURE.md)
- [High-Level Design (HLD)](docs/HLD.md)
- [Low-Level Design (LLD)](docs/LLD.md)
- [AI Usage Reflection](docs/AI_USAGE_REFLECTION.md)
- [Deployment Guide](deploy/DEPLOYMENT.md)
