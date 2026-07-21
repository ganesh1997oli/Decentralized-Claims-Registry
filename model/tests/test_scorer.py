from datetime import date

from backend.app.models import ClaimSubmission
from model.scorer import SyntheticFraudScorer, claim_features


def claim(**overrides) -> ClaimSubmission:
    payload = {
        "claimReference": "synthetic-model-test",
        "policyReference": "synthetic-policy",
        "claimType": "vehicle_damage",
        "incidentDate": "2026-07-20",
        "amountPence": 100_000,
        "description": "A detailed fictional description of minor bumper damage",
        "evidence": ["ipfs://synthetic-evidence"],
    }
    payload.update(overrides)
    return ClaimSubmission.model_validate(payload)


def test_feature_extraction_is_bounded_and_deterministic():
    features = claim_features(
        claim(amountPence=9_000_000, incidentDate="2024-01-01"),
        as_of=date(2026, 7, 21),
    )

    assert features["amount_ratio"] == 1.0
    assert features["incident_age_ratio"] == 1.0
    assert features["no_evidence"] == 0.0


def test_high_risk_synthetic_claim_scores_above_low_risk_claim():
    scorer = SyntheticFraudScorer.from_path()
    low_risk = scorer.score(claim(), as_of=date(2026, 7, 21))
    high_risk = scorer.score(
        claim(
            claimType="vehicle_theft",
            amountPence=1_000_000,
            incidentDate="2025-01-01",
            description="Urgent payment requested; no witness and lost receipt",
            evidence=[],
        ),
        as_of=date(2026, 7, 21),
    )

    assert high_risk.probability > low_risk.probability
    assert high_risk.flagged is True
    assert low_risk.flagged is False
    assert high_risk.score_basis_points <= 10_000
    assert high_risk.model_version == "synthetic-logistic-v1"
    assert high_risk.reasons[0].contribution > 0
