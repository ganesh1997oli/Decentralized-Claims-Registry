"""The request and response shapes shared with the frontend."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MotorClaimType = Literal["collision", "theft", "fire", "flood"]
VehicleType = Literal[
    "sedan",
    "suv",
    "pickup",
    "minibus",
    "truck",
    "motorcycle",
    "bus",
    "hatchback",
    "van",
    "other",
]
RegionType = Literal["urban", "rural"]
Country = Literal[
    "South Africa",
    "Nigeria",
    "Kenya",
    "Ghana",
    "Tanzania",
    "Uganda",
    "Rwanda",
    "Ethiopia",
    "Senegal",
    "Cote d'Ivoire",
    "Zambia",
    "Mozambique",
]


class ClaimSubmission(BaseModel):
    """A synthetic motor claim accepted by the application."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    insurer_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
        alias="insurerId",
    )
    claim_reference: str = Field(min_length=1, max_length=100, alias="claimReference")
    policy_reference: str = Field(min_length=1, max_length=100, alias="policyReference")
    claim_type: MotorClaimType = Field(alias="claimType")
    incident_date: date = Field(alias="incidentDate")
    claim_amount_usd: float = Field(
        gt=0,
        le=100_000_000,
        alias="claimAmountUsd",
    )
    policy_premium_usd: float = Field(
        gt=0,
        le=10_000_000,
        alias="policyPremiumUsd",
    )
    vehicle_age: int = Field(ge=1, le=30, alias="vehicleAge")
    vehicle_type: VehicleType = Field(alias="vehicleType")
    country: Country
    region_type: RegionType = Field(alias="regionType")
    third_party_injury_flag: bool = Field(alias="thirdPartyInjuryFlag")
    total_loss_flag: bool = Field(alias="totalLossFlag")
    description: str = Field(min_length=1, max_length=2_000)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class ClaimantChallengeRequest(BaseModel):
    """Wallet selected by a person starting a public claim session."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    wallet_address: str = Field(
        pattern=r"^0x[0-9a-fA-F]{40}$",
        alias="walletAddress",
    )


class ClaimantChallengeResponse(BaseModel):
    """Human-readable, short-lived message the claimant wallet must sign."""

    challenge_id: UUID
    message: str
    expires_at: datetime


class ClaimantSessionRequest(BaseModel):
    """Signature that consumes one previously issued wallet challenge."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID
    signature: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")


