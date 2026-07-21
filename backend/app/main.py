"""HTTP routes used by the claims web application."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from backend.app.models import (
    ClaimPageResponse,
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

frontend_origins = [
    origin.strip()
    for origin in os.environ.get(
        "FRONTEND_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@lru_cache
def get_claim_submission_service() -> ClaimSubmissionService:
    """Create the external clients once and reuse them for later requests."""

    return ClaimSubmissionService.from_env()


ClaimServiceDependency = Annotated[
    ClaimSubmissionService, Depends(get_claim_submission_service)
]


@app.get("/health", response_model=HealthResponse, tags=["operations"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/claims", response_model=ClaimPageResponse, tags=["claims"])
def list_claims(
    service: ClaimServiceDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ClaimPageResponse:
    # FastAPI checks the page values before this function is called.
    try:
        return service.list_claims(page=page, page_size=page_size)
    except ClaimSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@app.post(
    "/claims",
    response_model=ClaimSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["claims"],
)
def submit_claim(
    claim: ClaimSubmission, service: ClaimServiceDependency
) -> ClaimSubmissionResponse:
    # The service owns the workflow; this route only translates failures into
    # an HTTP response the frontend can understand.
    try:
        return service.submit(claim)
    except ClaimSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
