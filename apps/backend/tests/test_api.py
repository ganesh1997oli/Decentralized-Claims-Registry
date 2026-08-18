from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from apps.backend.app.claimant_auth import ClaimantAuthenticationError, ClaimantSession
from apps.backend.app.gasless_service import (
    GaslessClaimSubmissionService,
    GaslessSubmissionEligibilityError,
    GaslessSubmissionRateLimitError,
    GaslessSubmissionServiceError,
)
from apps.backend.app.main import (
    app,
    get_active_deployment,
    get_claim_query_service,
    get_claimant_session,
    get_claimant_session_manager,
    get_gasless_submission_service,
    get_postgres_repositories,
)
from apps.backend.app.models import (
    ClaimListItemResponse,
    ClaimPageResponse,
    ClaimSubmissionResponse,
    GaslessSubmissionResponse,
)
from apps.backend.app.service import (
    ClaimQueryService,
    ClaimQueryServiceError,
)
from packages.duplicates import DuplicateCheck, DuplicateMatch
from packages.integrations.postgres import AssessmentRecord
from packages.model.contracts import FraudReason

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


SUBMISSION_ID = UUID("11111111-1111-4111-8111-111111111111")
SIGNER = "0x1111111111111111111111111111111111111111"
FORWARDER = "0x2222222222222222222222222222222222222222"
CONTRACT = "0x3333333333333333333333333333333333333333"


def gasless_response(state="prepared"):
    typed_data = None
    receipt = None
    if state == "prepared":
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                ],
                "ForwardRequest": [
                    {"name": "from", "type": "address"},
                ],
            },
            "primaryType": "ForwardRequest",
            "domain": {
                "name": "ClaimsRegistryForwarder",
                "version": "1",
                "chainId": 11_155_111,
                "verifyingContract": FORWARDER,
            },
            "message": {
                "from": SIGNER,
                "to": CONTRACT,
                "value": "0",
                "gas": "250000",
                "nonce": "0",
                "deadline": "2000000000",
                "data": "0x1234",
            },
        }
    if state == "confirmed":
        receipt = ClaimSubmissionResponse(
            claim_id=7,
            transaction_hash="0xtransaction",
            block_number=123,
            data_pointer="ipfs://bafytest",
            claim_hash="0xhash",
            assessment=None,
        )
    return GaslessSubmissionResponse(
        submission_id=SUBMISSION_ID,
        state=state,
        signer_address=SIGNER,
        chain_id=11_155_111,
        contract_address=CONTRACT,
        forwarder_address=FORWARDER,
        claim_hash="0x" + ("12" * 32),
        data_pointer="ipfs://bafytest",
        deadline=2_000_000_000,
        typed_data=typed_data,
        receipt=receipt,
    )


class SuccessfulGaslessService:
    def network(self):
        return {
            "chain_id": 11_155_111,
            "contract_address": CONTRACT,
            "forwarder_address": FORWARDER,
            "domain_name": "ClaimsRegistryForwarder",
            "domain_version": "1",
        }

    def prepare(self, claim, principal, *, idempotency_key, client_ip):
        assert claim.claim_reference == "synthetic-claim-api-1"
        assert claim.insurer_id == "northstar-mutual"
        assert principal.claimant_address == SIGNER
        assert idempotency_key == "claim-request-1"
        assert client_ip == "testclient"
        return gasless_response()

    def authorize(self, submission_id, signature, principal):
        assert submission_id == SUBMISSION_ID
        assert signature == "0x" + ("ab" * 65)
        assert principal.credential_id == "claimant-" + ("a" * 64)
        return gasless_response("authorized")

    def status(self, submission_id, principal):
        assert submission_id == SUBMISSION_ID
        assert principal.credential_id == "claimant-" + ("a" * 64)
        return gasless_response("confirmed")

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
            indexed_through_block=11_400_000,
        )


