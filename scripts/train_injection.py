"""Train offline injection baselines. Run with python -m scripts.train_injection."""

import argparse
import hashlib
import json
import platform
import shutil
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, confusion_matrix, roc_auc_score
from sklearn.pipeline import FeatureUnion
from xgboost import XGBClassifier

from sentinel import injection_detector, intent

ROOT = Path(__file__).resolve().parents[1]


def features() -> FeatureUnion:
    return FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=10_000,
                                 sublinear_tf=True, dtype=np.float32)),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2,
                                 max_features=15_000, sublinear_tf=True, dtype=np.float32)),
    ])


def metrics(labels, scores, threshold: float) -> dict:
    tn, fp, fn, tp = map(int, confusion_matrix(labels, np.asarray(scores) >= threshold, labels=[0, 1]).ravel())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return dict(threshold=float(threshold), true_positive=tp, false_positive=fp, true_negative=tn,
                false_negative=fn, precision=precision, recall=recall,
                false_positive_rate=fp / (fp + tn) if fp + tn else 0.0,
                f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0)


def select_threshold(labels, scores, max_fpr: float) -> dict:
    """Maximize recall under the validation FPR budget; break ties by fewer false positives."""
    if not 0 <= max_fpr <= 1:
        raise ValueError("max_fpr must be between 0 and 1")
    if set(labels) != {0, 1}:
        raise ValueError("Threshold selection requires both labels")
    scores = np.asarray(scores)
    if not np.all(np.isfinite(scores)) or np.any((scores < 0) | (scores > 1)):
        raise ValueError("Expected finite scores between 0 and 1")
    candidates = [metrics(labels, scores, t) for t in np.append(np.unique(scores), np.nextafter(1.0, 2.0))]
    feasible = [m for m in candidates if m["false_positive_rate"] <= max_fpr]
    return max(feasible, key=lambda m: (m["recall"], -m["false_positive_rate"], m["threshold"]))


def load_splits(directory: Path) -> tuple[dict, dict]:
    manifest = json.loads((directory / "manifest.json").read_text())
    splits = {}
    for name in ("train", "validation"):
        raw = (directory / f"{name}.jsonl").read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["splits"][name]["sha256"]:
            raise ValueError(f"{name} checksum differs from split manifest; regenerate splits")
        splits[name] = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        if {r["label"] for r in splits[name]} != {0, 1}:
            raise ValueError(f"{name} must contain both labels")
    train_groups = {r["split_group"] for r in splits["train"]}
    if train_groups & {r["split_group"] for r in splits["validation"]}:
        raise ValueError("Training and validation groups overlap")
    return splits, manifest


