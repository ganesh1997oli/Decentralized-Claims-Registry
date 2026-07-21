# Week 5 synthetic fraud model

This module supplies the inference seam needed to demonstrate the complete
claim lifecycle. `train.py` creates deterministic synthetic feature rows, fits
a logistic-regression baseline, tunes its threshold on a held-out validation
split, and writes a versioned JSON artifact. No real insurance records are used.

```bash
python -m model.train
pytest model/tests -q
```

The JSON artifact is data rather than executable pickle/joblib content, making
loading predictable. Per-feature logistic contributions provide transparent
prototype reasons; they are **not SHAP values**. Replace this artifact with the
proposal's validated real-data pipeline before presenting model-performance
claims. A low fraud score results in `UnderReview`, never automatic approval.
