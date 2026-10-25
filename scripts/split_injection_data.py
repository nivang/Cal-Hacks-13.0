"""Create reproducible splits, preserving upstream test examples and grouping lexical near duplicates."""

import argparse
import hashlib
import json
import random
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def split_rows(rows: list[dict], seed: int = 42, validation_fraction: float = 0.2,
               similarity: float = 0.8) -> dict[str, list[dict]]:
    if not 0 < validation_fraction < 1 or not 0 < similarity <= 1:
        raise ValueError("Fractions must be between 0 and 1 (similarity may equal 1)")
    if not rows:
        raise ValueError("No input examples")
    for row in rows:
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("Every example needs nonempty text")
        if type(row.get("label")) is not int or row["label"] not in (0, 1):
            raise ValueError("Labels must be integers 0 or 1")
        if not row.get("provenance"):
            raise ValueError("Every example needs provenance to preserve upstream splits")
    parents = list(range(len(rows)))

    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def join(i: int, j: int) -> None:
        parents[find(j)] = find(i)

    seen: dict[tuple[str, ...], int] = {}
    shingles = []
    for i, row in enumerate(rows):
        text = normalize(row["text"])
        shingles.append({text[j:j + 5] for j in range(max(1, len(text) - 4))})
        keys: list[tuple[str, ...]] = [("text", text)]
        if row.get("group"):
            keys.append(("group", row["group"]))
        for p in row["provenance"]:
            if p.get("family"):
                keys.append(("family", p["source"], p["family"]))
        for key in keys:
            if key in seen:
                join(i, seen[key])
            seen[key] = i
    for i, a in enumerate(shingles):
        for j in range(i):
            b = shingles[j]
            if min(len(a), len(b)) / max(len(a), len(b)) < similarity:
                continue
            if len(a & b) / len(a | b) >= similarity:
                join(i, j)

    clusters = defaultdict(list)
    for i, row in enumerate(rows):
        clusters[find(i)].append(row)
    result: dict[str, list[dict]] = {name: [] for name in ("train", "validation", "test", "quarantine")}
    candidates = []
    for cluster in clusters.values():
        cluster_id = hashlib.sha256("\n".join(sorted(normalize(r["text"]) for r in cluster)).encode()).hexdigest()
        members = [dict(r, split_group=f"cluster:{cluster_id}") for r in cluster]
        is_test = lambda r: any(p.get("source_split") == "test" for p in r["provenance"])  # noqa: E731
        if any(is_test(r) for r in members):
            for row in members:
                # Never train on a near duplicate of an upstream test example.
                result["test" if is_test(row) else "quarantine"].append(row)
        else:
            candidates.append(members)
    random.Random(seed).shuffle(candidates)
    totals = Counter(r["label"] for cluster in candidates for r in cluster)
    targets = {label: count * validation_fraction for label, count in totals.items()}
    counts: Counter[int] = Counter()
    for cluster in candidates:
        added = Counter(r["label"] for r in cluster)
        before = sum((counts[label] - target) ** 2 for label, target in targets.items())
        after = sum((counts[label] + added[label] - target) ** 2 for label, target in targets.items())
        destination = "validation" if after < before else "train"
        result[destination].extend(cluster)
        if destination == "validation":
            counts.update(added)
    if any(not result[name] for name in ("train", "validation", "test")):
        raise ValueError("Grouping produced an empty split; inspect dataset families and group sizes")
    active_groups = [{r["split_group"] for r in result[name]} for name in ("train", "validation", "test")]
    assert all(not a & b for i, a in enumerate(active_groups) for b in active_groups[:i])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/injection/generated/examples.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "data/injection/generated/splits")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--similarity", type=float, default=0.8)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    splits = split_rows(rows, args.seed, args.validation_fraction, args.similarity)
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name, examples in splits.items():
        payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in examples)
        (args.output / f"{name}.jsonl").write_text(payload)
        summary[name] = dict(rows=len(examples), labels=dict(Counter(r["label"] for r in examples)),
                             sha256=hashlib.sha256(payload.encode()).hexdigest())
    manifest = dict(seed=args.seed, validation_fraction=args.validation_fraction,
                    similarity=args.similarity, input_sha256=hashlib.sha256(raw).hexdigest(), splits=summary,
                    method="Upstream test preserved; normalized text, supplied groups/families, and character "
                           "5-gram Jaccard clusters kept together. Test overlaps quarantined.",
                    limitation="Lexical similarity does not guarantee semantic paraphrase or attack family isolation.",
                    vocabulary="No vocabulary fitted. Fit the training vectorizer only on train.jsonl.",
                    external_benchmark="Not assembled; these are internal dataset splits only.")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
