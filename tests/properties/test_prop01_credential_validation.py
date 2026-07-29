# Feature: intelliknow-kms, Property 1: Credential validation gates storage
"""Property 1: Credential validation gates storage.

For any integration and any submitted credential field set,
``validate_credentials`` returns no errors if and only if every required field
is present, non-empty, and matches its format pattern; when errors are
returned, there is exactly one error per missing/empty/invalid field and the
Credential_Store contents are unchanged by the submission.

Validates: Requirements 1.2, 1.4
"""

from __future__ import annotations

import re
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

INTEGRATIONS = sorted(CREDENTIAL_SCHEMAS)

# Known-valid example values per integration field, used to build valid
# submissions and to seed a store with prior credentials.
VALID_EXAMPLES = {
    "telegram": {"bot_token": "123456789:AAHkabcdefghijklmnopqrstuvwxyz0123456"},
    "teams": {
        "app_id": "12345678-1234-1234-1234-1234567890ab",
        "app_password": "s3cretpassword",
    },
    "dashscope": {"api_key": "sk-abcdef0123456789ABCDEF"},
}


def _is_valid_field(integration: str, name: str, value) -> bool:
    """Reference check: is a single field value present, non-empty, valid."""
    if not isinstance(value, str) or value == "":
        return False
    return re.fullmatch(CREDENTIAL_SCHEMAS[integration][name], value) is not None


def _store(tmp_key: str, data_dir) -> CredentialStore:
    settings = load_settings(
        {"DATA_DIR": str(data_dir), "CREDENTIAL_MASTER_KEY": tmp_key}
    )
    return CredentialStore(settings)


# A field value generator that produces a healthy mix of valid values, empty
# strings, and arbitrary (usually invalid) text so both branches are exercised.
_field_value = st.one_of(
    st.sampled_from(
        [v for fields in VALID_EXAMPLES.values() for v in fields.values()]
    ),
    st.just(""),
    st.text(max_size=40),
)


@st.composite
def submissions(draw):
    """Draw (integration, fields) where each required field may be present."""
    integration = draw(st.sampled_from(INTEGRATIONS))
    schema = CREDENTIAL_SCHEMAS[integration]
    fields: dict[str, str] = {}
    for name in schema:
        if draw(st.booleans()):  # sometimes omit the field entirely
            # Bias toward the valid example so fully-valid submissions occur.
            if draw(st.booleans()):
                fields[name] = VALID_EXAMPLES[integration][name]
            else:
                fields[name] = draw(_field_value)
    return integration, fields


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=submissions())
def test_validation_matches_reference_and_gates_storage(data, tmp_path):
    integration, fields = data
    schema = CREDENTIAL_SCHEMAS[integration]

    errors = validate_credentials(integration, fields)
    offending = {
        name for name in schema if not _is_valid_field(integration, name, fields.get(name))
    }

    # Exactly one error per offending field, and none for valid fields.
    assert {e.field for e in errors} == offending
    assert len(errors) == len(offending)

    # Iff: empty error list exactly when every required field is valid.
    assert (len(errors) == 0) == (len(offending) == 0)

    # Store is unchanged by an invalid submission: seed a prior valid value,
    # attempt the (possibly invalid) save, and confirm survival on rejection.
    key = Fernet.generate_key().decode()
    # Each Hypothesis example gets an isolated data dir (tmp_path is shared
    # across examples in one test function).
    store = _store(key, tmp_path / uuid.uuid4().hex)
    prior = VALID_EXAMPLES[integration]
    store.save(integration, prior)

    if errors:
        try:
            store.save(integration, fields)
            raised = False
        except CredentialValidationError as exc:
            raised = True
            assert {e.field for e in exc.errors} == offending
        assert raised, "invalid submission must be rejected"
        # Prior credentials survive the rejected submission (Req 1.6).
        assert store.load(integration) == prior
    else:
        store.save(integration, fields)
        assert store.load(integration) == {name: fields[name] for name in schema}
