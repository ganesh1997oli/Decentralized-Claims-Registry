from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from packages.model.ablation_study import (
    ABLATION_SPECS,
    build_ablation_preprocessor,
    calculate_country_metrics,
    choose_threshold_for_selection_rate,
    evaluate_predictions,
)


def claims_frame(rows: int = 120) -> pd.DataFrame:
    start = date(2024, 1, 1)
    records = []
    for index in range(rows):
        fraud = int(index % 5 == 0)
        records.append(
            {
                "claim_id": f"claim-{index:04d}",
                "claim_date": start + timedelta(days=index),
                "fraud_flag": fraud,
                "vehicle_age": 14 if fraud else 6,
                "claim_amount_usd": 8_000 if fraud else 2_000,
                "policy_premium_usd": 600,
                "claim_frequency_per_1000_policies": 70 if index % 2 else 30,
                "third_party_injury_flag": 0,
                "total_loss_flag": fraud,
                "country": "Nigeria" if index % 2 else "Rwanda",
                "vehicle_type": "suv" if fraud else "sedan",
                "claim_type": "theft" if fraud else "collision",
                "region_type": "urban",
            }
        )
    return pd.DataFrame.from_records(records)


def test_ablation_matrix_covers_direct_and_proxy_removals():
    by_name = {spec.name: spec for spec in ABLATION_SPECS}

    assert set(by_name) == {
        "full",
        "without_country",
        "without_vehicle_age",
        "without_country_and_vehicle_age",
        "without_country_and_market_frequency",
        "strict_claim_features",
    }
    assert by_name["without_country"].dropped_features == ("country",)
    assert set(by_name["strict_claim_features"].dropped_features) == {
        "country",
        "vehicle_age",
        "claim_frequency_per_1000_policies",
    }


def test_preprocessor_excludes_every_dropped_feature():
    spec = next(spec for spec in ABLATION_SPECS if spec.name == "strict_claim_features")
    preprocessor = build_ablation_preprocessor(spec)
    selected = {
        feature
        for _name, _transformer, features in preprocessor.transformers
        for feature in features
    }

    assert not selected.intersection(spec.dropped_features)
    assert selected == set(spec.feature_columns)


def test_prediction_metrics_use_test_prevalence_as_the_no_skill_baseline():
    labels = np.array([0, 0, 0, 1, 1])
    probabilities = np.array([0.1, 0.2, 0.3, 0.8, 0.9])
    metrics = evaluate_predictions(labels, probabilities, threshold=0.5)

    assert metrics["prevalence"] == 0.4
    assert metrics["selection_rate"] == 0.4
    assert metrics["pr_auc"] == 1.0
    assert metrics["pr_auc_lift"] == 0.6


def test_capacity_threshold_matches_the_reference_selection_rate():
    probabilities = np.array([0.1, 0.2, 0.3, 0.4])

    threshold = choose_threshold_for_selection_rate(
        probabilities,
        target_selection_rate=0.5,
    )

    assert threshold == 0.3
    assert np.mean(probabilities >= threshold) == 0.5


def test_country_audit_keeps_country_for_evaluation_when_not_used_for_training():
    frame = claims_frame(20)
    probabilities = np.where(frame["country"].eq("Nigeria"), 0.9, 0.1)
    rows = calculate_country_metrics(frame, probabilities, threshold=0.5)
    by_country = {row["country"]: row for row in rows}

    assert set(by_country) == {"Nigeria", "Rwanda"}
    assert by_country["Nigeria"]["selection_rate"] == 1.0
    assert by_country["Rwanda"]["selection_rate"] == 0.0
    assert by_country["Nigeria"]["n"] == 10
