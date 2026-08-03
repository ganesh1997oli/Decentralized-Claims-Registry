# Initial XGBoost research result

This is the first complete run of the research pipeline on the pinned African
Motor Insurance Claims dataset.

## Setup

- Dataset rows: 99,982
- Split: oldest 70% for training, next 15% for validation, newest 15% for testing
- Test rows: 14,998
- Test fraud rate: 11.17%
- Threshold: chosen on validation F1, never on the test set
- Leakage fields: settlement values, processing days, generated fraud probability,
  loss ratio and scenario were excluded

## Untouched test result

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

