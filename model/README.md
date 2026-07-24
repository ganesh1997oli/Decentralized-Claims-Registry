# Fraud models

This module has two deliberately separate workflows:

- the lightweight logistic scorer keeps the web demonstration working;
- the research pipeline compares that kind of baseline with XGBoost and creates
  SHAP explanations from a larger motor-claims dataset.

Neither workflow is a validated real-world insurance-fraud model.

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

## Research XGBoost and SHAP workflow

The research workflow uses the CC BY 4.0
[African Motor Insurance Claims dataset](https://huggingface.co/datasets/electricsheepafrica/africa-synth-motor-insurance-claims-all).
The download is pinned to one revision and checked with SHA-256 before training.
The dataset is synthetic and its results must not be presented as evidence of
performance on real claims.

The model uses ten fields that could be known when a claim is submitted:

| Numeric | Categorical |
| --- | --- |
| Vehicle age | Country |
| Claim amount | Vehicle type |
| Policy premium | Claim type |
| Market claim frequency | Urban or rural region |
| Third-party injury flag | |
| Total-loss flag | |

Settlement values, processing time, the generated fraud probability, loss ratio
and scenario are excluded. They either happen after submission or reveal how the
synthetic target was made.

### Install and run

Create the same environment used by the backend, then add the research packages:

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt -r model/requirements.txt
```

XGBoost needs OpenMP. On macOS, install it once with `brew install libomp`.

Run the complete workflow from the repository root:

```bash
python -m model.train_xgboost --download
```

The command:

1. downloads and verifies the reviewed dataset revision;
2. keeps the newest 15% of claims as an untouched chronological test set;
3. trains logistic regression and XGBoost;
4. chooses each threshold using validation data only;
5. reports PR-AUC, ROC-AUC, precision, recall, F1 and calibration;
6. saves the XGBoost pipeline, metadata and a SHAP summary plot under
   `model/artifacts/xgboost-african-motor-v1/`.

The downloaded dataset and generated artifacts are ignored by Git because they
can be reproduced with the command above.

See [RESULTS.md](RESULTS.md) for the first complete run on the pinned dataset.

### Use the artifact in the application

Training now saves an artifact schema containing:

- the preprocessing and XGBoost pipeline;
- the model SHA-256 digest;
- the validation threshold and model version;
- country-level synthetic claim-frequency reference values;
- test metrics and global SHAP importance.

`XGBoostFraudScorer` verifies the checksum before loading the trusted joblib
file. It loads the pipeline and SHAP explainer once, enriches each claim with
the reviewed country reference value, and returns the existing `FraudScore`
interface with three claim-specific SHAP reasons.

Copy the environment template after training:

```bash
cp model/.env.example model/.env.local
```

The asynchronous Kafka worker is the intended application seam:

```bash
python -m integrations.kafka.scoring_worker
```

Do not load joblib files received from users or untrusted storage. Joblib uses
Python object deserialization and must be restricted to reviewed artifacts.

### Explore in Jupyter or Colab

The same workflow is available as two lightweight notebooks:

- [Dataset exploration](notebooks/01_dataset_exploration.ipynb)
- [XGBoost and SHAP](notebooks/02_xgboost_and_shap.ipynb)

They can be opened directly in Google Colab or run locally from the repository.
The notebooks reuse the functions in `research_pipeline.py`, so experiments and
the command-line workflow follow the same preprocessing and evaluation rules.
See the [notebook guide](notebooks/README.md) for local setup.

## Research boundaries

The synthetic artifact proves the integration only. Meaningful evaluation would
ultimately require a justified real dataset and independent labels. The research
pipeline adds documented preprocessing, a leakage-safe temporal split, class
weighting, appropriate metrics, a baseline comparison and SHAP analysis, but it
cannot make synthetic results representative of deployment.

SHAP explains how this model behaves. It does not prove that a claim is
fraudulent or that a feature caused fraud.

See the [backend guide](../backend/README.md) for where scoring enters the claim
workflow.
