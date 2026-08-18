"""Tests for the digest-only coverage-governance authentication seam."""

import hashlib
import json

import pytest

from apps.backend.app.governance_auth import (
    GovernanceAuthenticationError,
    GovernanceBoundary,
    GovernanceConfigurationError,
)


def settings(api_key: str = "governance-test-key-that-is-long-enough") -> dict[str, str]:
    return {
        "GOVERNANCE_CREDENTIALS_JSON": json.dumps(
            [
                {
                    "governanceReference": "northstar-coverage-maker-1",
                    "insurerAddress": "0x1111111111111111111111111111111111111111",
                    "apiKeySha256": hashlib.sha256(api_key.encode()).hexdigest(),
                }
            ]
        )
    }


def test_authentication_returns_server_bound_insurer_scope():
    boundary = GovernanceBoundary.from_settings(settings())

    principal = boundary.authenticate("governance-test-key-that-is-long-enough")

    assert principal.governance_reference == "northstar-coverage-maker-1"
    assert principal.insurer_address == "0x1111111111111111111111111111111111111111"


def test_authentication_rejects_unknown_keys_without_exposing_identity():
    boundary = GovernanceBoundary.from_settings(settings())

    with pytest.raises(GovernanceAuthenticationError, match="Invalid governance"):
        boundary.authenticate("another-governance-key-that-is-long-enough")


def test_configuration_rejects_raw_or_extra_credential_fields():
    unsafe = settings()
    values = json.loads(unsafe["GOVERNANCE_CREDENTIALS_JSON"])
    values[0]["apiKey"] = "raw-secret-must-never-be-configured"
    unsafe["GOVERNANCE_CREDENTIALS_JSON"] = json.dumps(values)

    with pytest.raises(GovernanceConfigurationError, match="invalid fields"):
        GovernanceBoundary.from_settings(unsafe)
