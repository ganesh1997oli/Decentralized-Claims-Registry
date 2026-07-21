"""Validated HTTP request and response models."""

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
        """Return the versioned document committed to IPFS and Ethereum."""

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


class HealthResponse(BaseModel):
    status: str
