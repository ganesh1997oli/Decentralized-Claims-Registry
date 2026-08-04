"""The request and response shapes shared with the frontend."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class SubmissionAuthorization(BaseModel):
    """Gateway attestation proving which credential authorized a claim."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Literal["insurer-principal-hmac-sha256-v1"]
    credential_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        alias="credentialId",
    )
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class StoredClaimDocument(ClaimSubmission):
    """The versioned claim document downloaded from IPFS by workers."""

    schema_version: Literal[4] = Field(alias="schemaVersion")
    submission_authorization: SubmissionAuthorization = Field(
        alias="submissionAuthorization"
    )


class ClaimSubmissionResponse(BaseModel):
    """Receipt returned after IPFS and Sepolia submission both succeed."""

    claim_id: int
    transaction_hash: str
    block_number: int
    data_pointer: str
    claim_hash: str
    assessment: ClaimAssessmentResponse | None = None


class AssessmentReasonResponse(BaseModel):
    feature: str
    label: str
    contribution: float


class DuplicateMatchResponse(BaseModel):
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
    status: str


class ReadinessResponse(BaseModel):
    """Safe per-dependency state for load balancers and operators."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, str]
