from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app.main import (
    app,
    get_active_deployment,
    get_claim_query_service,
    get_claim_submission_service,
    get_insurer_principal,
    get_postgres_repositories,
    get_submission_boundary,
)
from backend.app.models import (
    ClaimAssessmentResponse,
    ClaimListItemResponse,
    ClaimPageResponse,
    ClaimSubmissionResponse,
)
from backend.app.service import (
    ClaimQueryService,
    ClaimQueryServiceError,
    ClaimSubmissionService,
    ClaimSubmissionServiceError,
)
from backend.app.submission_auth import (
    InsurerPrincipal,
    SubmissionAuthenticationError,
    SubmissionAuthorizationError,
    SubmissionRateLimitError,
)
from duplicates import DuplicateCheck, DuplicateMatch
from integrations.postgres import AssessmentRecord
from model.contracts import FraudReason

VALID_CLAIM = {
    "insurerId": "northstar-mutual",
    "claimReference": "synthetic-claim-api-1",
    "policyReference": "synthetic-policy-42",
    "claimType": "collision",
    "incidentDate": "2026-07-13",
    "claimAmountUsd": 2500,
    "policyPremiumUsd": 480,
    "vehicleAge": 6,
    "vehicleType": "sedan",
    "country": "Nigeria",
    "regionType": "urban",
    "thirdPartyInjuryFlag": False,
    "totalLossFlag": False,
    "description": "Synthetic bumper damage for API testing",
    "evidence": [],
}


class SuccessfulService:
    def submit(self, claim, principal):
        assert claim.claim_reference == "synthetic-claim-api-1"
        assert claim.insurer_id == "northstar-mutual"
        assert principal.insurer_id == "northstar-mutual"
        return ClaimSubmissionResponse(
            claim_id=7,
            transaction_hash="0xtransaction",
            block_number=123,
            data_pointer="ipfs://bafy-test",
            claim_hash="0xhash",
            assessment=ClaimAssessmentResponse(
                status="Flagged",
                fraud_score=8500,
                probability=0.85,
                threshold=0.3,
                model_version="test-model-v1",
                reasons=[],
                on_chain=True,
                transaction_hash="0xassessment",
                block_number=124,
            ),
        )

    def list_claims(self, *, page, page_size):
        assert page == 2
        assert page_size == 5
        return ClaimPageResponse(
            items=[
                ClaimListItemResponse(
                    claim_id=7,
                    claimant="0x0000000000000000000000000000000000000001",
                    claim_hash="0xhash",
                    data_pointer="ipfs://bafy-test",
                    status="Flagged",
                    fraud_score=8500,
                    submitted_at=1_750_000_000,
                    updated_at=1_750_000_010,
                )
            ],
            page=2,
            page_size=5,
            total_items=7,
            total_pages=2,
        )


class FailingService:
    def submit(self, claim, principal):
        raise ClaimSubmissionServiceError("upstream unavailable")


class PendingService:
    def submit(self, claim, principal):
        assert claim.claim_reference == "synthetic-claim-api-1"
        assert principal.insurer_id == "northstar-mutual"
        return ClaimSubmissionResponse(
            claim_id=7,
            transaction_hash="0xtransaction",
            block_number=123,
            data_pointer="ipfs://bafy-test",
            claim_hash="0xhash",
            assessment=None,
        )


class UnexpectedService:
    def submit(self, claim, principal):
        raise AssertionError("Invalid input must not reach the submission service")


class SuccessfulAssessmentRepository:
    def get_latest_for_claim(self, *, chain_id, contract_address, claim_id):
        assert chain_id == 11_155_111
        assert contract_address == "0xcontract"
        assert claim_id == 7
        return AssessmentRecord(
            event_id="11155111:0xtransaction:0",
            chain_id=11_155_111,
            contract_address="0xcontract",
            claim_id=7,
            model_version="african-motor-xgboost-v1",
            probability=0.68,
            threshold=0.47,
            fraud_score=6800,
            status="Flagged",
            reasons=(FraudReason("claim_amount_usd", "Claim amount", 0.42),),
            processing_status="completed",
            transaction_hash="0xassessment",
            block_number=124,
        )

    def get_duplicate_check_for_claim(self, *, chain_id, contract_address, claim_id):
        assert chain_id == 11_155_111
        assert contract_address == "0xcontract"
        assert claim_id == 7
        return DuplicateCheck(
            insurer_id="harbour-shield",
            fingerprint_version="incident-hmac-sha256-v1",
            matches=(DuplicateMatch(3, "northstar-mutual"),),
        )


