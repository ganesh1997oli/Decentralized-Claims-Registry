from fastapi.testclient import TestClient

from backend.app.main import app, get_claim_submission_service
from backend.app.models import ClaimSubmissionResponse
from backend.app.service import ClaimSubmissionServiceError


VALID_CLAIM = {
    "claimReference": "synthetic-claim-api-1",
    "policyReference": "synthetic-policy-42",
    "claimType": "vehicle_damage",
    "incidentDate": "2026-07-13",
    "amountPence": 250000,
    "description": "Synthetic bumper damage for API testing",
    "evidence": [],
}


class SuccessfulService:
    def submit(self, claim):
        assert claim.claim_reference == "synthetic-claim-api-1"
        return ClaimSubmissionResponse(
            claim_id=7,
            transaction_hash="0xtransaction",
            block_number=123,
            data_pointer="ipfs://bafy-test",
            claim_hash="0xhash",
        )


class FailingService:
    def submit(self, claim):
        raise ClaimSubmissionServiceError("upstream unavailable")


class UnexpectedService:
    def submit(self, claim):
        raise AssertionError("Invalid input must not reach the submission service")


def test_health_does_not_require_external_services():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    }


def test_submit_claim_rejects_invalid_amount_before_external_calls():
    invalid_claim = {**VALID_CLAIM, "amountPence": -1}

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
