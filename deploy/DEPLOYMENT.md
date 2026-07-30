# IntelliKnow KMS — AWS China Deployment Guide

Target topology (design, Req 12): a single EC2 instance in an AWS China region
(cn-north-1 Beijing or cn-northwest-1 Ningxia) running the stack via Docker
Compose, with CloudWatch monitoring and SNS alarm notifications.

## 1. Prerequisites

| Item | Notes |
|---|---|
| AWS China account | Separate partition (`aws-cn`) from global AWS; needs ICP-related account verification for public web exposure |
| EC2 instance | Amazon Linux 2023, `t3.medium`+ recommended (FAISS + embeddings in memory), EBS gp3 volume ≥ 20 GB |
| IAM instance profile | `cloudwatch:PutMetricData`, `logs:CreateLogGroup/CreateLogStream/PutLogEvents`; for the alarm script additionally `cloudwatch:PutMetricAlarm`, `sns:CreateTopic`, `sns:Subscribe` |
| Security group | Inbound: 443/80 → 8000 only if Teams webhook is exposed publicly (behind HTTPS); 8501 restricted to admin IP ranges; SSH from admin IPs. Outbound: 443 open |
| Outbound proxy | HTTPS forward proxy reachable from the instance, deployed in a region with Telegram connectivity (e.g., a small instance in an overseas region). Only Telegram traffic uses it (Req 12.2) |
| DashScope API key | Aliyun DashScope is directly reachable from AWS China — no proxy needed |
| Teams webhook HTTPS | Bot Framework requires an HTTPS endpoint. Terminate TLS in front of port 8000 (ALB + ACM cert, or nginx + certificate on the host) |

## 2. Host bootstrap (once)

```bash
sudo bash deploy/ec2_bootstrap.sh
```

This installs Docker, enables it at boot via systemd (Req 12.3), and creates:

- `/opt/intelliknow/data/` — persistent bind mount for SQLite, FAISS indexes,
  uploads, encrypted credentials, and the CloudWatch log buffer (Req 12.4)
- `/opt/intelliknow/.env` — host-only secrets file (Req 11.1), mode 600

Fill in `/opt/intelliknow/.env` (see `.env.example` for docs):

```bash
# Generate the Fernet master key once and keep it safe — losing it makes
# the encrypted credential store unreadable.
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
sudo vi /opt/intelliknow/.env   # set CREDENTIAL_MASTER_KEY, TELEGRAM_PROXY_URL
```

## 3. Build and start the stack

```bash
git clone <repository-url> intelliknow-kms && cd intelliknow-kms
docker compose up -d --build
docker compose ps        # app, admin-ui, cloudwatch-agent all Up
```

- `restart: unless-stopped` on every service: dockerd restarts a crashed
  container within 60 seconds (Req 12.5) and brings the stack back after an
  OS boot (Req 12.3).
- Admin UI: `http://<instance>:8501` (restrict by security group).
- Enter Telegram / Teams / WhatsApp / DashScope credentials via the Frontend Integration
  screen — they are stored Fernet-encrypted under `/data/credentials/`.

## 4. Provision CloudWatch alarms (once per environment)

```bash
python3 -m pip install --user boto3
python3 deploy/setup_cloudwatch_alarms.py \
    --region cn-north-1 \
    --alarm-email ops@example.com \
    --latency-threshold-ms 3000 \
    --error-rate-threshold-pct 5
```

Creates the `intelliknow-alarms` SNS topic and three alarm groups (Req 13.3–13.5, 13.7):

| Alarm | Trigger |
|---|---|
| `IntelliKnow-QueryLatencyP95` | p95 of `QueryLatencyMs` > 3000 ms over 5 min |
| `IntelliKnow-ErrorRate` | `ErrorRatePercent` > 5% over 5 min |
| `IntelliKnow-IntegrationHealth-{telegram,teams}` | health metric < 1 (or missing) over 5 min |

Confirm the SNS email subscription from the message you receive.

## 5. Manual deployment verification checklist

Run after every fresh deployment or infrastructure change. These behaviors are
environment-dependent and are verified manually, not by the automated suite.

- [ ] `docker compose ps` shows `app`, `admin-ui`, `cloudwatch-agent` all `Up`
- [ ] `curl http://localhost:8000/health` returns 200 from the instance
- [ ] Admin UI loads at `:8501` and the Dashboard renders all summary cards
- [ ] Credentials saved via the UI appear masked (last ≤4 chars) on re-read
- [ ] Telegram: send a message to the bot → cited answer arrives (proxy path works)
- [ ] Teams: send a message to the bot → cited answer arrives (inbound webhook works)
- [ ] WhatsApp: send a message to the test number → cited answer arrives (webhook + proxy path works). See `deploy/WHATSAPP_SETUP.md`
- [ ] Upload a sample document → status transitions Pending → Processed and it becomes searchable
- [ ] **Restart recovery (Req 12.5):** `docker kill intelliknow-kms-app-1` → container is back `Up` within 60 s and answers queries
- [ ] **Boot recovery (Req 12.3):** `sudo reboot` → all three containers `Up` within 5 min of boot without manual action
- [ ] **Volume persistence (Req 12.4):** after the reboot, previously uploaded documents, credentials, and query history are intact
- [ ] CloudWatch: `IntelliKnow` namespace shows fresh `QueryLatencyMs`, `ErrorRatePercent`, `IntegrationHealthy{tool}` datapoints (≤ 2 min old)
- [ ] CloudWatch Logs: `/intelliknow/containers` log group receives application JSON logs within ~60 s (Req 13.2)
- [ ] Alarms: temporarily set `--latency-threshold-ms 1` and re-run the alarm script, generate a query, confirm the SNS email fires, then restore the real threshold (Req 13.7)
- [ ] **Proxy failure handling (Req 12.6):** point `TELEGRAM_PROXY_URL` at an unreachable host, restart `app`, confirm 3 retries are logged as proxy failures, then restore
- [ ] No credential values appear in `docker compose logs app` output (Req 11.3)

## 6. Operations quick reference

```bash
docker compose logs -f app            # tail backend logs (JSON)
docker compose restart app            # restart backend only
docker compose up -d --build          # deploy a new code version (data survives)
sudo tar czf /tmp/intelliknow-backup.tgz -C /opt/intelliknow data   # cold backup
```

Notes:

- The SQLite database uses WAL mode; for a consistent hot backup prefer
  `sqlite3 /opt/intelliknow/data/app.db ".backup /tmp/app.db.bak"`.
- FAISS index files are derived artifacts — they are rebuilt from SQLite chunk
  embeddings, so the database and `/data/uploads` are the critical backup set.
