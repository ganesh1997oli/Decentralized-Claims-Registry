"""HTTP routes used by the claims web application."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Security,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from apps.backend.app.assessor_outcomes import (
    AssessorOutcomeAuthenticationError,
    AssessorOutcomeBoundary,
    AssessorOutcomeConfigurationError,
    AssessorPrincipal,
)
from apps.backend.app.claimant_auth import (
    ClaimantAuthConfigurationError,
    ClaimantAuthenticationError,
    ClaimantAuthenticationRateLimitError,
    ClaimantSession,
    ClaimantSessionManager,
)
from apps.backend.app.coverage_governance import (
    CoverageGovernanceAccessError,
    CoverageGovernanceError,
    CoverageGovernanceService,
    CoverageGovernanceStateError,
)
from apps.backend.app.gasless_service import (
    GaslessClaimSubmissionService,
    GaslessSubmissionAccessError,
    GaslessSubmissionEligibilityError,
    GaslessSubmissionRateLimitError,
    GaslessSubmissionServiceError,
    GaslessSubmissionStateError,
)
from apps.backend.app.governance_auth import (
    GovernanceAuthenticationError,
    GovernanceBoundary,
    GovernanceConfigurationError,
    GovernancePrincipal,
)
from apps.backend.app.health import ReadinessProbe, build_readiness_probe
from apps.backend.app.indexer_operations import (
    IndexerOperationsAuthenticationError,
    IndexerOperationsBoundary,
    IndexerOperationsConfigurationError,
    IndexerOperationsQueryError,
    IndexerOperationsService,
    IndexerOperationsServiceError,
)
from apps.backend.app.models import (
    AssessmentReasonResponse,
    AssessorOutcomeRequest,
    AssessorOutcomeResponse,
    AssessorSessionResponse,
    ClaimantChallengeRequest,
    ClaimantChallengeResponse,
    ClaimantSessionRequest,
    ClaimantSessionResponse,
    ClaimAssessmentResponse,
    ClaimIndexEventPageResponse,
    ClaimPageResponse,
    ClaimSubmission,
    CoverageDecisionProposalResponse,
    CoverageDecisionRequest,
    DuplicateDetectionResponse,
    DuplicateMatchResponse,
    GaslessAuthorizationRequest,
    GaslessNetworkResponse,
    GaslessSubmissionResponse,
    GovernanceSessionResponse,
    HealthResponse,
    IndexerOperationsResponse,
    ReadinessResponse,
)
from apps.backend.app.policy_eligibility import PolicyEligibilityConfigurationError
from apps.backend.app.service import (
    ClaimQueryService,
    ClaimQueryServiceError,
)
from apps.backend.app.submission_auth import (
    ClaimRequestSizeLimitMiddleware,
    SubmissionAuthConfigurationError,
)
from packages.integrations.ethereum import (
    ClaimsDeployment,
    DeploymentConfigurationError,
    load_claims_deployment,
)
from packages.integrations.postgres import (
    CoverageDecisionConflictError,
    PostgresConfigurationError,
    PostgresRepositories,
    PostgresStorageError,
)
from packages.observability import configure_logging, get_event_logger

logger = get_event_logger(__name__)
indexer_operations_api_key_header = APIKeyHeader(
    name="X-Operations-API-Key",
    auto_error=False,
)
assessor_outcome_api_key_header = APIKeyHeader(
    name="X-Assessor-API-Key",
    auto_error=False,
)
governance_api_key_header = APIKeyHeader(
    name="X-Governance-API-Key",
    auto_error=False,
)
claimant_bearer = HTTPBearer(auto_error=False)


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
def load_assessor_outcome_boundary() -> AssessorOutcomeBoundary:
    """Load the independent human-review credential set once per API process."""

    return AssessorOutcomeBoundary.from_env()


def get_assessor_outcome_boundary() -> AssessorOutcomeBoundary:
    """Expose human-review authentication without affecting public API startup.

    Deployments that do not enable the optional assessor step may continue to
    serve claim submission and screening. Only the dedicated assessor endpoints
    return 503 when their digest-only credentials have not been configured.
    """

    try:
        return load_assessor_outcome_boundary()
    except AssessorOutcomeConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Assessor outcome authentication is unavailable: {exc}",
        ) from exc


AssessorOutcomeBoundaryDependency = Annotated[
    AssessorOutcomeBoundary,
    Depends(get_assessor_outcome_boundary),
]


def get_assessor_principal(
    boundary: AssessorOutcomeBoundaryDependency,
    api_key: Annotated[str | None, Security(assessor_outcome_api_key_header)],
) -> AssessorPrincipal:
    """Authenticate a human reviewer before reading or writing private outcomes."""

    try:
        return boundary.authenticate(api_key)
    except AssessorOutcomeAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "ApiKey"},
        ) from exc


AssessorPrincipalDependency = Annotated[
    AssessorPrincipal,
    Depends(get_assessor_principal),
]


@lru_cache
def load_governance_boundary() -> GovernanceBoundary:
    """Load digest-only maker credentials once per API process."""

    return GovernanceBoundary.from_env()


def get_governance_boundary() -> GovernanceBoundary:
    try:
        return load_governance_boundary()
    except GovernanceConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Coverage governance authentication is unavailable: {exc}",
        ) from exc


GovernanceBoundaryDependency = Annotated[
    GovernanceBoundary,
    Depends(get_governance_boundary),
]


def get_governance_principal(
    boundary: GovernanceBoundaryDependency,
    api_key: Annotated[str | None, Security(governance_api_key_header)],
) -> GovernancePrincipal:
    """Authenticate the proposal maker; the wallet remains a second checker."""

    try:
        return boundary.authenticate(api_key)
    except GovernanceAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "ApiKey"},
        ) from exc


GovernancePrincipalDependency = Annotated[
    GovernancePrincipal,
    Depends(get_governance_principal),
]


@lru_cache
def load_coverage_governance_service() -> CoverageGovernanceService:
    return CoverageGovernanceService.from_env()


def get_coverage_governance_service() -> CoverageGovernanceService:
    try:
        return load_coverage_governance_service()
    except (CoverageGovernanceError, DeploymentConfigurationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Coverage governance is unavailable: {exc}",
        ) from exc


CoverageGovernanceServiceDependency = Annotated[
    CoverageGovernanceService,
    Depends(get_coverage_governance_service),
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Validate critical local configuration before the API accepts traffic.

    Deployment metadata, claimant authentication, permit issuance, and policy
    eligibility are deterministic startup prerequisites. Remote dependencies
    remain readiness checks so a temporary outage does not force a process
    restart loop.
    """

    configure_logging("claims-api")
    # Artifact selection is local and fast. Refuse to start with a missing,
    # legacy, or incompatible contract instead of discovering it on first use.
    deployment = load_active_deployment()
    deployment.require_public_intake()
    claimant_sessions = load_claimant_session_manager()
    gasless = GaslessClaimSubmissionService.from_env()
    if gasless.eligibility is None:
        raise PolicyEligibilityConfigurationError(
            "POLICY_ELIGIBILITY_RECORDS_JSON is required for public intake"
        )
    logger.info(
        "api.deployment_configured",
        deployment_id=deployment.deployment_id,
        chain_id=deployment.chain_id,
        contract_address=deployment.address,
    )
    logger.info(
        "api.public_intake_configured",
        authentication_type=type(claimant_sessions).__name__,
        eligibility_type=type(gasless.eligibility).__name__,
    )
    yield


