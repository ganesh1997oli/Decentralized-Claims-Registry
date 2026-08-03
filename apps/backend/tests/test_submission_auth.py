import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from apps.backend.app.models import ClaimSubmission, StoredClaimDocument
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
    ClaimAuthorizationVerificationError,
    InsurerPrincipal,
    SubmissionAuthConfigurationError,
    SubmissionAuthenticationError,
    SubmissionAuthorizationError,
    SubmissionBoundary,
    SubmissionRateLimitError,
)

API_KEY = "northstar-test-api-key-with-enough-entropy"


def claim() -> ClaimSubmission:
    return ClaimSubmission.model_validate(
        {
            "insurerId": "northstar-mutual",
            "claimReference": "synthetic-auth-1",
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
            "description": "Synthetic authentication test claim",
            "evidence": [],
        }
    )


def settings(*, daily_quota=2, insurer_rate=2, ip_rate=4):
    credentials = [
        {
            "credentialId": "northstar-test-v1",
            "insurerId": "northstar-mutual",
            "apiKeySha256": hashlib.sha256(API_KEY.encode()).hexdigest(),
            "dailyQuota": daily_quota,
        }
    ]
    return {
        "INSURER_CREDENTIALS_JSON": json.dumps(credentials),
        "INSURER_RATE_LIMIT_PER_MINUTE": str(insurer_rate),
        "IP_RATE_LIMIT_PER_MINUTE": str(ip_rate),
    }


def test_boundary_returns_authoritative_principal_and_reserves_capacity():
    boundary = SubmissionBoundary.from_mapping(settings())

    principal = boundary.authorize_and_reserve(
        api_key=API_KEY,
        claimed_insurer_id="northstar-mutual",
        client_ip="192.0.2.10",
    )

    assert principal.insurer_id == "northstar-mutual"
    assert principal.credential_id == "northstar-test-v1"
    assert principal.permitted_operations == frozenset({"submit_claim"})


def test_boundary_does_not_accept_an_unknown_api_key():
    boundary = SubmissionBoundary.from_mapping(settings())

    with pytest.raises(SubmissionAuthenticationError, match="Invalid"):
        boundary.authorize_and_reserve(
            api_key="wrong-api-key-with-enough-characters",
            claimed_insurer_id="northstar-mutual",
            client_ip="192.0.2.10",
        )


def test_browser_selected_insurer_must_match_authenticated_principal():
    boundary = SubmissionBoundary.from_mapping(settings())

    with pytest.raises(SubmissionAuthorizationError, match="does not match"):
        boundary.authorize_and_reserve(
            api_key=API_KEY,
            claimed_insurer_id="harbour-shield",
            client_ip="192.0.2.10",
        )


def test_ip_limit_counts_invalid_authentication_attempts():
    boundary = SubmissionBoundary.from_mapping(settings(ip_rate=2))
    for _ in range(2):
        with pytest.raises(SubmissionAuthenticationError):
            boundary.authorize_and_reserve(
                api_key="wrong-api-key-with-enough-characters",
                claimed_insurer_id="northstar-mutual",
                client_ip="192.0.2.10",
            )

    with pytest.raises(SubmissionRateLimitError, match="IP address") as error:
        boundary.authorize_and_reserve(
            api_key=API_KEY,
            claimed_insurer_id="northstar-mutual",
            client_ip="192.0.2.10",
        )
    assert error.value.retry_after <= 60


def test_per_insurer_minute_limit_is_enforced_before_external_work():
    boundary = SubmissionBoundary.from_mapping(settings(insurer_rate=1))
    boundary.authorize_and_reserve(
        api_key=API_KEY,
        claimed_insurer_id="northstar-mutual",
        client_ip="192.0.2.10",
    )

    with pytest.raises(SubmissionRateLimitError, match="per-minute"):
        boundary.authorize_and_reserve(
            api_key=API_KEY,
            claimed_insurer_id="northstar-mutual",
            client_ip="192.0.2.11",
        )


def test_daily_quota_resets_on_the_next_utc_day():
    current = [datetime(2026, 7, 31, 23, 59, tzinfo=UTC)]
    boundary = SubmissionBoundary.from_mapping(
        settings(daily_quota=1, insurer_rate=10),
        clock=lambda: current[0],
    )
    boundary.authorize_and_reserve(
        api_key=API_KEY,
        claimed_insurer_id="northstar-mutual",
        client_ip="192.0.2.10",
    )
    with pytest.raises(SubmissionRateLimitError, match="daily"):
        boundary.authorize_and_reserve(
            api_key=API_KEY,
            claimed_insurer_id="northstar-mutual",
            client_ip="192.0.2.11",
        )

    current[0] += timedelta(minutes=2)
    principal = boundary.authorize_and_reserve(
        api_key=API_KEY,
        claimed_insurer_id="northstar-mutual",
        client_ip="192.0.2.10",
    )
    assert principal.insurer_id == "northstar-mutual"


def test_configuration_accepts_only_digest_not_a_plaintext_key():
    raw = json.loads(settings()["INSURER_CREDENTIALS_JSON"])
    raw[0]["apiKeySha256"] = API_KEY

    with pytest.raises(SubmissionAuthConfigurationError, match="64 hexadecimal"):
        SubmissionBoundary.from_mapping(
            {**settings(), "INSURER_CREDENTIALS_JSON": json.dumps(raw)}
        )


def test_configuration_rejects_a_credential_id_the_worker_cannot_parse():
    raw = json.loads(settings()["INSURER_CREDENTIALS_JSON"])
    raw[0]["credentialId"] = "invalid credential id"

    with pytest.raises(SubmissionAuthConfigurationError, match="credentialId"):
        SubmissionBoundary.from_mapping(
            {**settings(), "INSURER_CREDENTIALS_JSON": json.dumps(raw)}
        )


def test_worker_can_verify_gateway_authorized_claim_identity():
    signer = ClaimAuthorizationSigner(b"authorization-test-key-32-bytes-minimum")
    principal = InsurerPrincipal(
        insurer_id="northstar-mutual",
        credential_id="northstar-test-v1",
        permitted_operations=frozenset({"submit_claim"}),
        daily_quota=2,
    )

    payload = signer.authorized_claim_bytes(claim(), principal)
    stored = StoredClaimDocument.model_validate_json(payload)
    verified = signer.verify_claim(stored)

    assert stored.schema_version == 4
    assert verified.insurer_id == "northstar-mutual"
    assert verified.credential_id == "northstar-test-v1"


def test_worker_rejects_insurer_label_changed_after_gateway_authorization():
    signer = ClaimAuthorizationSigner(b"authorization-test-key-32-bytes-minimum")
    principal = InsurerPrincipal(
        insurer_id="northstar-mutual",
        credential_id="northstar-test-v1",
        permitted_operations=frozenset({"submit_claim"}),
        daily_quota=2,
    )
    document = json.loads(signer.authorized_claim_bytes(claim(), principal))
    document["insurerId"] = "harbour-shield"
    tampered = StoredClaimDocument.model_validate(document)

    with pytest.raises(ClaimAuthorizationVerificationError, match="not authorized"):
        signer.verify_claim(tampered)
