"""Train the small demonstration model with repeatable synthetic data."""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from model.scorer import DEFAULT_MODEL_PATH, FEATURES


SEED = 20_260_721
SAMPLE_COUNT = 4_000
TRUE_INTERCEPT = -3.0
TRUE_WEIGHTS = (1.2, 1.6, 0.7, 0.8, 0.5, 1.2)


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(value, 40), -40)))


def synthetic_rows() -> list[tuple[list[float], int]]:
    """Create repeatable test rows. These are not real insurance records."""

    # A fixed seed gives us the same rows and model every time we train.
    randomizer = random.Random(SEED)
    rows: list[tuple[list[float], int]] = []
    for _ in range(SAMPLE_COUNT):
        values = [
            randomizer.random(),
            float(randomizer.random() < 0.28),
            randomizer.random(),
            float(randomizer.random() < 0.58),
            float(randomizer.random() < 0.32),
            float(randomizer.random() < 0.12),
        ]
        log_odds = TRUE_INTERCEPT + sum(
            weight * value for weight, value in zip(TRUE_WEIGHTS, values, strict=True)
        )
        label = int(randomizer.random() < sigmoid(log_odds))
        rows.append((values, label))
    return rows


def fit(rows: list[tuple[list[float], int]]) -> tuple[float, list[float]]:
    intercept = 0.0
    weights = [0.0] * len(FEATURES)
    learning_rate = 0.8

    # This is plain gradient descent. Each pass makes a small correction to the
    # intercept and weights based on the prediction errors.
    for _ in range(2_000):
        intercept_gradient = 0.0
        weight_gradients = [0.0] * len(FEATURES)
        for values, label in rows:
            prediction = sigmoid(
                intercept
                + sum(
                    weight * value
                    for weight, value in zip(weights, values, strict=True)
                )
            )
            error = prediction - label
            intercept_gradient += error
            for index, value in enumerate(values):
                weight_gradients[index] += error * value

        scale = learning_rate / len(rows)
        intercept -= scale * intercept_gradient
        for index in range(len(weights)):
            weights[index] -= scale * weight_gradients[index]

    return intercept, weights


def f1_score(
    rows: list[tuple[list[float], int]],
    intercept: float,
    weights: list[float],
    threshold: float,
) -> float:
    true_positive = false_positive = false_negative = 0
    for values, label in rows:
        probability = sigmoid(
            intercept
            + sum(
                weight * value for weight, value in zip(weights, values, strict=True)
            )
        )
        prediction = probability >= threshold
        true_positive += int(prediction and label == 1)
        false_positive += int(prediction and label == 0)
        false_negative += int(not prediction and label == 1)
    denominator = 2 * true_positive + false_positive + false_negative
    return 2 * true_positive / denominator if denominator else 0.0


def train_artifact() -> dict[str, object]:
    rows = synthetic_rows()

    # Keep the last 800 rows out of training. We use them only to choose the
    # decision threshold, which avoids tuning it on rows the model just learned.
    training_rows, validation_rows = rows[:3_200], rows[3_200:]
    intercept, weights = fit(training_rows)
    thresholds = [value / 100 for value in range(30, 81, 5)]
    # Try a small set of thresholds and keep the one with the best validation
    # F1 score. This balances missed fraud and false alarms for the demo.
    threshold = max(
        thresholds,
        key=lambda candidate: f1_score(
            validation_rows, intercept, weights, candidate
        ),
    )

    return {
        "artifactSchema": 1,
        "version": "synthetic-logistic-v1",
        "modelType": "logistic_regression",
        "trainingData": "deterministic synthetic rows only",
        "seed": SEED,
        "trainingRows": len(training_rows),
        "validationRows": len(validation_rows),
        "validationF1": round(
            f1_score(validation_rows, intercept, weights, threshold), 6
        ),
        "features": list(FEATURES),
        "intercept": round(intercept, 8),
        "coefficients": {
            name: round(value, 8)
            for name, value in zip(FEATURES, weights, strict=True)
        },
        "threshold": threshold,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL_PATH)
    arguments = parser.parse_args()
    artifact = train_artifact()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {artifact['version']} to {arguments.output}")
    print(f"Validation F1: {artifact['validationF1']:.3f}")


if __name__ == "__main__":
    main()
