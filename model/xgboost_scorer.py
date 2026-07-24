"""Load the reviewed XGBoost artifact and explain one motor claim at a time."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import joblib
import numpy as np
import pandas as pd
import shap

from model.research_pipeline import FEATURE_COLUMNS
from model.scorer import FraudReason, FraudScore


DEFAULT_ARTIFACT_DIR = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "xgboost-african-motor-v1"
)

NUMERIC_LABELS = {
    "vehicle_age": "Vehicle age",
    "claim_amount_usd": "Claim amount",
    "policy_premium_usd": "Policy premium",
    "claim_frequency_per_1000_policies": "Market claim frequency",
    "third_party_injury_flag": "Third-party injury",
    "total_loss_flag": "Total loss",
}
CATEGORY_LABELS = {
    "country_": "Country",
    "vehicle_type_": "Vehicle type",
    "claim_type_": "Claim type",
    "region_type_": "Region type",
}


class ScorableMotorClaim(Protocol):
    """The small claim interface required by the research scorer."""

    vehicle_age: int
    claim_amount_usd: float
    policy_premium_usd: float
    third_party_injury_flag: bool
    total_loss_flag: bool
    country: str
    vehicle_type: str
    claim_type: str
    region_type: str


@dataclass(frozen=True)
class ClaimFeaturesV1:
    """The exact feature contract used by the saved XGBoost pipeline."""

    vehicle_age: int
    claim_amount_usd: float
    policy_premium_usd: float
    claim_frequency_per_1000_policies: float
    third_party_injury_flag: int
    total_loss_flag: int
    country: str
    vehicle_type: str
    claim_type: str
    region_type: str

    @classmethod
    def from_claim(
        cls,
        claim: ScorableMotorClaim,
        *,
        claim_frequency: float,
    ) -> "ClaimFeaturesV1":
        return cls(
            vehicle_age=claim.vehicle_age,
            claim_amount_usd=claim.claim_amount_usd,
            policy_premium_usd=claim.policy_premium_usd,
            claim_frequency_per_1000_policies=claim_frequency,
            third_party_injury_flag=int(claim.third_party_injury_flag),
            total_loss_flag=int(claim.total_loss_flag),
            country=claim.country,
            vehicle_type=claim.vehicle_type,
            claim_type=claim.claim_type,
            region_type=claim.region_type,
        )

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(self)], columns=list(FEATURE_COLUMNS))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reason_label(feature: str, transformed_value: float) -> str:
    if feature in NUMERIC_LABELS:
        return NUMERIC_LABELS[feature]

    for prefix, group in CATEGORY_LABELS.items():
        if feature.startswith(prefix):
            value = feature.removeprefix(prefix).replace("_", " ").title()
            if transformed_value >= 0.5:
                return f"{group}: {value}"
            return f"{group} is not {value}"
    return feature.replace("_", " ").title()


class XGBoostFraudScorer:
    """Hide artifact validation, preprocessing, prediction and SHAP."""

    def __init__(self, pipeline: Any, metadata: dict[str, Any]) -> None:
        if metadata.get("artifact_schema") != 2:
            raise ValueError("Unsupported XGBoost artifact schema")
        if metadata.get("features") != list(FEATURE_COLUMNS):
            raise ValueError("XGBoost artifact feature contract does not match")

        try:
            report = metadata["report"]
            model_version = str(report["model_version"])
            threshold = float(report["xgboost"]["threshold"])
            claim_frequency = {
                str(country): float(value)
                for country, value in metadata[
                    "market_claim_frequency_by_country"
                ].items()
            }
            preprocessor = pipeline.named_steps["prepare"]
            classifier = pipeline.named_steps["classifier"]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Invalid XGBoost artifact metadata or pipeline") from exc

        if not 0 < threshold < 1:
            raise ValueError("XGBoost threshold must be between zero and one")
        if not claim_frequency:
            raise ValueError("XGBoost metadata has no market claim frequencies")

        self.pipeline = pipeline
        self.metadata = metadata
        self.model_version = model_version
        self.threshold = threshold
        self.claim_frequency = claim_frequency
        self.preprocessor = preprocessor
        self.classifier = classifier
        self.feature_names = tuple(preprocessor.get_feature_names_out())
        # Building the explainer once keeps per-claim scoring small and predictable.
        self.explainer = shap.TreeExplainer(classifier)

    @classmethod
    def from_directory(
        cls,
        artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
        *,
        expected_sha256: str | None = None,
    ) -> "XGBoostFraudScorer":
        model_path = artifact_dir / "model.joblib"
        metadata_path = artifact_dir / "metadata.json"
        if not model_path.is_file() or not metadata_path.is_file():
            raise ValueError(f"XGBoost artifacts are missing from {artifact_dir}")

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Could not read XGBoost metadata") from exc

        actual_digest = file_sha256(model_path)
        reviewed_digest = expected_sha256 or metadata.get("model_sha256")
        if not reviewed_digest or actual_digest != reviewed_digest:
            raise ValueError("XGBoost model checksum does not match metadata")

        try:
            pipeline = joblib.load(model_path)
        except Exception as exc:
            raise ValueError("Could not load the XGBoost model pipeline") from exc
        return cls(pipeline, metadata)

    @classmethod
    def from_env(cls) -> "XGBoostFraudScorer":
        return cls.from_directory(
            Path(os.environ.get("XGBOOST_MODEL_DIR", str(DEFAULT_ARTIFACT_DIR))),
            expected_sha256=os.environ.get("XGBOOST_MODEL_SHA256") or None,
        )

    def score(self, claim: ScorableMotorClaim) -> FraudScore:
        try:
            frequency = self.claim_frequency[claim.country]
        except KeyError as exc:
            raise ValueError(
                f"No reviewed market claim frequency for {claim.country!r}"
            ) from exc

        features = ClaimFeaturesV1.from_claim(
            claim,
            claim_frequency=frequency,
        )
        frame = features.as_frame()
        probability = float(self.pipeline.predict_proba(frame)[0, 1])
        transformed = np.asarray(self.preprocessor.transform(frame))
        shap_values = np.asarray(self.explainer.shap_values(transformed))
        if shap_values.ndim == 1:
            contributions = shap_values
        elif shap_values.ndim == 2:
            contributions = shap_values[0]
        else:
            raise ValueError("Unexpected SHAP result shape")

        strongest = np.argsort(np.abs(contributions))[::-1][:3]
        reasons = tuple(
            FraudReason(
                feature=str(self.feature_names[index]),
                label=_reason_label(
                    str(self.feature_names[index]),
                    float(transformed[0, index]),
                ),
                contribution=round(float(contributions[index]), 6),
            )
            for index in strongest
        )
        score_basis_points = min(max(round(probability * 10_000), 0), 10_000)
        return FraudScore(
            probability=probability,
            score_basis_points=score_basis_points,
            threshold=self.threshold,
            flagged=probability >= self.threshold,
            model_version=self.model_version,
            reasons=reasons,
        )