app = FastAPI(
    title="Decentralized Claims Registry API",
    version="0.1.0",
    description=(
        "Verify a claimant wallet and policy, prepare an insurer-permitted "
        "ERC-2771 request, and track its sponsored Sepolia transaction."
    ),
    lifespan=lifespan,
)


def _claim_body_limit() -> int:
    """Read the positive request cap used by pre-parser ASGI middleware."""

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
        "Authorization",
        "Idempotency-Key",
        "X-Operations-API-Key",
        "X-Assessor-API-Key",
        "X-Governance-API-Key",
    ],
)


@lru_cache
def get_gasless_submission_service() -> GaslessClaimSubmissionService:
    """Create and cache the transaction-keyless service for this API process.

    Construction performs configuration and adapter validation but receives no
    relayer or assessor key. Uvicorn must be restarted after environment changes
    because this dependency is intentionally cached for consistent requests.
    """

    try:
        return GaslessClaimSubmissionService.from_env()
    except (
        GaslessSubmissionServiceError,
        SubmissionAuthConfigurationError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Gasless claim submission is unavailable: {exc}",
        ) from exc


GaslessSubmissionServiceDependency = Annotated[
    GaslessClaimSubmissionService,
    Depends(get_gasless_submission_service),
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
def load_claimant_session_manager() -> ClaimantSessionManager:
    """Construct the wallet-session module over the durable challenge store."""

    deployment = load_active_deployment()
    repositories = PostgresRepositories.from_env()
    return ClaimantSessionManager.from_env(
        repositories.claimant_auth_challenges,
        chain_id=deployment.chain_id,
    )


def get_claimant_session_manager() -> ClaimantSessionManager:
    """Translate configuration/storage startup failures into safe unavailability."""

    try:
        return load_claimant_session_manager()
    except (
        ClaimantAuthConfigurationError,
        DeploymentConfigurationError,
        PostgresConfigurationError,
        PostgresStorageError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Claimant authentication is unavailable: {exc}",
        ) from exc


ClaimantSessionManagerDependency = Annotated[
    ClaimantSessionManager,
    Depends(get_claimant_session_manager),
]


def get_claimant_session(
    manager: ClaimantSessionManagerDependency,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(claimant_bearer),
    ],
) -> ClaimantSession:
    """Authenticate the wallet-backed bearer session used for claim ownership."""

    token = (
        credentials.credentials
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else None
    )
    try:
        return manager.authenticate(token)
    except ClaimantAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


