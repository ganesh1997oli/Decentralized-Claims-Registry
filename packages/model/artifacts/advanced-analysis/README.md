# Advanced model-analysis evidence

This directory is the repository-controlled replacement for the advanced
analysis script and JSON that were missing during the dissertation audit. The
historical file was not recovered. Instead, the analysis was reimplemented and
rerun from the pinned dataset with a fully declared seed, search space, trial
manifest and software environment.

## Controls

- Seed: `20260724`
- Split: chronological 70/15/15
- Random search: 24 seeded configurations,
  selected by validation PR-AUC
- Threshold: selected on validation fraud-class F1
- Bootstrap: 2000 paired test-row resamples
- Permutations: 100 independent training-label permutations
- Test rows: 14,998
- Test prevalence: 0.111748
- Deployed artifact changed: no

## Rerun result

The selected XGBoost configuration was `{"colsample_bytree": 0.75, "learning_rate": 0.025, "max_depth": 3, "min_child_weight": 2, "n_estimators": 400, "reg_lambda": 1.0, "scale_pos_weight": 7.204806565064478, "subsample": 0.8}`.
Its test PR-AUC was 0.172016, compared with
0.171142 for logistic regression and
0.164694 for the deployed XGBoost model. The
paired interval for logistic minus tuned XGBoost was
[-0.004983, 0.002992].

The permutation null mean was 0.113364, with empirical
one-sided p=0.009901. These values demonstrate
detectable signal inside the synthetic generator, not field validity.

## Archive contents

- `advanced_analysis_results.json`: complete configuration and raw results
- `random_search_trials.csv`: every evaluated candidate and validation result
- `bootstrap_summary.csv`: model and paired-comparison intervals
- `permutation_scores.csv`: all permuted-label PR-AUC scores
- `mutual_information.csv`: training-period feature diagnostics
- `country_metrics.csv`: deployed-model country audit
- `calibration_bins.csv`: ten-bin reliability points
- `bootstrap_differences.png`: paired PR-AUC difference distributions
- `permutation_diagnostic.png`: observed score against the permutation null
- `reliability_diagram.png`: quantile-bin calibration curves
- `execution_manifest.json`: command, versions, hashes and archive metadata
- `checksums.sha256`: SHA-256 digest for each retained output except itself

Reproduce from the repository root:

```bash
python -m packages.model.advanced_analysis \
  --data packages/model/data/african_motor_claims.csv \
  --output-dir packages/model/artifacts/advanced-analysis \
  --trials 24 \
  --bootstrap-samples 2000 \
  --permutations 100 \
  --seed 20260724
```
