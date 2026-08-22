"""Authentication seam for private, human-recorded fraud outcomes.

The scoring worker already owns an on-chain ``ASSESSOR_ROLE`` wallet, but that
machine credential must not authenticate a person. Human reviewers therefore
use independent high-entropy API keys. The server stores only SHA-256 digests and
binds every digest to a stable, non-secret assessor reference used by the audit
record; callers cannot choose or impersonate that reference in a request body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

_ASSESSOR_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MINIMUM_API_KEY_LENGTH = 24


class AssessorOutcomeConfigurationError(ValueError):
    """Raised when the human-review authentication boundary is unsafe."""


class AssessorOutcomeAuthenticationError(ValueError):
    """Raised when no configured human assessor matches the supplied key."""


@dataclass(frozen=True)
class AssessorPrincipal:
    """Server-verified identity attached to one human outcome revision."""

    assessor_reference: str


@dataclass(frozen=True)
class _AssessorCredential:
    """Internal digest-to-principal binding that never leaves this module."""

    principal: AssessorPrincipal
    api_key_sha256: str


class AssessorOutcomeBoundary:
    """Authenticate human reviewers without sharing insurer or worker secrets.

    A request either resolves to a verified assessor principal or fails. Rate
    limits and enterprise identity proxies can
    be added around this seam later without changing the persistence interface or
    allowing a browser-provided assessor name into the audit trail.
    """

    def __init__(self, credentials: tuple[_AssessorCredential, ...]) -> None:
        if not credentials:
            raise AssessorOutcomeConfigurationError(
                "At least one assessor outcome credential is required"
            )
        self._credentials = credentials

    @classmethod
    def from_settings(
        cls, settings: Mapping[str, str]
    ) -> AssessorOutcomeBoundary:
        """Parse digest-only assessor credentials from deployment settings.

        The JSON array supports rotation and multiple reviewers. Unlike insurer
        credentials, it grants access only to
        the off-chain outcome endpoints and carries no wallet, insurer, quota, or
        model authority.
        """

        raw_json = settings.get("ASSESSOR_OUTCOME_CREDENTIALS_JSON", "").strip()
        if not raw_json:
            raise AssessorOutcomeConfigurationError(
                "ASSESSOR_OUTCOME_CREDENTIALS_JSON is required"
            )
        try:
            raw_credentials = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise AssessorOutcomeConfigurationError(
                "ASSESSOR_OUTCOME_CREDENTIALS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw_credentials, list) or not raw_credentials:
            raise AssessorOutcomeConfigurationError(
                "ASSESSOR_OUTCOME_CREDENTIALS_JSON must contain a non-empty array"
            )

        credentials: list[_AssessorCredential] = []
        references: set[str] = set()
        digests: set[str] = set()
        for index, raw in enumerate(raw_credentials):
            if not isinstance(raw, dict):
                raise AssessorOutcomeConfigurationError(
                    f"Assessor outcome credential at index {index} must be an object"
                )
            reference = raw.get("assessorReference")
            digest = raw.get("apiKeySha256")
            if not isinstance(reference, str) or not _ASSESSOR_REFERENCE.fullmatch(
                reference
            ):
                raise AssessorOutcomeConfigurationError(
                    f"assessorReference at index {index} must use 1-100 letters, "
                    "numbers, dots, underscores, or hyphens"
                )
            if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
                raise AssessorOutcomeConfigurationError(
                    f"apiKeySha256 at index {index} must contain exactly 64 "
                    "lowercase hexadecimal characters"
                )
            if set(raw) != {"assessorReference", "apiKeySha256"}:
                raise AssessorOutcomeConfigurationError(
                    f"Assessor outcome credential at index {index} contains "
                    "unsupported fields"
                )
            if reference in references:
                raise AssessorOutcomeConfigurationError(
                    f"Duplicate assessorReference {reference!r}"
                )
            if digest in digests:
                raise AssessorOutcomeConfigurationError(
                    "Assessor outcome credential digests must be unique"
                )
            references.add(reference)
            digests.add(digest)
            credentials.append(
                _AssessorCredential(
                    principal=AssessorPrincipal(reference),
                    api_key_sha256=digest,
                )
            )
        return cls(tuple(credentials))

    @classmethod
    def from_env(cls) -> AssessorOutcomeBoundary:
        """Build the boundary from process configuration without raw keys."""

        return cls.from_settings(os.environ)

    def authenticate(self, api_key: str | None) -> AssessorPrincipal:
        """Return the bound reviewer after comparing every configured digest.

        Iterating the complete credential list avoids revealing a match position.
        ``compare_digest`` avoids ordinary early-exit string comparison, and the
        minimum length rejects obviously invalid input before hashing. The error
        uses the same response for a missing, malformed, or unknown key.
        """

        candidate = (api_key or "").strip()
        if len(candidate) < _MINIMUM_API_KEY_LENGTH:
            raise AssessorOutcomeAuthenticationError(
                "Invalid assessor outcome credential"
            )
        supplied_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        match: AssessorPrincipal | None = None
        for credential in self._credentials:
            if hmac.compare_digest(supplied_digest, credential.api_key_sha256):
                match = credential.principal
        if match is None:
            raise AssessorOutcomeAuthenticationError(
                "Invalid assessor outcome credential"
            )
        return match
