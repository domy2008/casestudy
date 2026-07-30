# WhatsApp Integration — Setup Record

Completed: 2026-07-30. End-to-end verified: WhatsApp client question →
cited RAG answer (query log id 22, tool=whatsapp, Success, 8.2s).

## Architecture

```
WhatsApp user ──► Meta Cloud API ──► https://kms.autobuy.top/webhooks/whatsapp
                  (inbound webhook)   nginx (TLS) ──► FastAPI :8000 (EC2)
                                      orchestrator ──► FAISS/RAG ──► DashScope
outbound reply ◄── Outbound_Proxy (127.0.0.1:8888 tunnel) ◄── FastAPI
                   (graph.facebook.com is blocked from AWS China)
```

Unlike Teams (inbound webhook only), WhatsApp needs BOTH an inbound webhook
AND an outbound path to `graph.facebook.com`. Outbound Graph API calls are
routed through `WHATSAPP_PROXY_URL` (same local proxy tunnel as Telegram).

## Meta resources

| Item | Value |
|---|---|
| Meta App | `cs` (App ID `919018393883221`), Business type, WhatsApp product |
| Business portfolio | `CustomerService` (`1339837631679781`) |
| WABA ID | `1070654518844979` |
| Test sender number | `+1 (555) 664-0311`, Phone Number ID `1301529486366246` |
| Access token | System user `AIA_KMS_BOT` (Admin), never-expiring token with `whatsapp_business_messaging` + `whatsapp_business_management`; stored Fernet-encrypted via Admin UI. **Rotate it** — it was exposed in a chat session during setup |
| Webhook | `https://kms.autobuy.top/webhooks/whatsapp`, verify token stored in Credential_Store, `messages` field subscribed |
| Test recipient | `+86 139-1844-3628` (registered + verified in API Setup → To list) |

## Server configuration

| Item | Value |
|---|---|
| Code | `app/bots/whatsapp.py` + updated modules deployed to `/opt/intelliknow/app` (tar over scp; the server checkout is not a git repo) |
| Proxy | `WHATSAPP_PROXY_URL=http://127.0.0.1:8888` in `/opt/intelliknow/.env` (reuses the Telegram proxy tunnel) |
| Service | systemd `ikms-api.service` restarted after deploy + env change |
| nginx | `location /` proxies everything to :8000 — no extra block needed |

## Gotcha that cost time (record for future)

Meta's webhook **verification** succeeded but no `messages` POSTs arrived.
Root cause: the WABA was not subscribed to the app — the dashboard flow only
subscribed Meta's internal "WA DevX Webhook Events 1P App". Fix via API:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "https://graph.facebook.com/v20.0/1070654518844979/subscribed_apps"
# then GET the same URL and confirm app id 919018393883221 is listed
```

## Known gaps / follow-ups

- **Rotate the access token** (exposed in chat during setup): Business
  Settings → System Users → `AIA_KMS_BOT` → revoke + generate new →
  re-save via Admin UI.
- Webhook POST does not validate Meta's `X-Hub-Signature-256` signature
  (accepted for demo, like the Teams no-op JWT validator).
- Latency observed 8.2s > the 3s P95 target — bottleneck is LLM generation
  plus the cross-border proxy hop; mention proactively if asked in the demo.
- Test number limits: up to 5 registered recipients; user must message first
  (business-initiated free-form messages are not allowed outside the
  24-hour customer-service window).

## Demo notes

- The phone needs VPN to reach WhatsApp from a mainland network.
- The user must send the first message (opens the 24h window); replies from
  the KMS then flow freely.

## Troubleshooting

- No reply: check integration error log (Admin UI → Frontend Integration),
  `sudo journalctl -u ikms-api -n 100`, and
  `sudo grep "POST /webhooks/whatsapp" /var/log/nginx/access.log`.
- Webhook verified but no message POSTs: re-check WABA `subscribed_apps`
  (see gotcha above) and that the `messages` field is subscribed.
- Proxy errors in the log: the local :8888 tunnel is down — same tunnel as
  Telegram, so Telegram will be failing too.
