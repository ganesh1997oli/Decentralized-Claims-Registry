"""The request and response shapes shared with the frontend."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class ClaimSubmission(BaseModel):
    """A synthetic insurance claim accepted by the demonstration API."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    claim_reference: str = Field(
        min_length=1, max_length=100, alias="claimReference"
    )
    policy_reference: str = Field(
        min_length=1, max_length=100, alias="policyReference"
    )
    claim_type: str = Field(min_length=1, max_length=50, alias="claimType")
    incident_date: date = Field(alias="incidentDate")
    amount_pence: int = Field(ge=0, le=1_000_000_000, alias="amountPence")
    description: str = Field(min_length=1, max_length=2_000)
    evidence: list[str] = Field(default_factory=list, max_length=20)

    def canonical_document(self) -> dict[str, object]:
        """Build the exact document that will be stored and hashed."""

        # The schema version gives us a safe way to change this document later.
        return {
            "schemaVersion": 1,
            **self.model_dump(by_alias=True, mode="json"),
        }


class ClaimSubmissionResponse(BaseModel):
    """Receipt returned after IPFS and Sepolia submission both succeed."""

    claim_id: int
    transaction_hash: str
    block_number: int
    data_pointer: str
    claim_hash: str
    assessment: "ClaimAssessmentResponse"


class AssessmentReasonResponse(BaseModel):
    feature: str
    label: str
    contribution: float


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
    """One page of claims, with the newest claims first."""

    items: list[ClaimListItemResponse]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=1)


class HealthResponse(BaseModel):
    status: str
