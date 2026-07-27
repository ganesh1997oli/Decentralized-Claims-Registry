from fastapi.testclient import TestClient

from backend.app.main import (
    app,
    get_assessment_repository,
    get_claim_submission_service,
)
from backend.app.models import (
    ClaimAssessmentResponse,
    ClaimListItemResponse,
    ClaimPageResponse,
    ClaimSubmissionResponse,
)
from backend.app.service import ClaimSubmissionServiceError
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
    def submit(self, claim):
        assert claim.claim_reference == "synthetic-claim-api-1"
        assert claim.insurer_id == "northstar-mutual"
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
    def submit(self, claim):
        raise ClaimSubmissionServiceError("upstream unavailable")


class PendingService:
    def submit(self, claim):
        assert claim.claim_reference == "synthetic-claim-api-1"
        return ClaimSubmissionResponse(
            claim_id=7,
            transaction_hash="0xtransaction",
            block_number=123,
            data_pointer="ipfs://bafy-test",
            claim_hash="0xhash",
            assessment=None,
        )


class UnexpectedService:
    def submit(self, claim):
        raise AssertionError("Invalid input must not reach the submission service")


class SuccessfulAssessmentRepository:
    def get_latest_for_claim(self, claim_id):
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

    def get_duplicate_check_for_claim(self, claim_id):
        assert claim_id == 7
        return DuplicateCheck(
            insurer_id="harbour-shield",
            fingerprint_version="incident-hmac-sha256-v1",
            matches=(DuplicateMatch(3, "northstar-mutual"),),
        )


class PendingAssessmentRepository:
    def get_latest_for_claim(self, claim_id):
        assert claim_id == 7
        return None

    def get_duplicate_check_for_claim(self, claim_id):
        assert claim_id == 7
        return None


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
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "http://127.0.0.1:5173"
    )
    assert "POST" in response.headers["access-control-allow-methods"]


def test_submit_claim_returns_created_receipt():
    app.dependency_overrides[get_claim_submission_service] = SuccessfulService
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
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["assessment"] is None


def test_list_claims_returns_current_on_chain_state():
    app.dependency_overrides[get_claim_submission_service] = SuccessfulService
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
    app.dependency_overrides[
        get_assessment_repository
    ] = SuccessfulAssessmentRepository
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
    app.dependency_overrides[get_assessment_repository] = PendingAssessmentRepository
    try:
        response = TestClient(app).get("/claims/7/assessment")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Assessment is still pending"}


def test_list_claims_validates_pagination_parameters():
    app.dependency_overrides[get_claim_submission_service] = SuccessfulService
    try:
        response = TestClient(app).get("/claims?page=0&page_size=100")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_submit_claim_rejects_invalid_amount_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "claimAmountUsd": -1}

    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    try:
        response = TestClient(app).post("/claims", json=invalid_claim)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_submit_claim_rejects_an_invalid_insurer_id_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "insurerId": "Northstar Mutual"}

    app.dependency_overrides[get_claim_submission_service] = UnexpectedService
    try:
        response = TestClient(app).post("/claims", json=invalid_claim)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_submit_claim_reports_upstream_failure():
    app.dependency_overrides[get_claim_submission_service] = FailingService
    try:
        response = TestClient(app).post("/claims", json=VALID_CLAIM)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "upstream unavailable"}
