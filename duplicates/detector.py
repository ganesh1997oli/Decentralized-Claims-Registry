"""Create private incident fingerprints and find matches from other insurers.

The detector intentionally returns a review candidate rather than a fraud
decision. A keyed HMAC makes stored fingerprints impractical to reproduce
without the server-side key, while deterministic canonicalization lets
participating synthetic insurers compare the same incident consistently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Protocol


FINGERPRINT_VERSION = "incident-hmac-sha256-v1"
MINIMUM_KEY_BYTES = 32


class DuplicateDetectionConfigurationError(ValueError):
    """Raised when private duplicate-fingerprinting cannot be configured safely."""


class ClaimForDuplicateDetection(Protocol):
    insurer_id: str
    claim_type: str
    incident_date: date
    claim_amount_usd: float
    vehicle_age: int
    vehicle_type: str
    country: str
    region_type: str
    third_party_injury_flag: bool
    total_loss_flag: bool


class ClaimEventForDuplicateDetection(Protocol):
    event_id: str
    chain_id: int
    contract_address: str
    claim_id: int


@dataclass(frozen=True)
class DuplicateMatch:
    """A claim from another insurer with the same private incident fingerprint."""

    claim_id: int
    insurer_id: str


@dataclass(frozen=True)
class DuplicateCheck:
    """The review result for one claim; the private fingerprint is never exposed."""

    insurer_id: str
    fingerprint_version: str
    matches: tuple[DuplicateMatch, ...] = ()

    @property
    def duplicate_detected(self) -> bool:
        return bool(self.matches)


class DuplicateStore(Protocol):
    def record_and_find_duplicates(
        self,
        *,
        event_id: str,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        insurer_id: str,
        fingerprint_version: str,
        incident_fingerprint: str,
    ) -> DuplicateCheck: ...


def _normalized_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _amount_in_cents(value: float) -> int:
    decimal_value = Decimal(str(value)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_EVEN,
    )
    return int(decimal_value * 100)


class CrossInsurerDuplicateDetector:
    """Hide incident canonicalization, HMAC generation, and persistence."""

    def __init__(self, key: bytes, store: DuplicateStore) -> None:
        if len(key) < MINIMUM_KEY_BYTES:
            raise DuplicateDetectionConfigurationError(
                f"Duplicate fingerprint key must contain at least "
                f"{MINIMUM_KEY_BYTES} bytes"
            )
        self._key = key
        self._store = store

    @classmethod
    def from_env(cls, store: DuplicateStore) -> "CrossInsurerDuplicateDetector":
        raw_key = os.environ.get("DUPLICATE_FINGERPRINT_KEY", "")
        if not raw_key:
            raise DuplicateDetectionConfigurationError(
                "DUPLICATE_FINGERPRINT_KEY is required by the scoring worker"
            )
        return cls(raw_key.encode("utf-8"), store)

    def check(
        self,
        event: ClaimEventForDuplicateDetection,
        claim: ClaimForDuplicateDetection,
    ) -> DuplicateCheck:
        return self._store.record_and_find_duplicates(
            event_id=event.event_id,
            chain_id=event.chain_id,
            contract_address=event.contract_address,
            claim_id=event.claim_id,
            insurer_id=claim.insurer_id,
            fingerprint_version=FINGERPRINT_VERSION,
            incident_fingerprint=self._fingerprint(claim),
        )

    def _fingerprint(self, claim: ClaimForDuplicateDetection) -> str:
        # References, policy premium, and free text are deliberately excluded:
        # different insurers can assign different references and descriptions to
        # the same underlying incident.
        incident = {
            "claim_amount_cents": _amount_in_cents(claim.claim_amount_usd),
            "claim_type": _normalized_text(claim.claim_type),
            "country": _normalized_text(claim.country),
            "incident_date": claim.incident_date.isoformat(),
            "region_type": _normalized_text(claim.region_type),
            "third_party_injury": bool(claim.third_party_injury_flag),
            "total_loss": bool(claim.total_loss_flag),
            "vehicle_age": int(claim.vehicle_age),
            "vehicle_type": _normalized_text(claim.vehicle_type),
            "version": FINGERPRINT_VERSION,
        }
        canonical_bytes = json.dumps(
            incident,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self._key, canonical_bytes, hashlib.sha256).hexdigest()
