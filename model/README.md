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
the reviewed country reference value, and returns the stable `FraudScore`
interface with five claim-specific SHAP reasons.

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
python -m integrations.kafka.scoring_worker
```

Do not load joblib files received from users or untrusted storage. Joblib uses
Python object deserialization and must be restricted to reviewed artifacts.

### Run the tests

From the repository root:

```bash
source backend/.venv/bin/activate
python -m pytest model/tests -q
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

See the [backend guide](../backend/README.md) for where scoring enters the claim
workflow.
