"""Optional local XGBoost screen of untrusted seller/catalog metadata, before Claude's cache.

SENTINEL_INJECTION_MODEL_DIR selects a trusted local training run. No downloads or training at runtime.
An unset directory keeps this optional layer disabled. Configured load failures refuse startup;
inference failures hold purchases. An explicit owner Allow once retains its existing semantics.
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

from sentinel import ROOT, intent

log = logging.getLogger("sentinel.injection_detector")
ENV = "SENTINEL_INJECTION_MODEL_DIR"
MAX_TEXT_CHARS = 20_000
RUNTIME_FILES = ("vectorizer.joblib", "xgboost.json", "thresholds.json", "environment.json")


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
    model: Any
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
            for package in ("scikit-learn", "xgboost", "joblib", "numpy", "scipy"):
                if version(package) != environment["packages"][package]:
                    raise ValueError(f"Install the training environment's {package} version")
            # Imported only for an enabled detector. These are trusted local artifacts, not seller uploads.
            import joblib
            from xgboost import XGBClassifier

            threshold = json.loads((directory / "thresholds.json").read_text())["xgboost"]
            # Training may select nextafter(1, +inf) to flag nothing under a strict FPR budget.
            if type(threshold) not in (int, float) or not math.isfinite(threshold) \
                    or not 0 <= threshold <= math.nextafter(1.0, math.inf):
                raise ValueError("Invalid XGBoost threshold")
            vectorizer = joblib.load(directory / "vectorizer.joblib")
            model = XGBClassifier(n_jobs=2)
            model.load_model(directory / "xgboost.json")
            if vectorizer.transform([""]).shape[1] != model.n_features_in_:
                raise ValueError("Vectorizer and classifier feature counts differ")
            if list(model.classes_) != [0, 1]:
                raise ValueError("Classifier must use benign=0 and injection=1")
            detector = cls(vectorizer, model, float(threshold))
            detector.score(["Local startup check"])
            return detector
        except Exception as error:
            raise DetectorUnavailable("Could not load the configured injection detector") from error

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
            probabilities = self.model.predict_proba(self.vectorizer.transform(inputs))
            if probabilities.shape != (len(inputs), 2):
                raise ValueError("Invalid classifier output shape")
            scores = [float(row[1]) for row in probabilities]
            if any(not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
                raise ValueError("Invalid classifier score")
            return max(scores)
        except Exception as error:
            raise DetectorUnavailable("The injection detector could not score seller metadata") from error


@lru_cache(maxsize=1)
def get_detector() -> InjectionDetector | None:
    """One detector per process, eagerly loaded by the web lifespan and reused by payment requests."""
    configured = os.environ.get(ENV, "").strip()
    if not configured:
        return None
    directory = Path(configured).expanduser()
    if not directory.is_absolute():
        directory = ROOT / directory
    return InjectionDetector.load(directory)


def initialize() -> None:
    detector = get_detector()
    log.info("local injection detector %s", "enabled" if detector is not None else "disabled")


def review(seller: intent.Seller, listing: intent.Listing | None) -> intent.Verdict | None:
    """Return only a hold or no opinion. A low score can never authorize a payment."""
    try:
        detector = get_detector()
        if detector is None:
            return None
        fields = [seller.description, seller.service_name, *seller.tags]
        if listing is not None:
            fields.extend((listing.name, listing.description))
        texts = [text for text in fields if text and text.strip()]
        score = detector.score(texts)
        log.info("injection screen score=%.6f threshold=%.6f", score, detector.threshold)
        if score >= detector.threshold:
            return intent.Verdict(
                intent.ESCALATE, 1, "injection_ml",
                "Seller or catalog text may contain instructions aimed at the buying agent. "
                "Nothing was paid. The owner must review this purchase.",
            )
    except Exception as error:
        log.warning("injection screen unavailable (%s)", type(error).__name__)
        return intent.Verdict(
            intent.ESCALATE, 1, "injection_unavailable",
            "The seller's text could not be checked for prompt injection. Nothing was paid. "
            "The owner must review this purchase.",
        )
    return None
