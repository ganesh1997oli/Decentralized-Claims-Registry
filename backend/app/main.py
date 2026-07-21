"""FastAPI entry point for proposal Week 3."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status

from backend.app.models import (
    ClaimSubmission,
    ClaimSubmissionResponse,
    HealthResponse,
)
from backend.app.service import ClaimSubmissionService, ClaimSubmissionServiceError


app = FastAPI(
    title="Decentralized Claims Registry API",
    version="0.1.0",
    description=(
        "Synthetic-data demonstration API: validate a claim, upload it to public "
        "IPFS, and anchor its hash and CID on Sepolia."
    ),
)


@lru_cache
def get_claim_submission_service() -> ClaimSubmissionService:
    """Construct the external adapters once, on the first submission request."""

    return ClaimSubmissionService.from_env()


ClaimServiceDependency = Annotated[
    ClaimSubmissionService, Depends(get_claim_submission_service)
]


@app.get("/health", response_model=HealthResponse, tags=["operations"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/claims",
    response_model=ClaimSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["claims"],
)
def submit_claim(
    claim: ClaimSubmission, service: ClaimServiceDependency
) -> ClaimSubmissionResponse:
    try:
        return service.submit(claim)
    except ClaimSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
