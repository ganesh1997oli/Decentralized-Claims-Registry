# Country and vehicle-age ablation evidence

This directory archives the evaluation requested after review of the subgroup
audit. The experiment retrains logistic regression and XGBoost under six
controlled feature sets. It does not alter the deployed model artifact.

## Experimental controls

- Dataset: pinned synthetic African motor-claims CSV
- Split: chronological 70% training, 15% validation and 15% test
- Seed: `20260724`
- Test rows: 14,998
- Test fraud prevalence and no-skill PR-AUC reference: 0.111748
- Threshold: selected on validation fraud F1
- Uncertainty: 2,000 paired test-row bootstrap resamples, percentile 95% interval
- Capacity check: each ablated model is also matched to the full model's
  validation selection rate before test and country metrics are calculated

## Main finding

Removing direct country information did not reduce PR-AUC. Logistic regression
changed from 0.171142 to 0.171546 and XGBoost from 0.164694 to 0.165719; both
paired difference intervals included zero. Removing vehicle age alone also had
little effect.

The important dependency was the country-linked market-frequency feature.
Removing both country and market claim frequency reduced PR-AUC to 0.133185 for
logistic regression and 0.133997 for XGBoost. The strict removal of country,
market frequency and vehicle age reduced PR-AUC further to 0.132072 and
0.129043. These models remained above the 0.111748 prevalence reference, but
retained only 34.2% and 32.7% of the original lift respectively.

At matched review capacity, strict removal substantially narrowed the XGBoost
country FPR range from 0.8282 to 0.1495 and the FNR range from 0.8789 to 0.2538.
The remaining differences show that removing explicit geography is not a
fairness guarantee. Because the data and labels are synthetic, the result is
evidence about generator dependence, not real national fraud risk.

## Files

- `ablation_results.json`: complete configuration, metrics, uncertainty and
  both country-audit views
- `ablation_summary.csv`: compact validation-F1-threshold comparison
- `capacity_matched_summary.csv`: comparison at matched review capacity
- `country_metrics.csv`: country audit at each model's validation-F1 threshold
- `capacity_matched_country_metrics.csv`: country audit at matched capacity
- `pr_auc_ablation.png`: PR-AUC and paired-bootstrap intervals
- `precision_recall_curves.png`: threshold-free precision-recall curves
- `country_error_rates.png`: capacity-matched XGBoost FPR and FNR comparison

Reproduce the archive using `python -m packages.model.ablation_study`; the exact
command is documented in `packages/model/README.md`.