def train(directory: Path, output: Path, max_fpr: float, seed: int) -> dict:
    if not 0 <= max_fpr <= 1:
        raise ValueError("max_fpr must be between 0 and 1")
    splits, split_manifest = load_splits(directory)
    # Refuse to overwrite an experiment. No test rows are opened anywhere in this script.
    output.mkdir(parents=True, exist_ok=False)
    train_text = [r["text"] for r in splits["train"]]
    val_text = [r["text"] for r in splits["validation"]]
    y_train = np.array([r["label"] for r in splits["train"]])
    y_val = np.array([r["label"] for r in splits["validation"]])
    vectorizer = features()
    x_train = vectorizer.fit_transform(train_text)
    x_val = vectorizer.transform(val_text)
    joblib.dump(vectorizer, output / "vectorizer.joblib")
    restored_vectorizer = joblib.load(output / "vectorizer.joblib")
    restored_x_val = restored_vectorizer.transform(val_text)
    np.testing.assert_allclose(x_val.data, restored_x_val.data)
    np.testing.assert_array_equal(x_val.indices, restored_x_val.indices)
    np.testing.assert_array_equal(x_val.indptr, restored_x_val.indptr)

    models = {
        "logistic_regression": LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000,
                                                   solver="liblinear", random_state=seed),
        "xgboost": XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                  subsample=0.9, colsample_bytree=0.9, min_child_weight=2,
                                  reg_lambda=2, objective="binary:logistic", eval_metric="logloss",
                                  tree_method="hist", n_jobs=2, random_state=seed,
                                  scale_pos_weight=float(sum(y_train == 0) / sum(y_train == 1))),
    }
    report = dict(
        evaluated_split="validation", test_evaluated=False, seed=seed,
        train_rows=len(y_train), validation_rows=len(y_val), feature_count=x_train.shape[1],
        threshold_policy=dict(max_validation_false_positive_rate=max_fpr,
                              selection="Maximize recall, then minimize FPR, then prefer the higher threshold"),
        warning="Thresholds are tuned on these validation examples; these are not final test results. "
                "Scores are uncalibrated. A small public dataset does not establish marketplace performance.",
        models={},
    )
    predictions = [dict(text=r["text"], label=r["label"], group=r["split_group"],
                        regex_match=(match.group(0) if (match := intent.INJECTION.search(r["text"])) else None))
                   for r in splits["validation"]]
    for name, model in models.items():
        started = perf_counter()
        model.fit(x_train, y_train)
        fit_seconds = perf_counter() - started
        scores = model.predict_proba(x_val)[:, 1]
        selected = select_threshold(y_val, scores, max_fpr)
        if name == "xgboost":
            model.save_model(output / "xgboost.json")
            restored = XGBClassifier()
            restored.load_model(output / "xgboost.json")
        else:
            joblib.dump(model, output / "logistic_regression.joblib")
            restored = joblib.load(output / "logistic_regression.joblib")
        np.testing.assert_allclose(scores, restored.predict_proba(restored_x_val)[:, 1])
        # Warm model before measuring end to end per text latency, including vectorization.
        model.predict_proba(vectorizer.transform(val_text[:1]))
        latency = []
        for text in val_text:
            started = perf_counter()
            model.predict_proba(vectorizer.transform([text]))
            latency.append(1000 * (perf_counter() - started))
        report["models"][name] = dict(
            selected_threshold_metrics=selected, default_threshold_metrics=metrics(y_val, scores, 0.5),
            average_precision=float(average_precision_score(y_val, scores)),
            roc_auc=float(roc_auc_score(y_val, scores)), fit_seconds=fit_seconds,
            latency_ms=dict(median=float(np.median(latency)), p95=float(np.percentile(latency, 95))),
            parameters=model.get_params(), artifact_roundtrip_verified=True,
        )
        for prediction, score in zip(predictions, scores, strict=True):
            prediction[name] = dict(score=float(score), flagged=bool(score >= selected["threshold"]))
    regex_scores = np.array([int(p["regex_match"] is not None) for p in predictions])
    report["models"]["regex"] = dict(metrics=metrics(y_val, regex_scores, 0.5),
                                              pattern=intent.INJECTION.pattern, flags=intent.INJECTION.flags)
    (output / "validation_predictions.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in predictions))
    (output / "thresholds.json").write_text(json.dumps({
        name: report["models"][name]["selected_threshold_metrics"]["threshold"] for name in models
    }, indent=2) + "\n")
    # Save the exact development data, metadata, source, and environment alongside the models.
    for name in ("train.jsonl", "validation.jsonl"):
        shutil.copyfile(directory / name, output / name)
    (output / "split_manifest.json").write_text(json.dumps(split_manifest, indent=2) + "\n")
    for filename in ("manifest.json",):
        upstream_manifest = directory.parent / filename
        if upstream_manifest.exists():
            shutil.copyfile(upstream_manifest, output / "assembly_manifest.json")
    shutil.copyfile(__file__, output / "train_injection.py")
    (output / "environment.json").write_text(json.dumps(dict(
        python=platform.python_version(), platform=platform.platform(),
        packages={p: version(p) for p in ("scikit-learn", "xgboost", "numpy", "scipy", "joblib", "threadpoolctl")},
    ), indent=2) + "\n")
    injection_detector.write_manifest(output)
    (output / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Injection classifier validation experiment", "", report["warning"], "",
             "| Model | Threshold | Attacks caught | Benign flagged | Precision | Recall | FPR |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, result in report["models"].items():
        m = result["metrics"] if name == "regex" else result["selected_threshold_metrics"]
        lines.append(f"| {name} | {m['threshold']:.6f} | {m['true_positive']}/{sum(y_val == 1)} | "
                     f"{m['false_positive']}/{sum(y_val == 0)} | {m['precision']:.1%} | "
                     f"{m['recall']:.1%} | {m['false_positive_rate']:.1%} |")
    lines += ["", "Both models share TF-IDF features fitted only on training text. Fixed initial model settings; "
              "no hyperparameter search. Regex evaluated on the same full validation texts.", "",
              "Artifacts remain offline. No Sentinel payment behavior has changed."]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nArtifacts: {output}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--splits", type=Path, default=ROOT / "data/injection/generated/splits")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts/injection" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    parser.add_argument("--max-fpr", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    train(args.splits, args.output, args.max_fpr, args.seed)


if __name__ == "__main__":
    main()
