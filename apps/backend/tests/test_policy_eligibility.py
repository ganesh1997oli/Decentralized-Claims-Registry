import json
from datetime import UTC, datetime

import pytest

from apps.backend.app.claimant_auth import ClaimantSession
from apps.backend.app.models import ClaimSubmission
from apps.backend.app.policy_eligibility import (
    ConfiguredPolicyEligibility,
    PolicyEligibilityError,
    policy_reference_hmac,
)

CLAIMANT = "0x1111111111111111111111111111111111111111"
REPRESENTATIVE = "0x2222222222222222222222222222222222222222"
INSURER = "0x3333333333333333333333333333333333333333"
LOOKUP_KEY = b"policy-reference-lookup-key-32-bytes"


def claim(**changes) -> ClaimSubmission:
    values = {
        "insurerId": "northstar-mutual",
        "claimReference": "synthetic-public-1",
        "policyReference": "policy-private-42",
        "claimType": "collision",
        "incidentDate": "2026-07-13",
        "claimAmountUsd": 2_500,
        "policyPremiumUsd": 480,
        "vehicleAge": 6,
        "vehicleType": "sedan",
        "country": "Nigeria",
        "regionType": "urban",
        "thirdPartyInjuryFlag": False,
        "totalLossFlag": False,
        "description": "Synthetic public eligibility test",
        "evidence": [],
    }
    values.update(changes)
    return ClaimSubmission.model_validate(values)


def eligibility() -> ConfiguredPolicyEligibility:
    return ConfiguredPolicyEligibility.from_mapping(
        {
            "POLICY_REFERENCE_LOOKUP_KEY": LOOKUP_KEY.decode(),
            "CLAIMANT_COMMITMENT_KEY": "claimant-commitment-key-at-least-32-bytes",
            "POLICY_ELIGIBILITY_RECORDS_JSON": json.dumps(
                [
                    {
                        "policyId": "policy-internal-42",
                        "policyReferenceHmac": policy_reference_hmac(
                            LOOKUP_KEY,
                            "policy-private-42",
                        ),
                        "insurerId": "northstar-mutual",
                        "insurerAddress": INSURER,
                        "claimantAddress": CLAIMANT,
                        "authorizedSubmitterAddresses": [CLAIMANT, REPRESENTATIVE],
                        "coverageStart": "2026-01-01",
                        "coverageEnd": "2026-12-31",
                        "allowedClaimTypes": ["collision", "theft"],
                        "maxClaimAmountUsd": 50_000,
                        "dailyQuota": 4,
                    }
                ]
            ),
        }
    )


def representative_session() -> ClaimantSession:
    return ClaimantSession(
        subject_id="claimant-" + ("a" * 64),
        claimant_address=REPRESENTATIVE,
        expires_at=datetime(2026, 8, 18, 12, 15, tzinfo=UTC),
    )


def test_policy_verification_separates_claimant_submitter_and_insurer():
    principal = eligibility().verify(claim(), representative_session())

    assert principal.claimant_address == CLAIMANT
    assert principal.submitter_address == REPRESENTATIVE
    assert principal.insurer_address == INSURER
    assert principal.policy_id == "policy-internal-42"
    assert principal.claimant_commitment.startswith("0x")
    assert len(principal.claimant_commitment) == 66


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"insurerId": "harbour-shield"}, "selected insurer"),
        ({"incidentDate": "2019-01-01"}, "not active"),
        ({"incidentDate": "2027-01-01"}, "future"),
        ({"claimType": "fire"}, "not covered"),
        ({"claimAmountUsd": 50_001}, "exceeds"),
    ],
)
def test_policy_verification_fails_closed_for_ineligible_claims(changes, message):
    with pytest.raises(PolicyEligibilityError, match=message):
        eligibility().verify(claim(**changes), representative_session())