class FailingGaslessService:
    def prepare(self, claim, principal, **_values):
        raise GaslessSubmissionServiceError("upstream unavailable")


class FailingQueryService:
    def list_claims(self, *, page, page_size):
        raise ClaimQueryServiceError("PostgreSQL claim storage is unavailable")


class UnexpectedService:
    def prepare(self, claim, principal, **_values):
        raise AssertionError("Invalid input must not reach the submission service")


class IneligibleGaslessService:
    def prepare(self, claim, principal, **_values):
        raise GaslessSubmissionEligibilityError(
            "The selected insurer does not match the verified policy"
        )


class RateLimitedGaslessService:
    def prepare(self, claim, principal, **_values):
        raise GaslessSubmissionRateLimitError(
            "Daily quota reached",
            retry_after=3600,
        )


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


def authenticated_claimant():
    return ClaimantSession(
        subject_id="claimant-" + ("a" * 64),
        claimant_address=SIGNER,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def allow_authenticated_submission():
    app.dependency_overrides[get_claimant_session] = authenticated_claimant


class ClaimantSessionManagerProbe:
    def __init__(self, result=None, error=None):
        self.result = result or authenticated_claimant()
        self.error = error
        self.calls = []

    def authenticate(self, access_token):
        self.calls.append(access_token)
        if self.error is not None:
            raise self.error
        return self.result


def test_health_does_not_require_external_services():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_preflight_allows_the_local_react_app():
    response = TestClient(app).options(
        "/claims/gasless/prepare",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("http://127.0.0.1:5173")
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]
    assert "Idempotency-Key" in response.headers["access-control-allow-headers"]


def test_legacy_custodial_submission_is_disabled():
    response = TestClient(app).post("/claims", json=VALID_CLAIM)

    assert response.status_code == 410
    assert "EIP-712" in response.json()["detail"]


def test_gasless_network_preflight_is_public_and_server_authoritative():
    app.dependency_overrides[get_gasless_submission_service] = (
        SuccessfulGaslessService
    )
    try:
        response = TestClient(app).get("/claims/gasless/config")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "chain_id": 11_155_111,
        "contract_address": CONTRACT,
        "forwarder_address": FORWARDER,
        "domain_name": "ClaimsRegistryForwarder",
        "domain_version": "1",
    }


def test_prepare_gasless_claim_returns_wallet_typed_data():
    app.dependency_overrides[get_gasless_submission_service] = (
        SuccessfulGaslessService
    )
    allow_authenticated_submission()
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["submission_id"] == str(SUBMISSION_ID)
    assert body["state"] == "prepared"
    assert body["signer_address"] == SIGNER
    assert body["typed_data"]["primaryType"] == "ForwardRequest"
    assert body["typed_data"]["domain"]["verifyingContract"] == FORWARDER


