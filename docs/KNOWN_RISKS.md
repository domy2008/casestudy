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
