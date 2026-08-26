"""Reproduce the dissertation's advanced model-analysis evidence.

The originally cited extension script was not retained. This replacement is a
repository-controlled rerun: it declares the complete search space and seed,
recomputes every reported extension from the pinned dataset, and archives the
machine-readable results, trial manifest, uncertainty samples and figures.

It does not replace the deployed XGBoost artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import pandas as pd
from sklearn.feature_selection import mutual_info_classif
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from sklearn.model_selection import ParameterSampler
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

from packages.model.ablation_study import (
    calculate_country_metrics,
    evaluate_predictions,
)
from packages.model.research_pipeline import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    RANDOM_SEED,
    TARGET_COLUMN,
    DatasetSplit,
    build_preprocessor,
    choose_threshold,
    prepare_claims,
    temporal_split,
)

DEFAULT_DATASET = Path("packages/model/data/african_motor_claims.csv")
DEFAULT_OUTPUT = Path("packages/model/artifacts/advanced-analysis")
DEFAULT_TRIALS = 24
DEFAULT_BOOTSTRAP_SAMPLES = 2_000
DEFAULT_PERMUTATIONS = 100


@dataclass(frozen=True)
class AdvancedAnalysisRun:
    """Complete in-memory evidence needed to write the archive."""

    split: DatasetSplit
    report: dict[str, Any]
    probabilities: dict[str, np.ndarray]
    bootstrap_samples: dict[str, np.ndarray]


def _fit_pipeline(classifier: Any, training: pd.DataFrame) -> Pipeline:
    pipeline = Pipeline(
        [
            ("prepare", build_preprocessor()),
            ("classifier", classifier),
        ]
    )
    pipeline.fit(training[list(FEATURE_COLUMNS)], training[TARGET_COLUMN])
    return pipeline


def build_search_candidates(
    *,
    class_weight: float,
    trials: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
    """Return a deterministic, fully declared random-search candidate set."""

    if trials <= 0:
        raise ValueError("trials must be greater than zero")
    search_space: dict[str, list[Any]] = {
        "n_estimators": [150, 200, 250, 300, 350, 400],
        "max_depth": [3, 4, 5, 6, 7],
        "learning_rate": [0.025, 0.03387, 0.05, 0.075, 0.1],
        "min_child_weight": [1, 2, 4, 6],
        "subsample": [0.65, 0.75, 0.8, 0.9, 1.0],
        "colsample_bytree": [0.65, 0.75, 0.8, 0.9, 1.0],
        "reg_lambda": [0.5, 1.0, 2.0, 5.0, 10.0],
        "scale_pos_weight": [
            class_weight * 0.75,
            class_weight,
            class_weight * 1.25,
            class_weight * 1.5,
        ],
    }
    sampled = list(
        ParameterSampler(
            search_space,
            n_iter=trials,
            random_state=seed,
        )
    )
    candidates = []
    for values in sampled:
        candidates.append(
            {
                "n_estimators": int(values["n_estimators"]),
                "max_depth": int(values["max_depth"]),
                "learning_rate": float(values["learning_rate"]),
                "min_child_weight": int(values["min_child_weight"]),
                "subsample": float(values["subsample"]),
                "colsample_bytree": float(values["colsample_bytree"]),
                "reg_lambda": float(values["reg_lambda"]),
                "scale_pos_weight": float(values["scale_pos_weight"]),
            }
        )
    serializable_space = {
        name: [
            int(value) if isinstance(value, (int, np.integer)) else float(value)
            for value in values
        ]
        for name, values in search_space.items()
    }
    return candidates, serializable_space


def _train_base_models(
    split: DatasetSplit,
    *,
    class_weight: float,
    seed: int,
) -> tuple[Pipeline, Pipeline]:
    baseline = _fit_pipeline(
        LogisticRegression(
            class_weight="balanced",
            max_iter=1_000,
            random_state=seed,
        ),
        split.train,
    )
    deployed = _fit_pipeline(
        XGBClassifier(
            n_estimators=250,
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
        split.train,
    )
    return baseline, deployed


def _run_random_search(
    split: DatasetSplit,
    *,
    candidates: list[dict[str, Any]],
    seed: int,
) -> tuple[Pipeline, float, list[dict[str, Any]], int]:
    validation_features = split.validation[list(FEATURE_COLUMNS)]
    validation_labels = split.validation[TARGET_COLUMN]
    best_model: Pipeline | None = None
    best_score = float("-inf")
    best_threshold = 0.5
    best_index = -1
    trial_rows: list[dict[str, Any]] = []

    for trial_index, parameters in enumerate(candidates):
        started = perf_counter()
        model = _fit_pipeline(
            XGBClassifier(
                **parameters,
                eval_metric="logloss",
                tree_method="hist",
                random_state=seed,
                n_jobs=1,
            ),
            split.train,
        )
        probabilities = model.predict_proba(validation_features)[:, 1]
        score = float(average_precision_score(validation_labels, probabilities))
        threshold = choose_threshold(validation_labels, probabilities)
        trial_rows.append(
            {
                "trial": trial_index + 1,
                "validation_pr_auc": round(score, 9),
                "validation_threshold": round(threshold, 6),
                "elapsed_seconds": round(perf_counter() - started, 6),
                **{
                    name: round(value, 9) if isinstance(value, float) else value
                    for name, value in parameters.items()
                },
            }
        )
        if score > best_score:
            best_model = model
            best_score = score
            best_threshold = threshold
            best_index = trial_index

    if best_model is None:
        raise RuntimeError("random search did not produce a model")
    return best_model, best_threshold, trial_rows, best_index


def _paired_bootstrap(
    labels: np.ndarray,
    probabilities: dict[str, np.ndarray],
    *,
    samples: int,
    seed: int,
) -> dict[str, np.ndarray]:
    if samples < 0:
        raise ValueError("bootstrap samples cannot be negative")
    distributions = {
        model_name: np.full(samples, np.nan, dtype=float)
        for model_name in probabilities
    }
    rng = np.random.default_rng(seed)
    for sample_index in range(samples):
        indices = rng.integers(0, len(labels), size=len(labels))
        sampled_labels = labels[indices]
        if np.unique(sampled_labels).size != 2:
            continue
        for model_name, values in probabilities.items():
            distributions[model_name][sample_index] = average_precision_score(
                sampled_labels,
                values[indices],
            )
    return distributions


def _interval(values: np.ndarray) -> list[float]:
    finite = values[np.isfinite(values)]
    if not len(finite):
        return []
    return [round(float(value), 6) for value in np.percentile(finite, [2.5, 97.5])]


def _permutation_diagnostic(
    split: DatasetSplit,
    baseline: Pipeline,
    *,
    permutations: int,
    seed: int,
) -> tuple[dict[str, Any], np.ndarray]:
    if permutations <= 0:
        raise ValueError("permutations must be greater than zero")

    preprocessor = baseline.named_steps["prepare"]
    training_features = preprocessor.transform(split.train[list(FEATURE_COLUMNS)])
    test_features = preprocessor.transform(split.test[list(FEATURE_COLUMNS)])
    training_labels = split.train[TARGET_COLUMN].to_numpy(dtype=int)
    test_labels = split.test[TARGET_COLUMN].to_numpy(dtype=int)
    observed_probabilities = baseline.predict_proba(
        split.test[list(FEATURE_COLUMNS)]
    )[:, 1]
    observed = float(average_precision_score(test_labels, observed_probabilities))

    rng = np.random.default_rng(seed)
    scores = np.empty(permutations, dtype=float)
    for index in range(permutations):
        permuted_labels = rng.permutation(training_labels)
        model = LogisticRegression(
            class_weight="balanced",
            max_iter=1_000,
            random_state=seed + index + 1,
        )
        model.fit(training_features, permuted_labels)
        probabilities = model.predict_proba(test_features)[:, 1]
        scores[index] = average_precision_score(test_labels, probabilities)

    exceedances = int(np.sum(scores >= observed))
    summary = {
        "method": "independently permuted training labels; test labels retained",
        "permutations": permutations,
        "observed_pr_auc": round(observed, 6),
        "null_mean": round(float(scores.mean()), 6),
        "null_interval_95": _interval(scores),
        "null_min": round(float(scores.min()), 6),
        "null_max": round(float(scores.max()), 6),
        "exceedances": exceedances,
        "empirical_p_value": round((exceedances + 1) / (permutations + 1), 6),
        "raw_scores": [round(float(value), 9) for value in scores],
    }
    return summary, scores


def calculate_mutual_information(
    training: pd.DataFrame,
    *,
    seed: int,
) -> list[dict[str, Any]]:
    """Estimate univariate MI jointly so numeric jitter is seed-reproducible."""

    features = training[list(FEATURE_COLUMNS)].copy()
    discrete = []
    for feature in FEATURE_COLUMNS:
        if feature in CATEGORICAL_FEATURES:
            features[feature] = pd.factorize(features[feature], sort=True)[0]
            discrete.append(True)
        else:
            discrete.append(feature in {"third_party_injury_flag", "total_loss_flag"})
    values = mutual_info_classif(
        features,
        training[TARGET_COLUMN],
        discrete_features=discrete,
        random_state=seed,
    )
    rows = [
        {
            "feature": feature,
            "mutual_information": round(float(value), 9),
            "treated_as_discrete": bool(discrete[index]),
        }
        for index, (feature, value) in enumerate(zip(FEATURE_COLUMNS, values, strict=True))
    ]
    return sorted(rows, key=lambda row: row["mutual_information"], reverse=True)


def calculate_calibration(
    labels: np.ndarray,
    probabilities: np.ndarray,
    *,
    bins: int = 10,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return quantile-bin Brier decomposition and reliability points."""

    frame = pd.DataFrame(
        {
            "label": np.asarray(labels, dtype=int),
            "probability": np.asarray(probabilities, dtype=float),
        }
    )
    frame["bin"] = pd.qcut(
        frame["probability"],
        q=bins,
        labels=False,
        duplicates="drop",
    )
    prevalence = float(frame["label"].mean())
    reliability = 0.0
    resolution = 0.0
    rows: list[dict[str, Any]] = []
    for bin_number, group in frame.groupby("bin", sort=True, observed=True):
        weight = len(group) / len(frame)
        mean_probability = float(group["probability"].mean())
        observed_rate = float(group["label"].mean())
        reliability += weight * (mean_probability - observed_rate) ** 2
        resolution += weight * (observed_rate - prevalence) ** 2
        rows.append(
            {
                "bin": int(bin_number) + 1,
                "n": len(group),
                "mean_probability": round(mean_probability, 9),
                "observed_rate": round(observed_rate, 9),
                "probability_min": round(float(group["probability"].min()), 9),
                "probability_max": round(float(group["probability"].max()), 9),
            }
        )
    uncertainty = prevalence * (1 - prevalence)
    brier = float(brier_score_loss(frame["label"], frame["probability"]))
    summary = {
        "bins_requested": bins,
        "bins_retained": len(rows),
        "brier_score": round(brier, 6),
        "reliability": round(reliability, 6),
        "resolution": round(resolution, 6),
        "uncertainty": round(uncertainty, 6),
        "decomposition_approximation": round(reliability - resolution + uncertainty, 6),
    }
    return summary, rows


