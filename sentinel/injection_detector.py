"""Local XGBoost screen of untrusted seller and catalog text, after the deterministic rules and before Claude.

On by default with the model shipped in sentinel/models/injection/. SENTINEL_INJECTION_MODEL_DIR selects another
trusted training run, or "off" disables the screen. No downloads or training at runtime, and no XGBoost native
library: the saved trees are evaluated in plain Python (xgb_trees). A score at or over the threshold blocks the
payment and is recorded. A load failure refuses startup; a scoring failure blocks (fail closed).
"""

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any

from sentinel import ROOT, guard, intent, xgb_trees

log = logging.getLogger("sentinel.injection_detector")
ENV = "SENTINEL_INJECTION_MODEL_DIR"
OFF = "off"
DEFAULT_DIR = Path(__file__).resolve().parent / "models" / "injection"
MAX_TEXT_CHARS = 20_000
RUNTIME_FILES = ("vectorizer.joblib", "xgboost.json", "thresholds.json", "environment.json")
# The vectorizer is a pickle: these must match the training environment. XGBoost itself is not loaded.
RUNTIME_PACKAGES = ("scikit-learn", "joblib", "numpy", "scipy")


class DetectorUnavailable(RuntimeError):
    """The configured injection screen cannot safely produce a score."""


def write_manifest(directory: Path) -> None:
    """Seal the local runtime files after training; hashes detect mismatched or damaged artifacts."""
    manifest = dict(schema=1, model="xgboost", files={
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in RUNTIME_FILES
    })
    (directory / "detector_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


@dataclass(frozen=True)
class InjectionDetector:
    vectorizer: Any
    model: xgb_trees.Forest
    threshold: float

    @classmethod
    def load(cls, directory: Path) -> "InjectionDetector":
        try:
            manifest = json.loads((directory / "detector_manifest.json").read_text())
            if manifest["schema"] != 1 or manifest["model"] != "xgboost":
                raise ValueError("Unsupported detector manifest")
            for name in RUNTIME_FILES:
                if hashlib.sha256((directory / name).read_bytes()).hexdigest() != manifest["files"][name]:
                    raise ValueError("Detector artifacts do not match their manifest")
            environment = json.loads((directory / "environment.json").read_text())
            for package in RUNTIME_PACKAGES:
                if version(package) != environment["packages"][package]:
                    raise ValueError(f"Install the training environment's {package} version")
            # Imported only for an enabled detector. These are trusted local artifacts, not seller uploads.
            import joblib

            threshold = json.loads((directory / "thresholds.json").read_text())["xgboost"]
            # Training may select nextafter(1, +inf) to flag nothing under a strict FPR budget.
            if type(threshold) not in (int, float) or not math.isfinite(threshold) \
                    or not 0 <= threshold <= math.nextafter(1.0, math.inf):
                raise ValueError("Invalid XGBoost threshold")
            vectorizer = joblib.load(directory / "vectorizer.joblib")
            model = xgb_trees.Forest(json.loads((directory / "xgboost.json").read_text()))
            if vectorizer.transform([""]).shape[1] != model.num_features:
                raise ValueError("Vectorizer and classifier feature counts differ")
            detector = cls(vectorizer, model, float(threshold))
            detector.score(["Local startup check"])
            return detector
        except Exception as error:
            raise DetectorUnavailable("Could not load the configured injection detector") from error

    def score_matrix(self, matrix: Any) -> list[float]:
        """Injection probability per row of a vectorized (scipy CSR) matrix."""
        matrix = matrix.tocsr()
        return [self.model.score({int(i): float(v) for i, v in zip(
                    matrix.indices[matrix.indptr[r]:matrix.indptr[r + 1]],
                    matrix.data[matrix.indptr[r]:matrix.indptr[r + 1]], strict=True)})
                for r in range(matrix.shape[0])]

    def score(self, texts: list[str]) -> float:
        """Score fields and their combined text; no field is silently truncated or diluted by other fields."""
        if not texts:
            return 0.0
        try:
            if any(len(text) > MAX_TEXT_CHARS for text in texts):
                raise ValueError("Seller metadata exceeds the detector's input limit")
            combined = " ".join(texts)
            if len(combined) > MAX_TEXT_CHARS:
                raise ValueError("Combined metadata exceeds the detector's input limit")
            inputs = list(dict.fromkeys([*texts, combined]))
            scores = self.score_matrix(self.vectorizer.transform(inputs))
            if len(scores) != len(inputs):
                raise ValueError("Invalid classifier output shape")
            if any(not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
                raise ValueError("Invalid classifier score")
            return max(scores)
        except Exception as error:
            raise DetectorUnavailable("The injection detector could not score seller metadata") from error


def model_dir() -> Path | None:
    """The configured run, the shipped model when unset, or None when set to off."""
    configured = os.environ.get(ENV, "").strip()
    if configured.lower() == OFF:
        return None
    if not configured:
        return DEFAULT_DIR
    directory = Path(configured).expanduser()
    return directory if directory.is_absolute() else ROOT / directory


@lru_cache(maxsize=1)
def get_detector() -> InjectionDetector | None:
    """One detector per process, eagerly loaded by the web lifespan and reused by payment requests."""
    directory = model_dir()
    return None if directory is None else InjectionDetector.load(directory)


def initialize() -> None:
    detector = get_detector()
    log.info("local injection detector %s", "enabled" if detector is not None else "disabled")


def assess(seller: intent.Seller, listing: intent.Listing | None) -> guard.MLScore | None:
    """The model's score for this seller's text, or None when the detector is off. Raises DetectorUnavailable."""
    detector = get_detector()
    if detector is None:
        return None
    fields = [seller.description, seller.service_name, *seller.tags]
    if listing is not None:
        fields.extend((listing.name, listing.description))
    score = detector.score([text for text in fields if text and text.strip()])
    log.info("injection screen score=%.6f threshold=%.6f", score, detector.threshold)
    return guard.MLScore("injection", score, detector.threshold)


def verdict(score: guard.MLScore | None) -> intent.Verdict | None:
    """BLOCK at or over the threshold (score recorded), else no opinion. A low score never authorizes a payment."""
    if score is None or score.score < score.threshold:
        return None
    return intent.Verdict(
        intent.BLOCK, 1, "injection_ml",
        f"Sentinel's injection model scored the seller's text {score.score:.2f} (blocks at "
        f"{score.threshold:.2f}): it reads like instructions aimed at the buying agent. Nothing was paid.",
        ml=score,
    )


UNAVAILABLE = intent.Verdict(
    intent.BLOCK, 1, "injection_unavailable",
    "The seller's text could not be checked for prompt injection, so Sentinel blocked the payment. Nothing was paid.",
)


def review(seller: intent.Seller, listing: intent.Listing | None) -> intent.Verdict | None:
    """BLOCK at or over the threshold, BLOCK when scoring fails, else no opinion."""
    try:
        return verdict(assess(seller, listing))
    except Exception as error:
        log.warning("injection screen unavailable (%s)", type(error).__name__)
        return UNAVAILABLE
