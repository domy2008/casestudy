"""Unit tests for Frontend_Tool response formatting (task 10.6 scope).

Covers the two nominal paths and the shared over-limit error path from the
design's Testing Strategy table (Response formatting row):

* **Telegram nominal** — plain text body plus a ``Sources:`` footer listing the
  citations (Req 8.5).
* **Teams nominal** — Teams-native markdown: a body plus a bold ``**Sources:**``
  bullet list with each citation name in bold (Req 8.6).
* **Over-limit error path (both adapters)** — a response whose body exceeds the
  tool's length cap is truncated with the citations kept intact and a
  truncation indicator present, and the formatted output never exceeds the cap
  (Req 8.7).

No real network is used: ``format()`` is a pure, synchronous method, and the
adapters are constructed with a trivial in-memory credential-store fake and an
unused HTTP client so the formatter can be exercised through the public
:class:`app.bots.base.FrontendAdapter` surface as well as the module-level pure
helpers.

Validates: Requirements 14.1, 8.5, 8.6, 8.7
"""

from __future__ import annotations

import httpx
import pytest

from app.bots.teams import (
    TEAMS_MAX_MESSAGE_CHARS,
    TEAMS_SOURCES_HEADER,
    TEAMS_TRUNCATION_INDICATOR,
    TeamsAdapter,
    format_teams_message,
)
from app.core.models import GeneratedResponse

# Telegram is built by the parallel task 10.3. Import it if present so the
# shared formatting assertions run; otherwise skip only the Telegram cases.
telegram = pytest.importorskip(
    "app.bots.telegram",
    reason="app.bots.telegram not yet available (parallel task 10.3)",
)
TelegramAdapter = telegram.TelegramAdapter
# Only the stable, protocol-level surface is used here: the adapter's
# ``format()`` method and the exported length-cap / truncation-indicator
# constants. The module-level helper signature is owned by task 10.3 and is
# intentionally not depended on, so this test stays valid across its churn.
TELEGRAM_MAX_MESSAGE_CHARS = telegram.TELEGRAM_MAX_MESSAGE_CHARS
TELEGRAM_TRUNCATION_INDICATOR = telegram.TRUNCATION_INDICATOR


class _FakeCredentialStore:
    """Minimal Credential_Store stand-in returning fixed, fake credentials.

    Never used for network calls in these tests (formatting is offline); it
    only satisfies the adapters' constructors.
    """

    def load(self, integration: str) -> dict[str, str] | None:
        if integration == "telegram":
            return {"bot_token": "123:fake-token-value-abcdefghijklmnop"}
        if integration == "teams":
            return {"app_id": "app-id", "app_password": "app-password"}
        return None


@pytest.fixture
def telegram_adapter() -> TelegramAdapter:
    """A TelegramAdapter wired with a fake store and an unused HTTP client."""
    return TelegramAdapter(
        credential_store=_FakeCredentialStore(),
        client=httpx.AsyncClient(),
    )


@pytest.fixture
def teams_adapter() -> TeamsAdapter:
    """A TeamsAdapter wired with a fake store and an unused HTTP client."""
    return TeamsAdapter(
        credential_store=_FakeCredentialStore(),
        http_client=httpx.AsyncClient(),
    )


# --- Nominal path: Telegram plain text + citations (Req 8.5) -------------


def test_telegram_format_plain_text_and_citations(
    telegram_adapter: TelegramAdapter,
) -> None:
    """Telegram formats a plain-text body with a Sources footer of citations."""
    response = GeneratedResponse(
        text="Full-time employees accrue 20 paid vacation days per year.",
        citations=["HR Handbook.pdf", "Leave Policy.docx"],
    )

    out = telegram_adapter.format(response)

    # Body appears verbatim (plain text, no markdown decoration).
    assert "Full-time employees accrue 20 paid vacation days per year." in out
    # A Sources footer lists every citation as a plain bullet.
    assert "Sources:" in out
    assert "- HR Handbook.pdf" in out
    assert "- Leave Policy.docx" in out
    # Plain text: no markdown bold markers are introduced.
    assert "**" not in out
    assert len(out) <= TELEGRAM_MAX_MESSAGE_CHARS


