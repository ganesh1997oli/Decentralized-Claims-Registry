# Fraud models

This module has one application scoring path: the asynchronous Kafka worker
loads the reviewed XGBoost artifact and produces claim-specific SHAP reasons.
The training pipeline also evaluates logistic regression as a scientific
baseline, but that baseline is not saved or served by the application.

The dataset is synthetic, so this workflow is not a validated real-world
insurance-fraud model.

## XGBoost and SHAP workflow

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
python3 -m venv apps/backend/.venv
source apps/backend/.venv/bin/activate
pip install -r apps/backend/requirements.txt -r packages/model/requirements.txt
```

XGBoost needs OpenMP. On macOS, install it once with `brew install libomp`.

Run the complete workflow from the repository root:

```bash
python -m packages.model.train_xgboost --download
```

The command:

1. downloads and verifies the reviewed dataset revision;
2. keeps the newest 15% of claims as an untouched chronological test set;
3. trains logistic regression and XGBoost;
4. chooses each threshold using validation data only;
5. reports PR-AUC, ROC-AUC, precision, recall, F1 and calibration;
6. saves the XGBoost pipeline, metadata and a SHAP summary plot under
   `packages/model/artifacts/xgboost-african-motor-v1/`.

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
the reviewed country reference value, and returns the stable `FraudScore`
interface with five claim-specific SHAP reasons.

In plain language, one claim moves through the scorer like this:

1. The scorer looks up the reviewed market claim frequency for the submitted
   country. This value comes from the model artifact, not from the browser.
2. `ClaimFeaturesV1.from_claim` combines that reference value with the submitted
   claim and converts the two yes/no flags into the numeric form used in
   training.
3. `ClaimFeaturesV1.as_frame` creates a one-row table in the exact column order
   expected by the saved pipeline.
4. The pipeline preprocesses that row and XGBoost produces a risk probability.
5. SHAP calculates how much each transformed feature moved this individual
   prediction. The scorer ranks those effects by absolute size and returns the
   strongest five without losing whether each effect was positive or negative.
6. The probability is also converted to an integer out of `10,000` so it can be
   stored by the Solidity contract.

### Scorer function guide

The code is deliberately split into small functions so each safety or
translation step has one clear responsibility:

| Function or method | What it does in everyday language |
| --- | --- |
| `ClaimFeaturesV1.from_claim` | Builds the model-ready claim from submitted values and the trusted country reference value. |
| `ClaimFeaturesV1.as_frame` | Places one claim into the ordered table format expected by scikit-learn. |
| `file_sha256` | Creates a fingerprint of the saved model so the application can detect an unexpected or tampered file before loading it. |
| `_reason_label` | Converts machine names such as `country_Ghana` into wording an investigator can understand. |
| `XGBoostFraudScorer.__init__` | Checks the artifact schema, feature list, threshold and reference data, then prepares the model and SHAP explainer for repeated use. |
| `XGBoostFraudScorer.from_directory` | Reads an artifact directory, verifies the model fingerprint and only then loads the joblib pipeline. |
| `XGBoostFraudScorer.from_env` | Reads the artifact location and optional approved fingerprint from deployment environment variables. |
| `XGBoostFraudScorer.score` | Enriches one claim, predicts its probability, selects five local SHAP reasons and returns the complete application result. |

### Reading the five SHAP reasons

Each reason contains a readable label and a signed contribution:

- a positive contribution moved the prediction toward higher modelled risk;
- a negative contribution moved it toward lower modelled risk;
- the absolute size shows how strongly that feature influenced this claim
  relative to its other features.

The reasons are local to one prediction. They are not a statement that a field
is always risky, and they do not prove cause, fraud or innocence. Categorical
values can be worded as either `Country: Ghana` or `Country is not Kenya`
because the preprocessing pipeline represents each possible category as a
separate yes/no column.

The artifact location and optional checksum live in the shared root
configuration:

```bash
cp .env.example .env.local
set -a
source .env.local
set +a
```

The asynchronous Kafka worker is the intended application seam:

```bash
python -m packages.integrations.kafka.scoring_worker
```

Do not load joblib files received from users or untrusted storage. Joblib uses
Python object deserialization and must be restricted to reviewed artifacts.

### Run the tests

From the repository root:

```bash
source apps/backend/.venv/bin/activate
python -m pytest packages/model/tests -q
```

The tests cover leakage controls, artifact integrity, feature compatibility,
XGBoost scoring, and SHAP reason generation.

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

See the [backend guide](../../apps/backend/README.md) for where scoring enters the claim
workflow.
