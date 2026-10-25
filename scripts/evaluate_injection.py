"""Evaluate frozen models and thresholds once on the reserved internal test split. Never fit or tune."""

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

import joblib
import numpy as np

from scripts.train_injection import metrics
from sentinel import injection_detector, intent

ROOT = Path(__file__).resolve().parents[1]


def evaluate(experiment: Path, splits: Path) -> Path:
    # Loading checks the runtime artifact manifest and training library versions.
    detector = injection_detector.InjectionDetector.load(experiment)
    manifest = json.loads((experiment / "split_manifest.json").read_text())
    raw = (splits / "test.jsonl").read_bytes()
    test_hash = hashlib.sha256(raw).hexdigest()
    if test_hash != manifest["splits"]["test"]["sha256"]:
        raise ValueError("Test data differs from the frozen experiment's split manifest")
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    labels = np.array([r["label"] for r in rows])
    if set(labels) != {0, 1}:
        raise ValueError("Test data must contain both labels")
    development_groups: set[str] = set()
    for name in ("train", "validation"):
        development = (experiment / f"{name}.jsonl").read_bytes()
        if hashlib.sha256(development).hexdigest() != manifest["splits"][name]["sha256"]:
            raise ValueError("Development data differs from the frozen experiment")
        development_groups.update(json.loads(line)["split_group"] for line in development.decode().splitlines())
    if development_groups & {r["split_group"] for r in rows}:
        raise ValueError("Test groups overlap development groups")
    output = experiment / "test_evaluation"
    output.mkdir(exist_ok=False)
    vectorizer = detector.vectorizer
    matrix = vectorizer.transform([r["text"] for r in rows])
    logistic = joblib.load(experiment / "logistic_regression.joblib")
    thresholds = json.loads((experiment / "thresholds.json").read_text())
    models = {"xgboost": detector.model, "logistic_regression": logistic}
    predictions = [dict(text=r["text"], label=r["label"], group=r["split_group"]) for r in rows]
    results = {}
    for name in ("xgboost", "logistic_regression", "regex"):
        if name == "regex":
            scores = np.array([int(bool(intent.INJECTION.search(r["text"]))) for r in rows])
            threshold = 0.5
        else:
            scores = models[name].predict_proba(matrix)[:, 1]
            threshold = thresholds[name]
        latency = []
        for row in rows:
            started = perf_counter()
            if name == "regex":
                intent.INJECTION.search(row["text"])
            else:
                models[name].predict_proba(vectorizer.transform([row["text"]]))
            latency.append(1000 * (perf_counter() - started))
        results[name] = dict(metrics=metrics(labels, scores, threshold),
                             latency_ms=dict(median=float(np.median(latency)),
                                             p95=float(np.percentile(latency, 95))))
        for prediction, score in zip(predictions, scores, strict=True):
            prediction[name] = dict(score=float(score), flagged=bool(score >= threshold))
    report = dict(evaluated_split="test", threshold_selection="Frozen from validation; no retuning",
                  rows=len(rows), test_sha256=test_hash, models=results,
                  regex=dict(pattern=intent.INJECTION.pattern, flags=intent.INJECTION.flags),
                  experiment_sha256={name: hashlib.sha256((experiment / name).read_bytes()).hexdigest()
                                     for name in (*injection_detector.RUNTIME_FILES, "logistic_regression.joblib")},
                  limitations="Internal public dataset only; semantic attack family overlap may remain. "
                              "Not an external benchmark or reviewed marketplace evaluation. "
                              "Runtime scans multiple metadata fields; text-level false positive rates do not "
                              "establish per-purchase false positive rates.")
    (output / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "evaluate_injection.py").write_bytes(Path(__file__).read_bytes())
    (output / "predictions.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in predictions))
    lines = ["# Frozen injection models: reserved test results", "",
             "Thresholds were selected on validation and frozen before this test. No fitting or tuning here.", "",
             "| Model | Threshold | Attacks caught | Benign flagged | Recall | Precision | FPR | Median ms |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, result in results.items():
        m = result["metrics"]
        lines.append(f"| {name} | {m['threshold']:.6f} | {m['true_positive']}/{sum(labels == 1)} | "
                     f"{m['false_positive']}/{sum(labels == 0)} | {m['recall']:.1%} | {m['precision']:.1%} | "
                     f"{m['false_positive_rate']:.1%} | {result['latency_ms']['median']:.3f} |")
    lines += ["", str(report["limitations"]), "", "Latency includes vectorization and prediction, with models loaded."]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nReport: {output / 'report.md'}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--splits", type=Path, default=ROOT / "data/injection/generated/splits")
    args = parser.parse_args()
    evaluate(args.experiment, args.splits)


if __name__ == "__main__":
    main()