class ClaimantSessionResponse(BaseModel):
    """Short-lived bearer session returned after wallet ownership is proven."""

    access_token: str = Field(min_length=32)
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    claimant_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class SubmissionAuthorization(BaseModel):
    """Gateway attestation proving which credential authorized a claim."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Literal[
        "insurer-principal-wallet-hmac-sha256-v2",
        "claimant-policy-permit-hmac-sha256-v3",
    ]
    credential_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        alias="credentialId",
    )
    subject_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        alias="subjectId",
    )
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    signer_address: str = Field(
        pattern=r"^0x[0-9a-fA-F]{40}$",
        alias="signerAddress",
    )
    claimant_address: str | None = Field(
        default=None,
        pattern=r"^0x[0-9a-fA-F]{40}$",
        alias="claimantAddress",
    )
    claimant_commitment: str | None = Field(
        default=None,
        pattern=r"^0x[0-9a-fA-F]{64}$",
        alias="claimantCommitment",
    )
    insurer_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
        alias="insurerId",
    )
    insurer_address: str | None = Field(
        default=None,
        pattern=r"^0x[0-9a-fA-F]{40}$",
        alias="insurerAddress",
    )
    policy_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        alias="policyId",
    )

    @model_validator(mode="after")
    def validate_versioned_identity(self) -> SubmissionAuthorization:
        """Require exactly the identity vocabulary declared by the version."""

        public_fields = (
            self.subject_id,
            self.claimant_address,
            self.claimant_commitment,
            self.insurer_id,
            self.insurer_address,
            self.policy_id,
        )
        if self.version.endswith("-v2"):
            if self.credential_id is None or any(value is not None for value in public_fields):
                raise ValueError("Legacy insurer authorization fields are inconsistent")
        elif self.credential_id is not None or any(value is None for value in public_fields):
            raise ValueError("Public claimant authorization fields are incomplete")
        return self


class StoredClaimDocument(ClaimSubmission):
    """The versioned claim document downloaded from IPFS by workers."""

    schema_version: Literal[5, 6] = Field(alias="schemaVersion")
    submission_authorization: SubmissionAuthorization = Field(
        alias="submissionAuthorization"
    )

    @model_validator(mode="after")
    def validate_schema_authorization_version(self) -> StoredClaimDocument:
        if self.schema_version == 5 and not self.submission_authorization.version.endswith(
            "-v2"
        ):
            raise ValueError("Schema version 5 requires insurer authorization v2")
        if self.schema_version == 6 and not self.submission_authorization.version.endswith(
            "-v3"
        ):
            raise ValueError("Schema version 6 requires public claimant authorization v3")
        return self


class ClaimSubmissionResponse(BaseModel):
    """Receipt returned after IPFS and Sepolia submission both succeed."""

    claim_id: int
    transaction_hash: str
    block_number: int
    data_pointer: str
    claim_hash: str
    assessment: ClaimAssessmentResponse | None = None


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


class EIP712Field(BaseModel):
    """One field in an EIP-712 type definition returned to a wallet."""

    name: str
    type: str


class EIP712DomainData(BaseModel):
    """Immutable domain of the checked-in ClaimsForwarder deployment."""

    model_config = ConfigDict(populate_by_name=True)

    name: Literal["ClaimsRegistryForwarder"]
    version: Literal["1"]
    chain_id: int = Field(gt=0, alias="chainId")
    verifying_contract: str = Field(
        pattern=r"^0x[0-9a-fA-F]{40}$",
        alias="verifyingContract",
    )


class EIP712ForwardRequestMessage(BaseModel):
    """Exact ERC2771Forwarder values covered by the submitter signature."""

    from_address: str = Field(
        pattern=r"^0x[0-9a-fA-F]{40}$",
        alias="from",
    )
    to: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    value: str = Field(pattern=r"^[0-9]+$")
    gas: str = Field(pattern=r"^[0-9]+$")
    nonce: str = Field(pattern=r"^[0-9]+$")
    deadline: str = Field(pattern=r"^[0-9]+$")
    data: str = Field(pattern=r"^0x[0-9a-fA-F]+$")


class EIP712TypedData(BaseModel):
    """Wallet-ready typed data; clients must sign this object unchanged."""

    model_config = ConfigDict(populate_by_name=True)

    types: dict[str, list[EIP712Field]]
    primary_type: Literal["ForwardRequest"] = Field(alias="primaryType")
    domain: EIP712DomainData
    message: EIP712ForwardRequestMessage


class GaslessAuthorizationRequest(BaseModel):
    """Submitter wallet signature authorizing a prepared forward request."""

    model_config = ConfigDict(extra="forbid")

    signature: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")


class GaslessSubmissionResponse(BaseModel):
    """Idempotent status shared by prepare, authorize, and polling routes."""

    submission_id: UUID
    state: GaslessSubmissionState
    signer_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    chain_id: int = Field(gt=0)
    contract_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    forwarder_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    claim_hash: str | None = Field(
        default=None,
        pattern=r"^0x[0-9a-fA-F]{64}$",
    )
    data_pointer: str | None = Field(
        default=None,
        pattern=r"^ipfs://[A-Za-z0-9]{1,121}$",
    )
    deadline: int | None = None
    typed_data: EIP712TypedData | None = None
    receipt: ClaimSubmissionResponse | None = None
    error_code: str | None = None
    poll_after_ms: int = Field(default=1_500, ge=250, le=10_000)


class GaslessNetworkResponse(BaseModel):
    """Public wallet preflight data for the active sponsored deployment."""

    chain_id: int = Field(gt=0)
    contract_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    forwarder_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    domain_name: Literal["ClaimsRegistryForwarder"] = "ClaimsRegistryForwarder"
    domain_version: Literal["1"] = "1"


class AssessmentReasonResponse(BaseModel):
    """One human-readable feature contribution from the fraud model."""

    feature: str
    label: str
    contribution: float


class DuplicateMatchResponse(BaseModel):
    """One earlier public claim associated with a duplicate fingerprint."""

    claim_id: int = Field(ge=0)
    insurer_id: str


class DuplicateDetectionResponse(BaseModel):
    """A possible cross-insurer match that always requires human review."""

    insurer_id: str
    fingerprint_version: str
    duplicate_detected: bool
    matches: list[DuplicateMatchResponse]


class ClaimAssessmentResponse(BaseModel):
    """Model result and the receipt showing whether it reached the contract."""

    status: str
    fraud_score: int = Field(ge=0, le=10_000)
    probability: float = Field(ge=0, le=1)
    threshold: float = Field(gt=0, lt=1)
    model_version: str
    reasons: list[AssessmentReasonResponse]
    on_chain: bool
    transaction_hash: str | None = None
    block_number: int | None = None
    error: str | None = None
    duplicate_detection: DuplicateDetectionResponse | None = None


HumanFraudOutcome = Literal["ConfirmedFraud", "Legitimate", "Inconclusive"]


class AssessorOutcomeRequest(BaseModel):
    """A human conclusion supplied after reviewing one screened claim.

    The assessor identity is absent because FastAPI derives it from the
    authenticated credential so a caller cannot write another reviewer's name.
    Approval and rejection are also absent because claim disposition is a
    separate business decision and must never be converted into a fraud label.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: HumanFraudOutcome
    notes: str | None = Field(default=None, max_length=2_000)


