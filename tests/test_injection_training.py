"""Validate threshold policy and ensure held out text cannot enter the vocabulary."""

import numpy as np
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("xgboost")

from scripts.train_injection import features, select_threshold  # noqa: E402


def test_threshold_obeys_false_positive_budget_and_preserves_best_recall():
    selected = select_threshold([0, 0, 1, 1], [0.1, 0.7, 0.6, 0.9], max_fpr=0)
    assert selected["false_positive"] == 0
    assert selected["true_positive"] == 1
    assert selected["threshold"] == 0.9


def test_equal_scores_can_require_flagging_nothing():
    selected = select_threshold([0, 1], [1.0, 1.0], max_fpr=0)
    assert selected["threshold"] > 1
    assert selected["false_positive"] == selected["true_positive"] == 0


def test_transform_does_not_learn_validation_vocabulary():
    vectorizer = features()
    vectorizer.fit_transform(["ordinary seller data", "ordinary seller pricing"])
    before = vectorizer.get_feature_names_out().copy()
    vectorizer.transform(["validationonlytoken"])
    np.testing.assert_array_equal(before, vectorizer.get_feature_names_out())
    assert not any("validationonlytoken" in feature for feature in before)