ClaimantSessionDependency = Annotated[
    ClaimantSession,
    Depends(get_claimant_session),
]


@lru_cache
def load_indexer_operations_boundary() -> IndexerOperationsBoundary:
    """Load the digest-only operator credential once per API process."""

    return IndexerOperationsBoundary.from_env()


def get_indexer_operations_boundary() -> IndexerOperationsBoundary:
    """Translate digest configuration failure into a dependency-level 503.

    Authentication cannot be attempted safely without a valid configured digest.
    Treating that as service unavailability distinguishes operator error from an
    incorrect browser credential, which returns 401 later in the dependency chain.
    """

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
    """Construct and cache the read-only indexer operations service.

    The service owns environment-backed PostgreSQL and RPC adapters. FastAPI
    caches it per process, while each request still performs fresh database and
    chain reads through those adapters.
    """

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
    """Preserve the legacy process-only health endpoint for existing monitors."""

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
    """Report whether every dependency required for traffic is currently usable.

    A not-ready result deliberately returns the same structured body with HTTP
    503, allowing load balancers to stop routing traffic while operators retain
    per-dependency diagnostics.
    """

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


@app.post(
    "/claimant/session/challenge",
    response_model=ClaimantChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["claimant"],
)
def create_claimant_challenge(
    request: Request,
    challenge: ClaimantChallengeRequest,
    manager: ClaimantSessionManagerDependency,
) -> ClaimantChallengeResponse:
    """Issue a short-lived, human-readable wallet ownership challenge."""

    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        issued = manager.issue_challenge(
            challenge.wallet_address,
            client_ip=client_ip,
        )
    except ClaimantAuthenticationRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except ClaimantAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except PostgresStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Claimant authentication is temporarily unavailable",
        ) from exc
    return ClaimantChallengeResponse(
        challenge_id=issued.challenge_id,
        message=issued.message,
        expires_at=issued.expires_at,
    )


