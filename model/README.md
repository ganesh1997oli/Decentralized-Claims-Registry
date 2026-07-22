# Synthetic fraud model

This module provides a small, explainable scoring step for the end-to-end
prototype. It demonstrates how a versioned model can receive validated claim
fields, return a probability, explain the main contributing indicators, and
write a score back to the blockchain.

It is not a validated insurance-fraud model. All training rows are generated
locally from deterministic synthetic data.

## How scoring works

The model is a logistic-regression baseline using six values derived from the
claim form:

| Feature | What it represents |
| --- | --- |
| `amount_ratio` | Claim amount scaled to the demonstration range |
| `high_risk_type` | Whether the selected category is marked higher risk |
| `incident_age_ratio` | Time between the incident and scoring date |
| `no_evidence` | Whether the evidence list is empty |
| `short_description` | Whether the description has fewer than eight words |
| `suspicious_language` | Presence of a small demonstration phrase list |

The weighted values are converted to a probability with the sigmoid function.
The probability is then converted to an integer from `0` to `10,000` for the
smart contract. For example, `0.1479` becomes `1,479`, displayed as `14.79%`.

The three strongest positive feature contributions become the human-readable
reasons shown in the interface. These contributions are transparent
logistic-regression terms; they are not SHAP values.

## Train the demonstration artifact

Run from the repository root:

```bash
python -m model.train
```

Training uses a fixed random seed and generates 4,000 rows:

- 3,200 rows fit the logistic-regression weights;
- 800 held-out rows select a threshold using validation F1;
- the result is saved as readable JSON at
  `model/artifacts/synthetic-logistic-v1.json`.

Use a different output path when experimenting without replacing the tracked
artifact:

```bash
python -m model.train --output /tmp/claims-model.json
```

## Run the tests

The model uses the Python standard library; the shared backend environment
already contains pytest:

```bash
source backend/.venv/bin/activate
python -m pytest model/tests -q
```

The tests check feature extraction, artifact loading, repeatable scoring,
reasons, and threshold behaviour.

## Application behaviour

- A probability below the saved threshold becomes `UnderReview`.
- A probability at or above the threshold becomes `Flagged`.
- The model never automatically sets `Approved` or `Rejected`.
- FastAPI can load another compatible artifact through `FRAUD_MODEL_PATH`.

## What must change before research evaluation

The synthetic artifact proves the integration only. Meaningful evaluation would
require a justified real dataset, documented preprocessing, leakage-safe train
and validation splits, class-imbalance handling, appropriate precision/recall
and AUC-PR reporting, comparison with a baseline, and explanation analysis.

Do not present the current synthetic validation score as evidence of real-world
fraud-detection performance.

See the [backend guide](../backend/README.md) for where scoring enters the claim
workflow.
