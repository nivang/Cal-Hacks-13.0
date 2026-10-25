# Assemble injection examples

Run from the repository root, with no additional dependencies:

```sh
.venv/bin/python scripts/assemble_injection_data.py
```

This downloads both splits of deepset/prompt-injections from the Hugging Face dataset viewer API. It preserves the original labels and split metadata, saves raw pages, deduplicates normalized identical texts, and writes examples.jsonl and manifest.json under data/injection/generated/. Conflicting duplicate labels stop assembly for review. The manifest records collection time and an output checksum; this API snapshot is not pinned to a dataset revision. Retain the raw pages with any trained model for reproducibility. The upstream dataset lists Apache 2.0; preserve its license and attribution when redistributing the data.

## Review marketplace examples

Edit data/injection/marketplace.jsonl. Each line is one complete JSON object. Six authored examples are included as candidates, not human reviewed ground truth. For each candidate:

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

Start with a balanced batch of marketplace candidates, including legitimate descriptions that mention agents or payment. Keep a separate untouched evaluation set. These six examples demonstrate the format; they are not sufficient to validate a detector.

## Train offline baselines

```sh
.venv/bin/python -m pip install -e '.[ml]'
.venv/bin/python -m scripts.train_injection
```

The optional ML dependencies do not change the server dependencies. On macOS XGBoost also needs the OpenMP runtime (libomp). The script fits word and character TF-IDF features on training text only, then trains logistic regression and XGBoost with fixed initial settings. Each model's threshold maximizes validation recall while flagging at most 5% of validation benign examples, with ties favoring fewer false positives. Change that experimental budget with --max-fpr. This is a validation selection target, not a guarantee on future content; with 68 benign validation examples it allows at most three false positives.

Each run creates a new folder under artifacts/injection/ containing the vectorizer, both models, thresholds, environment versions, data manifests and checksums, exact training/validation examples, validation predictions, and a readable report. Artifact reload predictions are checked against the originals. Joblib files should only be loaded from trusted local runs. The comparison includes the existing regex on identical full validation texts. Both default 0.5 and selected threshold metrics are saved. The test file is not opened. Scores are uncalibrated; the reported validation performance is used for selection and is not final test or marketplace performance. Training alone does not enable the payment screen; configure it as described below.

## Freeze and evaluate the models

```sh
.venv/bin/python -m scripts.evaluate_injection --experiment artifacts/injection/<training-run>
```

For the first run created before runtime integration, create its detector manifest once:

```sh
.venv/bin/python -c 'from pathlib import Path; from sentinel.injection_detector import write_manifest; write_manifest(Path("artifacts/injection/20261025T043009512747Z"))'
```

The evaluation command opens only the reserved test split for scoring, verifies its checksum and group separation, loads frozen artifacts and thresholds, and writes test_evaluation/report.md, metrics.json, and predictions.jsonl. It refuses to overwrite a previous evaluation. It does not tune on test results. This is an internal public dataset test, not an external benchmark or a measurement on real marketplace traffic.

## Enable the local payment screen

Install the ML dependencies and use a trusted training run with detector_manifest.json. The loader requires the exact scikit-learn, XGBoost, joblib, NumPy, and SciPy versions recorded in that run's environment.json. Put the directory in .env:

```sh
SENTINEL_INJECTION_MODEL_DIR=artifacts/injection/20261025T043009512747Z
```

Restart Sentinel. Relative directories resolve from the repository root. The app lifespan loads the vectorizer, XGBoost model, and validation threshold once per process, checks runtime artifact hashes, and refuses startup if configured artifacts or dependencies are unusable. Hashes detect mismatched files, not maliciously replaced bundles; only load trusted artifacts. An unset or empty setting disables this optional layer. Each request uses the startup instance; a configuration change requires restart.

After deterministic rules pass, the screen checks seller descriptions, service names, tags, and recorded catalog names/descriptions before the cached or fresh Claude task check. It scores individual fields and their combined text, using the maximum. High scores hold the purchase for owner review; low scores continue to Claude. Runtime failures and metadata over 20,000 characters hold rather than silently skipping the check. Existing hard rules keep precedence, and an explicit purchase scoped Allow once can override an ML hold. The existing admission lock recheck preserves holds and still consumes allowances once. This integration covers task bound purchases and previews, not legacy direct purchases without tasks or delivered response bodies.

The screen sees metadata already retained by Sentinel: seller descriptions and names have existing upstream length limits. It cannot detect instructions outside those fields or recover text already discarded by parsing. Scanning several fields changes the false positive tradeoff relative to the single text benchmark. The public dataset results do not establish purchase level error rates. Delivered content retains its existing regex and Claude checks; adding the local model there is deferred.
