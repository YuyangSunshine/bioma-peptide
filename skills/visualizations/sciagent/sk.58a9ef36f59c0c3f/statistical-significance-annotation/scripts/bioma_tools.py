"""Callable p-value annotation extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class SignificanceAnnotationInput(ToolInput):
    p_values: list[float] = Field(min_length=1, description="Raw or adjusted p-values")
    correction: Literal["none", "bonferroni", "benjamini_hochberg"] = Field(default="none", description="Multiple-testing correction")


@bioma_tool(
    name="sciagent_annotate_significance",
    description="Optionally adjust p-values and convert them to standard ns/*/**/***/**** annotations.",
    category="analysis",
    author="SciAgent",
    args_schema=SignificanceAnnotationInput,
)
def annotate_significance(p_values: list[float], correction: str = "none") -> str:
    if any(value < 0 or value > 1 for value in p_values):
        raise ValueError("p-values must be between 0 and 1")
    count = len(p_values)
    if correction == "bonferroni":
        adjusted = [min(1.0, value * count) for value in p_values]
    elif correction == "benjamini_hochberg":
        order = sorted(range(count), key=p_values.__getitem__)
        adjusted = [0.0] * count
        running = 1.0
        for rank_index in range(count - 1, -1, -1):
            original_index = order[rank_index]
            running = min(running, p_values[original_index] * count / (rank_index + 1))
            adjusted[original_index] = min(1.0, running)
    else:
        adjusted = list(p_values)

    def label(value: float) -> str:
        if value <= 0.0001:
            return "****"
        if value <= 0.001:
            return "***"
        if value <= 0.01:
            return "**"
        if value <= 0.05:
            return "*"
        return "ns"

    return json.dumps(
        {"correction": correction, "raw_p_values": p_values, "adjusted_p_values": adjusted, "annotations": [label(value) for value in adjusted]}
    )
