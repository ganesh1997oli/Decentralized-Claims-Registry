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
from fastapi.security import APIKeyHeader

from apps.backend.app.gasless_service import (
    GaslessClaimSubmissionService,
    GaslessSubmissionAccessError,
    GaslessSubmissionRateLimitError,
    GaslessSubmissionServiceError,
    GaslessSubmissionStateError,
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
    ClaimAssessmentResponse,
    ClaimIndexEventPageResponse,
    ClaimPageResponse,
    ClaimSubmission,
    DuplicateDetectionResponse,
    DuplicateMatchResponse,
    GaslessAuthorizationRequest,
    GaslessNetworkResponse,
    GaslessSubmissionResponse,
    HealthResponse,
    IndexerOperationsResponse,
    ReadinessResponse,
)
from apps.backend.app.service import (
    ClaimQueryService,
    ClaimQueryServiceError,
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
    """Translate unsafe insurer-auth configuration into service unavailability.

    A missing boundary is a server deployment problem, not a bad caller key, so it
    returns 503 before request-specific authentication and quota reservation.
    """

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
    signer_address: Annotated[
        str,
        Header(
            alias="X-Insurer-Signer-Address",
            pattern=r"^0x[0-9a-fA-F]{40}$",
        ),
    ],
) -> InsurerPrincipal:
    """Authenticate the insurer and reserve quota before any external write."""

    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        principal = boundary.authorize_and_reserve(
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
    if principal.signer_address.lower() != signer_address.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The connected wallet does not match the insurer's authorized signer"
            ),
        )
    return principal


InsurerPrincipalDependency = Annotated[
    InsurerPrincipal,
    Depends(get_insurer_principal),
]


def get_authenticated_insurer_principal(
    request: Request,
    boundary: SubmissionBoundaryDependency,
    api_key: Annotated[str | None, Security(insurer_api_key_header)],
) -> InsurerPrincipal:
    """Authenticate idempotent authorize/status operations without new quota."""

    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        return boundary.authenticate(api_key=api_key, client_ip=client_ip)
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


AuthenticatedInsurerPrincipalDependency = Annotated[
    InsurerPrincipal,
    Depends(get_authenticated_insurer_principal),
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Validate critical local configuration before the API accepts traffic.

    Deployment metadata and insurer authentication are deterministic startup
    prerequisites. Remote dependencies remain readiness checks so a temporary
    outage does not force a process restart loop.
    """

    configure_logging("claims-api")
    # Artifact selection is local and fast. Refuse to start with a missing,
    # legacy, or incompatible contract instead of discovering it on first use.
    deployment = load_active_deployment()
    deployment.require_gasless()
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
        "Validate a claim, prepare an insurer-signed ERC-2771 request, and track "
        "its sponsored Sepolia transaction without holding a submitter key."
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
        "Idempotency-Key",
        "X-Insurer-API-Key",
        "X-Insurer-Signer-Address",
        "X-Operations-API-Key",
    ],
)


@lru_cache
def get_gasless_submission_service() -> GaslessClaimSubmissionService:
    """Create and cache the keyless preparation service for this API process.

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
        Literal["ClaimSubmitted", "ClaimAssessed"] | None,
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


@app.post("/claims", status_code=status.HTTP_410_GONE, tags=["claims"])
def legacy_submit_claim() -> None:
    """Refuse the former backend-custodial transaction path."""

    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Direct server-signed submission is disabled. Use "
            "/claims/gasless/prepare and authorize the returned EIP-712 request."
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
    principal: InsurerPrincipalDependency,
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

    Authentication binds the header credential and browser wallet; the
    Idempotency-Key binds network retries to one exact claim payload. A 201 may
    therefore describe either the newly prepared record or its safe replay.
    """

    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        return service.prepare(
            claim,
            principal,
            idempotency_key=idempotency_key,
            client_ip=client_ip,
        )
    except GaslessSubmissionRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
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
    principal: AuthenticatedInsurerPrincipalDependency,
    service: GaslessSubmissionServiceDependency,
) -> GaslessSubmissionResponse:
    """Verify insurer intent and expose the durable record to the relayer.

    The response is accepted/queued, not a blockchain receipt. Broadcasting and
    confirmation happen asynchronously in the isolated payer process.
    """

    try:
        return service.authorize(
            submission_id,
            authorization.signature,
            principal,
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
    principal: AuthenticatedInsurerPrincipalDependency,
    service: GaslessSubmissionServiceDependency,
) -> GaslessSubmissionResponse:
    """Return credential-scoped outbox state until safe confirmation.

    Polling is read-only and safe at browser frequency; it never allocates an
    Ethereum nonce, signs a payer transaction, or performs an RPC write.
    """

    try:
        return service.status(submission_id, principal)
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
