"""Insurer-backed policy eligibility for public claim intake.

Policy eligibility is intentionally deeper than a collection of route checks:
one interface resolves the submitted policy reference, claimant relationship,
coverage dates, claim type, amount, insurer identity, quota, and claimant
commitment. A future insurer HTTP adapter can replace the configured adapter at
this seam without spreading eligibility rules through FastAPI or the relayer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from web3 import Web3

from apps.backend.app.claimant_auth import ClaimantSession
from apps.backend.app.models import ClaimSubmission

_MINIMUM_KEY_BYTES = 32
_INSURER_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]\Z")
_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_CLAIM_TYPES = frozenset({"collision", "theft", "fire", "flood"})


class PolicyEligibilityConfigurationError(ValueError):
    """Raised when configured policy records are malformed or ambiguous."""


class PolicyEligibilityError(PermissionError):
    """Raised when a claimant or incident is not eligible under the policy."""


@dataclass(frozen=True)
class ClaimantPrincipal:
    """Policy-eligible parties and limits for one public claim submission."""

    subject_id: str
    claimant_address: str
    submitter_address: str
    claimant_commitment: str
    insurer_id: str
    insurer_address: str
    policy_id: str
    daily_quota: int
    rate_limit_exempt: bool = False

    @property
    def credential_id(self) -> str:
        """Bridge stable claimant ownership into the rolling outbox schema."""

        return self.subject_id

    @property
    def signer_address(self) -> str:
        """Return the submitter that must sign the ERC-2771 request."""

        return self.submitter_address

    @property
    def permitted_operations(self) -> frozenset[str]:
        return frozenset({"submit_public_claim"})


@dataclass(frozen=True)
class _PolicyRecord:
    policy_id: str
    policy_reference_hmac: str
    insurer_id: str
    insurer_address: str
    claimant_address: str
    authorized_submitters: frozenset[str]
    coverage_start: date
    coverage_end: date
    allowed_claim_types: frozenset[str]
    max_claim_amount_usd: float
    daily_quota: int


def policy_reference_hmac(key: bytes, policy_reference: str) -> str:
    """Return the configured lookup form without storing a raw policy number."""

    normalized = " ".join(policy_reference.strip().lower().split())
    return hmac.new(
        key,
        f"policy-reference:v1:{normalized}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _required_key(settings: Mapping[str, str], name: str) -> bytes:
    value = settings.get(name, "").encode("utf-8")
    if len(value) < _MINIMUM_KEY_BYTES:
        raise PolicyEligibilityConfigurationError(
            f"{name} must contain at least {_MINIMUM_KEY_BYTES} bytes"
        )
    return value


def _positive_int(value: Any, *, name: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise PolicyEligibilityConfigurationError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyEligibilityConfigurationError(f"{name} must be an integer") from exc
    if parsed < 1 or parsed > maximum:
        raise PolicyEligibilityConfigurationError(
            f"{name} must be between 1 and {maximum}"
        )
    return parsed


def _positive_amount(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise PolicyEligibilityConfigurationError(f"{name} must be positive")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyEligibilityConfigurationError(f"{name} must be positive") from exc
    if parsed <= 0 or parsed > 100_000_000:
        raise PolicyEligibilityConfigurationError(
            f"{name} must be between 0 and 100000000"
        )
    return parsed


def _date(value: Any, *, name: str) -> date:
    if not isinstance(value, str):
        raise PolicyEligibilityConfigurationError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PolicyEligibilityConfigurationError(
            f"{name} must be an ISO date"
        ) from exc


def _address(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise PolicyEligibilityConfigurationError(f"{name} must be an address")
    try:
        address = Web3.to_checksum_address(value)
    except ValueError as exc:
        raise PolicyEligibilityConfigurationError(f"{name} must be an address") from exc
    if int(address, 16) == 0:
        raise PolicyEligibilityConfigurationError(f"{name} cannot be zero")
    return address


class ConfiguredPolicyEligibility:
    """Verify synthetic/local policies from digest-only configuration.

    This adapter is suitable for controlled deployments and local research. It
    deliberately stores keyed policy-reference digests rather than raw policy
    numbers. A production insurer integration should implement the same
    `verify` interface and return the same `ClaimantPrincipal` after consulting
    its authoritative policy system.
    """

    def __init__(
        self,
        records: tuple[_PolicyRecord, ...],
        *,
        lookup_key: bytes,
        commitment_key: bytes,
        today: Callable[[], date] = date.today,
    ) -> None:
        if not records:
            raise PolicyEligibilityConfigurationError(
                "At least one public-claim policy record is required"
            )
        self._by_reference = {
            record.policy_reference_hmac: record for record in records
        }
        self._configured_insurers = tuple(
            sorted({(record.insurer_id, record.insurer_address) for record in records})
        )
        self.lookup_key = lookup_key
        self.commitment_key = commitment_key
        self.today = today

    @property
    def configured_insurers(self) -> tuple[tuple[str, str], ...]:
        """Return public insurer identities for startup role validation."""

        return self._configured_insurers

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, str],
    ) -> ConfiguredPolicyEligibility:
        lookup_key = _required_key(settings, "POLICY_REFERENCE_LOOKUP_KEY")
        commitment_key = _required_key(settings, "CLAIMANT_COMMITMENT_KEY")
        raw_json = settings.get("POLICY_ELIGIBILITY_RECORDS_JSON", "")
        try:
            values = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PolicyEligibilityConfigurationError(
                "POLICY_ELIGIBILITY_RECORDS_JSON must be valid JSON"
            ) from exc
        if not isinstance(values, list) or not values:
            raise PolicyEligibilityConfigurationError(
                "POLICY_ELIGIBILITY_RECORDS_JSON must contain policy records"
            )

        records: list[_PolicyRecord] = []
        policy_ids: set[str] = set()
        reference_hmacs: set[str] = set()
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                raise PolicyEligibilityConfigurationError(
                    f"Policy record {index} must be an object"
                )
            policy_id = str(raw.get("policyId", "")).strip()
            if not _POLICY_ID.fullmatch(policy_id):
                raise PolicyEligibilityConfigurationError(
                    f"policyId at index {index} is invalid"
                )
            insurer_id = str(raw.get("insurerId", "")).strip()
            if not _INSURER_ID.fullmatch(insurer_id):
                raise PolicyEligibilityConfigurationError(
                    f"insurerId at index {index} is invalid"
                )
            reference_hmac = str(raw.get("policyReferenceHmac", "")).lower()
            if len(reference_hmac) != 64 or any(
                character not in "0123456789abcdef" for character in reference_hmac
            ):
                raise PolicyEligibilityConfigurationError(
                    f"policyReferenceHmac at index {index} must be 64 hexadecimal characters"
                )
            claimant = _address(
                raw.get("claimantAddress"),
                name=f"claimantAddress at index {index}",
            )
            raw_submitters = raw.get("authorizedSubmitterAddresses", [claimant])
            if not isinstance(raw_submitters, list) or not raw_submitters:
                raise PolicyEligibilityConfigurationError(
                    f"authorizedSubmitterAddresses at index {index} must be a non-empty list"
                )
            submitters = frozenset(
                _address(value, name=f"authorizedSubmitterAddresses at index {index}")
                for value in raw_submitters
            )
            coverage_start = _date(
                raw.get("coverageStart"), name=f"coverageStart at index {index}"
            )
            coverage_end = _date(
                raw.get("coverageEnd"), name=f"coverageEnd at index {index}"
            )
            if coverage_end < coverage_start:
                raise PolicyEligibilityConfigurationError(
                    f"coverageEnd at index {index} precedes coverageStart"
                )
            raw_claim_types = raw.get("allowedClaimTypes")
            if (
                not isinstance(raw_claim_types, list)
                or not raw_claim_types
                or any(value not in _CLAIM_TYPES for value in raw_claim_types)
            ):
                raise PolicyEligibilityConfigurationError(
                    f"allowedClaimTypes at index {index} is invalid"
                )
            if policy_id in policy_ids or reference_hmac in reference_hmacs:
                raise PolicyEligibilityConfigurationError(
                    "Policy IDs and reference HMACs must be unique"
                )
            policy_ids.add(policy_id)
            reference_hmacs.add(reference_hmac)
            records.append(
                _PolicyRecord(
                    policy_id=policy_id,
                    policy_reference_hmac=reference_hmac,
                    insurer_id=insurer_id,
                    insurer_address=_address(
                        raw.get("insurerAddress"),
                        name=f"insurerAddress at index {index}",
                    ),
                    claimant_address=claimant,
                    authorized_submitters=submitters,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    allowed_claim_types=frozenset(raw_claim_types),
                    max_claim_amount_usd=_positive_amount(
                        raw.get("maxClaimAmountUsd"),
                        name=f"maxClaimAmountUsd at index {index}",
                    ),
                    daily_quota=_positive_int(
                        raw.get("dailyQuota", 10),
                        name=f"dailyQuota at index {index}",
                        maximum=10_000,
                    ),
                )
            )
        return cls(
            tuple(records),
            lookup_key=lookup_key,
            commitment_key=commitment_key,
        )

    @classmethod
    def from_env(cls) -> ConfiguredPolicyEligibility:
        return cls.from_mapping(os.environ)

    def verify(
        self,
        claim: ClaimSubmission,
        session: ClaimantSession,
    ) -> ClaimantPrincipal:
        """Resolve one policy and enforce every public-intake eligibility rule."""

        reference = policy_reference_hmac(self.lookup_key, claim.policy_reference)
        record = self._by_reference.get(reference)
        # Use one deliberately nonspecific response for unknown policies and
        # mismatched claimants to reduce policy-number enumeration value.
        if record is None or session.claimant_address not in record.authorized_submitters:
            raise PolicyEligibilityError(
                "The policy could not be verified for this claimant"
            )
        if not hmac.compare_digest(record.insurer_id, claim.insurer_id):
            raise PolicyEligibilityError(
                "The selected insurer does not match the verified policy"
            )
        if claim.incident_date > self.today():
            raise PolicyEligibilityError("The incident date cannot be in the future")
        if not (record.coverage_start <= claim.incident_date <= record.coverage_end):
            raise PolicyEligibilityError(
                "The policy was not active on the incident date"
            )
        if claim.claim_type not in record.allowed_claim_types:
            raise PolicyEligibilityError(
                "This type of incident is not covered by the verified policy"
            )
        if claim.claim_amount_usd > record.max_claim_amount_usd:
            raise PolicyEligibilityError(
                "The claim amount exceeds the configured policy coverage"
            )

        commitment = hmac.new(
            self.commitment_key,
            (
                "claimant-commitment:v1:"
                f"{record.insurer_id}:{record.policy_id}:"
                f"{record.claimant_address.lower()}"
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        return ClaimantPrincipal(
            subject_id=session.subject_id,
            claimant_address=record.claimant_address,
            submitter_address=session.claimant_address,
            claimant_commitment=f"0x{commitment}",
            insurer_id=record.insurer_id,
            insurer_address=record.insurer_address,
            policy_id=record.policy_id,
            daily_quota=record.daily_quota,
        )
