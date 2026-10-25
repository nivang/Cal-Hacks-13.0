"""Evaluate a saved binary:logistic XGBoost model (its JSON file) in plain Python.

The server never loads XGBoost's native library (it needs OpenMP, which macOS lacks by default).
Rows are sparse: {feature index: value}. An absent feature is missing and follows the node's default
branch, exactly as XGBoost treats entries left out of a scipy sparse matrix. Values compare in float32.
"""

import math
import struct
from typing import Any


def _f32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


class Forest:
    def __init__(self, dump: dict[str, Any]) -> None:
        learner = dump["learner"]
        if learner["objective"]["name"] != "binary:logistic":
            raise ValueError("Expected a binary:logistic model")
        booster = learner["gradient_booster"]
        if booster["name"] != "gbtree":
            raise ValueError("Expected a gbtree booster")
        base = float(str(learner["learner_model_param"]["base_score"]).strip("[]"))
        if not 0 < base < 1:
            raise ValueError("Invalid base score")
        self.base_margin = math.log(base / (1 - base))
        self.num_features = int(learner["learner_model_param"]["num_feature"])
        self.trees = []
        for tree in booster["model"]["trees"]:
            if any(tree["split_type"]):
                raise ValueError("Categorical splits are not supported")
            self.trees.append((tree["left_children"], tree["right_children"], tree["split_indices"],
                               [_f32(c) for c in tree["split_conditions"]], tree["default_left"]))
        if not self.trees:
            raise ValueError("Model has no trees")

    def margin(self, row: dict[int, float]) -> float:
        x = {index: _f32(value) for index, value in row.items()}
        total = self.base_margin
        for left, right, index, condition, default_left in self.trees:
            node = 0
            while left[node] != -1:
                value = x.get(index[node])
                if value is None or math.isnan(value):
                    node = left[node] if default_left[node] else right[node]
                else:
                    node = left[node] if value < condition[node] else right[node]
            total += condition[node]  # a leaf keeps its value in split_conditions
        return total

    def score(self, row: dict[int, float]) -> float:
        margin = self.margin(row)
        return 1 / (1 + math.exp(-margin)) if margin >= 0 else math.exp(margin) / (1 + math.exp(margin))
