### Feature ablation and sensitivity analysis

The subgroup audit showed that the evaluated XGBoost model treated countries
very differently. Nigeria had a false-positive rate of 0.8934, whereas Rwanda
had a false-negative rate of 0.9615. Because country also appeared prominently
in the SHAP analysis, a controlled ablation study was conducted to determine
whether the models had learned claim-level patterns or relied mainly on
geographic structure introduced by the synthetic-data generator.

Six feature configurations were evaluated. These comprised the complete model;
removal of country; removal of vehicle age; removal of both country and vehicle
age; removal of country and market claim frequency; and a strict configuration
that removed country, market claim frequency and vehicle age. Market claim
frequency was included as a separate sensitivity condition because the serving
pipeline derives its value from country-level artifact metadata. Removing the
country category alone could therefore leave an indirect geographic proxy in
the model.

All configurations used the same pinned dataset, chronological 70/15/15 split
and random seed. Logistic regression and XGBoost were retrained from the
beginning for each configuration. Thresholds were selected on validation data,
and the held-out test period was evaluated once. PR-AUC uncertainty and paired
differences from the complete model were estimated with 2,000 paired bootstrap
resamples. The test fraud prevalence was 0.111748, which provides the no-skill
PR-AUC reference for this imbalanced dataset.

| Model and feature configuration | Test PR-AUC | 95% bootstrap interval | Change from full model | Original lift retained |
| --- | ---: | ---: | ---: | ---: |
| Logistic regression: full | 0.171142 | 0.159460–0.184635 | — | 100.0% |
| Logistic regression: without country | 0.171546 | 0.159785–0.185856 | +0.000404 | 100.7% |
| Logistic regression: without vehicle age | 0.170321 | 0.158520–0.183808 | −0.000821 | 98.6% |
| Logistic regression: without country and vehicle age | 0.170469 | 0.158452–0.184691 | −0.000673 | 98.9% |
| Logistic regression: without country and market frequency | 0.133185 | 0.124664–0.144001 | −0.037957 | 36.1% |
| Logistic regression: strict claim features | 0.132072 | 0.123733–0.142765 | −0.039070 | 34.2% |
| XGBoost: full | 0.164694 | 0.153453–0.177296 | — | 100.0% |
| XGBoost: without country | 0.165719 | 0.153983–0.179667 | +0.001025 | 101.9% |
| XGBoost: without vehicle age | 0.163496 | 0.152006–0.176809 | −0.001198 | 97.7% |
| XGBoost: without country and vehicle age | 0.158232 | 0.147513–0.171023 | −0.006462 | 87.8% |
| XGBoost: without country and market frequency | 0.133997 | 0.125375–0.145300 | −0.030697 | 42.0% |
| XGBoost: strict claim features | 0.129043 | 0.120675–0.139122 | −0.035651 | 32.7% |

Removing country alone did not reduce PR-AUC for either model. The paired 95%
interval for the change was −0.005747 to 0.006745 for logistic regression and
−0.005648 to 0.007875 for XGBoost. Vehicle-age removal also produced only small
changes, with both paired intervals including zero. These results indicate that
neither the explicit country category nor vehicle age was individually
necessary for the reported ranking performance.

The result changed when the country-linked market-frequency input was also
removed. PR-AUC decreased to 0.133185 for logistic regression and 0.133997 for
XGBoost. The paired reductions were clearly below zero: −0.048171 to −0.027960
and −0.040422 to −0.021345 respectively. The strict configuration retained only
34.2% of the logistic-regression lift and 32.7% of the XGBoost lift above
prevalence. PR-AUC remained above the 0.111748 reference, so the remaining claim
attributes contained some synthetic ranking information, but most of the
original lift depended on the geographic market-frequency structure.

Threshold-dependent subgroup results required additional care. At its
validation-F1 threshold, the XGBoost model without country and market frequency
selected 99.93% of test claims. Its subgroup rates consequently appeared
similar only because almost every record was flagged. A second comparison
therefore matched each ablated model to the complete model's validation review
capacity. Under this control, strict feature removal reduced the XGBoost
country-level false-positive-rate range from 0.8282 to 0.1495 and the
false-negative-rate range from 0.8789 to 0.2538. The smaller but non-zero gaps
show that removing direct geography can reduce generator-driven disparity, but
does not by itself establish fairness.

The ablation therefore refines the interpretation of the original audit. The
extreme country behaviour was not caused solely by the one-hot country field;
the country-linked market-frequency variable preserved much of the same
structure. Vehicle age contributed little independently. These findings apply
only to the synthetic generator and do not show that the retained attributes
represent real insurance-fraud mechanisms. The model remains unsuitable for
deployment without governed real-world data, external validation, subgroup
confidence intervals and an agreed human-review policy.

**Suggested figure captions**

- *PR-AUC across country, vehicle-age and market-frequency ablations. Error bars
  show percentile 95% intervals from 2,000 paired test-set bootstrap resamples;
  the dashed line is the test-set fraud prevalence.*
- *Country-level XGBoost false-positive and false-negative rates after matching
  each ablated model to the full model's validation review capacity. Capacity
  matching prevents near-universal selection from being interpreted as a
  fairness improvement.*