class PendingAssessmentRepository:
    def get_latest_for_claim(self, *, chain_id, contract_address, claim_id):
        assert chain_id == 11_155_111
        assert contract_address == "0xcontract"
        assert claim_id == 7

    def get_duplicate_check_for_claim(self, *, chain_id, contract_address, claim_id):
        assert chain_id == 11_155_111
        assert contract_address == "0xcontract"
        assert claim_id == 7


def active_deployment():
    return SimpleNamespace(chain_id=11_155_111, address="0xcontract")


def authenticated_principal():
    return InsurerPrincipal(
        insurer_id="northstar-mutual",
        credential_id="northstar-test-v1",
        permitted_operations=frozenset({"submit_claim"}),
        daily_quota=25,
    )


def allow_authenticated_submission():
    app.dependency_overrides[get_insurer_principal] = authenticated_principal


class BoundaryProbe:
    def __init__(self, result=None, error=None):
        self.result = result or authenticated_principal()
        self.error = error
        self.calls = []

    def authorize_and_reserve(self, *, api_key, claimed_insurer_id, client_ip):
        self.calls.append((api_key, claimed_insurer_id, client_ip))
        if self.error is not None:
            raise self.error
        return self.result


def test_health_does_not_require_external_services():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_preflight_allows_the_local_react_app():
    response = TestClient(app).options(
        "/claims",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": ("content-type,x-insurer-api-key"),
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("http://127.0.0.1:5173")
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "X-Insurer-API-Key" in response.headers["access-control-allow-headers"]


def test_submit_claim_returns_created_receipt():
    app.dependency_overrides[get_claim_submission_service] = SuccessfulService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json() == {
        "claim_id": 7,
        "transaction_hash": "0xtransaction",
        "block_number": 123,
        "data_pointer": "ipfs://bafy-test",
        "claim_hash": "0xhash",
        "assessment": {
            "status": "Flagged",
            "fraud_score": 8500,
            "probability": 0.85,
            "threshold": 0.3,
            "model_version": "test-model-v1",
            "reasons": [],
            "on_chain": True,
            "transaction_hash": "0xassessment",
            "block_number": 124,
            "error": None,
            "duplicate_detection": None,
        },
    }


def test_submit_claim_returns_anchor_while_async_assessment_is_pending():
    app.dependency_overrides[get_claim_submission_service] = PendingService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["assessment"] is None


def test_submit_claim_authenticates_api_key_and_authoritative_insurer():
    boundary = BoundaryProbe()
    app.dependency_overrides[get_claim_submission_service] = SuccessfulService
    app.dependency_overrides[get_submission_boundary] = lambda: boundary
    try:
        response = TestClient(app).post(
            "/claims",
            json=VALID_CLAIM,
            headers={"X-Insurer-API-Key": "test-api-key"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert boundary.calls == [("test-api-key", "northstar-mutual", "testclient")]


def test_submit_claim_requires_an_insurer_api_key():
    boundary = BoundaryProbe(
        error=SubmissionAuthenticationError("Invalid insurer API credential")
    )
    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    app.dependency_overrides[get_submission_boundary] = lambda: boundary
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"


def test_authentication_runs_before_submission_service_initialization():
    boundary = BoundaryProbe(
        error=SubmissionAuthenticationError("Invalid insurer API credential")
    )

    def unexpected_service_initialization():
        raise AssertionError("Authentication must run before external clients load")

    app.dependency_overrides[get_claim_submission_service] = (
        unexpected_service_initialization
    )
    app.dependency_overrides[get_submission_boundary] = lambda: boundary
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_submit_claim_rejects_insurer_identity_mismatch():
    boundary = BoundaryProbe(
        error=SubmissionAuthorizationError(
            "The selected insurer does not match the authenticated credential"
        )
    )
    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    app.dependency_overrides[get_submission_boundary] = lambda: boundary
    try:
        response = TestClient(app).post(
            "/claims",
            json=VALID_CLAIM,
            headers={"X-Insurer-API-Key": "test-api-key"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert "does not match" in response.json()["detail"]


def test_submit_claim_returns_retry_after_for_rate_limit():
    boundary = BoundaryProbe(
        error=SubmissionRateLimitError("Daily quota reached", retry_after=3600)
    )
    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    app.dependency_overrides[get_submission_boundary] = lambda: boundary
    try:
        response = TestClient(app).post(
            "/claims",
            json=VALID_CLAIM,
            headers={"X-Insurer-API-Key": "test-api-key"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3600"


def test_submit_claim_rejects_oversized_body_before_dependencies():
    oversized = {**VALID_CLAIM, "description": "x" * 17_000}

    response = TestClient(app).post("/claims", json=oversized)

    assert response.status_code == 413
    assert "exceeds 16384 bytes" in response.json()["detail"]


def test_list_claims_returns_current_on_chain_state():
    app.dependency_overrides[get_claim_query_service] = SuccessfulService
    try:
        response = TestClient(app).get("/claims?page=2&page_size=5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "claim_id": 7,
                "claimant": "0x0000000000000000000000000000000000000001",
                "claim_hash": "0xhash",
                "data_pointer": "ipfs://bafy-test",
                "status": "Flagged",
                "fraud_score": 8500,
                "submitted_at": 1_750_000_000,
                "updated_at": 1_750_000_010,
            }
        ],
        "page": 2,
        "page_size": 5,
        "total_items": 7,
        "total_pages": 2,
    }


def test_get_claim_assessment_returns_postgres_result():
    repository = SuccessfulAssessmentRepository()
    app.dependency_overrides[get_postgres_repositories] = lambda: SimpleNamespace(
        assessments=repository,
        duplicates=repository,
    )
    app.dependency_overrides[get_active_deployment] = active_deployment
    try:
        response = TestClient(app).get("/claims/7/assessment")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "Flagged",
        "fraud_score": 6800,
        "probability": 0.68,
        "threshold": 0.47,
        "model_version": "african-motor-xgboost-v1",
        "reasons": [
            {
                "feature": "claim_amount_usd",
                "label": "Claim amount",
                "contribution": 0.42,
            }
        ],
        "on_chain": True,
        "transaction_hash": "0xassessment",
        "block_number": 124,
        "error": None,
        "duplicate_detection": {
            "insurer_id": "harbour-shield",
            "fingerprint_version": "incident-hmac-sha256-v1",
            "duplicate_detected": True,
            "matches": [
                {
                    "claim_id": 3,
                    "insurer_id": "northstar-mutual",
                }
            ],
        },
    }


def test_get_claim_assessment_returns_not_found_while_pending():
    repository = PendingAssessmentRepository()
    app.dependency_overrides[get_postgres_repositories] = lambda: SimpleNamespace(
        assessments=repository,
        duplicates=repository,
    )
    app.dependency_overrides[get_active_deployment] = active_deployment
    try:
        response = TestClient(app).get("/claims/7/assessment")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Assessment is still pending"}


def test_list_claims_validates_pagination_parameters():
    app.dependency_overrides[get_claim_query_service] = SuccessfulService
    try:
        response = TestClient(app).get("/claims?page=0&page_size=100")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_submit_claim_rejects_invalid_amount_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "claimAmountUsd": -1}

    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post("/claims", json=invalid_claim)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_submit_claim_rejects_an_invalid_insurer_id_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "insurerId": "Northstar Mutual"}

    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post("/claims", json=invalid_claim)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_submit_claim_reports_upstream_failure():
    app.dependency_overrides[get_claim_submission_service] = FailingService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "upstream unavailable"}


def test_list_claims_reports_missing_read_configuration_as_json_503(monkeypatch):
    """A missing public RPC should be understandable to the browser and user."""

    def unavailable(_service_class):
        raise ClaimQueryServiceError("SEPOLIA_RPC_URL is not configured")

    # This test exercises the real FastAPI dependency instead of replacing it.
    # Clearing the small process cache ensures the patched factory is called.
    get_claim_query_service.cache_clear()
    monkeypatch.setattr(
        ClaimQueryService,
        "from_env",
        classmethod(unavailable),
    )
    try:
        response = TestClient(app).get("/claims")
    finally:
        get_claim_query_service.cache_clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": ("Claims registry is unavailable: SEPOLIA_RPC_URL is not configured")
    }


def test_submit_claim_reports_missing_write_configuration_as_json_503(monkeypatch):
    """Missing wallet or Pinata settings must not escape as a plain HTTP 500."""

    def unavailable(_service_class):
        raise ClaimSubmissionServiceError(
            "SEPOLIA_SUBMITTER_PRIVATE_KEY is not configured"
        )

    get_claim_submission_service.cache_clear()
    allow_authenticated_submission()
    monkeypatch.setattr(
        ClaimSubmissionService,
        "from_env",
        classmethod(unavailable),
    )
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        get_claim_submission_service.cache_clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": (
            "Claim submission is unavailable: "
            "SEPOLIA_SUBMITTER_PRIVATE_KEY is not configured"
        )
    }
