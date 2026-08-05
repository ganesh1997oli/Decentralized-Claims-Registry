"""HTTP routes used by the claims web application."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    Request,
    Security,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from apps.backend.app.health import ReadinessProbe, build_readiness_probe
from apps.backend.app.indexer_operations import (
    IndexerOperationsAuthenticationError,
    IndexerOperationsBoundary,
    IndexerOperationsConfigurationError,
    IndexerOperationsService,
    IndexerOperationsServiceError,
)
from apps.backend.app.models import (
    AssessmentReasonResponse,
    ClaimAssessmentResponse,
    ClaimPageResponse,
    ClaimSubmission,
    ClaimSubmissionResponse,
    DuplicateDetectionResponse,
    DuplicateMatchResponse,
    HealthResponse,
    IndexerOperationsResponse,
    ReadinessResponse,
)
from apps.backend.app.service import (
    ClaimQueryService,
    ClaimQueryServiceError,
    ClaimSubmissionService,
    ClaimSubmissionServiceError,
)
from apps.backend.app.submission_auth import (
    ClaimRequestSizeLimitMiddleware,
    InsurerPrincipal,
    SubmissionAuthConfigurationError,
    SubmissionAuthenticationError,
    SubmissionAuthorizationError,
    SubmissionBoundary,
    SubmissionRateLimitError,
)
from packages.integrations.ethereum import (
    ClaimsDeployment,
    DeploymentConfigurationError,
    load_claims_deployment,
)
from packages.integrations.postgres import (
    PostgresConfigurationError,
    PostgresRepositories,
    PostgresStorageError,
)
from packages.observability import configure_logging, get_event_logger

logger = get_event_logger(__name__)
insurer_api_key_header = APIKeyHeader(
    name="X-Insurer-API-Key",
    auto_error=False,
)
indexer_operations_api_key_header = APIKeyHeader(
    name="X-Operations-API-Key",
    auto_error=False,
)


@lru_cache
def load_active_deployment() -> ClaimsDeployment:
    """Resolve deployment identity once for this process."""

    return load_claims_deployment(os.environ)


def get_active_deployment() -> ClaimsDeployment:
    """Expose the selected chain and address to deployment-scoped routes."""

    try:
        return load_active_deployment()
    except DeploymentConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@lru_cache
def load_submission_boundary() -> SubmissionBoundary:
    """Load hashed insurer credentials and process-local abuse controls."""

    return SubmissionBoundary.from_env()


def get_submission_boundary() -> SubmissionBoundary:
    try:
        return load_submission_boundary()
    except SubmissionAuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Insurer authentication is unavailable: {exc}",
        ) from exc


SubmissionBoundaryDependency = Annotated[
    SubmissionBoundary,
    Depends(get_submission_boundary),
]


def get_insurer_principal(
    request: Request,
    claim: ClaimSubmission,
    boundary: SubmissionBoundaryDependency,
    api_key: Annotated[str | None, Security(insurer_api_key_header)],
) -> InsurerPrincipal:
    """Authenticate the insurer and reserve quota before any external write."""

    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        return boundary.authorize_and_reserve(
            api_key=api_key,
            claimed_insurer_id=claim.insurer_id,
            client_ip=client_ip,
        )
    except SubmissionAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "ApiKey"},
        ) from exc
    except SubmissionAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except SubmissionRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


InsurerPrincipalDependency = Annotated[
    InsurerPrincipal,
    Depends(get_insurer_principal),
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging("claims-api")
    # Artifact selection is local and fast. Refuse to start with a missing,
    # legacy, or incompatible contract instead of discovering it on first use.
    deployment = load_active_deployment()
    boundary = load_submission_boundary()
    logger.info(
        "api.deployment_configured",
        deployment_id=deployment.deployment_id,
        chain_id=deployment.chain_id,
        contract_address=deployment.address,
    )
    logger.info(
        "api.submission_boundary_configured",
        boundary_type=type(boundary).__name__,
        # This is deliberately the global, non-secret switch only. Credential
        # exemptions and API-key material must not be disclosed at startup.
        rate_limit_bypass_enabled=boundary.rate_limit_bypass_enabled,
    )
    yield


app = FastAPI(
    title="Decentralized Claims Registry API",
    version="0.1.0",
    description=(
        "Synthetic-data demonstration API: validate a claim, upload it to public "
        "IPFS, and anchor its hash and CID on Sepolia."
    ),
    lifespan=lifespan,
)


def _claim_body_limit() -> int:
    try:
        value = int(os.environ.get("MAX_CLAIM_BODY_BYTES", "16384"))
    except ValueError as exc:
        raise SubmissionAuthConfigurationError(
            "MAX_CLAIM_BODY_BYTES must be a positive integer"
        ) from exc
    if value < 1:
        raise SubmissionAuthConfigurationError(
            "MAX_CLAIM_BODY_BYTES must be a positive integer"
        )
    return value


app.add_middleware(
    ClaimRequestSizeLimitMiddleware,
    max_bytes=_claim_body_limit(),
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
    allow_headers=[
        "Content-Type",
        "X-Insurer-API-Key",
        "X-Operations-API-Key",
    ],
)


@lru_cache
def get_claim_submission_service() -> ClaimSubmissionService:
    """Create write clients once and explain missing configuration as JSON."""

    try:
        return ClaimSubmissionService.from_env()
    except ClaimSubmissionServiceError as exc:
        # Dependency construction happens before the route function. Translating
        # the error here prevents FastAPI from returning an unexplained plain 500.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Claim submission is unavailable: {exc}",
        ) from exc


ClaimServiceDependency = Annotated[
    ClaimSubmissionService, Depends(get_claim_submission_service)
]


@lru_cache
def get_claim_query_service() -> ClaimQueryService:
    """Create the deployment-scoped index reader with no wallet credential."""

    try:
        return ClaimQueryService.from_env()
    except ClaimQueryServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Claims registry is unavailable: {exc}",
        ) from exc


ClaimQueryDependency = Annotated[ClaimQueryService, Depends(get_claim_query_service)]


@lru_cache
def get_postgres_repositories() -> PostgresRepositories:
    """Create focused readers only when the dashboard asks for stored data."""

    try:
        return PostgresRepositories.from_env()
    except (PostgresConfigurationError, PostgresStorageError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


PostgresRepositoriesDependency = Annotated[
    PostgresRepositories,
    Depends(get_postgres_repositories),
]
ActiveDeploymentDependency = Annotated[
    ClaimsDeployment,
    Depends(get_active_deployment),
]


@lru_cache
def load_indexer_operations_boundary() -> IndexerOperationsBoundary:
    """Load the digest-only operator credential once per API process."""

    return IndexerOperationsBoundary.from_env()


def get_indexer_operations_boundary() -> IndexerOperationsBoundary:
    try:
        return load_indexer_operations_boundary()
    except IndexerOperationsConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Indexer operations authentication is unavailable: {exc}",
        ) from exc


IndexerOperationsBoundaryDependency = Annotated[
    IndexerOperationsBoundary,
    Depends(get_indexer_operations_boundary),
]


def require_indexer_operations_access(
    boundary: IndexerOperationsBoundaryDependency,
    api_key: Annotated[
        str | None,
        Security(indexer_operations_api_key_header),
    ],
) -> None:
    """Reject unauthenticated telemetry reads before constructing adapters."""

    try:
        boundary.authenticate(api_key)
    except IndexerOperationsAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "ApiKey"},
        ) from exc


IndexerOperationsAccessDependency = Annotated[
    None,
    Depends(require_indexer_operations_access),
]


@lru_cache
def get_indexer_operations_service() -> IndexerOperationsService:
    try:
        return IndexerOperationsService.from_env()
    except IndexerOperationsServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Indexer operations are unavailable: {exc}",
        ) from exc


IndexerOperationsServiceDependency = Annotated[
    IndexerOperationsService,
    Depends(get_indexer_operations_service),
]


@lru_cache
def get_readiness_probe() -> ReadinessProbe:
    """Construct check definitions once; each evaluation uses fresh adapters."""

    return build_readiness_probe()


@app.get("/health", response_model=HealthResponse, tags=["operations"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/health/live", response_model=HealthResponse, tags=["operations"])
def health_live() -> HealthResponse:
    """Confirm only that the FastAPI process can serve a request."""

    return HealthResponse(status="ok")


@app.get(
    "/health/ready",
    response_model=ReadinessResponse,
    tags=["operations"],
    responses={503: {"model": ReadinessResponse}},
)
def health_ready(
    probe: Annotated[ReadinessProbe, Depends(get_readiness_probe)],
) -> ReadinessResponse | JSONResponse:
    """Report whether every dependency required for traffic is usable."""

    result = probe.evaluate()
    body = ReadinessResponse(
        status="ready" if result.ready else "not_ready",
        checks=result.checks,
    )
    if result.ready:
        return body
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body.model_dump()
    )


@app.get(
    "/operations/indexer",
    response_model=IndexerOperationsResponse,
    tags=["operations"],
    responses={
        401: {"description": "Missing or invalid operations API key"},
        503: {"description": "Operations dependencies are unavailable"},
    },
)
def get_indexer_operations(
    _access: IndexerOperationsAccessDependency,
    service: IndexerOperationsServiceDependency,
) -> IndexerOperationsResponse:
    """Return a bounded read-only indexer snapshot for trusted operators."""

    try:
        return service.snapshot()
    except IndexerOperationsServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.get("/claims", response_model=ClaimPageResponse, tags=["claims"])
def list_claims(
    service: ClaimQueryDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ClaimPageResponse:
    # FastAPI checks the page values before this function is called.
    try:
        return service.list_claims(page=page, page_size=page_size)
    except ClaimQueryServiceError as exc:
        raise HTTPException(
            # The query path now depends on the local read-model database, so a
            # transient failure means this API instance is unavailable rather
            # than that an upstream blockchain gateway returned a bad response.
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.get(
    "/claims/{claim_id}/assessment",
    response_model=ClaimAssessmentResponse,
    tags=["claims"],
)
def get_claim_assessment(
    claim_id: Annotated[int, Path(ge=0)],
    repositories: PostgresRepositoriesDependency,
    deployment: ActiveDeploymentDependency,
) -> ClaimAssessmentResponse:
    try:
        query = {
            "chain_id": deployment.chain_id,
            "contract_address": deployment.address,
            "claim_id": claim_id,
        }
        record = repositories.assessments.get_latest_for_claim(**query)
        duplicate_check = repositories.duplicates.get_duplicate_check_for_claim(**query)
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
    claim: ClaimSubmission,
    principal: InsurerPrincipalDependency,
    service: ClaimServiceDependency,
) -> ClaimSubmissionResponse:
    # The service owns the workflow; this route only translates failures into
    # an HTTP response the frontend can understand.
    try:
        return service.submit(claim, principal)
    except ClaimSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
