# Assemble injection examples

Run from the repository root, with no additional dependencies:

```sh
.venv/bin/python scripts/assemble_injection_data.py
```

This downloads both splits of deepset/prompt-injections from the Hugging Face dataset viewer API. It preserves the original labels and split metadata, saves raw pages, deduplicates normalized identical texts, and writes examples.jsonl and manifest.json under data/injection/generated/. Conflicting duplicate labels stop assembly for review. The manifest records collection time and an output checksum; this API snapshot is not pinned to a dataset revision. Retain the raw pages with any trained model for reproducibility. The upstream dataset lists Apache 2.0; preserve its license and attribution when redistributing the data.

## Review marketplace examples

Edit data/injection/marketplace.jsonl. Each line is one complete JSON object. It holds 214 authored examples (146 benign, 68 injection), all synthetic and reviewed by sentinel-dev: ordinary listings across company data, news, legal, finance, web tools, people, maps and AI tools; hard negatives that use words like everything, must, free, agent, owner or payment in normal seller copy; and injection families (fake authorization, approval bypass, wallet redirection, urgency upsells, system overrides, data exfiltration, tool invocation, hidden or covert instructions). None copies a test site listing verbatim; some injection examples paraphrase the test site's traps. To add more:

1. Read the text as content returned by an untrusted seller, rather than an instruction from the owner.
2. Use label 1 if it tries to control the agent, claim owner authority, bypass policy, redirect funds, or induce actions outside the authorized task. An unsuccessful attempt still gets label 1.
3. Use label 0 for ordinary product descriptions, pricing, and protocol documentation. Words such as "payment" or "AI agents" alone do not establish an injection.
4. Check the rationale, choose a family, set reviewer to your name or initials, then set reviewed to true. Leave ambiguous examples unreviewed until resolved.
5. Rerun the assembly command. Only reviewed manual examples enter the output.

For actual marketplace text, copy the exact relevant seller field, set synthetic to false, record its source URL and field in source, and add collected_at. Remove secrets and personal information before adding examples. For authored or generated text keep synthetic true. Do not claim synthetic examples are observed attacks.

The group field is a text hash for identical examples, not an inferred attack family. Original row metadata, manual families, and review information remain in provenance. Before training, group close paraphrases and template variants together, honor the upstream test separation, and check for overlap across splits. Do not randomly split this combined output and call it an independent benchmark. Public labels have not been rechecked for Sentinel's precise threat model.

## Create training, validation, and test files

```sh
.venv/bin/python scripts/split_injection_data.py
```

The script preserves the upstream test set, groups normalized identical text, supplied families/groups, and lexical near duplicates (character 5 gram Jaccard similarity at least 0.8). Training examples resembling test examples are quarantined. It assigns roughly 20% of the remaining pool to validation while balancing labels and keeping clusters intact. Seed 42 makes reruns reproducible. Outputs and a manifest are under data/injection/generated/splits/.

This is an automatic lexical check, not proof of semantic paraphrase or attack family separation. Keep that limitation in any reported prototype results. An external benchmark is deferred. Fit the vocabulary on train.jsonl only, use validation.jsonl for model selection and thresholds, and use test.jsonl once for final evaluation. Never fit the vectorizer on examples.jsonl, validation, test, or quarantine. Quarantined rows are excluded from training and evaluation.

The splitter keeps each manual family together, so whole marketplace families land in validation (for example ai-tools, finance, covert-instruction) rather than leaking paraphrases across splits. The reserved test split holds only upstream rows; there is no untouched marketplace test set yet.

## Train offline baselines

```sh
.venv/bin/python -m pip install -e '.[ml]'
.venv/bin/python -m scripts.train_injection
```

The ml extra adds only XGBoost, which training needs; the server never loads it. On macOS XGBoost also needs the OpenMP runtime (libomp). Without Homebrew's libomp, point DYLD_LIBRARY_PATH at a directory holding scikit-learn's bundled copy (.venv/lib/python3.13/site-packages/sklearn/.dylibs) for the training and evaluation commands. The script fits word and character TF-IDF features on training text only, then trains logistic regression and XGBoost with fixed initial settings. Each model's threshold maximizes validation recall while flagging at most 5% of validation benign examples, with ties favoring fewer false positives. Change that experimental budget with --max-fpr. This is a validation selection target, not a guarantee on future content; with 100 benign validation examples it allows at most five false positives.

