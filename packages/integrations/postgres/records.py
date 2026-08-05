"""Typed persistence records shared by PostgreSQL adapters and callers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.model.contracts import FraudReason, FraudScore


@dataclass(frozen=True)
class IndexedClaim:
    """Current public contract state reconstructed from confirmed event logs.

    The record intentionally mirrors the compact Solidity ``Claim`` structure.
    It contains no downloaded IPFS document or insurer credential, so returning
    it from the public dashboard cannot widen the existing data boundary.
    """

    claim_id: int
    claimant: str
    claim_hash: str
    data_pointer: str
    status: int
    fraud_score: int
    submitted_at: int
    updated_at: int


@dataclass(frozen=True)
class ClaimIndexStatus:
    """Durable progress of one chain-and-contract projection."""

    chain_id: int
    contract_address: str
    last_processed_block: int
    updated_at: datetime


@dataclass(frozen=True)
class ClaimIndexEventRecord:
    """One immutable public contract event shown to index operators."""

    event_id: str
    claim_id: int
    event_type: str
    block_number: int
    transaction_hash: str
    log_index: int
    event_timestamp: int
    status: int
    fraud_score: int
    indexed_at: datetime


@dataclass(frozen=True)
class ClaimIndexEventPage:
    """One stable newest-first slice of the immutable event audit stream."""

    events: tuple[ClaimIndexEventRecord, ...]
    has_more: bool


@dataclass(frozen=True)
class ClaimIndexReconciliationRecord:
    """Durable result of comparing one checkpoint with authoritative state."""

    indexed_through_block: int
    chain_claims: int
    indexed_claims: int
    missing_claim_ids: tuple[int, ...]
    unexpected_claim_ids: tuple[int, ...]
    mismatched_claim_ids: tuple[int, ...]
    consistent: bool
    duration_ms: int
    checked_at: datetime


@dataclass(frozen=True)
class ClaimIndexOperationsSnapshot:
    """One bounded database snapshot for the authenticated operations UI."""

    checkpoint: ClaimIndexStatus | None
    total_claims: int
    total_events: int
    submitted_events: int
    assessed_events: int
    claim_status_counts: tuple[int, int, int, int, int]
    recent_events: tuple[ClaimIndexEventRecord, ...]
    last_reconciliation: ClaimIndexReconciliationRecord | None


@dataclass(frozen=True)
class AssessmentRecord:
    """One model decision and its progress toward Sepolia write-back."""

    event_id: str
    chain_id: int
    contract_address: str
    claim_id: int
    model_version: str
    probability: float
    threshold: float
    fraud_score: int
    status: str
    reasons: tuple[FraudReason, ...]
    processing_status: str = "scored"
    transaction_hash: str | None = None
    block_number: int | None = None
    error: str | None = None

    @classmethod
    def from_score(
        cls,
        *,
        event_id: str,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        score: FraudScore,
    ) -> AssessmentRecord:
        return cls(
            event_id=event_id,
            chain_id=chain_id,
            contract_address=contract_address.lower(),
            claim_id=claim_id,
            model_version=score.model_version,
            probability=score.probability,
            threshold=score.threshold,
            fraud_score=score.score_basis_points,
            status="Flagged" if score.flagged else "UnderReview",
            reasons=score.reasons,
        )


def assessment_from_row(row: dict[str, Any] | None) -> AssessmentRecord | None:
    """Translate an internal database row into the stable domain record."""

    if row is None:
        return None
    raw_reasons = row["reasons"]
    if isinstance(raw_reasons, str):
        raw_reasons = json.loads(raw_reasons)
    reasons = tuple(
        FraudReason(
            feature=str(reason["feature"]),
            label=str(reason["label"]),
            contribution=float(reason["contribution"]),
        )
        for reason in raw_reasons
    )
    return AssessmentRecord(
        event_id=str(row["event_id"]),
        chain_id=int(row["chain_id"]),
        contract_address=str(row["contract_address"]),
        claim_id=int(row["claim_id"]),
        model_version=str(row["model_version"]),
        probability=float(row["probability"]),
        threshold=float(row["threshold"]),
        fraud_score=int(row["fraud_score"]),
        status=str(row["assessment_status"]),
        reasons=reasons,
        processing_status=str(row["processing_status"]),
        transaction_hash=row["transaction_hash"],
        block_number=(
            int(row["block_number"]) if row["block_number"] is not None else None
        ),
        error=row["error"],
    )
