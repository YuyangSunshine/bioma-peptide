"""
Unified parameter processing entry:
1) construct
2) fill
3) validate

This module centralizes parameter construction/filling/validation logic.
"""

from __future__ import annotations

from typing import Dict, Any, List, Tuple

from bioma.backends.automation.parameters.parameter_builder import parameter_builder
from bioma.backends.automation.parameters.parameter_filler import parameter_builder as parameter_filler
from bioma.backends.automation.registry.resource_registry import resource_registry


class ParameterProcessor:
    """
    Unified parameter processor facade.
    """

    def __init__(self):
        self.builder = parameter_builder
        self.filler = parameter_filler
        self.registry = resource_registry

    def construct(self, simplified_flow: Dict[str, Any]) -> Dict[str, Any]:
        return self.builder.build_experiment_flow(simplified_flow)

    def fill(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        return self.filler.build_node(operation)

    def validate(self, node: Dict[str, Any]) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        ok, msg = self.registry.validate_node_resource(node)
        if not ok and msg:
            issues.append(msg)
        return ok, issues


parameter_processor = ParameterProcessor()
