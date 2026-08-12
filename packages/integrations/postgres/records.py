"""Typed persistence records shared by PostgreSQL adapters and callers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from packages.model.contracts import FraudReason, FraudScore

GaslessSubmissionState = Literal[
    "preparing",
    "prepared",
    "authorized",
    "signed",
    "broadcast",
    "confirmed",
    "failed",
    "expired",
]


@dataclass(frozen=True)
class GaslessSubmissionRecord:
    """Durable state of one idempotent insurer-authorized relay request.

    One record is the current view of the state machine. Raw transaction fields
    appear only after signing, receipt fields only after confirmation, and
    immutable replacement history lives in ``gasless_relay_attempts`` rather
    than being flattened into this view.
    """

    submission_id: UUID
    credential_id: str
    insurer_id: str
    signer_address: str
    chain_id: int
    contract_address: str
    forwarder_address: str
    idempotency_key_hash: str
    client_fingerprint: str
    state: GaslessSubmissionState
    request_fingerprint: str = ""
    claim_hash: str | None = None
    data_pointer: str | None = None
    call_data: str | None = None
    forwarder_nonce: int | None = None
    forward_gas: int | None = None
    deadline: int | None = None
    insurer_signature: str | None = None
    relayer_address: str | None = None
    relayer_nonce: int | None = None
    raw_transaction: str | None = None
    transaction_hash: str | None = None
    max_fee_per_gas: int | None = None
    max_priority_fee_per_gas: int | None = None
    block_number: int | None = None
    claim_id: int | None = None
    relay_attempts: int = 0
    last_error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    authorized_at: datetime | None = None
    broadcast_at: datetime | None = None
    last_broadcast_at: datetime | None = None
    confirmed_at: datetime | None = None


def gasless_submission_from_row(
    row: dict[str, Any] | None,
) -> GaslessSubmissionRecord | None:
    """Translate one internal outbox row into its stable typed domain record.

    Numeric PostgreSQL adapters may return ``Decimal`` or integer-compatible
    values. Normalizing at this boundary keeps service and relayer code free of
    database-driver details while preserving nullable state-specific fields.
    """

    if row is None:
        return None

    def optional_int(name: str) -> int | None:
        """Normalize one nullable numeric database field to Python ``int``."""

        value = row.get(name)
        return int(value) if value is not None else None

    return GaslessSubmissionRecord(
        submission_id=UUID(str(row["submission_id"])),
        credential_id=str(row["credential_id"]),
        insurer_id=str(row["insurer_id"]),
        signer_address=str(row["signer_address"]),
        chain_id=int(row["chain_id"]),
        contract_address=str(row["contract_address"]),
        forwarder_address=str(row["forwarder_address"]),
        idempotency_key_hash=str(row["idempotency_key_hash"]),
        client_fingerprint=str(row["client_fingerprint"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        request_fingerprint=str(row["request_fingerprint"]),
        claim_hash=row.get("claim_hash"),
        data_pointer=row.get("data_pointer"),
        call_data=row.get("call_data"),
        forwarder_nonce=optional_int("forwarder_nonce"),
        forward_gas=optional_int("forward_gas"),
        deadline=optional_int("deadline"),
        insurer_signature=row.get("insurer_signature"),
        relayer_address=row.get("relayer_address"),
        relayer_nonce=optional_int("relayer_nonce"),
        raw_transaction=row.get("raw_transaction"),
        transaction_hash=row.get("transaction_hash"),
        max_fee_per_gas=optional_int("max_fee_per_gas"),
        max_priority_fee_per_gas=optional_int("max_priority_fee_per_gas"),
        block_number=optional_int("block_number"),
        claim_id=optional_int("claim_id"),
        relay_attempts=int(row.get("relay_attempts", 0)),
        last_error_code=row.get("last_error_code"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        authorized_at=row.get("authorized_at"),
        broadcast_at=row.get("broadcast_at"),
        last_broadcast_at=row.get("last_broadcast_at"),
        confirmed_at=row.get("confirmed_at"),
    )


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
    """One immutable public contract event shown to index operators.

    ``event_id`` is the cross-retry identity; block number, log index, and event
    ID together form the newest-first keyset pagination position.
    """

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
    """One stable newest-first slice of the immutable event audit stream.

    ``has_more`` comes from fetching one row beyond the requested limit. The
    service converts the last returned event into an opaque browser cursor.
    """

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
    """One bounded database snapshot for the authenticated operations UI.

    This record contains only durable PostgreSQL facts. The service layer adds a
    best-effort chain-head sample and derives lag/state without contaminating the
    repository with RPC availability concerns.
    """

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
        """Create the initial durable worker record from a deterministic score.

        Status is derived once from the model's flagged decision and contract scope
        is normalized for later idempotent lookups. On-chain receipt fields remain
        empty until the worker completes write-back.
        """

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


# These values describe a human investigative conclusion, not the model's
# UnderReview/Flagged screening status or the claim's Approved/Rejected business
# disposition. ``Inconclusive`` intentionally has no binary training-label value.
HumanFraudOutcome = Literal["ConfirmedFraud", "Legitimate", "Inconclusive"]


@dataclass(frozen=True)
class AssessorOutcomeRecord:
    """One immutable revision of an authorised human fraud conclusion.

    Revisions are append-only. ``assessor_reference`` comes from server-side
    authentication rather than the request body, making the record attributable
    without storing an API key or asserting that the reference is a public legal
    identity. The detailed outcome remains off-chain by design.
    """

    outcome_id: UUID
    chain_id: int
    contract_address: str
    claim_id: int
    revision: int
    outcome: HumanFraudOutcome
    assessor_reference: str
    notes: str | None
    assessed_at: datetime


def assessor_outcome_from_row(
    row: dict[str, Any] | None,
) -> AssessorOutcomeRecord | None:
    """Translate a trusted PostgreSQL row into the human-outcome record."""

    if row is None:
        return None
    assessed_at = row["assessed_at"]
    if not isinstance(assessed_at, datetime):
        raise TypeError("PostgreSQL returned an invalid assessor outcome timestamp")
    outcome = str(row["outcome"])
    if outcome not in {"ConfirmedFraud", "Legitimate", "Inconclusive"}:
        raise ValueError("PostgreSQL returned an invalid human fraud outcome")
    return AssessorOutcomeRecord(
        outcome_id=UUID(str(row["outcome_id"])),
        chain_id=int(row["chain_id"]),
        contract_address=str(row["contract_address"]),
        claim_id=int(row["claim_id"]),
        revision=int(row["revision"]),
        outcome=outcome,  # type: ignore[arg-type]
        assessor_reference=str(row["assessor_reference"]),
        notes=(str(row["notes"]) if row.get("notes") is not None else None),
        assessed_at=assessed_at,
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
