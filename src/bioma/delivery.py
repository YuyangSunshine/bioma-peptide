from __future__ import annotations

import re
from pathlib import Path

from bioma.core.external_tasks import (
    CoverageSufficiency, DeliveryState, DeliveryStatus, ExternalTaskContract,
    assess_external_coverage, validate_delivery,
)
from bioma.llm import ChatProvider
from bioma.execution_context import TaskExecutionContext


class ArtifactDeliveryService:
    """Generate and materialize explicit task artifacts outside ScientificState."""

    def __init__(self, provider: ChatProvider, *, max_coverage_repairs: int = 1) -> None:
        self.provider = provider
        self.max_coverage_repairs = max_coverage_repairs

    def deliver(self, contract: ExternalTaskContract, workspace: Path) -> DeliveryState:
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        explicit_context = all(key in contract.execution_context for key in (
            "task_root", "input_root", "artifact_root", "output_root",
            "working_directory", "writable_roots"))
        context = (TaskExecutionContext.from_mapping(contract.execution_context, task_root=workspace)
                   if explicit_context else None)
        if context is not None:
            context.prepare()
        state = DeliveryState.from_contract(contract)
        state.transition(DeliveryStatus.IN_PROGRESS)
        coverage = assess_external_coverage(contract, max_repairs=self.max_coverage_repairs)
        if coverage.outcome != CoverageSufficiency.SUFFICIENT:
            state.transition(DeliveryStatus.FAILED, reason="; ".join(coverage.reasons))
            return state
        try:
            for obligation in contract.artifact_obligations:
                target = ((context.artifact_path(obligation.path) if context is not None
                           else workspace / obligation.path)).resolve()
                if workspace not in target.parents:
                    raise ValueError("artifact target escapes workspace")
                target.parent.mkdir(parents=True, exist_ok=True)
                content = self.provider.chat(self._messages(contract, obligation, context))
                target.write_text(_artifact_content(content, obligation.kind), encoding="utf-8")
            return validate_delivery(contract, workspace, state,
                                     artifact_root=context.artifact_root if context else None)
        except Exception as exc:
            if state.status in {DeliveryStatus.IN_PROGRESS, DeliveryStatus.DELIVERED}:
                state.transition(DeliveryStatus.FAILED, reason=f"{type(exc).__name__}: {exc}")
            return state

    @staticmethod
    def _messages(contract, obligation, context=None):
        return [
            {"role": "system", "content": (
                "Produce the complete content of exactly one requested artifact. "
                "Return only its content, optionally in one matching code fence. "
                "Do not claim completion, describe your reasoning, or emit other files.")},
            {"role": "user", "content": (
                f"Objective:\n{contract.objective}\n\n"
                f"Public inputs:\n" + "\n".join(contract.public_inputs) + "\n\n"
                f"Execution context:\n{context.prompt_contract() if context else contract.execution_context}\n\n"
                f"Artifact name: {obligation.name}\nArtifact kind: {obligation.kind}\n"
                f"Required path: {obligation.path}")},
        ]


def _artifact_content(content: str, kind: str) -> str:
    match = re.fullmatch(r"\s*```(?:python|py|json|csv|tsv|markdown|md|text)?\s*\n(.*?)\n```\s*", content, re.DOTALL)
    return match.group(1) + "\n" if match else content
