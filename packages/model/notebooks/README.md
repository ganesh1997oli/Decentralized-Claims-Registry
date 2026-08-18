# Research notebooks

The notebooks are a visual route through the tested model pipeline, not a
second implementation.

```mermaid
flowchart LR
    N1["01 Dataset exploration"] -->|"quality, balance, time split"| Pipeline["research_pipeline.py"]
    Pipeline --> N2["02 XGBoost and SHAP"]
    N2 --> Output["metrics + explanation charts"]
```

| Notebook | Use it to inspect |
| --- | --- |
| `01_dataset_exploration.ipynb` | Required columns, target balance, selected features and chronological split |
| `02_xgboost_and_shap.ipynb` | Baseline comparison, XGBoost metrics and global SHAP behaviour |

## Run locally

From the repository root:

```bash
source apps/backend/.venv/bin/activate
python -m pip install jupyterlab
jupyter lab packages/model/notebooks
```

Run cells in order. The notebooks are committed without outputs; downloaded
data, trained artifacts, and generated charts remain ignored and reproducible
from the pinned dataset.

The setup cells also support Google Colab. Review any clone and install command
before running it in a hosted notebook.

All figures describe synthetic research data only. See the
[model guide](../README.md) for the serving boundary and [initial research result](../README.md#initial-xgboost-research-result)
for the checked-in evaluation summary.
