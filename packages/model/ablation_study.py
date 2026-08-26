"""Reproducible country and vehicle-age ablation for the research models.

This module deliberately does not save or replace the deployed XGBoost artifact.
It retrains evaluation-only pipelines on the existing chronological split and
archives metrics, paired bootstrap intervals and plots for dissertation review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

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
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

from packages.model.research_pipeline import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    RANDOM_SEED,
    TARGET_COLUMN,
    DatasetSplit,
    choose_threshold,
    prepare_claims,
    temporal_split,
)


@dataclass(frozen=True)
class AblationSpec:
    """One controlled feature-removal condition."""

    name: str
    label: str
    dropped_features: tuple[str, ...]

    @property
    def numeric_features(self) -> tuple[str, ...]:
        return tuple(
            feature
            for feature in NUMERIC_FEATURES
            if feature not in self.dropped_features
        )

    @property
    def categorical_features(self) -> tuple[str, ...]:
        return tuple(
            feature
            for feature in CATEGORICAL_FEATURES
            if feature not in self.dropped_features
        )

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return self.numeric_features + self.categorical_features


ABLATION_SPECS = (
    AblationSpec("full", "Full model", ()),
    AblationSpec(
        "without_country",
        "Without country",
        ("country",),
    ),
    AblationSpec(
        "without_vehicle_age",
        "Without vehicle age",
        ("vehicle_age",),
    ),
    AblationSpec(
        "without_country_and_vehicle_age",
        "Without country and vehicle age",
        ("country", "vehicle_age"),
    ),
    AblationSpec(
        "without_country_and_market_frequency",
        "Without country and market frequency",
        ("country", "claim_frequency_per_1000_policies"),
    ),
    AblationSpec(
        "strict_claim_features",
        "Without country, market frequency and vehicle age",
        (
            "country",
            "claim_frequency_per_1000_policies",
            "vehicle_age",
        ),
    ),
)


@dataclass(frozen=True)
class AblationRun:
    split: DatasetSplit
    report: dict[str, Any]
    probabilities: dict[str, dict[str, np.ndarray]]


def build_ablation_preprocessor(spec: AblationSpec) -> ColumnTransformer:
    """Create preprocessing using only features retained by ``spec``."""

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
    transformers: list[tuple[str, Any, list[str]]] = []
    if spec.numeric_features:
        transformers.append(("numeric", numeric, list(spec.numeric_features)))
    if spec.categorical_features:
        transformers.append(
            ("category", categorical, list(spec.categorical_features))
        )
    return ColumnTransformer(transformers, verbose_feature_names_out=False)


def evaluate_predictions(
    labels: pd.Series | np.ndarray,
    probabilities: np.ndarray,
    *,
    threshold: float,
) -> dict[str, Any]:
    """Calculate threshold-free and threshold-dependent test metrics."""

    label_values = np.asarray(labels, dtype=int)
    probability_values = np.asarray(probabilities, dtype=float)
    predictions = probability_values >= threshold
    matrix = confusion_matrix(label_values, predictions, labels=[0, 1])
    prevalence = float(label_values.mean())
    pr_auc = float(average_precision_score(label_values, probability_values))
    return {
        "threshold": round(float(threshold), 6),
        "prevalence": round(prevalence, 6),
        "selection_rate": round(float(predictions.mean()), 6),
        "precision": round(
            float(precision_score(label_values, predictions, zero_division=0)),
            6,
        ),
        "recall": round(
            float(recall_score(label_values, predictions, zero_division=0)),
            6,
        ),
        "f1": round(
            float(f1_score(label_values, predictions, zero_division=0)),
            6,
        ),
        "roc_auc": round(
            float(roc_auc_score(label_values, probability_values)),
            6,
        ),
        "pr_auc": round(pr_auc, 6),
        "pr_auc_lift": round(pr_auc - prevalence, 6),
        "brier_score": round(
            float(brier_score_loss(label_values, probability_values)),
            6,
        ),
        "confusion_matrix": matrix.astype(int).tolist(),
    }


def choose_threshold_for_selection_rate(
    probabilities: np.ndarray,
    *,
    target_selection_rate: float,
) -> float:
    """Choose a validation threshold close to a fixed review capacity."""

    if not 0 < target_selection_rate < 1:
        raise ValueError("target_selection_rate must be between zero and one")
    return float(
        np.quantile(
            np.asarray(probabilities, dtype=float),
            1 - target_selection_rate,
            method="higher",
        )
    )


def _safe_rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def calculate_country_metrics(
    frame: pd.DataFrame,
    probabilities: np.ndarray,
    *,
    threshold: float,
) -> list[dict[str, Any]]:
    """Audit country after scoring, even when country was not a model input."""

    audit = frame[["country", TARGET_COLUMN]].copy()
    audit["probability"] = np.asarray(probabilities, dtype=float)
    audit["prediction"] = audit["probability"] >= threshold
    rows: list[dict[str, Any]] = []
    for country, group in audit.groupby("country", sort=True):
        labels = group[TARGET_COLUMN].to_numpy(dtype=int)
        predictions = group["prediction"].to_numpy(dtype=bool)
        tn, fp, fn, tp = confusion_matrix(
            labels,
            predictions,
            labels=[0, 1],
        ).ravel()
        rows.append(
            {
                "country": str(country),
                "n": len(group),
                "positives": int(labels.sum()),
                "prevalence": round(float(labels.mean()), 6),
                "selection_rate": round(float(predictions.mean()), 6),
                "false_positive_rate": _safe_rate(int(fp), int(fp + tn)),
                "false_negative_rate": _safe_rate(int(fn), int(fn + tp)),
                "precision": _safe_rate(int(tp), int(tp + fp)),
                "brier_score": round(
                    float(brier_score_loss(labels, group["probability"])),
                    6,
                ),
            }
        )
    return rows


def _fit_pipeline(
    classifier: Any,
    training: pd.DataFrame,
    spec: AblationSpec,
) -> Pipeline:
    pipeline = Pipeline(
        [
            ("prepare", build_ablation_preprocessor(spec)),
            ("classifier", classifier),
        ]
    )
    pipeline.fit(training[list(spec.feature_columns)], training[TARGET_COLUMN])
    return pipeline


def _bootstrap_pr_auc(
    labels: np.ndarray,
    probabilities: dict[str, dict[str, np.ndarray]],
    *,
    samples: int,
    seed: int,
) -> dict[str, dict[str, np.ndarray]]:
    """Use the same resampled test rows for every paired comparison."""

    distributions = {
        model_name: {
            spec_name: np.full(samples, np.nan, dtype=float)
            for spec_name in model_probabilities
        }
        for model_name, model_probabilities in probabilities.items()
    }
    if samples == 0:
        return distributions

    rng = np.random.default_rng(seed)
    for sample_number in range(samples):
        indices = rng.integers(0, len(labels), size=len(labels))
        sampled_labels = labels[indices]
        if np.unique(sampled_labels).size != 2:
            continue
        for model_name, model_probabilities in probabilities.items():
            for spec_name, values in model_probabilities.items():
                distributions[model_name][spec_name][sample_number] = (
                    average_precision_score(sampled_labels, values[indices])
                )
    return distributions


def _percentile_interval(values: np.ndarray) -> list[float]:
    finite_values = values[np.isfinite(values)]
    if len(finite_values) == 0:
        return []
    return [
        round(float(value), 6)
        for value in np.percentile(finite_values, [2.5, 97.5])
    ]


def run_ablation_study(
    frame: pd.DataFrame,
    *,
    xgboost_estimators: int = 250,
    bootstrap_samples: int = 2_000,
    seed: int = RANDOM_SEED,
) -> AblationRun:
    """Train every controlled ablation and evaluate one untouched test period."""

    if bootstrap_samples < 0:
        raise ValueError("bootstrap_samples cannot be negative")

    prepared = prepare_claims(frame)
    split = temporal_split(prepared)
    fraud_count = int(split.train[TARGET_COLUMN].sum())
    non_fraud_count = len(split.train) - fraud_count
    class_weight = non_fraud_count / fraud_count
    labels = split.test[TARGET_COLUMN].to_numpy(dtype=int)
    probabilities: dict[str, dict[str, np.ndarray]] = {
        "logistic_regression": {},
        "xgboost": {},
    }
    metrics: dict[str, dict[str, dict[str, Any]]] = {
        "logistic_regression": {},
        "xgboost": {},
    }
    thresholds: dict[str, dict[str, float]] = {
        "logistic_regression": {},
        "xgboost": {},
    }
    validation_probabilities_by_model: dict[str, dict[str, np.ndarray]] = {
        "logistic_regression": {},
        "xgboost": {},
    }

    for spec in ABLATION_SPECS:
        classifiers = {
            "logistic_regression": LogisticRegression(
                class_weight="balanced",
                max_iter=1_000,
                random_state=seed,
            ),
            "xgboost": XGBClassifier(
                n_estimators=xgboost_estimators,
                max_depth=5,
                learning_rate=0.05,
                min_child_weight=2,
                subsample=0.8,
                colsample_bytree=0.8,
                scale_pos_weight=class_weight,
                eval_metric="logloss",
                tree_method="hist",
                random_state=seed,
                n_jobs=1,
            ),
        }
        for model_name, classifier in classifiers.items():
            pipeline = _fit_pipeline(classifier, split.train, spec)
            validation_probabilities = pipeline.predict_proba(
                split.validation[list(spec.feature_columns)]
            )[:, 1]
            threshold = choose_threshold(
                split.validation[TARGET_COLUMN],
                validation_probabilities,
            )
            validation_probabilities_by_model[model_name][spec.name] = (
                validation_probabilities
            )
            test_probabilities = pipeline.predict_proba(
                split.test[list(spec.feature_columns)]
            )[:, 1]
            probabilities[model_name][spec.name] = test_probabilities
            thresholds[model_name][spec.name] = threshold
            metrics[model_name][spec.name] = evaluate_predictions(
                labels,
                test_probabilities,
                threshold=threshold,
            )

    bootstrap = _bootstrap_pr_auc(
        labels,
        probabilities,
        samples=bootstrap_samples,
        seed=seed,
    )
    country_audit: list[dict[str, Any]] = []
    capacity_matched_metrics: dict[str, dict[str, dict[str, Any]]] = {
        "logistic_regression": {},
        "xgboost": {},
    }
    capacity_matched_country_audit: list[dict[str, Any]] = []
    capacity_reference: dict[str, dict[str, float]] = {}
    for model_name, model_metrics in metrics.items():
        full_pr_auc = model_metrics["full"]["pr_auc"]
        full_lift = model_metrics["full"]["pr_auc_lift"]
        full_bootstrap = bootstrap[model_name]["full"]
        full_validation_probabilities = validation_probabilities_by_model[model_name][
            "full"
        ]
        target_selection_rate = float(
            np.mean(full_validation_probabilities >= thresholds[model_name]["full"])
        )
        capacity_reference[model_name] = {
            "validation_selection_rate": round(target_selection_rate, 6),
            "full_model_f1_threshold": round(thresholds[model_name]["full"], 6),
        }
        for spec in ABLATION_SPECS:
            values = bootstrap[model_name][spec.name]
            result = model_metrics[spec.name]
            if bootstrap_samples:
                result["pr_auc_ci_95"] = _percentile_interval(values)
                result["delta_pr_auc_ci_95"] = _percentile_interval(
                    values - full_bootstrap
                )
            else:
                result["pr_auc_ci_95"] = [result["pr_auc"], result["pr_auc"]]
                delta = result["pr_auc"] - full_pr_auc
                result["delta_pr_auc_ci_95"] = [delta, delta]
            result["delta_pr_auc_vs_full"] = round(
                result["pr_auc"] - full_pr_auc,
                6,
            )
            result["lift_retained_fraction"] = (
                round(result["pr_auc_lift"] / full_lift, 6)
                if full_lift
                else None
            )
            for row in calculate_country_metrics(
                split.test,
                probabilities[model_name][spec.name],
                threshold=thresholds[model_name][spec.name],
            ):
                country_audit.append(
                    {
                        "model": model_name,
                        "ablation": spec.name,
                        **row,
                    }
                )
            capacity_threshold = (
                thresholds[model_name]["full"]
                if spec.name == "full"
                else choose_threshold_for_selection_rate(
                    validation_probabilities_by_model[model_name][spec.name],
                    target_selection_rate=target_selection_rate,
                )
            )
            capacity_matched_metrics[model_name][spec.name] = evaluate_predictions(
                labels,
                probabilities[model_name][spec.name],
                threshold=capacity_threshold,
            )
            for row in calculate_country_metrics(
                split.test,
                probabilities[model_name][spec.name],
                threshold=capacity_threshold,
            ):
                capacity_matched_country_audit.append(
                    {
                        "model": model_name,
                        "ablation": spec.name,
                        **row,
                    }
                )

    report = {
        "study": {
            "name": "country-and-vehicle-age-ablation",
            "random_seed": seed,
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_method": (
                "paired row resampling; percentile 95% confidence interval"
            ),
            "deployment_artifact_changed": False,
            "model_configuration": {
                "logistic_regression": {
                    "class_weight": "balanced",
                    "max_iter": 1_000,
                },
                "xgboost": {
                    "n_estimators": xgboost_estimators,
                    "max_depth": 5,
                    "learning_rate": 0.05,
                    "min_child_weight": 2,
                    "subsample": 0.8,
                    "colsample_bytree": 0.8,
                    "scale_pos_weight": round(class_weight, 6),
                    "tree_method": "hist",
                    "n_jobs": 1,
                },
            },
            "software_versions": {
                package: version(package)
                for package in (
                    "numpy",
                    "pandas",
                    "scikit-learn",
                    "xgboost",
                    "matplotlib",
                )
            },
        },
        "split": {
            "strategy": "chronological 70/15/15",
            "training_rows": len(split.train),
            "validation_rows": len(split.validation),
            "test_rows": len(split.test),
            "training_prevalence": round(
                float(split.train[TARGET_COLUMN].mean()),
                6,
            ),
            "validation_prevalence": round(
                float(split.validation[TARGET_COLUMN].mean()),
                6,
            ),
            "test_prevalence": round(float(labels.mean()), 6),
        },
        "feature_sets": {
            spec.name: {
                "label": spec.label,
                "dropped_features": list(spec.dropped_features),
                "retained_features": list(spec.feature_columns),
            }
            for spec in ABLATION_SPECS
        },
        "models": metrics,
        "country_audit": country_audit,
        "capacity_matching": {
            "method": (
                "threshold selected on validation probabilities to approximate "
                "the full model's validation selection rate"
            ),
            "reference": capacity_reference,
        },
        "capacity_matched_models": capacity_matched_metrics,
        "capacity_matched_country_audit": capacity_matched_country_audit,
    }
    return AblationRun(split=split, report=report, probabilities=probabilities)


def _write_summary_csv(report: dict[str, Any], output_path: Path) -> None:
    rows = []
    for model_name, model_results in report["models"].items():
        for spec_name, metrics in model_results.items():
            rows.append(
                {
                    "model": model_name,
                    "ablation": spec_name,
                    "dropped_features": ", ".join(
                        report["feature_sets"][spec_name]["dropped_features"]
                    ),
                    **{
                        key: value
                        for key, value in metrics.items()
                        if key != "confusion_matrix"
                    },
                }
            )
    pd.DataFrame(rows).to_csv(output_path, index=False)


def _write_capacity_summary_csv(report: dict[str, Any], output_path: Path) -> None:
    rows = []
    for model_name, model_results in report["capacity_matched_models"].items():
        for spec_name, metrics in model_results.items():
            rows.append(
                {
                    "model": model_name,
                    "ablation": spec_name,
                    "dropped_features": ", ".join(
                        report["feature_sets"][spec_name]["dropped_features"]
                    ),
                    **{
                        key: value
                        for key, value in metrics.items()
                        if key != "confusion_matrix"
                    },
                }
            )
    pd.DataFrame(rows).to_csv(output_path, index=False)


def _plot_pr_auc(report: dict[str, Any], output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    specs = list(ABLATION_SPECS)
    x_values = np.arange(len(specs))
    colours = {"logistic_regression": "#1f77b4", "xgboost": "#d95f02"}
    offsets = {"logistic_regression": -0.08, "xgboost": 0.08}
    figure, axis = plt.subplots(figsize=(12, 6.5))
    for model_name, results in report["models"].items():
        centres = []
        lower_errors = []
        upper_errors = []
        for spec in specs:
            metrics = results[spec.name]
            centre = metrics["pr_auc"]
            low, high = metrics["pr_auc_ci_95"]
            centres.append(centre)
            lower_errors.append(centre - low)
            upper_errors.append(high - centre)
        axis.errorbar(
            x_values + offsets[model_name],
            centres,
            yerr=[lower_errors, upper_errors],
            fmt="o",
            capsize=4,
            linewidth=1.6,
            label=model_name.replace("_", " ").title(),
            color=colours[model_name],
        )
    prevalence = report["split"]["test_prevalence"]
    axis.axhline(
        prevalence,
        linestyle="--",
        color="#555555",
        label=f"No-skill baseline ({prevalence:.3f})",
    )
    axis.set_xticks(x_values, [spec.label for spec in specs], rotation=22, ha="right")
    axis.set_ylabel("Test PR-AUC")
    axis.set_title("Country and vehicle-age ablation with paired-bootstrap intervals")
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def _plot_pr_curves(run: AblationRun, output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = run.split.test[TARGET_COLUMN].to_numpy(dtype=int)
    figure, axes = plt.subplots(1, 2, figsize=(13, 5.5), sharey=True)
    for axis, (model_name, model_probabilities) in zip(
        axes,
        run.probabilities.items(),
        strict=True,
    ):
        for spec in ABLATION_SPECS:
            precision, recall, _ = precision_recall_curve(
                labels,
                model_probabilities[spec.name],
            )
            pr_auc = run.report["models"][model_name][spec.name]["pr_auc"]
            axis.plot(
                recall,
                precision,
                linewidth=1.4,
                label=f"{spec.label} ({pr_auc:.3f})",
            )
        axis.axhline(
            run.report["split"]["test_prevalence"],
            linestyle="--",
            color="#555555",
            linewidth=1,
        )
        axis.set_title(model_name.replace("_", " ").title())
        axis.set_xlabel("Recall")
        axis.grid(alpha=0.2)
    axes[0].set_ylabel("Precision")
    axes[1].legend(fontsize=8, loc="upper right")
    figure.suptitle("Precision-recall curves across ablation conditions")
    figure.tight_layout()
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def _plot_country_errors(report: dict[str, Any], output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    audit = pd.DataFrame(report["capacity_matched_country_audit"])
    selected_ablations = (
        "full",
        "without_country",
        "without_country_and_market_frequency",
        "strict_claim_features",
    )
    audit = audit[
        audit["model"].eq("xgboost")
        & audit["ablation"].isin(selected_ablations)
    ]
    countries = sorted(audit["country"].unique())
    x_values = np.arange(len(countries))
    figure, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True)
    for ablation in selected_ablations:
        rows = audit[audit["ablation"].eq(ablation)].set_index("country")
        label = next(spec.label for spec in ABLATION_SPECS if spec.name == ablation)
        axes[0].plot(
            x_values,
            rows.loc[countries, "false_positive_rate"],
            marker="o",
            linewidth=1.3,
            label=label,
        )
        axes[1].plot(
            x_values,
            rows.loc[countries, "false_negative_rate"],
            marker="o",
            linewidth=1.3,
            label=label,
        )
    axes[0].set_ylabel("False-positive rate")
    axes[1].set_ylabel("False-negative rate")
    axes[1].set_xticks(x_values, countries, rotation=30, ha="right")
    for axis in axes:
        axis.set_ylim(0, 1)
        axis.grid(alpha=0.2)
    axes[0].legend(fontsize=8, ncol=2)
    figure.suptitle(
        "XGBoost country-level errors at matched validation review capacity"
    )
    figure.tight_layout()
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def save_ablation_run(
    run: AblationRun,
    output_dir: Path,
    *,
    dataset_path: Path,
) -> None:
    """Archive machine-readable results and dissertation-ready figures."""

    output_dir.mkdir(parents=True, exist_ok=True)
    run.report["study"]["generated_at"] = datetime.now(UTC).isoformat()
    run.report["study"]["dataset_path"] = str(dataset_path)
    run.report["study"]["dataset_sha256"] = hashlib.sha256(
        dataset_path.read_bytes()
    ).hexdigest()
    run.report["study"]["analysis_script_sha256"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    (output_dir / "ablation_results.json").write_text(
        json.dumps(run.report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_summary_csv(run.report, output_dir / "ablation_summary.csv")
    _write_capacity_summary_csv(
        run.report,
        output_dir / "capacity_matched_summary.csv",
    )
    pd.DataFrame(run.report["country_audit"]).to_csv(
        output_dir / "country_metrics.csv",
        index=False,
    )
    pd.DataFrame(run.report["capacity_matched_country_audit"]).to_csv(
        output_dir / "capacity_matched_country_metrics.csv",
        index=False,
    )
    _plot_pr_auc(run.report, output_dir / "pr_auc_ablation.png")
    _plot_pr_curves(run, output_dir / "precision_recall_curves.png")
    _plot_country_errors(run.report, output_dir / "country_error_rates.png")


def _print_summary(report: dict[str, Any]) -> None:
    rows = []
    for model_name, model_results in report["models"].items():
        for spec_name, metrics in model_results.items():
            rows.append(
                {
                    "model": model_name,
                    "ablation": spec_name,
                    "pr_auc": metrics["pr_auc"],
                    "95% CI": metrics["pr_auc_ci_95"],
                    "delta": metrics["delta_pr_auc_vs_full"],
                    "lift retained": metrics["lift_retained_fraction"],
                }
            )
    print(pd.DataFrame(rows).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("packages/model/data/african_motor_claims.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("packages/model/artifacts/country-vehicle-age-ablation"),
    )
    parser.add_argument("--xgboost-estimators", type=int, default=250)
    parser.add_argument("--bootstrap-samples", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    frame = pd.read_csv(args.data)
    run = run_ablation_study(
        frame,
        xgboost_estimators=args.xgboost_estimators,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )
    save_ablation_run(run, args.output_dir, dataset_path=args.data)
    _print_summary(run.report)
    print(f"\nArchived ablation evidence in {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