@app.post(
    "/claimant/session",
    response_model=ClaimantSessionResponse,
    tags=["claimant"],
)
def create_claimant_session(
    request: ClaimantSessionRequest,
    manager: ClaimantSessionManagerDependency,
) -> ClaimantSessionResponse:
    """Consume one wallet signature and return a short-lived bearer session."""

    try:
        issued = manager.create_session(request.challenge_id, request.signature)
    except ClaimantAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except PostgresStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Claimant authentication is temporarily unavailable",
        ) from exc
    return ClaimantSessionResponse(
        access_token=issued.access_token,
        expires_at=issued.expires_at,
        claimant_address=issued.claimant_address,
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
    """Return a bounded read-only indexer snapshot for a trusted operator.

    The access dependency executes before this function and before a snapshot is
    assembled, so unauthenticated requests cannot trigger PostgreSQL or RPC work.
    Service failures become sanitized 503 responses rather than partial JSON.
    """

    try:
        return service.snapshot()
    except IndexerOperationsServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.get(
    "/operations/indexer/events",
    response_model=ClaimIndexEventPageResponse,
    tags=["operations"],
    responses={
        400: {"description": "Invalid event-search filter or cursor"},
        401: {"description": "Missing or invalid operations API key"},
        503: {"description": "Operations dependencies are unavailable"},
    },
)
def search_indexer_events(
    _access: IndexerOperationsAccessDependency,
    service: IndexerOperationsServiceDependency,
    claim_id: Annotated[int | None, Query(ge=0)] = None,
    transaction_hash: Annotated[
        str | None,
        Query(pattern=r"^0x[0-9a-fA-F]{64}$"),
    ] = None,
    event_type: Annotated[
        Literal["ClaimSubmitted", "ClaimAssessed", "ClaimDecided"] | None,
        Query(),
    ] = None,
    claim_status: Annotated[
        Literal["Submitted", "UnderReview", "Approved", "Rejected", "Flagged"] | None,
        Query(alias="status"),
    ] = None,
    from_block: Annotated[int | None, Query(ge=0)] = None,
    to_block: Annotated[int | None, Query(ge=0)] = None,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ClaimIndexEventPageResponse:
    """Search confirmed immutable events using an opaque keyset cursor.

    FastAPI performs primitive range/pattern validation, then the service applies
    cross-field and cursor validation. Filters describe public blockchain data;
    the operator key remains exclusively in the authentication header.
    """

    try:
        return service.search_events(
            claim_id=claim_id,
            transaction_hash=transaction_hash,
            event_type=event_type,
            status=claim_status,
            from_block=from_block,
            to_block=to_block,
            cursor=cursor,
            limit=limit,
        )
    except IndexerOperationsQueryError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
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
    """Return one validated page from the deployment-scoped PostgreSQL index.

    This route performs no blockchain RPC scan. The response includes the durable
    checkpoint so consumers can distinguish confirmed projection progress from
    the volatile latest chain head shown on the operations surface.
    """

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
    """Return the latest durable assessment and duplicate-review context.

    Both reads are scoped to the active chain and contract. A missing assessment is
    an expected asynchronous state and returns 404 for the browser polling loop;
    storage failure is operational unavailability and returns 503.
    """

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


def _assessor_outcome_response(record) -> AssessorOutcomeResponse:
    """Map the private persistence record without exposing deployment columns.

    A future governed dataset builder can use the same outcome vocabulary, but
    this request path performs no export, label-quality decision, or retraining.
    Keeping those decisions out of the response prevents an assessor submission
    from being mistaken for a model-ready training example.
    """

    return AssessorOutcomeResponse(
        outcome_id=record.outcome_id,
        claim_id=record.claim_id,
        revision=record.revision,
        outcome=record.outcome,
        assessor_reference=record.assessor_reference,
        notes=record.notes,
        assessed_at=record.assessed_at,
    )


@app.get(
    "/assessor/session",
    response_model=AssessorSessionResponse,
    tags=["assessor"],
    responses={401: {"description": "Missing or invalid human-assessor API key"}},
)
def get_assessor_session(
    principal: AssessorPrincipalDependency,
) -> AssessorSessionResponse:
    """Validate a browser-held key without disclosing claim or outcome data."""

    return AssessorSessionResponse(
        assessor_reference=principal.assessor_reference,
    )


@app.get(
    "/assessor/claims/{claim_id}/outcome",
    response_model=AssessorOutcomeResponse,
    tags=["assessor"],
    responses={
        401: {"description": "Missing or invalid human-assessor API key"},
        404: {"description": "Claim or human outcome not found"},
    },
)
def get_assessor_outcome(
    claim_id: Annotated[int, Path(ge=0)],
    _principal: AssessorPrincipalDependency,
    repositories: PostgresRepositoriesDependency,
    deployment: ActiveDeploymentDependency,
) -> AssessorOutcomeResponse:
    """Return the latest private human conclusion for one indexed claim.

    Authentication protects both presence and contents of the conclusion. A 404
    is used for an unreviewed claim so the assessor browser can represent that as
    normal pending work without weakening storage or authentication failures.
    """

    query = {
        "chain_id": deployment.chain_id,
        "contract_address": deployment.address,
        "claim_id": claim_id,
    }
    try:
        claim = repositories.claims.get_claim(**query)
        record = repositories.assessor_outcomes.get_latest_for_claim(**query)
    except PostgresStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Claim is not available in the confirmed index",
        )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No human assessor outcome has been recorded",
        )
    return _assessor_outcome_response(record)


