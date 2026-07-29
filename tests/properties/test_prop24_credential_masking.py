# Feature: intelliknow-kms, Property 24: Masking reveals at most the last four characters
"""Property 24: Masking reveals at most the last four characters.

For any credential value — including strings shorter than four characters —
masking reveals at most the last four characters and obscures everything else.
Short secrets (length <= 4) are revealed to zero characters so a short secret
is never disclosed in full.

Validates: Requirements 11.2, 11.6
"""

from __future__ import annotations

import uuid

from cryptography.fernet import Fernet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.config import load_settings
from app.security.credentials import CredentialStore, mask_value

# Secret characters exclude the mask character '*' so the revealed suffix is
# unambiguously detectable in the masked output.
_secret_chars = st.characters(blacklist_characters="*", min_codepoint=33, max_codepoint=126)
_secrets = st.text(alphabet=_secret_chars, min_size=0, max_size=60)

MASK_CHAR = "*"
MAX_REVEAL = 4


def _assert_masking(original: str, masked: str) -> None:
    # Length is preserved.
    assert len(masked) == len(original)

    # Count trailing characters that were left visible (not the mask char).
    revealed = 0
    for o, m in zip(reversed(original), reversed(masked)):
        if m == MASK_CHAR:
            break
        assert m == o  # a revealed position must match the original char
        revealed += 1

    # Never reveal more than the last four characters.
    assert revealed <= MAX_REVEAL

    # Short secrets (<= 4) are fully masked (zero revealed).
    if len(original) <= MAX_REVEAL:
        assert revealed == 0
        assert masked == MASK_CHAR * len(original)
    else:
        # Longer secrets reveal exactly the last four and mask the rest.
        assert revealed == MAX_REVEAL
        assert masked[:-MAX_REVEAL] == MASK_CHAR * (len(original) - MAX_REVEAL)


@settings(max_examples=200)
@given(value=_secrets)
def test_mask_value_reveals_at_most_last_four(value):
    _assert_masking(value, mask_value(value))


_token_body_chars = st.sampled_from(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(body=st.text(alphabet=_token_body_chars, min_size=30, max_size=50))
def test_store_masked_readback_hides_values(body, tmp_path):
    # A realistic telegram token (digits + ':' + >=30 body chars per schema).
    bot_token = "123456789:" + body
    key = Fernet.generate_key().decode()
    # Isolate each Hypothesis example (tmp_path is shared across examples).
    settings_obj = load_settings(
        {"DATA_DIR": str(tmp_path / uuid.uuid4().hex), "CREDENTIAL_MASTER_KEY": key}
    )
    store = CredentialStore(settings_obj)
    store.save("telegram", {"bot_token": bot_token})

    masked = store.masked("telegram")["bot_token"]
    _assert_masking(bot_token, masked)