def test_prepare_claim_authenticates_claimant_bearer_session():
    manager = ClaimantSessionManagerProbe()
    app.dependency_overrides[get_gasless_submission_service] = (
        SuccessfulGaslessService
    )
    app.dependency_overrides[get_claimant_session_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={
                "Authorization": "Bearer claimant-session-token",
                "Idempotency-Key": "claim-request-1",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert manager.calls == ["claimant-session-token"]


def test_prepare_claim_requires_a_claimant_bearer_session():
    manager = ClaimantSessionManagerProbe(
        error=ClaimantAuthenticationError("A claimant bearer session is required")
    )
    app.dependency_overrides[get_gasless_submission_service] = UnexpectedService
    app.dependency_overrides[get_claimant_session_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_authentication_runs_before_gasless_service_initialization():
    manager = ClaimantSessionManagerProbe(
        error=ClaimantAuthenticationError("A claimant bearer session is required")
    )

    def unexpected_service_initialization():
        raise AssertionError("Authentication must run before external clients load")

    app.dependency_overrides[get_gasless_submission_service] = (
        unexpected_service_initialization
    )
    app.dependency_overrides[get_claimant_session_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_prepare_claim_rejects_policy_eligibility_mismatch():
    app.dependency_overrides[get_gasless_submission_service] = IneligibleGaslessService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert "does not match" in response.json()["detail"]


def test_prepare_claim_returns_retry_after_for_rate_limit():
    app.dependency_overrides[get_gasless_submission_service] = RateLimitedGaslessService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3600"


def test_authorize_and_poll_gasless_claim():
    app.dependency_overrides[get_gasless_submission_service] = (
        SuccessfulGaslessService
    )
    app.dependency_overrides[get_claimant_session] = authenticated_claimant
    try:
        authorized = TestClient(app).post(
            f"/claims/gasless/{SUBMISSION_ID}/authorize",
            json={"signature": "0x" + ("ab" * 65)},
        )
        confirmed = TestClient(app).get(
            f"/claims/gasless/{SUBMISSION_ID}"
        )
    finally:
        app.dependency_overrides.clear()

    assert authorized.status_code == 202
    assert authorized.json()["state"] == "authorized"
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "confirmed"
    assert confirmed.json()["receipt"]["claim_id"] == 7


def test_prepare_claim_rejects_oversized_body_before_dependencies():
    oversized = {**VALID_CLAIM, "description": "x" * 17_000}

    response = TestClient(app).post(
        "/claims/gasless/prepare",
        json=oversized,
        headers={"Idempotency-Key": "claim-request-1"},
    )

    assert response.status_code == 413
    assert "exceeds 16384 bytes" in response.json()["detail"]


def test_list_claims_returns_current_on_chain_state():
    app.dependency_overrides[get_claim_query_service] = SuccessfulGaslessService
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
        "indexed_through_block": 11_400_000,
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
    app.dependency_overrides[get_claim_query_service] = SuccessfulGaslessService
    try:
        response = TestClient(app).get("/claims?page=0&page_size=100")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_list_claims_reports_a_transient_index_failure_as_unavailable():
    app.dependency_overrides[get_claim_query_service] = FailingQueryService
    try:
        response = TestClient(app).get("/claims")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": "PostgreSQL claim storage is unavailable"
    }


def test_prepare_claim_rejects_invalid_amount_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "claimAmountUsd": -1}

    app.dependency_overrides[get_gasless_submission_service] = UnexpectedService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=invalid_claim,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_prepare_claim_rejects_an_invalid_insurer_id_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "insurerId": "Northstar Mutual"}

    app.dependency_overrides[get_gasless_submission_service] = UnexpectedService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=invalid_claim,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_prepare_claim_reports_upstream_failure():
    app.dependency_overrides[get_gasless_submission_service] = FailingGaslessService
    allow_authenticated_submission()
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "upstream unavailable"}


def test_list_claims_reports_missing_read_configuration_as_json_503(monkeypatch):
    """A missing index database should be understandable to the browser."""

    def unavailable(_service_class):
        raise ClaimQueryServiceError("DATABASE_URL is not configured")

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
        "detail": ("Claims registry is unavailable: DATABASE_URL is not configured")
    }


def test_prepare_claim_reports_missing_write_configuration_as_json_503(monkeypatch):
    """Missing public-intake settings must return structured JSON."""

    def unavailable(_service_class):
        raise GaslessSubmissionServiceError("gasless deployment is not configured")

    get_gasless_submission_service.cache_clear()
    allow_authenticated_submission()
    monkeypatch.setattr(
        GaslessClaimSubmissionService,
        "from_env",
        classmethod(unavailable),
    )
    try:
        response = TestClient(app).post(
            "/claims/gasless/prepare",
            json=VALID_CLAIM,
            headers={"Idempotency-Key": "claim-request-1"},
        )
    finally:
        get_gasless_submission_service.cache_clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": (
            "Gasless claim submission is unavailable: "
            "gasless deployment is not configured"
        )
    }
