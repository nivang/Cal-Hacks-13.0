"""Download public labels and merge reviewed marketplace examples. Standard library only."""

import argparse
import hashlib
import json
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DATASET = "deepset/prompt-injections"


def text_key(text: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def public_rows(raw_dir: Path) -> list[dict]:
    result = []
    for split in ("train", "test"):
        offset = 0
        while True:
            query = urlencode(dict(dataset=DATASET, config="default", split=split, offset=offset, length=100))
            with urlopen(f"https://datasets-server.huggingface.co/rows?{query}", timeout=60) as response:
                raw = response.read()
            (raw_dir / f"{split}-{offset}.json").write_bytes(raw)
            page = json.loads(raw)
            rows = page["rows"]
            if not rows:
                raise ValueError(f"Unexpected empty page: {split}, offset {offset}")
            for entry in rows:
                if entry.get("truncated_cells"):
                    raise ValueError("Dataset server truncated a cell; use the original dataset files instead")
                row = entry["row"]
                result.append(dict(
                    text=row["text"], label=row["label"], source=DATASET,
                    source_split=split, source_row=entry["row_idx"], synthetic=None,
                    label_origin="upstream", family=None,
                ))
            offset += len(rows)
            if offset >= page["num_rows_total"]:
                break
    return result


def assemble(rows: list[dict]) -> list[dict]:
    unique = {}
    for row in rows:
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("Every example needs nonempty text")
        if type(row.get("label")) is not int or row["label"] not in (0, 1):
            raise ValueError("Labels must be integers: 0 or 1")
        key = text_key(row["text"])
        if key in unique:
            if unique[key]["label"] != row["label"]:
                raise ValueError(f"Conflicting labels for text hash {key}; review before proceeding")
            unique[key]["provenance"].append(row)
        else:
            unique[key] = dict(
                text=row["text"], label=row["label"], source=row["source"],
                group=f"text:{key}", provenance=[row],
            )
    return list(unique.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", type=Path, default=ROOT / "data/injection/marketplace.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "data/injection/generated")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows = public_rows(raw_dir)
    public_count = len(rows)
    pending = 0
    for line in args.manual.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("reviewed") is not True:
            pending += 1
            continue
        if not row.get("reviewer") or not row.get("rationale"):
            raise ValueError("Reviewed examples need a reviewer and rationale")
        if type(row.get("synthetic")) is not bool or not row.get("source") or not row.get("family"):
            raise ValueError("Manual examples need synthetic, source, and family fields")
        row["label_origin"] = "manual"
        rows.append(row)
    clean = assemble(rows)
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in clean)
    (args.output / "examples.jsonl").write_text(payload)
    manifest = dict(
        collected_at=datetime.now(UTC).isoformat(), dataset=DATASET,
        dataset_url=f"https://huggingface.co/datasets/{DATASET}", license="Apache-2.0",
        public_rows=public_count, reviewed_manual_rows=len(rows) - public_count,
        pending_manual_rows=pending, unique_rows=len(clean),
        labels=dict(Counter(row["label"] for row in clean)),
        sha256=hashlib.sha256(payload.encode()).hexdigest(),
        grouping="Normalized identical text only; families and paraphrases require review before splitting.",
        snapshot="Raw API pages retained; collection is not pinned to an upstream revision.",
    )
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
