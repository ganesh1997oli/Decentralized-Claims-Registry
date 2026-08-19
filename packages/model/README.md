# Fraud-screening model

This package has two paths: a reproducible research training pipeline and a
small serving adapter used by the Kafka worker. Only the reviewed XGBoost
artifact is served; logistic regression remains an evaluation baseline.

> The dataset is synthetic. The result is a research integration signal, not a
> real fraud finding or claim decision.

## Quick mental model

This package is both a **reproducible experiment** and a **small runtime
adapter**. Training decides what artifact is acceptable; serving loads only
that reviewed artifact and turns one verified claim into a bounded review
signal.

| Path | Input | Output | Safety rule |
| --- | --- | --- | --- |
| Training | Pinned synthetic dataset revision and leakage-safe feature list | Model, metadata, checksum, threshold, metrics and global SHAP plot | Test period is untouched until final evaluation |
| Serving | One verified claim plus reviewed metadata | Probability, basis-point score, flag and five local SHAP reasons | Artifact checksum and feature contract must match |

The model can move a claim to `UnderReview` or `Flagged`; it cannot approve,
reject, or write a human fraud label. A SHAP reason describes the model's
calculation, not the cause of an incident.

## Training path

```mermaid
flowchart LR
    Download["Pinned synthetic CSV"] -->|"verify SHA-256"| Prepare["Submission-time fields only"]
    Prepare --> Split["Chronological 70 / 15 / 15 split"]
    Split --> Baseline["Logistic baseline"]
    Split --> XGB["XGBoost"]
    Baseline --> Compare["Untouched test metrics"]
    XGB --> Compare
    XGB --> SHAP["Global SHAP summary"]
    Compare --> Artifact["model.joblib + metadata.json"]
    SHAP --> Artifact
```

The threshold is selected on validation F1, never on the test period. The first
complete run found that XGBoost did **not** outperform the simpler baseline on
PR-AUC or fraud F1; that honest negative result is recorded in
[initial research result](#initial-xgboost-research-result).

## Model inputs

| Numeric | Categorical |
| --- | --- |
| Vehicle age | Country |
| Claim amount | Vehicle type |
| Policy premium | Claim type |
| Market claim frequency | Urban or rural region |
| Third-party injury flag | |
| Total-loss flag | |

Settlement amounts, processing duration, generated fraud probability, loss
ratio, and scenario are excluded because they happen after submission or reveal
how the synthetic label was generated.

## Train the artifact

From the repository root:

```bash
apps/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-dev.lock
apps/backend/.venv/bin/python -m packages.model.train_xgboost --download
```

On macOS, install OpenMP once if XGBoost cannot find it:

```bash
brew install libomp
```

Generated files are ignored by Git and written under:

```text
packages/model/artifacts/xgboost-african-motor-v1/
├── model.joblib       fitted preprocessing + XGBoost pipeline
├── metadata.json      checksum, features, threshold, metrics and references
└── shap-summary.png   global research explanation
```

The downloader pins both the dataset revision and expected digest. The serving
adapter verifies `model.joblib` before loading it; joblib files must never come
from a user or untrusted storage because deserialization can execute code.

## Serving path

```mermaid
flowchart LR
    Claim["Verified schema-v5 or v6 claim"] --> Enrich["Add reviewed country frequency"]
    Enrich --> Frame["Ordered one-row feature frame"]
    Frame --> Pipeline["Saved preprocessing + XGBoost"]
    Pipeline --> Probability["Probability 0..1"]
    Pipeline --> LocalSHAP["Five strongest signed SHAP effects"]
    Probability --> Basis["Basis points 0..10,000"]
    Basis --> Result["UnderReview or Flagged"]
    LocalSHAP --> Result
```

The country frequency comes from reviewed artifact metadata, not the browser.
The scorer returns one stable `FraudScore` containing:

- raw probability;
- basis-point value for Solidity;
- reviewed threshold and model version;
- `flagged` as a screening result; and
- five readable, claim-specific SHAP reasons.

Example conversion:

```text
probability 0.2466 -> 24.66% -> 2,466 / 10,000 on-chain
```

## Reading SHAP reasons

| Sign | Plain meaning |
| --- | --- |
| Positive | This feature moved this model prediction toward higher modelled risk |
| Negative | This feature moved this model prediction toward lower modelled risk |
| Larger absolute value | Stronger influence relative to this claim's other features |

SHAP explains the model's calculation for one claim. It does not prove cause,
fraud, or innocence. One-hot categories may appear as `Country: Ghana` or
`Country is not Kenya` because the model receives separate yes/no columns.

## Application configuration

| Setting | Purpose |
| --- | --- |
| `XGBOOST_MODEL_DIR` | Directory containing the reviewed files |
| `XGBOOST_MODEL_SHA256` | Optional deployment-level checksum override |

The intended entry point is the asynchronous worker:

```bash
apps/backend/.venv/bin/python \
  -m packages.integrations.kafka.scoring_worker
```

## Verify

```bash
apps/backend/.venv/bin/python -m pytest packages/model/tests -q
```

Tests cover leakage controls, chronological splitting, artifact integrity,
feature compatibility, scoring, and local SHAP output.

## Notebooks

The notebooks call the tested functions in `research_pipeline.py` instead of
maintaining a second training implementation:

- [Dataset exploration](notebooks/01_dataset_exploration.ipynb)
- [XGBoost and SHAP](notebooks/02_xgboost_and_shap.ipynb)

See the [notebook guide](notebooks/README.md) and the
[Kafka guide](../integrations/kafka/README.md).

---

## Initial XGBoost research result

This is the first complete run of the research pipeline on the pinned African
Motor Insurance Claims dataset.

### Setup

- Dataset rows: 99,982
- Split: oldest 70% for training, next 15% for validation, newest 15% for testing
- Test rows: 14,998
- Test fraud rate: 11.17%
- Threshold: chosen on validation F1, never on the test set
- Leakage fields: settlement values, processing days, generated fraud probability,
  loss ratio and scenario were excluded

### Untouched test result

| Model | PR-AUC | ROC-AUC | Precision | Recall | Fraud F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Logistic regression | 0.171 | 0.628 | 0.173 | 0.408 | 0.243 |
| XGBoost | 0.165 | 0.615 | 0.148 | 0.616 | 0.238 |

XGBoost found more fraudulent rows, but it also produced more false alarms. The
logistic baseline had slightly stronger PR-AUC and F1. That is an important
result rather than a failed experiment: a more complex model did not outperform
the simpler baseline on this leakage-safe temporal split.

The strongest global SHAP signals for XGBoost were market claim frequency,
theft claims, policy premium, country, vehicle age, claim amount and total-loss
status.

These figures describe synthetic data only. They demonstrate the evaluation
method and must not be used as evidence of real-world predictive performance.