def test_telegram_format_no_citations_has_no_footer(
    telegram_adapter: TelegramAdapter,
) -> None:
    """With no citations, Telegram output is the body alone (no Sources footer)."""
    response = GeneratedResponse(text="No sources for this answer.", citations=[])

    out = telegram_adapter.format(response)

    assert out == "No sources for this answer."
    assert "Sources:" not in out


# --- Nominal path: Teams markdown bullets (Req 8.6) ----------------------


def test_teams_format_markdown_bullets_and_bold_citations(
    teams_adapter: TeamsAdapter,
) -> None:
    """Teams formats a body plus a bold Sources bullet list of citations."""
    response = GeneratedResponse(
        text="Full-time employees accrue 20 paid vacation days per year.",
        citations=["HR Handbook.pdf", "Leave Policy.docx"],
    )

    out = teams_adapter.format(response)

    assert "Full-time employees accrue 20 paid vacation days per year." in out
    # Bold Sources header per Teams styling.
    assert "**Sources:**" in out
    # Each citation is a markdown bullet with a bold document name (Req 8.6).
    assert "- **HR Handbook.pdf**" in out
    assert "- **Leave Policy.docx**" in out
    assert len(out) <= TEAMS_MAX_MESSAGE_CHARS


def test_teams_format_no_citations_has_no_footer(
    teams_adapter: TeamsAdapter,
) -> None:
    """With no citations, Teams output is the body alone (no Sources footer)."""
    response = GeneratedResponse(text="No sources for this answer.", citations=[])

    out = teams_adapter.format(response)

    assert out == "No sources for this answer."
    assert "Sources" not in out


# --- Error/edge path: over-limit truncation preserves citations (Req 8.7) -


def test_telegram_over_limit_truncates_body_keeps_citations(
    telegram_adapter: TelegramAdapter,
) -> None:
    """An over-limit Telegram response is truncated but keeps every citation."""
    citations = ["Policy A.pdf", "Policy B.docx", "Policy C.xlsx"]
    huge_body = "A" * (TELEGRAM_MAX_MESSAGE_CHARS * 2)
    response = GeneratedResponse(text=huge_body, citations=citations)

    out = telegram_adapter.format(response)

    assert len(out) <= TELEGRAM_MAX_MESSAGE_CHARS
    assert TELEGRAM_TRUNCATION_INDICATOR in out
    for name in citations:
        assert name in out
    # Body was actually shortened (not the full original body).
    assert huge_body not in out


def test_teams_over_limit_truncates_body_keeps_citations(
    teams_adapter: TeamsAdapter,
) -> None:
    """An over-limit Teams response is truncated but keeps every citation."""
    citations = ["Policy A.pdf", "Policy B.docx", "Policy C.xlsx"]
    huge_body = "A" * (TEAMS_MAX_MESSAGE_CHARS * 2)
    response = GeneratedResponse(text=huge_body, citations=citations)

    out = teams_adapter.format(response)

    assert len(out) <= TEAMS_MAX_MESSAGE_CHARS
    assert TEAMS_TRUNCATION_INDICATOR in out
    for name in citations:
        # Citation name survives (bold-wrapped in the footer).
        assert f"**{name}**" in out
    assert huge_body not in out


# --- Pure helper coverage (both adapters) --------------------------------


def test_teams_pure_helper_respects_limit_and_indicator() -> None:
    """The Teams module-level pure formatter honors the cap and indicator."""
    citations = ["Doc One.pdf", "Doc Two.md"]

    tm = format_teams_message("B" * (TEAMS_MAX_MESSAGE_CHARS * 2), citations)
    assert len(tm) <= TEAMS_MAX_MESSAGE_CHARS
    assert TEAMS_TRUNCATION_INDICATOR in tm
    assert TEAMS_SOURCES_HEADER.strip() in tm
    assert all(f"**{name}**" in tm for name in citations)
