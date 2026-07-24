"""Train and evaluate the research fraud model without changing the demo scorer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier


RANDOM_SEED = 20_260_724
MODEL_VERSION = "african-motor-xgboost-v1"
TARGET_COLUMN = "fraud_flag"
DATE_COLUMN = "claim_date"
ID_COLUMN = "claim_id"

NUMERIC_FEATURES = (
    "vehicle_age",
    "claim_amount_usd",
    "policy_premium_usd",
    "claim_frequency_per_1000_policies",
    "third_party_injury_flag",
    "total_loss_flag",
)
CATEGORICAL_FEATURES = (
    "country",
    "vehicle_type",
    "claim_type",
    "region_type",
)
FEATURE_COLUMNS = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# These values are known after a claim is investigated or were used to generate
# the synthetic target. Letting the model see them would make the result misleading.
LEAKAGE_COLUMNS = (
    "settlement_amount_usd",
    "settlement_ratio",
    "fraud_probability",
    "processing_days",
    "loss_ratio",
    "scenario",
)


@dataclass(frozen=True)
class DatasetSplit:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


@dataclass(frozen=True)
class TrainingRun:
    baseline: Pipeline
    xgboost: Pipeline
    split: DatasetSplit
    report: dict[str, Any]


def load_claims(path: Path) -> pd.DataFrame:
    """Load the CSV and keep only fields available when a claim is submitted."""

    return prepare_claims(pd.read_csv(path))


def prepare_claims(frame: pd.DataFrame) -> pd.DataFrame:
    required = {ID_COLUMN, DATE_COLUMN, TARGET_COLUMN, *FEATURE_COLUMNS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Dataset is missing required columns: {', '.join(missing)}")

    prepared = frame[[ID_COLUMN, DATE_COLUMN, TARGET_COLUMN, *FEATURE_COLUMNS]].copy()
    prepared[DATE_COLUMN] = pd.to_datetime(prepared[DATE_COLUMN], errors="raise")

    if prepared[ID_COLUMN].duplicated().any():
        raise ValueError("claim_id values must be unique")
    if prepared[TARGET_COLUMN].isna().any():
        raise ValueError("fraud_flag cannot contain missing values")

    if not prepared[TARGET_COLUMN].isin([0, 1]).all():
        raise ValueError("fraud_flag may only contain 0 and 1")

    labels = set(prepared[TARGET_COLUMN].unique())
    if labels != {0, 1}:
        raise ValueError("fraud_flag must contain both 0 and 1")

    prepared[TARGET_COLUMN] = prepared[TARGET_COLUMN].astype(int)
    return prepared.sort_values([DATE_COLUMN, ID_COLUMN]).reset_index(drop=True)


def temporal_split(
    frame: pd.DataFrame,
    *,
    train_fraction: float = 0.70,
    validation_fraction: float = 0.15,
) -> DatasetSplit:
    """Keep later claims out of training so the test better reflects future use."""

    if train_fraction <= 0 or validation_fraction <= 0:
        raise ValueError("Split fractions must be greater than zero")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("Training and validation must leave room for a test set")

    train_end = int(len(frame) * train_fraction)
    validation_end = train_end + int(len(frame) * validation_fraction)
    split = DatasetSplit(
        train=frame.iloc[:train_end].copy(),
        validation=frame.iloc[train_end:validation_end].copy(),
        test=frame.iloc[validation_end:].copy(),
    )

    for name, part in (
        ("training", split.train),
        ("validation", split.validation),
        ("test", split.test),
    ):
        if part.empty or part[TARGET_COLUMN].nunique() != 2:
            raise ValueError(f"The {name} split must contain fraud and non-fraud rows")
    return split


def build_preprocessor() -> ColumnTransformer:
    numeric = Pipeline(
        [
            ("fill_missing", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]
    )
    categorical = Pipeline(
        [
            ("fill_missing", SimpleImputer(strategy="most_frequent")),
            (
                "one_hot",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
            ),
        ]
    )
    return ColumnTransformer(
        [
            ("numeric", numeric, list(NUMERIC_FEATURES)),
            ("category", categorical, list(CATEGORICAL_FEATURES)),
        ],
        verbose_feature_names_out=False,
    )


def choose_threshold(labels: pd.Series, probabilities: np.ndarray) -> float:
    """Choose the validation threshold with the strongest fraud-class F1 score."""

    candidates = np.arange(0.05, 0.96, 0.01)
    return float(
        max(
            candidates,
            key=lambda value: f1_score(
                labels,
                probabilities >= value,
                zero_division=0,
            ),
        )
    )


def evaluate_model(
    model: Pipeline,
    frame: pd.DataFrame,
    *,
    threshold: float,
) -> dict[str, Any]:
    labels = frame[TARGET_COLUMN]
    probabilities = model.predict_proba(frame[list(FEATURE_COLUMNS)])[:, 1]
    predictions = probabilities >= threshold
    matrix = confusion_matrix(labels, predictions)

    return {
        "threshold": round(threshold, 4),
        "precision": round(precision_score(labels, predictions, zero_division=0), 6),
        "recall": round(recall_score(labels, predictions, zero_division=0), 6),
        "f1": round(f1_score(labels, predictions, zero_division=0), 6),
        "roc_auc": round(roc_auc_score(labels, probabilities), 6),
        "pr_auc": round(average_precision_score(labels, probabilities), 6),
        "brier_score": round(brier_score_loss(labels, probabilities), 6),
        "confusion_matrix": matrix.astype(int).tolist(),
    }


def _fit_pipeline(classifier: Any, training: pd.DataFrame) -> Pipeline:
    pipeline = Pipeline(
        [
            ("prepare", build_preprocessor()),
            ("classifier", classifier),
        ]
    )
    pipeline.fit(training[list(FEATURE_COLUMNS)], training[TARGET_COLUMN])
    return pipeline


def train_research_models(
    frame: pd.DataFrame,
    *,
    xgboost_estimators: int = 250,
) -> TrainingRun:
    """Fit a simple baseline and XGBoost, then score the untouched test period."""

    prepared = prepare_claims(frame)
    split = temporal_split(prepared)
    fraud_count = int(split.train[TARGET_COLUMN].sum())
    non_fraud_count = len(split.train) - fraud_count
    class_weight = non_fraud_count / fraud_count

    baseline = _fit_pipeline(
        LogisticRegression(
            class_weight="balanced",
            max_iter=1_000,
            random_state=RANDOM_SEED,
        ),
        split.train,
    )
    xgboost = _fit_pipeline(
        XGBClassifier(
            n_estimators=xgboost_estimators,
            max_depth=5,
            learning_rate=0.05,
            min_child_weight=2,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=class_weight,
            eval_metric="logloss",
            tree_method="hist",
            random_state=RANDOM_SEED,
            n_jobs=1,
        ),
        split.train,
    )

    validation_features = split.validation[list(FEATURE_COLUMNS)]
    baseline_threshold = choose_threshold(
        split.validation[TARGET_COLUMN],
        baseline.predict_proba(validation_features)[:, 1],
    )
    xgboost_threshold = choose_threshold(
        split.validation[TARGET_COLUMN],
        xgboost.predict_proba(validation_features)[:, 1],
    )

    report = {
        "model_version": MODEL_VERSION,
        "split": {
            "strategy": "chronological 70/15/15",
            "training_rows": len(split.train),
            "validation_rows": len(split.validation),
            "test_rows": len(split.test),
            "training_fraud_rate": round(split.train[TARGET_COLUMN].mean(), 6),
            "validation_fraud_rate": round(
                split.validation[TARGET_COLUMN].mean(), 6
            ),
            "test_fraud_rate": round(split.test[TARGET_COLUMN].mean(), 6),
        },
        "baseline_logistic_regression": evaluate_model(
            baseline,
            split.test,
            threshold=baseline_threshold,
        ),
        "xgboost": evaluate_model(
            xgboost,
            split.test,
            threshold=xgboost_threshold,
        ),
    }
    return TrainingRun(
        baseline=baseline,
        xgboost=xgboost,
        split=split,
        report=report,
    )


def create_shap_summary(
    model: Pipeline,
    test_rows: pd.DataFrame,
    output_path: Path,
    *,
    sample_size: int = 500,
) -> list[dict[str, float | str]]:
    """Save one readable SHAP chart and return the strongest global features."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap

    sample = test_rows.sample(
        n=min(sample_size, len(test_rows)),
        random_state=RANDOM_SEED,
    )
    preprocessor = model.named_steps["prepare"]
    classifier = model.named_steps["classifier"]
    transformed = preprocessor.transform(sample[list(FEATURE_COLUMNS)])
    feature_names = preprocessor.get_feature_names_out()

    explainer = shap.TreeExplainer(classifier)
    shap_values = np.asarray(explainer.shap_values(transformed))
    importance = np.abs(shap_values).mean(axis=0)
    order = np.argsort(importance)[::-1][:15]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shap.summary_plot(
        shap_values,
        transformed,
        feature_names=feature_names,
        max_display=15,
        show=False,
    )
    plt.tight_layout()
    plt.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close()

    return [
        {
            "feature": str(feature_names[index]),
            "mean_absolute_shap": round(float(importance[index]), 6),
        }
        for index in order
    ]


def save_training_run(
    run: TrainingRun,
    output_dir: Path,
    *,
    dataset_reference: str,
    dataset_sha256: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "model.joblib"
    shap_path = output_dir / "shap-summary.png"
    metadata_path = output_dir / "metadata.json"

    joblib.dump(run.xgboost, model_path)
    top_features = create_shap_summary(run.xgboost, run.split.test, shap_path)
    metadata = {
        "artifact_schema": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_reference": dataset_reference,
        "dataset_sha256": dataset_sha256,
        "features": list(FEATURE_COLUMNS),
        "excluded_leakage_fields": list(LEAKAGE_COLUMNS),
        "report": run.report,
        "shap_top_features": top_features,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata
