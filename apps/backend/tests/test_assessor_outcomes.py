import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from apps.backend.app.assessor_outcomes import (
    AssessorOutcomeAuthenticationError,
    AssessorOutcomeBoundary,
    AssessorOutcomeConfigurationError,
    AssessorPrincipal,
)
from apps.backend.app.main import (
    app,
    get_active_deployment,
    get_assessor_outcome_boundary,
    get_assessor_principal,
    get_assessor_read_principal,
    get_postgres_repositories,
    get_public_demo_access,
)
from apps.backend.app.public_demo_access import PublicDemoAccess
from packages.integrations.postgres import AssessorOutcomeRecord

ASSESSOR_KEY = "test-human-assessor-key-with-enough-entropy"


def boundary_settings():
    return {
        "ASSESSOR_OUTCOME_CREDENTIALS_JSON": json.dumps(
            [
                {
                    "assessorReference": "research-assessor-1",
                    "apiKeySha256": hashlib.sha256(
                        ASSESSOR_KEY.encode("utf-8")
                    ).hexdigest(),
                }
            ]
        )
    }


def test_human_assessor_authentication_is_independent_and_digest_only():
    boundary = AssessorOutcomeBoundary.from_settings(boundary_settings())

    assert boundary.authenticate(ASSESSOR_KEY) == AssessorPrincipal(
        "research-assessor-1"
    )
    with pytest.raises(AssessorOutcomeAuthenticationError, match="Invalid"):
        boundary.authenticate("wrong")
    with pytest.raises(AssessorOutcomeConfigurationError, match="required"):
        AssessorOutcomeBoundary.from_settings({})


def outcome_record(revision=1):
    return AssessorOutcomeRecord(
        outcome_id=UUID("11111111-1111-4111-8111-111111111111"),
        chain_id=11_155_111,
        contract_address="0xcontract",
        claim_id=7,
        revision=revision,
        outcome="ConfirmedFraud",
        assessor_reference="research-assessor-1",
        notes="Reviewed synthetic evidence.",
        assessed_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )


class ClaimRepository:
    def get_claim(self, **query):
        assert query == {
            "chain_id": 11_155_111,
            "contract_address": "0xcontract",
            "claim_id": 7,
        }
        return SimpleNamespace(claim_id=7)


class ScreeningRepository:
    def get_latest_for_claim(self, **_query):
        return SimpleNamespace(status="Flagged")


class OutcomeRepository:
    def __init__(self, existing=None):
        self.existing = existing
        self.recorded = []

    def get_latest_for_claim(self, **_query):
        return self.existing

    def record(self, **values):
        self.recorded.append(values)
        return outcome_record(revision=2 if self.existing else 1)


def repositories(outcomes):
    return SimpleNamespace(
        claims=ClaimRepository(),
        assessments=ScreeningRepository(),
        assessor_outcomes=outcomes,
    )


def install_overrides(outcomes):
    principal = lambda: AssessorPrincipal("research-assessor-1")
    app.dependency_overrides[get_assessor_principal] = principal
    app.dependency_overrides[get_assessor_read_principal] = principal
    app.dependency_overrides[get_active_deployment] = lambda: SimpleNamespace(
        chain_id=11_155_111,
        address="0xcontract",
    )
    app.dependency_overrides[get_postgres_repositories] = lambda: repositories(
        outcomes
    )


def test_record_endpoint_appends_private_human_conclusion():
    outcomes = OutcomeRepository()
    install_overrides(outcomes)
    try:
        response = TestClient(app).post(
            "/assessor/claims/7/outcome",
            json={
                "outcome": "ConfirmedFraud",
                "notes": "Reviewed synthetic evidence.",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json() == {
        "outcome_id": "11111111-1111-4111-8111-111111111111",
        "claim_id": 7,
        "revision": 1,
        "outcome": "ConfirmedFraud",
        "assessor_reference": "research-assessor-1",
        "notes": "Reviewed synthetic evidence.",
        "assessed_at": "2026-08-12T12:00:00Z",
    }
    assert outcomes.recorded[0]["assessor_reference"] == "research-assessor-1"
    assert "status" not in outcomes.recorded[0]
    assert "fraud_score" not in outcomes.recorded[0]


def test_get_endpoint_returns_latest_revision_and_inconclusive_is_not_a_label():
    inconclusive = AssessorOutcomeRecord(
        **{
            **outcome_record(revision=3).__dict__,
            "outcome": "Inconclusive",
        }
    )
    install_overrides(OutcomeRepository(inconclusive))
    try:
        response = TestClient(app).get("/assessor/claims/7/outcome")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["revision"] == 3
    assert response.json()["outcome"] == "Inconclusive"
    assert "training_label_eligible" not in response.json()


def test_request_rejects_business_disposition_as_fraud_outcome():
    install_overrides(OutcomeRepository())
    try:
        response = TestClient(app).post(
            "/assessor/claims/7/outcome",
            json={"outcome": "Rejected", "notes": None},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_public_demo_can_read_assessor_data_but_cannot_record_an_outcome():
    outcomes = OutcomeRepository(outcome_record())
    app.dependency_overrides[get_public_demo_access] = lambda: PublicDemoAccess(True)
    app.dependency_overrides[get_assessor_outcome_boundary] = lambda: (
        AssessorOutcomeBoundary.from_settings(boundary_settings())
    )
    app.dependency_overrides[get_active_deployment] = lambda: SimpleNamespace(
        chain_id=11_155_111,
        address="0xcontract",
    )
    app.dependency_overrides[get_postgres_repositories] = lambda: repositories(
        outcomes
    )
    client = TestClient(app)
    try:
        session = client.get("/assessor/session")
        latest = client.get("/assessor/claims/7/outcome")
        write = client.post(
            "/assessor/claims/7/outcome",
            json={"outcome": "Legitimate", "notes": "Public demo must not write"},
        )
    finally:
        app.dependency_overrides.clear()

    assert session.status_code == 200
    assert session.json() == {"assessor_reference": "public-demo-read-only"}
    assert latest.status_code == 200
    assert latest.json()["outcome"] == "ConfirmedFraud"
    assert write.status_code == 401
    assert outcomes.recorded == []


def test_public_prototype_records_anonymous_outcome_with_fixed_audit_identity():
    outcomes = OutcomeRepository()
    app.dependency_overrides[get_public_demo_access] = lambda: PublicDemoAccess(
        public_read_only=True,
        public_prototype_assessor=True,
    )
    app.dependency_overrides[get_assessor_outcome_boundary] = lambda: (
        AssessorOutcomeBoundary.from_settings(boundary_settings())
    )
    app.dependency_overrides[get_active_deployment] = lambda: SimpleNamespace(
        chain_id=11_155_111,
        address="0xcontract",
    )
    app.dependency_overrides[get_postgres_repositories] = lambda: repositories(
        outcomes
    )
    client = TestClient(app)
    try:
        session = client.get("/assessor/session")
        write = client.post(
            "/assessor/claims/7/outcome",
            json={
                "outcome": "Legitimate",
                "notes": "Fictional prototype review.",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert session.status_code == 200
    assert session.json() == {
        "assessor_reference": "public-prototype-assessor"
    }
    assert write.status_code == 201
    assert outcomes.recorded[0]["assessor_reference"] == (
        "public-prototype-assessor"
    )
