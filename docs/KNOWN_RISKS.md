# Known Risks (Accepted)

This file records risks that have been consciously reviewed and **accepted**
by the owner, with the rationale and possible future mitigations.

## 1. Telegram bot is publicly accessible (accepted 2026-07-30)

**Risk**: Telegram bots are public by username. Anyone who finds
`@AIA_Intelli_KMS_BOT` can open a private chat, ask questions against the
knowledge base (company policy documents), and consume DashScope API quota.
The bot can also be added to arbitrary group chats (`can_join_groups: true`),
and the app performs no sender authentication — the inbound gate
(`app/bots/base.py: evaluate_inbound`) validates message shape only, not
identity.

**Impact**:
- Knowledge-base content (HR/Legal/Finance policies) disclosed to strangers.
- Uncontrolled DashScope token spend from third-party queries.
- Query log polluted with external traffic, skewing analytics.

**Decision**: Accepted. This is a demo/MVP deployment with low security
expectations and non-sensitive sample documents.

**Future mitigations (if requirements change)**:
1. BotFather → `/setjoingroups` → Disable, so the bot only works in 1-on-1
   private chats.
2. App-level allowlist: a `TELEGRAM_ALLOWED_CHAT_IDS` env var checked in the
   Telegram adapter dispatch; unknown chat IDs get a "this bot is private"
   reply and are never forwarded to the orchestrator.
3. Rate limiting per chat ID to bound API spend.

## 2. Admin portal is internet-reachable behind one shared credential (accepted 2026-07-31)

**Risk**: The admin console originally sat behind an SSH tunnel with nginx
serving it only on `/admin/`. To make the demo usable by the customer it now
lives at the site root of `https://kms.autobuy.top`, protected by a **branded
in-app login page** (Streamlit) that validates a **single shared account**
(`aia`) against the `PORTAL_USER` / `PORTAL_PASSWORD` environment variables.
There is no per-user identity, no lockout or rate limiting on passcode
guesses, and the authenticated flag lives only in the per-session Streamlit
state (a full browser refresh requires signing in again; `/?logout=1` clears
it). Anyone holding or guessing that one passcode gets full admin power:
upload/delete knowledge-base documents, read masked integration status,
reconfigure IM credentials, and use the Test Chat.

Direct backend exposure is limited: ports 8000/8501 are closed in the
security group (verified 2026-07-31); only nginx on 80/443 is reachable, and
it forwards nothing but the UI, `/webhooks/{teams,whatsapp}`, `/health`, and
`/logout`. Because auth is now enforced in the Streamlit app rather than at
nginx, the app process is reachable (pre-login) by anyone on the internet —
acceptable for the demo, but a determined attacker faces the app layer, not
just nginx.

**Impact**:
- A leaked or brute-forced password compromises the whole admin surface at
  once, with no audit trail of who acted.
- The Test Chat page runs full RAG queries, so portal access directly
  converts to DashScope token spend and query-log entries.

**Decision**: Accepted. Demo deployment, non-sensitive sample corpus, and the
credential is distributed out-of-band to a small audience.

**Future mitigations (if requirements change)**:
1. Per-user accounts via a real auth layer (OIDC/SSO reverse proxy such as
   oauth2-proxy) instead of a shared in-app passcode.
2. Rate limiting / lockout on failed sign-in attempts to throttle guessing.
3. IP allowlist for the UI location block, keeping only webhooks public.
4. Separate a read-only "viewer" role from the admin role.

## 3. Portal exposes infrastructure metrics via a broadened EC2 role (accepted 2026-07-31)

**Risk**: To embed monitoring in the portal without requiring AWS accounts,
the EC2 instance role gained read permissions (`cloudwatch:GetMetricData`,
`cloudwatch:DescribeAlarms`) and the backend proxies them at
`/monitoring/cloudwatch` (reachable only through the Streamlit UI, i.e.
behind the portal login). Every portal user can therefore see query-latency,
error-rate, and alarm-state data for the deployment.

**Impact**:
- Low-sensitivity operational telemetry disclosed to anyone with the shared
  portal credential (see risk 2 — the same single password gates it).
- Marginal widening of the instance role's blast radius; the added actions
  are read-only and metrics-scoped (no logs, no configuration access).

**Decision**: Accepted. The convenience of AWS-account-free monitoring for
the customer outweighs the exposure of non-sensitive metrics.

**Future mitigations (if requirements change)**:
1. Restrict the monitoring endpoint to a privileged role once per-user auth
   exists.
2. Scope the IAM statement to the `IntelliKnow` namespace via a condition key
   if tighter least-privilege is required.

## 4. The 3-second latency target is an SLO, not a guarantee (accepted 2026-07-31)

**Risk**: The project specification asks for query/response delivery within
3 seconds. End-to-end latency is dominated by the AI generation call
(Qwen-Max, up to a 10s timeout), which is outside the application's control,
so individual answers can and do exceed 3 seconds (typical range 2–4s).
Enforcing a hard 3s deadline would require cutting generation off
mid-answer, converting a slightly slow success into a truncated or failed
response.

**Impact**:
- Some queries exceed the 3s target; users wait a moment longer but still
  receive a complete, cited answer.
- Strict reading of the spec's "≤3s latency" is met as a monitored SLO
  rather than a per-request bound.

**Decision**: Accepted. Latency is measured per query (`latency_ms` in the
Query_Log), published to CloudWatch, and alarmed on sustained p95 > 3,000 ms
(`latency_alarm_ms` setting), which protects the user experience without
degrading answer quality. The web Test Chat additionally streams tokens so
first text appears in about a second.

**Future mitigations (if requirements change)**:
1. Stream responses on the IM frontends (Telegram edit-message loop) so
   perceived latency drops below 3s even when total generation runs longer.
2. Use a faster generation model for short/simple queries, keeping the
   quality model for complex ones.
3. Cache answers for repeated queries within an intent space.
