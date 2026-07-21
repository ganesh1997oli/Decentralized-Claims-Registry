"""Turn a synthetic claim into a simple, explainable fraud score."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol


MODEL_ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = MODEL_ROOT / "artifacts" / "synthetic-logistic-v1.json"

FEATURES = (
    "amount_ratio",
    "high_risk_type",
    "incident_age_ratio",
    "no_evidence",
    "short_description",
    "suspicious_language",
)

FEATURE_LABELS = {
    "amount_ratio": "Claim amount",
    "high_risk_type": "Higher-risk claim category",
    "incident_age_ratio": "Time since incident",
    "no_evidence": "No supporting evidence",
    "short_description": "Limited incident description",
    "suspicious_language": "Synthetic risk language",
}


class ScorableClaim(Protocol):
    amount_pence: int
    claim_type: str
    incident_date: date
    description: str
    evidence: list[str]


@dataclass(frozen=True)
class FraudReason:
    feature: str
    label: str
    contribution: float


@dataclass(frozen=True)
class FraudScore:
    probability: float
    score_basis_points: int
    threshold: float
    flagged: bool
    model_version: str
    reasons: tuple[FraudReason, ...]


def claim_features(
    claim: ScorableClaim, *, as_of: date | None = None
) -> dict[str, float]:
    """Turn the form fields into the numbers expected by the model."""

    evaluation_date = as_of or date.today()
    incident_age_days = max((evaluation_date - claim.incident_date).days, 0)
    words = claim.description.split()
    description = claim.description.casefold()
    suspicious_terms = (
        "cash only",
        "urgent payment",
        "lost receipt",
        "no witness",
        "unknown driver",
    )

    # Each value is kept between 0 and 1. This stops one large input, such as
    # the claim amount, from overpowering all the other inputs.
    return {
        "amount_ratio": min(claim.amount_pence / 1_000_000, 1.0),
        "high_risk_type": float(
            claim.claim_type in {"vehicle_theft", "collision", "other_motor"}
        ),
        "incident_age_ratio": min(incident_age_days / 365, 1.0),
        "no_evidence": float(not claim.evidence),
        "short_description": float(len(words) < 8),
        "suspicious_language": float(
            any(term in description for term in suspicious_terms)
        ),
    }


class SyntheticFraudScorer:
    """Load the saved model once, then score claims locally."""

    def __init__(self, artifact: dict[str, object]) -> None:
        try:
            raw_features = artifact["features"]
            raw_coefficients = artifact["coefficients"]
            if not isinstance(raw_features, list) or not isinstance(
                raw_coefficients, dict
            ):
                raise ValueError("model features and coefficients must be collections")

            feature_names = tuple(str(name) for name in raw_features)
            coefficients = {
                str(name): float(value)
                for name, value in raw_coefficients.items()
            }
            if feature_names != FEATURES or set(coefficients) != set(FEATURES):
                raise ValueError("model feature schema does not match the application")

            self.version = str(artifact["version"])
            self.intercept = float(artifact["intercept"])
            self.threshold = float(artifact["threshold"])
            self.coefficients = coefficients
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid fraud model artifact") from exc

        if not 0 < self.threshold < 1:
            raise ValueError("Fraud model threshold must be between zero and one")

    @classmethod
    def from_path(cls, path: Path = DEFAULT_MODEL_PATH) -> "SyntheticFraudScorer":
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Could not load fraud model artifact from {path}"
            ) from exc
        return cls(artifact)

    @classmethod
    def from_env(cls) -> "SyntheticFraudScorer":
        model_path = Path(os.environ.get("FRAUD_MODEL_PATH", DEFAULT_MODEL_PATH))
        return cls.from_path(model_path)

    def score(self, claim: ScorableClaim, *, as_of: date | None = None) -> FraudScore:
        features = claim_features(claim, as_of=as_of)

        # A positive contribution pushes the score towards fraud. A negative
        # contribution pushes it away from fraud.
        contributions = {
            name: self.coefficients[name] * features[name] for name in FEATURES
        }
        log_odds = self.intercept + sum(contributions.values())

        # The sigmoid converts the model's raw number into a probability from
        # 0 to 1. Clamping avoids overflow for unusually large values.
        probability = 1.0 / (1.0 + math.exp(-max(min(log_odds, 40), -40)))

        # The contract stores whole numbers, so 0.1479 becomes 1,479 out of
        # 10,000. This is the 14.79% shown in the interface.
        fraud_score = min(max(round(probability * 10_000), 0), 10_000)

        # Show only the three strongest warning signs. These short reasons are
        # easier for a human reviewer to understand than the raw feature list.
        ranked = sorted(
            (
                FraudReason(name, FEATURE_LABELS[name], round(contribution, 4))
                for name, contribution in contributions.items()
                if contribution > 0.05
            ),
            key=lambda reason: reason.contribution,
            reverse=True,
        )[:3]
        if not ranked:
            ranked = [
                FraudReason(
                    "baseline",
                    "No strong synthetic fraud indicators",
                    0.0,
                )
            ]

        return FraudScore(
            probability=round(probability, 6),
            score_basis_points=fraud_score,
            threshold=self.threshold,
            flagged=probability >= self.threshold,
            model_version=self.version,
            reasons=tuple(ranked),
        )
