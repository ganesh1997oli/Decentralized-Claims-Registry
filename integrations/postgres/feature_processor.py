"""Build one privacy-safe, auditable feature snapshot for each claim event.

The scoring worker calls the small ``process`` interface. This module owns the
data-quality checks, deterministic policy HMAC, and claim-level calculations;
the store owns transactional historical aggregation and persistence.

Free text, evidence, and raw policy references are intentionally excluded from
the snapshot. They are unnecessary for the current research features and would
increase the amount of sensitive claim data copied into PostgreSQL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from duplicates import DuplicateCheck


FEATURE_VERSION = "claim-processing-v1"
POLICY_FINGERPRINT_VERSION = "policy-hmac-sha256-v1"
MINIMUM_FINGERPRINT_KEY_BYTES = 32


class ClaimFeatureConfigurationError(ValueError):
    """Raised when claim feature processing is not configured safely."""


class ClaimFeatureProcessingError(ValueError):
    """Raised when a claim event cannot produce trustworthy features."""


class ClaimForFeatureProcessing(Protocol):
    insurer_id: str
    policy_reference: str
    claim_type: str
    incident_date: date
    claim_amount_usd: float
    policy_premium_usd: float
    vehicle_age: int
    vehicle_type: str
    country: str
    region_type: str
    third_party_injury_flag: bool
    total_loss_flag: bool


class ClaimEventForFeatureProcessing(Protocol):
    event_id: str
    chain_id: int
    contract_address: str
    claim_id: int
    event_timestamp: int


@dataclass(frozen=True)
class ClaimFeatureInput:
    """Current-claim values ready for transactional historical enrichment."""

    event_id: str
    chain_id: int
    contract_address: str
    claim_id: int
    feature_version: str
    insurer_id: str
    policy_fingerprint_version: str
    policy_reference_fingerprint: str
    event_timestamp: int
    incident_date: date
    claim_type: str
    claim_amount_usd: float
    policy_premium_usd: float
    claim_to_premium_ratio: float
    vehicle_age: int
    vehicle_type: str
    country: str
    region_type: str
    third_party_injury_flag: bool
    total_loss_flag: bool
    report_delay_days: int
    cross_insurer_duplicate_match_count: int


@dataclass(frozen=True)
class ClaimFeatureSnapshot(ClaimFeatureInput):
    """The immutable feature values saved for the event at processing time."""

    prior_policy_claim_count: int
    prior_insurer_claim_count: int
    prior_insurer_average_claim_amount_usd: float | None
    claim_to_prior_insurer_average_ratio: float | None


class ClaimFeatureStore(Protocol):
    def record_feature_snapshot(
        self,
        values: ClaimFeatureInput,
    ) -> ClaimFeatureSnapshot: ...


def _normalized_identifier(value: str) -> str:
    return " ".join(value.strip().casefold().split())


class ClaimFeatureProcessor:
    """Hide feature validation, derivation, fingerprinting, and persistence."""

    def __init__(self, fingerprint_key: bytes, store: ClaimFeatureStore) -> None:
        if len(fingerprint_key) < MINIMUM_FINGERPRINT_KEY_BYTES:
            raise ClaimFeatureConfigurationError(
                "Claim feature fingerprint key must contain at least "
                f"{MINIMUM_FINGERPRINT_KEY_BYTES} bytes"
            )
        self._fingerprint_key = fingerprint_key
        self._store = store

    @classmethod
    def from_env(cls, store: ClaimFeatureStore) -> "ClaimFeatureProcessor":
        # Reuse the worker's existing secret, but domain-separate the policy
        # payload from incident fingerprints with its own version identifier.
        raw_key = os.environ.get("DUPLICATE_FINGERPRINT_KEY", "")
        if not raw_key:
            raise ClaimFeatureConfigurationError(
                "DUPLICATE_FINGERPRINT_KEY is required for claim features"
            )
        return cls(raw_key.encode("utf-8"), store)

    def process(
        self,
        event: ClaimEventForFeatureProcessing,
        claim: ClaimForFeatureProcessing,
        duplicate_check: DuplicateCheck,
    ) -> ClaimFeatureSnapshot:
        """Validate, derive, and transactionally persist one feature snapshot."""

        if duplicate_check.insurer_id != claim.insurer_id:
            raise ClaimFeatureProcessingError(
                "Duplicate result insurer does not match the claim"
            )

        try:
            reported_date = datetime.fromtimestamp(
                event.event_timestamp,
                tz=UTC,
            ).date()
        except (OSError, OverflowError, ValueError) as exc:
            raise ClaimFeatureProcessingError(
                "Claim event timestamp is outside the supported range"
            ) from exc

        report_delay_days = (reported_date - claim.incident_date).days
        if report_delay_days < 0:
            raise ClaimFeatureProcessingError(
                "Incident date cannot be later than the claim event"
            )

        values = ClaimFeatureInput(
            event_id=event.event_id,
            chain_id=event.chain_id,
            contract_address=event.contract_address.lower(),
            claim_id=event.claim_id,
            feature_version=FEATURE_VERSION,
            insurer_id=claim.insurer_id,
            policy_fingerprint_version=POLICY_FINGERPRINT_VERSION,
            policy_reference_fingerprint=self._policy_fingerprint(claim),
            event_timestamp=event.event_timestamp,
            incident_date=claim.incident_date,
            claim_type=claim.claim_type,
            claim_amount_usd=float(claim.claim_amount_usd),
            policy_premium_usd=float(claim.policy_premium_usd),
            claim_to_premium_ratio=(
                float(claim.claim_amount_usd) / float(claim.policy_premium_usd)
            ),
            vehicle_age=claim.vehicle_age,
            vehicle_type=claim.vehicle_type,
            country=claim.country,
            region_type=claim.region_type,
            third_party_injury_flag=claim.third_party_injury_flag,
            total_loss_flag=claim.total_loss_flag,
            report_delay_days=report_delay_days,
            cross_insurer_duplicate_match_count=len(duplicate_check.matches),
        )
        return self._store.record_feature_snapshot(values)

    def _policy_fingerprint(self, claim: ClaimForFeatureProcessing) -> str:
        policy_identity = {
            "insurer_id": _normalized_identifier(claim.insurer_id),
            "policy_reference": _normalized_identifier(claim.policy_reference),
            "version": POLICY_FINGERPRINT_VERSION,
        }
        canonical_bytes = json.dumps(
            policy_identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(
            self._fingerprint_key,
            canonical_bytes,
            hashlib.sha256,
        ).hexdigest()
