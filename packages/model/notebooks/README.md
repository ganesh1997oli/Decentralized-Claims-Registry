# Research notebooks

These notebooks sit on top of the tested Python pipeline:

1. `01_dataset_exploration.ipynb` checks the dataset, target balance, selected
   features and chronological split.
2. `02_xgboost_and_shap.ipynb` trains the baseline and XGBoost models, compares
   their test metrics and creates a SHAP summary.

The notebooks work locally and in Google Colab. In Colab, the setup cell clones
the repository and installs the research requirements. Locally, start Jupyter
from the repository root:

```bash
source apps/backend/.venv/bin/activate
pip install jupyterlab
jupyter lab packages/model/notebooks
```

Run the cells in order. Generated data and model artifacts remain ignored by
Git. The source notebooks are committed without execution output so their
results can always be reproduced from the pinned dataset.