@app.post(
    "/assessor/claims/{claim_id}/outcome",
    response_model=AssessorOutcomeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["assessor"],
    responses={
        401: {"description": "Missing or invalid human-assessor API key"},
        404: {"description": "Claim not found"},
        409: {"description": "Model screening has not completed"},
    },
)
def record_assessor_outcome(
    claim_id: Annotated[int, Path(ge=0)],
    request: AssessorOutcomeRequest,
    principal: AssessorPrincipalDependency,
    repositories: PostgresRepositoriesDependency,
    deployment: ActiveDeploymentDependency,
) -> AssessorOutcomeResponse:
    """Append an attributable, off-chain human fraud-outcome revision.

    A model screening record is required so this route cannot become an unrelated
    adjudication database. The outcome is never mapped to Approved/Rejected and
    this function deliberately performs no contract transaction. Submitting a
    correction creates a new revision rather than mutating prior audit evidence.
    """

    query = {
        "chain_id": deployment.chain_id,
        "contract_address": deployment.address,
        "claim_id": claim_id,
    }
    try:
        claim = repositories.claims.get_claim(**query)
        screening = repositories.assessments.get_latest_for_claim(**query)
        if claim is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Claim is not available in the confirmed index",
            )
        if screening is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Model screening must complete before human review",
            )
        record = repositories.assessor_outcomes.record(
            **query,
            outcome=request.outcome,
            assessor_reference=principal.assessor_reference,
            notes=request.notes,
        )
    except PostgresStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return _assessor_outcome_response(record)


@app.get(
    "/governance/session",
    response_model=GovernanceSessionResponse,
    tags=["governance"],
    responses={401: {"description": "Missing or invalid governance API key"}},
)
def get_governance_session(
    principal: GovernancePrincipalDependency,
) -> GovernanceSessionResponse:
    """Validate a proposal-maker key without contacting a wallet or contract."""

    return GovernanceSessionResponse(
        governance_reference=principal.governance_reference,
        insurer_address=principal.insurer_address,
    )