Each run creates a new folder under artifacts/injection/ containing the vectorizer, both models, thresholds, environment versions, data manifests and checksums, exact training/validation examples, validation predictions, and a readable report. Artifact reload predictions are checked against the originals. Joblib files should only be loaded from trusted local runs. The comparison includes the existing regex on identical full validation texts. Both default 0.5 and selected threshold metrics are saved. The test file is not opened. Scores are uncalibrated; the reported validation performance is used for selection and is not final test or marketplace performance. Training alone does not change the payment screen; ship the run as described below.

## Freeze and evaluate the models

```sh
.venv/bin/python -m scripts.evaluate_injection --experiment artifacts/injection/<training-run>
```

The evaluation command opens only the reserved test split for scoring, verifies its checksum and group separation, loads frozen artifacts and thresholds (XGBoost through the runtime's plain Python tree evaluator), and writes test_evaluation/report.md, metrics.json, and predictions.jsonl. It refuses to overwrite a previous evaluation. It does not tune on test results. This is an internal public dataset test, not an external benchmark or a measurement on real marketplace traffic.

## The shipped model and the payment screen

The screen is on by default. sentinel/models/injection/ holds the shipped run's runtime files (vectorizer.joblib, xgboost.json, thresholds.json, environment.json, detector_manifest.json, about 690 KB), its validation and test reports, and parity.json (XGBoost's own scores on reference texts). The server reads xgboost.json and walks the trees in plain Python (sentinel/xgb_trees.py; absent sparse features take the default branch, values compare in float32), so it never loads XGBoost or OpenMP. Its scores match XGBoost's to about 1e-7 (tests/test_injection_detector.py checks parity.json). scikit-learn, joblib, NumPy and SciPy are main dependencies pinned to the versions in environment.json, because the vectorizer is a pickle; the loader refuses other versions.

Shipped run: 20261025T071931299438Z, deepset/prompt-injections (662 rows) plus the 214 marketplace examples, 876 unique after deduplication, split 602 train, 155 validation, 116 test (3 quarantined). XGBoost threshold 0.507 (at most 5% validation false positives). Validation: 48/55 attacks caught, 5/100 benign flagged, average precision 0.954, ROC AUC 0.967. Reserved test (upstream rows only): 41/60 attacks caught, 1/56 benign flagged (recall 68.3%, precision 97.6%). On the test site every listing with injected text scores 0.94 or more (legal/filings-plus 0.96, intel/deep-dossier 0.94) and every other listing scores under 0.48 (the closest is reports/due-diligence at 0.476, so the margin there is thin).

To ship another run, copy those five runtime files from artifacts/injection/<run>/ (plus report.md and test_evaluation/report.md) into sentinel/models/injection/ and regenerate parity.json with XGBoost. To try a run without shipping it, set SENTINEL_INJECTION_MODEL_DIR to its directory (relative paths resolve from the repository root). SENTINEL_INJECTION_MODEL_DIR=off disables the screen. Hashes detect mismatched files, not maliciously replaced bundles; only load trusted artifacts. The app lifespan loads the model once per process and refuses startup if the artifacts or pinned packages are unusable; a configuration change requires restart.

After the deterministic task rules pass, the screen checks seller descriptions, service names, tags, and recorded catalog names and descriptions, before the cached or fresh Claude task check. It scores individual fields and their combined text, using the maximum. A score at or over the threshold blocks the payment (rule injection_ml); the attempt records ml_model injection, ml_score and ml_threshold, and the reason states the score, for example "Sentinel's injection model scored the seller's text 0.96 (blocks at 0.51)". There is no override: nobody can allow a purchase the model blocked. A low score never authorizes anything; Claude's check still runs. Scoring failures and metadata over 20,000 characters block (injection_unavailable) rather than silently skipping the check. Deterministic rules run first and keep precedence. This covers task bound purchases and previews, not delivered response bodies.

The screen sees metadata already retained by Sentinel: seller descriptions and names have existing upstream length limits. It cannot detect instructions outside those fields or recover text already discarded by parsing. Scanning several fields changes the false positive tradeoff relative to the single text benchmark, and a false positive is now a hard block. The public dataset results do not establish purchase level error rates. Delivered content retains its existing regex and Claude checks; adding the local model there is deferred.
