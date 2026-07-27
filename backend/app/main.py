"""HTTP routes used by the claims web application."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, status
from fastapi.middleware.cors import CORSMiddleware

from backend.app.models import (
    AssessmentReasonResponse,
    ClaimAssessmentResponse,
    ClaimPageResponse,
    ClaimSubmission,
    ClaimSubmissionResponse,
    DuplicateDetectionResponse,
    DuplicateMatchResponse,
    HealthResponse,
)
from backend.app.service import ClaimSubmissionService, ClaimSubmissionServiceError
from integrations.postgres import (
    PostgresAssessmentRepository,
    PostgresConfigurationError,
    PostgresStorageError,
)


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


@lru_cache
def get_assessment_repository() -> PostgresAssessmentRepository:
    """Create the assessment reader only when the dashboard asks for it."""

    try:
        repository = PostgresAssessmentRepository.from_env()
        repository.ensure_schema()
        return repository
    except (PostgresConfigurationError, PostgresStorageError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


AssessmentRepositoryDependency = Annotated[
    PostgresAssessmentRepository,
    Depends(get_assessment_repository),
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


@app.get(
    "/claims/{claim_id}/assessment",
    response_model=ClaimAssessmentResponse,
    tags=["claims"],
)
def get_claim_assessment(
    claim_id: Annotated[int, Path(ge=0)],
    repository: AssessmentRepositoryDependency,
) -> ClaimAssessmentResponse:
    try:
        record = repository.get_latest_for_claim(claim_id)
        duplicate_check = repository.get_duplicate_check_for_claim(claim_id)
    except PostgresStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assessment is still pending",
        )
    return ClaimAssessmentResponse(
        status=record.status,
        fraud_score=record.fraud_score,
        probability=record.probability,
        threshold=record.threshold,
        model_version=record.model_version,
        reasons=[
            AssessmentReasonResponse(
                feature=reason.feature,
                label=reason.label,
                contribution=reason.contribution,
            )
            for reason in record.reasons
        ],
        on_chain=record.processing_status == "completed",
        transaction_hash=record.transaction_hash,
        block_number=record.block_number,
        error=record.error,
        duplicate_detection=(
            DuplicateDetectionResponse(
                insurer_id=duplicate_check.insurer_id,
                fingerprint_version=duplicate_check.fingerprint_version,
                duplicate_detected=duplicate_check.duplicate_detected,
                matches=[
                    DuplicateMatchResponse(
                        claim_id=match.claim_id,
                        insurer_id=match.insurer_id,
                    )
                    for match in duplicate_check.matches
                ],
            )
            if duplicate_check is not None
            else None
        ),
    )


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