@app.post(
    "/governance/claims/{claim_id}/decision",
    response_model=CoverageDecisionProposalResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["governance"],
    responses={
        401: {"description": "Missing or invalid governance API key"},
        403: {"description": "Insurer or decision-wallet scope mismatch"},
        409: {"description": "Decision prerequisites are not satisfied"},
    },
)
def prepare_coverage_decision(
    claim_id: Annotated[int, Path(ge=0)],
    request: CoverageDecisionRequest,
    principal: GovernancePrincipalDependency,
    service: CoverageGovernanceServiceDependency,
) -> CoverageDecisionProposalResponse:
    """Persist governance evidence and return calldata for a separate wallet.

    A successful response does not mean the claim was decided. It means the
    proposal is durable and the connected wallet was confirmed to have the
    appropriate insurer scope. The browser still displays and sends the
    transaction; only a confirmed ClaimDecided event changes the indexed state.
    """

    try:
        prepared = service.prepare(
            claim_id=claim_id,
            decision_status=request.decision_status,
            decision_maker_address=request.decision_maker_address,
            principal=principal,
        )
    except CoverageGovernanceAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except (CoverageGovernanceStateError, CoverageDecisionConflictError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (CoverageGovernanceError, PostgresStorageError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Coverage governance is temporarily unavailable",
        ) from exc

    proposal = prepared.proposal
    return CoverageDecisionProposalResponse(
        decision_id=proposal.decision_id,
        claim_id=proposal.claim_id,
        decision_status=proposal.decision_status,
        decision_hash=proposal.decision_hash,
        decision_maker_address=proposal.decision_maker_address,
        proposed_by=proposal.proposed_by,
        human_outcome_id=proposal.human_outcome_id,
        human_outcome_revision=proposal.human_outcome_revision,
        created_at=proposal.created_at,
        confirmed_transaction_hash=proposal.confirmed_transaction_hash,
        confirmed_at=proposal.confirmed_at,
        chain_id=prepared.chain_id,
        contract_address=prepared.contract_address,
        transaction_data=prepared.transaction_data,
    )


@app.post("/claims", status_code=status.HTTP_410_GONE, tags=["claims"])
def legacy_submit_claim() -> None:
    """Refuse the former backend-custodial transaction path."""

    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Direct server-signed submission is disabled. Use "
            "a claimant session with /claims/gasless/prepare and authorize the "
            "returned EIP-712 request."
        ),
    )


@app.get(
    "/claims/gasless/config",
    response_model=GaslessNetworkResponse,
    tags=["claims"],
)
def get_gasless_network(
    service: GaslessSubmissionServiceDependency,
) -> GaslessNetworkResponse:
    """Return server-authoritative wallet preflight configuration.

    This read-only endpoint lets the browser select and pin the expected chain,
    registry, forwarder, and EIP-712 domain before any upload or signature.
    """

    try:
        return service.network()
    except GaslessSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.post(
    "/claims/gasless/prepare",
    response_model=GaslessSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["claims"],
)
def prepare_gasless_claim(
    request: Request,
    claim: ClaimSubmission,
    claimant: ClaimantSessionDependency,
    service: GaslessSubmissionServiceDependency,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
) -> GaslessSubmissionResponse:
    """Create or replay one durable, wallet-signable claim preparation.

    The wallet session identifies the submitter, policy eligibility identifies
    the claimant and insurer, and the Idempotency-Key binds network retries to
    one exact claim payload. A 201 may therefore describe either the newly
    prepared record or its safe replay.
    """

    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        return service.prepare(
            claim,
            claimant,
            idempotency_key=idempotency_key,
            client_ip=client_ip,
        )
    except GaslessSubmissionRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except GaslessSubmissionEligibilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except GaslessSubmissionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except GaslessSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@app.post(
    "/claims/gasless/{submission_id}/authorize",
    response_model=GaslessSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["claims"],
)
def authorize_gasless_claim(
    submission_id: UUID,
    authorization: GaslessAuthorizationRequest,
    claimant: ClaimantSessionDependency,
    service: GaslessSubmissionServiceDependency,
) -> GaslessSubmissionResponse:
    """Verify submitter intent and expose the durable record to the relayer.

    The response is accepted/queued, not a blockchain receipt. Broadcasting and
    confirmation happen asynchronously in the isolated payer process.
    """

    try:
        return service.authorize(
            submission_id,
            authorization.signature,
            claimant,
        )
    except GaslessSubmissionAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except GaslessSubmissionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except GaslessSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.get(
    "/claims/gasless/{submission_id}",
    response_model=GaslessSubmissionResponse,
    tags=["claims"],
)
def get_gasless_claim_status(
    submission_id: UUID,
    claimant: ClaimantSessionDependency,
    service: GaslessSubmissionServiceDependency,
) -> GaslessSubmissionResponse:
    """Return claimant-scoped outbox state until safe confirmation.

    Polling is read-only and safe at browser frequency; it never allocates an
    Ethereum nonce, signs a payer transaction, or performs an RPC write.
    """

    try:
        return service.status(submission_id, claimant)
    except GaslessSubmissionAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except GaslessSubmissionServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
