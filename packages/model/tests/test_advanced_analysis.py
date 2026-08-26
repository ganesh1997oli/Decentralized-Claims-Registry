from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from packages.model.advanced_analysis import (
    build_search_candidates,
    calculate_calibration,
    calculate_mutual_information,
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
                "region_type": "urban" if index % 3 else "rural",
            }
        )
    return pd.DataFrame.from_records(records)


def test_random_search_candidates_are_seeded_and_complete():
    first, space = build_search_candidates(
        class_weight=7.2,
        trials=24,
        seed=20260724,
    )
    second, _ = build_search_candidates(
        class_weight=7.2,
        trials=24,
        seed=20260724,
    )

    assert first == second
    assert len(first) == 24
    assert set(first[0]) == set(space)
    assert all(
        candidate["scale_pos_weight"] in space["scale_pos_weight"]
        for candidate in first
    )


def test_calibration_records_equal_count_bins_and_brier_components():
    labels = np.array([0, 0, 0, 0, 1, 0, 1, 1, 1, 1])
    probabilities = np.linspace(0.05, 0.95, 10)

    summary, rows = calculate_calibration(labels, probabilities, bins=5)

    assert len(rows) == 5
    assert sum(row["n"] for row in rows) == 10
    assert 0 <= summary["brier_score"] <= 1
    assert summary["uncertainty"] == 0.25


def test_mutual_information_keeps_all_features_and_is_seed_reproducible():
    frame = claims_frame()

    first = calculate_mutual_information(frame, seed=20260724)
    second = calculate_mutual_information(frame, seed=20260724)

    assert first == second
    assert len(first) == 10
    assert first[0]["mutual_information"] >= first[-1]["mutual_information"]
    assert {row["feature"] for row in first} == {
        "vehicle_age",
        "claim_amount_usd",
        "policy_premium_usd",
        "claim_frequency_per_1000_policies",
        "third_party_injury_flag",
        "total_loss_flag",
        "country",
        "vehicle_type",
        "claim_type",
        "region_type",
    }
