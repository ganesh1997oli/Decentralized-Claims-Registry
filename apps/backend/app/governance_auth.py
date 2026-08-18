"""Authentication boundary for insurer coverage-governance operators.

API credentials authorize preparation and audit attribution only. They never
hold an Ethereum private key and cannot finalize a claim. The browser must still
use a separately scoped DECISION_MAKER_ROLE wallet for the on-chain transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MINIMUM_API_KEY_LENGTH = 24


class GovernanceConfigurationError(ValueError):
    """Raised when governance credentials are absent or unsafe."""


class GovernanceAuthenticationError(ValueError):
    """Raised when a caller cannot authenticate as a governance operator."""


@dataclass(frozen=True)
class GovernancePrincipal:
    """Server-owned maker identity and insurer scope used in audit records."""

    governance_reference: str
    insurer_address: str


@dataclass(frozen=True)
class _GovernanceCredential:
    principal: GovernancePrincipal
    api_key_sha256: str


class GovernanceBoundary:
    """Authenticate proposal makers without granting transaction authority."""

    def __init__(self, credentials: tuple[_GovernanceCredential, ...]) -> None:
        if not credentials:
            raise GovernanceConfigurationError(
                "At least one governance credential is required"
            )
        self._credentials = credentials

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> GovernanceBoundary:
        raw_json = settings.get("GOVERNANCE_CREDENTIALS_JSON", "").strip()
        if not raw_json:
            raise GovernanceConfigurationError(
                "GOVERNANCE_CREDENTIALS_JSON is required"
            )
        try:
            raw_credentials = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise GovernanceConfigurationError(
                "GOVERNANCE_CREDENTIALS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw_credentials, list) or not raw_credentials:
            raise GovernanceConfigurationError(
                "GOVERNANCE_CREDENTIALS_JSON must contain a non-empty array"
            )

        credentials: list[_GovernanceCredential] = []
        references: set[str] = set()
        digests: set[str] = set()
        for index, raw in enumerate(raw_credentials):
            if not isinstance(raw, dict) or set(raw) != {
                "governanceReference",
                "insurerAddress",
                "apiKeySha256",
            }:
                raise GovernanceConfigurationError(
                    f"Governance credential at index {index} has invalid fields"
                )
            reference = raw["governanceReference"]
            insurer_address = raw["insurerAddress"]
            digest = raw["apiKeySha256"]
            if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
                raise GovernanceConfigurationError(
                    f"governanceReference at index {index} is invalid"
                )
            if not isinstance(insurer_address, str) or not _ADDRESS.fullmatch(
                insurer_address
            ):
                raise GovernanceConfigurationError(
                    f"insurerAddress at index {index} is invalid"
                )
            if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
                raise GovernanceConfigurationError(
                    f"apiKeySha256 at index {index} must be 64 lowercase hex characters"
                )
            if reference in references or digest in digests:
                raise GovernanceConfigurationError(
                    "Governance references and credential digests must be unique"
                )
            references.add(reference)
            digests.add(digest)
            credentials.append(
                _GovernanceCredential(
                    principal=GovernancePrincipal(
                        governance_reference=reference,
                        insurer_address=insurer_address.lower(),
                    ),
                    api_key_sha256=digest,
                )
            )
        return cls(tuple(credentials))

    @classmethod
    def from_env(cls) -> GovernanceBoundary:
        return cls.from_settings(os.environ)

    def authenticate(self, api_key: str | None) -> GovernancePrincipal:
        candidate = (api_key or "").strip()
        if len(candidate) < _MINIMUM_API_KEY_LENGTH:
            raise GovernanceAuthenticationError("Invalid governance credential")
        supplied_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        match: GovernancePrincipal | None = None
        # Traverse the full list and compare fixed-length digests so the response
        # does not disclose which configured operator was closest to a match.
        for credential in self._credentials:
            if hmac.compare_digest(supplied_digest, credential.api_key_sha256):
                match = credential.principal
        if match is None:
            raise GovernanceAuthenticationError("Invalid governance credential")
        return match
