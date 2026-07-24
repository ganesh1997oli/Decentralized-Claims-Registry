from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from model.research_pipeline import (
    FEATURE_COLUMNS,
    LEAKAGE_COLUMNS,
    create_shap_summary,
    prepare_claims,
    temporal_split,
    train_research_models,
)


def claims_frame(rows: int = 180) -> pd.DataFrame:
    start = date(2022, 1, 1)
    records = []
    for index in range(rows):
        fraud = int(index % 5 == 0)
        records.append(
            {
                "claim_id": f"claim-{index:04d}",
                "claim_date": (start + timedelta(days=index)).isoformat(),
                "fraud_flag": fraud,
                "vehicle_age": 14 if fraud else 5 + index % 4,
                "claim_amount_usd": 9_000 if fraud else 1_200 + index,
                "policy_premium_usd": 450 + index % 100,
                "claim_frequency_per_1000_policies": 80 if fraud else 35,
                "third_party_injury_flag": int(index % 7 == 0),
                "total_loss_flag": fraud,
                "country": "Kenya" if index % 2 else "Ghana",
                "vehicle_type": "suv" if fraud else "sedan",
                "claim_type": "theft" if fraud else "collision",
                "region_type": "urban" if index % 3 else "rural",
                "fraud_probability": 0.99 if fraud else 0.01,
            }
        )
    return pd.DataFrame.from_records(records)


def test_preparation_keeps_only_submission_time_features():
    prepared = prepare_claims(claims_frame())

    assert list(prepared.columns) == [
        "claim_id",
        "claim_date",
        "fraud_flag",
        *FEATURE_COLUMNS,
    ]
    assert not set(LEAKAGE_COLUMNS).intersection(prepared.columns)


def test_preparation_rejects_a_missing_required_field():
    frame = claims_frame().drop(columns=["vehicle_age"])

    with pytest.raises(ValueError, match="vehicle_age"):
        prepare_claims(frame)


def test_preparation_rejects_non_binary_fraud_labels():
    frame = claims_frame()
    frame["fraud_flag"] = frame["fraud_flag"].astype(float)
    frame.loc[0, "fraud_flag"] = 0.5

    with pytest.raises(ValueError, match="only contain 0 and 1"):
        prepare_claims(frame)


def test_temporal_split_keeps_the_latest_claims_for_testing():
    prepared = prepare_claims(claims_frame())
    split = temporal_split(prepared)

    assert split.train["claim_date"].max() < split.validation["claim_date"].min()
    assert split.validation["claim_date"].max() < split.test["claim_date"].min()
    assert len(split.train) == 125
    assert len(split.validation) == 27
    assert len(split.test) == 28


def test_xgboost_is_compared_with_a_baseline_on_untouched_rows():
    run = train_research_models(claims_frame(), xgboost_estimators=40)

    assert set(run.report) >= {
        "baseline_logistic_regression",
        "xgboost",
        "split",
    }
    for model_name in ("baseline_logistic_regression", "xgboost"):
        metrics = run.report[model_name]
        assert 0 <= metrics["pr_auc"] <= 1
        assert 0 <= metrics["f1"] <= 1
        assert len(metrics["confusion_matrix"]) == 2


def test_shap_summary_is_created_for_the_trained_xgboost_model(tmp_path: Path):
    run = train_research_models(claims_frame(), xgboost_estimators=20)
    output = tmp_path / "shap-summary.png"

    features = create_shap_summary(
        run.xgboost,
        run.split.test,
        output,
        sample_size=20,
    )

    assert output.exists()
    assert output.stat().st_size > 0
    assert features
    assert features[0]["mean_absolute_shap"] >= 0
