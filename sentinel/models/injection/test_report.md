# Frozen injection models: reserved test results

Thresholds were selected on validation and frozen before this test. No fitting or tuning here.

| Model | Threshold | Attacks caught | Benign flagged | Recall | Precision | FPR | Median ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| xgboost | 0.507337 | 41/60 | 1/56 | 68.3% | 97.6% | 1.8% | 0.416 |
| logistic_regression | 0.496090 | 46/60 | 1/56 | 76.7% | 97.9% | 1.8% | 0.288 |
| regex | 0.500000 | 4/60 | 0/56 | 6.7% | 100.0% | 0.0% | 0.004 |

Internal public dataset only; semantic attack family overlap may remain. Not an external benchmark or reviewed marketplace evaluation. Runtime scans multiple metadata fields; text-level false positive rates do not establish per-purchase false positive rates.

Latency includes vectorization and prediction, with models loaded.
