"""Turn one submitted motor claim into a score a person can inspect.

This module is the small bridge between the web application and the trained
XGBoost model. In everyday terms, it does four jobs:

1. load only the reviewed model artifact and check that it has not changed;
2. reshape an application claim into the fields used during model training;
3. ask XGBoost for a fraud-risk probability; and
4. turn the five strongest local SHAP effects into readable reasons.

SHAP explains what influenced this model prediction. It does not establish that
fraud happened, and the scorer therefore returns evidence for human review
rather than an automatic approval or rejection decision.
"""

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

from packages.model.contracts import FraudReason, FraudScore
from packages.model.research_pipeline import FEATURE_COLUMNS

# This is created by ``python -m packages.model.train_xgboost``.
# An environment variable can point to another reviewed artifact at deployment.
DEFAULT_ARTIFACT_DIR = (
    Path(__file__).resolve().parent / "artifacts" / "xgboost-african-motor-v1"
)

# The model works with machine-oriented feature names. These mappings give the
# API and dashboard short labels that are easier for an investigator to read.
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

# The dissertation proposal commits to showing five claim-specific reasons to
# an investigator. Keeping the limit named makes any future change to that
# user-facing contract deliberate and easy to find.
SHAP_REASON_LIMIT = 5


class ScorableMotorClaim(Protocol):
    """Describe the claim information the scorer actually needs.

    This is a structural interface rather than a database model. Any claim
    object with these fields can be scored, which keeps the model code separate
    from FastAPI, PostgreSQL and blockchain-specific classes.
    """

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
    """Hold one claim in exactly the same shape used during model training.

    ``frozen=True`` prevents a value from being changed halfway through a
    prediction. The ``V1`` suffix makes the feature contract explicit: adding,
    removing or renaming a field should create a deliberate new version rather
    than silently changing what an existing artifact receives.
    """

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
    ) -> ClaimFeaturesV1:
        """Build trusted model features from an application claim.

        Most values come directly from the submitted claim. Market claim
        frequency is supplied separately because it is a reviewed reference
        value stored with the model, not a value a browser is allowed to choose.
        Boolean flags become ``0`` or ``1`` because that is how the training
        pipeline learned those fields.
        """

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
        """Return a one-row table in the model's required column order.

        Scikit-learn pipelines expect tabular input. Supplying
        ``FEATURE_COLUMNS`` explicitly also protects the scorer from a dataclass
        refactor accidentally rearranging the model input.
        """

        return pd.DataFrame([asdict(self)], columns=list(FEATURE_COLUMNS))