class AssessorOutcomeResponse(BaseModel):
    """One immutable revision of the private human-review audit trail."""

    outcome_id: UUID
    claim_id: int = Field(ge=0)
    revision: int = Field(gt=0)
    outcome: HumanFraudOutcome
    assessor_reference: str
    notes: str | None = None
    assessed_at: datetime


class AssessorSessionResponse(BaseModel):
    """Minimal identity proof returned after assessor-key authentication."""

    assessor_reference: str


class ClaimListItemResponse(BaseModel):
    """Current on-chain state for one claim in the claims dashboard."""

    claim_id: int
    claimant: str
    claim_hash: str
    data_pointer: str
    status: str
    fraud_score: int = Field(ge=0, le=10_000)
    submitted_at: int
    updated_at: int


class ClaimPageResponse(BaseModel):
    """One page from the confirmed-event index, with newest claims first."""

    items: list[ClaimListItemResponse]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=1)
    # ``None`` is an explicit uninitialized state, not block zero. Exposing the
    # checkpoint lets operators and the UI detect an index that has not started
    # without requiring another RPC call in the dashboard request path.
    indexed_through_block: int | None = Field(default=None, ge=0)


class HealthResponse(BaseModel):
    """Process-only health response retained for simple load-balancer probes."""

    status: str


class ReadinessResponse(BaseModel):
    """Safe per-dependency state for load balancers and operators."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, str]


class ClaimStatusCountsResponse(BaseModel):
    """Current projection totals for each Solidity claim status."""

    submitted: int = Field(ge=0)
    under_review: int = Field(ge=0)
    approved: int = Field(ge=0)
    rejected: int = Field(ge=0)
    flagged: int = Field(ge=0)


class ClaimIndexEventResponse(BaseModel):
    """One recent immutable event with only publicly anchored fields."""

    event_id: str
    claim_id: int = Field(ge=0)
    event_type: Literal["ClaimSubmitted", "ClaimAssessed"]
    block_number: int = Field(ge=0)
    transaction_hash: str
    log_index: int = Field(ge=0)
    event_timestamp: int = Field(gt=0)
    status: str
    fraud_score: int = Field(ge=0, le=10_000)
    indexed_at: datetime


class ClaimIndexEventPageResponse(BaseModel):
    """One keyset-paginated slice of the authenticated event audit stream.

    ``next_cursor`` is an opaque position for the next older page, not a page
    number, secret, or stable bookmark across projection rebuilds.
    """

    items: list[ClaimIndexEventResponse]
    page_size: int = Field(ge=1, le=50)
    next_cursor: str | None = None


class ClaimIndexReconciliationResponse(BaseModel):
    """Most recent persisted proof that index and contract agreed."""

    indexed_through_block: int = Field(ge=0)
    chain_claims: int = Field(ge=0)
    indexed_claims: int = Field(ge=0)
    missing_claim_ids: list[int]
    unexpected_claim_ids: list[int]
    mismatched_claim_ids: list[int]
    consistent: bool
    duration_ms: int = Field(ge=0)
    checked_at: datetime


class IndexerOperationsResponse(BaseModel):
    """Authenticated, bounded telemetry for one indexer deployment.

    Stored PostgreSQL fields remain populated when RPC sampling fails; chain
    head, safe head, and lag then become null and ``state`` becomes ``degraded``.
    The response exposes no IPFS payload, private key, or repair capability.
    """

    state: Literal["healthy", "catching_up", "stalled", "uninitialized", "degraded"]
    rpc_status: Literal["available", "unavailable"]
    deployment_id: str
    chain_id: int = Field(gt=0)
    contract_address: str
    confirmation_blocks: int = Field(ge=0)
    stale_after_seconds: int = Field(ge=1)
    latest_block: int | None = Field(default=None, ge=0)
    safe_block: int | None = Field(default=None, ge=0)
    indexed_through_block: int | None = Field(default=None, ge=0)
    block_lag: int | None = Field(default=None, ge=0)
    checkpoint_updated_at: datetime | None = None
    checkpoint_age_seconds: int | None = Field(default=None, ge=0)
    total_claims: int = Field(ge=0)
    total_events: int = Field(ge=0)
    submitted_events: int = Field(ge=0)
    assessed_events: int = Field(ge=0)
    claim_status_counts: ClaimStatusCountsResponse
    recent_events: list[ClaimIndexEventResponse]
    last_reconciliation: ClaimIndexReconciliationResponse | None = None
    observed_at: datetime