def _country_audit(
    split: DatasetSplit,
    probabilities: np.ndarray,
    *,
    threshold: float,
) -> list[dict[str, Any]]:
    rows = calculate_country_metrics(
        split.test,
        probabilities,
        threshold=threshold,
    )
    for row in rows:
        false_negative_rate = row["false_negative_rate"]
        row["recall"] = (
            round(1 - false_negative_rate, 6)
            if false_negative_rate is not None
            else None
        )
    return rows


def run_advanced_analysis(
    frame: pd.DataFrame,
    *,
    trials: int = DEFAULT_TRIALS,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    permutations: int = DEFAULT_PERMUTATIONS,
    seed: int = RANDOM_SEED,
) -> AdvancedAnalysisRun:
    """Execute the complete replacement analysis on one chronological split."""

    prepared = prepare_claims(frame)
    split = temporal_split(prepared)
    fraud_count = int(split.train[TARGET_COLUMN].sum())
    non_fraud_count = len(split.train) - fraud_count
    class_weight = non_fraud_count / fraud_count

    baseline, deployed = _train_base_models(
        split,
        class_weight=class_weight,
        seed=seed,
    )
    candidates, search_space = build_search_candidates(
        class_weight=class_weight,
        trials=trials,
        seed=seed,
    )
    tuned, tuned_threshold, search_trials, selected_index = _run_random_search(
        split,
        candidates=candidates,
        seed=seed,
    )

    validation_features = split.validation[list(FEATURE_COLUMNS)]
    thresholds = {
        "logistic_regression": choose_threshold(
            split.validation[TARGET_COLUMN],
            baseline.predict_proba(validation_features)[:, 1],
        ),
        "deployed_xgboost": choose_threshold(
            split.validation[TARGET_COLUMN],
            deployed.predict_proba(validation_features)[:, 1],
        ),
        "tuned_xgboost": tuned_threshold,
    }
    models = {
        "logistic_regression": baseline,
        "deployed_xgboost": deployed,
        "tuned_xgboost": tuned,
    }
    test_labels = split.test[TARGET_COLUMN].to_numpy(dtype=int)
    probabilities = {
        name: model.predict_proba(split.test[list(FEATURE_COLUMNS)])[:, 1]
        for name, model in models.items()
    }
    model_metrics = {
        name: evaluate_predictions(
            test_labels,
            probabilities[name],
            threshold=thresholds[name],
        )
        for name in models
    }

    bootstrap = _paired_bootstrap(
        test_labels,
        probabilities,
        samples=bootstrap_samples,
        seed=seed,
    )
    for name, distribution in bootstrap.items():
        model_metrics[name]["pr_auc_ci_95"] = _interval(distribution)
    comparisons = {}
    for name, difference in {
        "logistic_minus_deployed": (
            bootstrap["logistic_regression"] - bootstrap["deployed_xgboost"]
        ),
        "logistic_minus_tuned": (
            bootstrap["logistic_regression"] - bootstrap["tuned_xgboost"]
        ),
        "tuned_minus_deployed": (
            bootstrap["tuned_xgboost"] - bootstrap["deployed_xgboost"]
        ),
    }.items():
        comparisons[name] = {
            "point_difference": round(
                {
                    "logistic_minus_deployed": model_metrics["logistic_regression"]["pr_auc"]
                    - model_metrics["deployed_xgboost"]["pr_auc"],
                    "logistic_minus_tuned": model_metrics["logistic_regression"]["pr_auc"]
                    - model_metrics["tuned_xgboost"]["pr_auc"],
                    "tuned_minus_deployed": model_metrics["tuned_xgboost"]["pr_auc"]
                    - model_metrics["deployed_xgboost"]["pr_auc"],
                }[name],
                6,
            ),
            "interval_95": _interval(difference),
            "raw_samples": [round(float(value), 9) for value in difference],
        }

    permutation_summary, _permutation_scores = _permutation_diagnostic(
        split,
        baseline,
        permutations=permutations,
        seed=seed,
    )
    mutual_information = calculate_mutual_information(split.train, seed=seed)

    calibration = {}
    calibration_rows = []
    for name, values in probabilities.items():
        summary, rows = calculate_calibration(test_labels, values)
        calibration[name] = summary
        calibration_rows.extend({"model": name, **row} for row in rows)

    report = {
        "study": {
            "name": "advanced-model-analysis-repository-rerun",
            "provenance_note": (
                "Replacement implementation created after the original cited script "
                "and JSON were found absent; this is not the recovered historical file."
            ),
            "random_seed": seed,
            "random_search_trials": trials,
            "bootstrap_samples": bootstrap_samples,
            "permutations": permutations,
            "test_set_used_for_search": False,
            "deployment_artifact_changed": False,
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
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "split": {
            "strategy": "chronological 70/15/15",
            "training_rows": len(split.train),
            "validation_rows": len(split.validation),
            "test_rows": len(split.test),
            "training_prevalence": round(float(split.train[TARGET_COLUMN].mean()), 6),
            "validation_prevalence": round(
                float(split.validation[TARGET_COLUMN].mean()),
                6,
            ),
            "test_prevalence": round(float(test_labels.mean()), 6),
        },
        "random_search": {
            "selection_metric": "validation PR-AUC",
            "threshold_rule": "maximum validation fraud-class F1",
            "search_space": search_space,
            "selected_trial": selected_index + 1,
            "selected_parameters": candidates[selected_index],
            "trials": search_trials,
        },
        "models": model_metrics,
        "paired_bootstrap": {
            "method": "paired test-row resampling; percentile 95% interval",
            "comparisons": comparisons,
            "model_raw_samples": {
                name: [round(float(value), 9) for value in distribution]
                for name, distribution in bootstrap.items()
            },
        },
        "permutation_diagnostic": permutation_summary,
        "mutual_information": mutual_information,
        "country_audit": _country_audit(
            split,
            probabilities["deployed_xgboost"],
            threshold=thresholds["deployed_xgboost"],
        ),
        "calibration": calibration,
        "calibration_bins": calibration_rows,
    }
    return AdvancedAnalysisRun(
        split=split,
        report=report,
        probabilities=probabilities,
        bootstrap_samples=bootstrap,
    )


def _plot_bootstrap(run: AdvancedAnalysisRun, output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    comparisons = run.report["paired_bootstrap"]["comparisons"]
    figure, axis = plt.subplots(figsize=(9, 5.4))
    colours = {
        "logistic_minus_deployed": "#1f77b4",
        "logistic_minus_tuned": "#d95f02",
    }
    labels = {
        "logistic_minus_deployed": "Logistic - deployed XGBoost",
        "logistic_minus_tuned": "Logistic - tuned XGBoost",
    }
    for name, colour in colours.items():
        axis.hist(
            comparisons[name]["raw_samples"],
            bins=40,
            alpha=0.52,
            color=colour,
            label=labels[name],
        )
    axis.axvline(0, color="#7A1F1F", linestyle="--", linewidth=1.4)
    axis.set_xlabel("Paired test-set PR-AUC difference")
    axis.set_ylabel("Bootstrap samples")
    axis.set_title("Paired-bootstrap uncertainty in model differences")
    axis.grid(axis="y", alpha=0.2)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def _plot_permutation(run: AdvancedAnalysisRun, output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    result = run.report["permutation_diagnostic"]
    figure, axis = plt.subplots(figsize=(9, 5.4))
    axis.hist(
        result["raw_scores"],
        bins=22,
        color="#6C8EBF",
        edgecolor="white",
        alpha=0.9,
    )
    axis.axvline(
        result["observed_pr_auc"],
        color="#B22222",
        linewidth=2,
        label=f"Observed PR-AUC {result['observed_pr_auc']:.4f}",
    )
    axis.axvline(
        run.report["split"]["test_prevalence"],
        color="#555555",
        linestyle="--",
        linewidth=1.3,
        label="Test prevalence",
    )
    axis.set_xlabel("Test PR-AUC")
    axis.set_ylabel("Permuted-label fits")
    axis.set_title("Logistic-regression label-permutation diagnostic")
    axis.grid(axis="y", alpha=0.2)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def _plot_reliability(run: AdvancedAnalysisRun, output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = pd.DataFrame(run.report["calibration_bins"])
    figure, axis = plt.subplots(figsize=(8.4, 6.4))
    colours = {
        "logistic_regression": "#1f77b4",
        "deployed_xgboost": "#2ca02c",
        "tuned_xgboost": "#d95f02",
    }
    for model_name, group in rows.groupby("model", sort=False):
        axis.plot(
            group["mean_probability"],
            group["observed_rate"],
            marker="o",
            linewidth=1.6,
            color=colours[model_name],
            label=model_name.replace("_", " ").title(),
        )
    axis.plot([0, 1], [0, 1], linestyle="--", color="#555555", label="Ideal")
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.set_xlabel("Mean predicted probability")
    axis.set_ylabel("Observed fraud-label rate")
    axis.set_title("Reliability diagram on the chronological test period")
    axis.grid(alpha=0.2)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def _write_readme(report: dict[str, Any], output_path: Path) -> None:
    selected = report["random_search"]["selected_parameters"]
    models = report["models"]
    permutation = report["permutation_diagnostic"]
    text = f"""# Advanced model-analysis evidence

This directory is the repository-controlled replacement for the advanced
analysis script and JSON that were missing during the dissertation audit. The
historical file was not recovered. Instead, the analysis was reimplemented and
rerun from the pinned dataset with a fully declared seed, search space, trial
manifest and software environment.

## Controls

- Seed: `{report['study']['random_seed']}`
- Split: chronological 70/15/15
- Random search: {report['study']['random_search_trials']} seeded configurations,
  selected by validation PR-AUC
- Threshold: selected on validation fraud-class F1
- Bootstrap: {report['study']['bootstrap_samples']} paired test-row resamples
- Permutations: {report['study']['permutations']} independent training-label permutations
- Test rows: {report['split']['test_rows']:,}
- Test prevalence: {report['split']['test_prevalence']:.6f}
- Deployed artifact changed: no

## Rerun result

The selected XGBoost configuration was `{json.dumps(selected, sort_keys=True)}`.
Its test PR-AUC was {models['tuned_xgboost']['pr_auc']:.6f}, compared with
{models['logistic_regression']['pr_auc']:.6f} for logistic regression and
{models['deployed_xgboost']['pr_auc']:.6f} for the deployed XGBoost model. The
paired interval for logistic minus tuned XGBoost was
{report['paired_bootstrap']['comparisons']['logistic_minus_tuned']['interval_95']}.

The permutation null mean was {permutation['null_mean']:.6f}, with empirical
one-sided p={permutation['empirical_p_value']:.6f}. These values demonstrate
detectable signal inside the synthetic generator, not field validity.

## Archive contents

- `advanced_analysis_results.json`: complete configuration and raw results
- `random_search_trials.csv`: every evaluated candidate and validation result
- `bootstrap_summary.csv`: model and paired-comparison intervals
- `permutation_scores.csv`: all permuted-label PR-AUC scores
- `mutual_information.csv`: training-period feature diagnostics
- `country_metrics.csv`: deployed-model country audit
- `calibration_bins.csv`: ten-bin reliability points
- `bootstrap_differences.png`: paired PR-AUC difference distributions
- `permutation_diagnostic.png`: observed score against the permutation null
- `reliability_diagram.png`: quantile-bin calibration curves
- `execution_manifest.json`: command, versions, hashes and archive metadata
- `checksums.sha256`: SHA-256 digest for each retained output except itself

Reproduce from the repository root:

```bash
python -m packages.model.advanced_analysis \\
  --data packages/model/data/african_motor_claims.csv \\
  --output-dir packages/model/artifacts/advanced-analysis \\
  --trials {report['study']['random_search_trials']} \\
  --bootstrap-samples {report['study']['bootstrap_samples']} \\
  --permutations {report['study']['permutations']} \\
  --seed {report['study']['random_seed']}
```
"""
    output_path.write_text(text, encoding="utf-8")


def _write_csvs(report: dict[str, Any], output_dir: Path) -> None:
    pd.DataFrame(report["random_search"]["trials"]).to_csv(
        output_dir / "random_search_trials.csv",
        index=False,
    )
    bootstrap_rows = []
    for name, metrics in report["models"].items():
        bootstrap_rows.append(
            {
                "estimate": name,
                "point": metrics["pr_auc"],
                "interval_low": metrics["pr_auc_ci_95"][0],
                "interval_high": metrics["pr_auc_ci_95"][1],
            }
        )
    for name, result in report["paired_bootstrap"]["comparisons"].items():
        bootstrap_rows.append(
            {
                "estimate": name,
                "point": result["point_difference"],
                "interval_low": result["interval_95"][0],
                "interval_high": result["interval_95"][1],
            }
        )
    pd.DataFrame(bootstrap_rows).to_csv(
        output_dir / "bootstrap_summary.csv",
        index=False,
    )
    pd.DataFrame(
        {
            "permutation": np.arange(
                1,
                report["permutation_diagnostic"]["permutations"] + 1,
            ),
            "test_pr_auc": report["permutation_diagnostic"]["raw_scores"],
        }
    ).to_csv(output_dir / "permutation_scores.csv", index=False)
    pd.DataFrame(report["mutual_information"]).to_csv(
        output_dir / "mutual_information.csv",
        index=False,
    )
    pd.DataFrame(report["country_audit"]).to_csv(
        output_dir / "country_metrics.csv",
        index=False,
    )
    pd.DataFrame(report["calibration_bins"]).to_csv(
        output_dir / "calibration_bins.csv",
        index=False,
    )


def save_advanced_analysis(
    run: AdvancedAnalysisRun,
    output_dir: Path,
    *,
    dataset_path: Path,
    command: list[str],
    elapsed_seconds: float,
) -> None:
    """Write a self-describing repository evidence bundle."""

    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_digest = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    script_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    generated_at = datetime.now(UTC).isoformat()
    run.report["study"].update(
        {
            "generated_at": generated_at,
            "dataset_path": str(dataset_path),
            "dataset_sha256": dataset_digest,
            "analysis_script": "packages/model/advanced_analysis.py",
            "analysis_script_sha256": script_digest,
            "elapsed_seconds": round(elapsed_seconds, 6),
        }
    )
    results_path = output_dir / "advanced_analysis_results.json"
    results_path.write_text(
        json.dumps(run.report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_csvs(run.report, output_dir)
    _plot_bootstrap(run, output_dir / "bootstrap_differences.png")
    _plot_permutation(run, output_dir / "permutation_diagnostic.png")
    _plot_reliability(run, output_dir / "reliability_diagram.png")
    _write_readme(run.report, output_dir / "README.md")

    manifest = {
        "archive_schema": 1,
        "generated_at": generated_at,
        "command": command,
        "working_directory": "repository root",
        "dataset_sha256": dataset_digest,
        "analysis_script_sha256": script_digest,
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "software_versions": run.report["study"]["software_versions"],
        "elapsed_seconds": round(elapsed_seconds, 6),
        "provenance_note": run.report["study"]["provenance_note"],
    }
    (output_dir / "execution_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    files = sorted(path for path in output_dir.iterdir() if path.is_file())
    checksum_lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in files
        if path.name != "checksums.sha256"
    ]
    (output_dir / "checksums.sha256").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )


def _print_summary(report: dict[str, Any]) -> None:
    rows = []
    for name, metrics in report["models"].items():
        rows.append(
            {
                "model": name,
                "threshold": metrics["threshold"],
                "pr_auc": metrics["pr_auc"],
                "pr_auc_ci_95": metrics["pr_auc_ci_95"],
                "roc_auc": metrics["roc_auc"],
                "f1": metrics["f1"],
                "brier": metrics["brier_score"],
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nSelected trial:", report["random_search"]["selected_trial"])
    print("Selected parameters:", report["random_search"]["selected_parameters"])
    print("Permutation:", report["permutation_diagnostic"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
    )
    parser.add_argument("--permutations", type=int, default=DEFAULT_PERMUTATIONS)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    started = perf_counter()
    frame = pd.read_csv(args.data)
    run = run_advanced_analysis(
        frame,
        trials=args.trials,
        bootstrap_samples=args.bootstrap_samples,
        permutations=args.permutations,
        seed=args.seed,
    )
    command = [
        "python",
        "-m",
        "packages.model.advanced_analysis",
        "--data",
        str(args.data),
        "--output-dir",
        str(args.output_dir),
        "--trials",
        str(args.trials),
        "--bootstrap-samples",
        str(args.bootstrap_samples),
        "--permutations",
        str(args.permutations),
        "--seed",
        str(args.seed),
    ]
    save_advanced_analysis(
        run,
        args.output_dir,
        dataset_path=args.data,
        command=command,
        elapsed_seconds=perf_counter() - started,
    )
    _print_summary(run.report)
    print(f"\nArchived advanced analysis in {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