def file_sha256(path: Path) -> str:
    """Calculate a stable fingerprint for a model file.

    The file is read in one-megabyte pieces so a large artifact does not need to
    be loaded into memory twice. The returned digest is compared with the
    reviewed digest before joblib is allowed to deserialize the model.
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reason_label(feature: str, transformed_value: float) -> str:
    """Translate one transformed feature into a label a person can understand.

    Numeric fields only need a friendly name such as ``Claim amount``.
    Categorical fields were one-hot encoded into columns such as
    ``country_Ghana``. A transformed value of ``1`` means that category is
    present; ``0`` means it is absent, so the wording becomes either
    ``Country: Ghana`` or ``Country is not Ghana``.

    The final fallback still produces readable text if a future reviewed model
    introduces a feature that has not yet been added to the label dictionaries.
    """

    if feature in NUMERIC_LABELS:
        return NUMERIC_LABELS[feature]

    # Check known one-hot prefixes before using the generic fallback below.
    for prefix, group in CATEGORY_LABELS.items():
        if feature.startswith(prefix):
            value = feature.removeprefix(prefix).replace("_", " ").title()
            if transformed_value >= 0.5:
                return f"{group}: {value}"
            return f"{group} is not {value}"
    return feature.replace("_", " ").title()


class XGBoostFraudScorer:
    """Provide one safe, application-friendly interface to the trained model.

    Callers only need to construct the scorer once and call :meth:`score` for
    each claim. The class keeps artifact validation, preprocessing, probability
    prediction and SHAP-specific details behind that small interface.
    """

    def __init__(self, pipeline: Any, metadata: dict[str, Any]) -> None:
        """Validate an already-loaded pipeline and prepare it for repeated use.

        Application code normally uses :meth:`from_directory` or
        :meth:`from_env`; this constructor performs the shared validation after
        those loaders have read the files. Invalid or incomplete artifacts fail
        immediately, before the worker starts accepting claims.
        """

        # The schema and ordered feature list confirm that the application and
        # training code agree about the artifact's structure.
        if metadata.get("artifact_schema") != 2:
            raise ValueError("Unsupported XGBoost artifact schema")
        if metadata.get("features") != list(FEATURE_COLUMNS):
            raise ValueError("XGBoost artifact feature contract does not match")

        # Pull every serving-time value from the reviewed artifact in one guarded
        # block. Any missing key or incompatible pipeline produces one clear
        # configuration error instead of failing later during a live claim.
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

        # A threshold outside this range cannot represent a useful probability
        # decision boundary. An empty frequency map would make every claim
        # impossible to enrich.
        if not 0 < threshold < 1:
            raise ValueError("XGBoost threshold must be between zero and one")
        if not claim_frequency:
            raise ValueError("XGBoost metadata has no market claim frequencies")

        # Keep the validated objects ready because a Kafka worker scores many
        # claims with the same model and should not rebuild them for every event.
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
    ) -> XGBoostFraudScorer:
        """Load and verify a reviewed model artifact from a directory.

        The directory must contain ``model.joblib`` and ``metadata.json``.
        Before loading joblib, the method compares the model file with either
        the explicitly configured digest or the digest recorded in metadata.
        This order matters because loading an untrusted joblib file can execute
        Python code.
        """

        model_path = artifact_dir / "model.joblib"
        metadata_path = artifact_dir / "metadata.json"

        # Report missing deployment files as one understandable startup error.
        if not model_path.is_file() or not metadata_path.is_file():
            raise ValueError(f"XGBoost artifacts are missing from {artifact_dir}")

        # Metadata is plain JSON, so it is safe to read before the model itself.
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Could not read XGBoost metadata") from exc

        # An explicit deployment digest takes priority. Otherwise, use the digest
        # written by the controlled training workflow.
        actual_digest = file_sha256(model_path)
        reviewed_digest = expected_sha256 or metadata.get("model_sha256")
        if not reviewed_digest or actual_digest != reviewed_digest:
            raise ValueError("XGBoost model checksum does not match metadata")

        # Joblib can execute Python objects while loading. Verify the reviewed
        # checksum first and never point this loader at a user-supplied artifact.
        try:
            pipeline = joblib.load(model_path)
        except Exception as exc:
            raise ValueError("Could not load the XGBoost model pipeline") from exc
        return cls(pipeline, metadata)

    @classmethod
    def from_env(cls) -> XGBoostFraudScorer:
        """Create the scorer from deployment environment variables.

        ``XGBOOST_MODEL_DIR`` selects the artifact directory and
        ``XGBOOST_MODEL_SHA256`` can pin the approved model digest. Local
        development falls back to :data:`DEFAULT_ARTIFACT_DIR`.
        """

        return cls.from_directory(
            Path(os.environ.get("XGBOOST_MODEL_DIR", str(DEFAULT_ARTIFACT_DIR))),
            expected_sha256=os.environ.get("XGBOOST_MODEL_SHA256") or None,
        )

    def score(self, claim: ScorableMotorClaim) -> FraudScore:
        """Score one claim and return both the result and five local reasons.

        The returned :class:`FraudScore` contains the raw probability, an
        integer form suitable for Solidity, the model threshold, the resulting
        review flag, the model version and the five strongest SHAP
        contributions. A positive contribution pushed this claim's prediction
        toward higher risk; a negative contribution pushed it toward lower
        risk.
        """

        try:
            # Market frequency came from the reviewed training metadata. It is not
            # accepted from the browser, where a user could manipulate it.
            frequency = self.claim_frequency[claim.country]
        except KeyError as exc:
            raise ValueError(
                f"No reviewed market claim frequency for {claim.country!r}"
            ) from exc

        features = ClaimFeaturesV1.from_claim(
            claim,
            claim_frequency=frequency,
        )

        # The full pipeline handles the same preprocessing used during training
        # and returns the probability assigned to the fraud-risk class.
        frame = features.as_frame()
        probability = float(self.pipeline.predict_proba(frame)[0, 1])

        # SHAP works on the transformed numeric matrix seen by XGBoost, so use
        # the pipeline's fitted preprocessor rather than recreating those values.
        transformed = np.asarray(self.preprocessor.transform(frame))
        shap_values = np.asarray(self.explainer.shap_values(transformed))

        # SHAP versions may return one claim as a flat vector or as a one-row
        # matrix. Normalize both supported forms into the same feature vector.
        if shap_values.ndim == 1:
            contributions = shap_values
        elif shap_values.ndim == 2:
            contributions = shap_values[0]
        else:
            raise ValueError("Unexpected SHAP result shape")

        # Absolute magnitude finds the features that moved this particular result
        # most. A contribution may push risk up or down; SHAP explains the model,
        # not whether fraud actually occurred.
        # NumPy slicing returns all available features if an older artifact
        # happens to contain fewer than SHAP_REASON_LIMIT features.
        strongest = np.argsort(np.abs(contributions))[::-1][:SHAP_REASON_LIMIT]
        # Keep the feature name, readable label and signed contribution together.
        # The API can then show the explanation without knowing about one-hot
        # encoding or NumPy positions.
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
        # Solidity has no floating-point type. Basis points retain two percentage
        # decimals: 0.2466 becomes 2,466 out of 10,000, displayed as 24.66%.
        score_basis_points = min(max(round(probability * 10_000), 0), 10_000)
        return FraudScore(
            probability=probability,
            score_basis_points=score_basis_points,
            threshold=self.threshold,
            flagged=probability >= self.threshold,
            model_version=self.model_version,
            reasons=reasons,
        )
