# Microsoft Teams Integration — Setup Record

完成日期: 2026-07-30. 端到端已验证: Teams 客户端提问 → 引用来源的 RAG 回答.

## Architecture

```
Teams client ──► Bot Connector (Microsoft) ──► https://kms.autobuy.top/webhooks/teams
                                                nginx (TLS) ──► FastAPI :8000 (EC2)
                                                orchestrator ──► FAISS/RAG ──► DashScope
```

## Identity & Azure resources

| Item | Value |
|---|---|
| Entra tenant | `Yunda855.onmicrosoft.com` (`0f643a1e-e53d-4e57-8f28-ede605fb1aef`) |
| Azure Bot resource | `intelliknow-kms-bot-1` (F0, **Single Tenant**) |
| Bot App ID | `9c65cc4e-be62-40d1-8101-65f6afe6ee38` |
| App password | Entra app registration client secret; stored Fernet-encrypted in `/data/credentials/credentials.enc` on the instance (saved via Admin UI / API). **Rotate it** — it was exposed in a chat session during setup |
| Messaging endpoint | `https://kms.autobuy.top/webhooks/teams` |
| Channels | Microsoft Teams (Commercial) enabled |
| Tenant admin account | `admin@Yunda855.onmicrosoft.com` (Global Administrator, M365 Business Basic license) |

Single Tenant note: Bot Framework tokens must be acquired from the
tenant-specific endpoint. Backend supports this via the `TEAMS_TENANT_ID`
env var (`app/config.py`, `app/bots/teams.py`); when unset it falls back to
the multi-tenant `botframework.com` endpoint.

## Infrastructure (AWS China, account 620469932353)

| Item | Value |
|---|---|
| EC2 instance | `intelliknow-demo` (`i-0f91043746850400b`), 52.80.35.65, cn-north-1 |
| Domain | `kms.autobuy.top` → A record 52.80.35.65 (Aliyun DNS, zone `autobuy.top`) — do NOT touch `www`/`dev`/`qywx`/apex records (autobuy prod/dev services) |
| TLS | Let's Encrypt via certbot, nginx `/etc/nginx/conf.d/kms.autobuy.top.conf`, auto-renewal via certbot systemd timer, expires/renews ~Oct 2026 |
| App service | systemd `ikms-api.service`, uvicorn on :8000, env from `/opt/intelliknow/.env` (includes `TEAMS_TENANT_ID`) |
| Admin UI | `http://52.80.35.65:8501` (Streamlit) |
| Security group | `intelliknow-demo-sg`: 22, 80 (ACME), 443, 8000, 8501 |

## M365 / Teams app distribution

- Subscription: Microsoft 365 Business Basic, ¥54/月/license (includes TEAMS1
  service plan). Purchased via the microsoft.com buy flow — the admin-center
  checkout was blocked by a billing-account review ("cong" MCA account).
- Teams app package: `deploy/teams-app/` (manifest.json + icons; zip is a
  build artifact, rebuild by zipping the three files).
- The app is published to the tenant app catalog ("Built for your org") and
  pre-installed for the admin user — done via Graph API
  (`scripts/publish_teams_app.py`).
- Helper scripts (device-code auth as tenant admin):
  - `scripts/assign_license.py` — assign the M365 license to a user
  - `scripts/publish_teams_app.py` — publish app to catalog + install for user
  - `scripts/alidns.py` — Aliyun DNS record list/add (needs active AccessKey;
    the key in `.env` was disabled at Aliyun as of setup date)

## Demo options for external customers

1. **Screen share** — zero setup.
2. **Licensed user (recommended, +¥54/月)** — raise license count in M365
   admin center, create `demo@Yunda855.onmicrosoft.com`, assign license.
   Customer gets personal 1:1 bot chat.
3. **Guest channel (free)** — create a team, invite customer by email as B2B
   guest, add the bot to the team; customer switches org in Teams and
   `@IntelliKnow KMS <question>`. Bots do NOT work in plain federated
   external chats — guest membership is required.
4. Public Teams store — not pursued (requires Microsoft partner validation).

## Known gaps / follow-ups

- **Webhook JWT validation is a no-op** (`NoopJwtValidator` in `app/main.py`
  wiring): the public endpoint accepts unauthenticated Bot Framework
  activities. Accepted for demo; implement real validation before production.
- Rotate the bot app password (exposed during setup) and re-save via Admin UI.
- M365 subscription renews monthly — cancel in M365 admin center → Billing →
  Your products when the demo period ends.
- Old billing account "cong" may still be under review; irrelevant unless
  purchasing through the admin-center catalog again.

## Troubleshooting

- Bot no reply: `ssh -i ~/.ssh/intelliknow-demo.pem ec2-user@52.80.35.65`,
  check `systemctl status ikms-api`, `journalctl -u ikms-api -n 100`, and the
  integration error log (Admin UI → Frontend Integration, last 50 entries).
- Webhook health: `curl https://kms.autobuy.top/health` (from a non-Amazon
  network; corporate DNS filtering may block the domain locally).
- Token errors (`unauthorized_client` at login.microsoftonline.com): verify
  `TEAMS_TENANT_ID` is set in `/opt/intelliknow/.env` — Single Tenant apps
  cannot use the multi-tenant endpoint.
- Wrong-space answers: check Analytics query log (`detected_space_id`), fix
  document space assignment in KB Management, or verify the query to feed
  the accuracy metric.
