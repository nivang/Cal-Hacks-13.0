# Injection classifier validation experiment

Thresholds are tuned on these validation examples; these are not final test results. Scores are uncalibrated. A small public dataset does not establish marketplace performance.

| Model | Threshold | Attacks caught | Benign flagged | Precision | Recall | FPR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| logistic_regression | 0.496090 | 52/55 | 3/100 | 94.5% | 94.5% | 3.0% |
| xgboost | 0.507337 | 48/55 | 5/100 | 90.6% | 87.3% | 5.0% |
| regex | 0.500000 | 11/55 | 0/100 | 100.0% | 20.0% | 0.0% |

Both models share TF-IDF features fitted only on training text. Fixed initial model settings; no hyperparameter search. Regex evaluated on the same full validation texts.

Ship a run by copying its runtime files to sentinel/models/injection/ (docs/injection-data.md).
