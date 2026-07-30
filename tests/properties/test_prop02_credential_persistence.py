# Feature: intelliknow-kms, Property 2: Credential store holds the last valid save
"""Property 2: Credential store holds the last valid save.

For any sequence of valid and invalid credential submissions for an
integration, loading from the Credential_Store returns exactly the field
values of the most recent valid submission (or nothing if there has never been
one).

Validates: Requirements 1.5, 1.6
"""

from __future__ import annotations

import uuid

from cryptography.fernet import Fernet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.config import load_settings
from app.security.credentials import (
    CREDENTIAL_SCHEMAS,
    CredentialStore,
    CredentialValidationError,
    validate_credentials,
)

# Pools of valid values per integration so multiple distinct valid saves occur.
VALID_POOL = {
    "telegram": {
        "bot_token": [
            "111111111:AAHkabcdefghijklmnopqrstuvwxyz0123456",
            "222222222:BBzzabcdefghijklmnopqrstuvwxyz9876543",
        ]
    },
    "teams": {
        "app_id": [
            "12345678-1234-1234-1234-1234567890ab",
            "abcdef01-abcd-abcd-abcd-abcdef012345",
        ],
        "app_password": ["s3cretpassword", "anotherStr0ngPass"],
    },
    "whatsapp": {
        "access_token": [
            "EAAexampleWhatsAppAccessToken0123456789",
            "EAAanotherWhatsAppAccessTokenABCDEFGHIJ",
        ],
        "phone_number_id": ["123456789012345", "987654321098765"],
        "verify_token": ["my-verify-token", "another-verify-token"],
    },
    "dashscope": {
        "api_key": ["sk-abcdef0123456789ABCDEF", "sk-ZZZZ0000111122223333"]
    },
}


@st.composite
def valid_fields(draw, integration):
    return {
        name: draw(st.sampled_from(values))
        for name, values in VALID_POOL[integration].items()
    }


@st.composite
def maybe_invalid_fields(draw, integration):
    """Sometimes valid, sometimes corrupted so an invalid save is attempted."""
    fields = draw(valid_fields(integration))
    if draw(st.booleans()):
        # Corrupt one field to force a validation failure.
        name = draw(st.sampled_from(sorted(fields)))
        fields[name] = draw(st.sampled_from(["", "bad!!", "x"]))
    return fields


@st.composite
def submission_sequences(draw):
    integration = draw(st.sampled_from(sorted(CREDENTIAL_SCHEMAS)))
    seq = draw(
        st.lists(maybe_invalid_fields(integration), min_size=1, max_size=8)
    )
    return integration, seq


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=submission_sequences())
def test_store_holds_last_valid_save(data, tmp_path):
    integration, seq = data
    key = Fernet.generate_key().decode()
    # Isolate each Hypothesis example (tmp_path is shared across examples).
    settings_obj = load_settings(
        {"DATA_DIR": str(tmp_path / uuid.uuid4().hex), "CREDENTIAL_MASTER_KEY": key}
    )
    store = CredentialStore(settings_obj)
    schema = CREDENTIAL_SCHEMAS[integration]

    last_valid: dict[str, str] | None = None
    for fields in seq:
        if validate_credentials(integration, fields):
            try:
                store.save(integration, fields)
                assert False, "invalid save should have raised"
            except CredentialValidationError:
                pass
        else:
            store.save(integration, fields)
            last_valid = {name: fields[name] for name in schema}

    assert store.load(integration) == last_valid
