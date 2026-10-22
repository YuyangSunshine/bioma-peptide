from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from bioma.llm import ChatProvider, parse_json_object


from bioma.core.protocols import VerificationReport


class ProtocolDesigner(ABC):
    @abstractmethod
    def generate(self, objective: str, prediction: dict[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    def review(self, objective: str, protocol_text: str) -> VerificationReport:
        raise NotImplementedError

    @abstractmethod
    def revise(
        self,
        objective: str,
        protocol_text: str,
        issues: tuple[str, ...],
    ) -> str:
        raise NotImplementedError


class LLMProtocolDesigner(ProtocolDesigner):
    """A configured model drafts and reviews the scientific protocol before automation."""

    def __init__(self, provider: ChatProvider) -> None:
        self.provider = provider

    def generate(self, objective: str, prediction: dict[str, Any]) -> str:
        prompt = (
            "Write a scientifically executable procedure limited to the objective and "
            "its execution mode, as plain text. Include controls, replicates, quantified "
            "parameters and measurements only as required by that objective, with acceptance criteria. "
            "For prerecorded data or fixture replay, preserve the supplied data and provenance; "
            "do not invent random data generation, noise models, new measurements or sample preparation. "
            "Do not generate JSON or machine code.\n"
            f"Objective: {objective}\nPrediction: {json.dumps(prediction, default=str)}"
        )
        return self._chat(prompt).strip()

    def review(self, objective: str, protocol_text: str) -> VerificationReport:
        prompt = (
            "Review this protocol for scientific logic, controls, quantified parameters, "
            "and fidelity to the objective and execution mode. Unrequested data generation or "
            "experimental scope expansion is a blocking defect. A supplied-data replay does not "
            "require a new data-generation algorithm or new measurements. Assess provenance "
            "and reproducibility. Unavailable optional scientific information is not itself a protocol defect. "
            "An explicitly recorded unavailable-evidence state satisfies a request to report that status; "
            "reject it only when the experimental logic truly requires the missing information and has no "
            "valid alternative. Judge required experimental groups, controls, replicates, measurable endpoints, "
            "acceptance criteria, executable logic, safety-critical omissions, and required resources. "
            "Do not invent unstated requirements such as distinct mutation positions, extra assays, "
            "or upstream computational deliverables. Review only the study objective supplied here. "
            'List only blocking scientific or reproducibility defects in issues; optional improvements are not failures. Set passed=true only when issues is empty. Return JSON only: {"passed": bool, "issues": [str]}.\n'
            f"Objective: {objective}\nProtocol:\n{protocol_text}"
        )
        payload = parse_json_object(self._chat(prompt, json_mode=True))
        issues_value = payload.get("issues")
        if not isinstance(issues_value, list):
            raise ValueError("Protocol review issues must be a list.")
        issues = tuple(str(item) for item in issues_value)
        return VerificationReport(payload.get("passed") is True and not issues, issues)

    def revise(
        self,
        objective: str,
        protocol_text: str,
        issues: tuple[str, ...],
    ) -> str:
        prompt = (
            "Revise the scientific protocol to resolve every issue. Return the complete "
            "procedure within the original objective and execution mode. Preserve supplied-data "
            "replay boundaries; remove invented data generation rather than expanding it. Preserve "
            "explicit unavailable-evidence records when the missing information is optional, and do not "
            "turn optional scientific capabilities into experimental prerequisites. Return the "
            "protocol as plain text, not JSON or machine code.\n"
            f"Objective: {objective}\nIssues: {json.dumps(issues)}\n"
            f"Protocol:\n{protocol_text}"
        )
        return self._chat(prompt).strip()

    def _chat(self, prompt: str, *, json_mode: bool = False) -> str:
        return self.provider.chat(
            [{"role": "user", "content": prompt}], json_mode=json_mode
        )


class ToyProtocolDesigner(ProtocolDesigner):
    def __init__(self, *, fail_first_review: bool = False) -> None:
        self.fail_first_review = fail_first_review
        self.review_count = 0

    def generate(self, objective: str, prediction: dict[str, Any]) -> str:
        candidate = str(prediction.get("design") or "GFP-candidate")
        return (
            f"Objective: {objective}\n"
            f"Candidate: {candidate}\n"
            "Controls: wild_type\n"
            "Replicates: three independent samples per group.\n"
            "1. Prepare candidate and wild_type samples at equal concentration.\n"
            "2. Apply a 60°C thermal challenge for 30 minutes.\n"
            "3. Measure fluorescence and compare stability_score with wild_type."
        )

    def review(self, objective: str, protocol_text: str) -> VerificationReport:
        self.review_count += 1
        if self.fail_first_review and self.review_count == 1:
            return VerificationReport(False, ("add a wild-type control",))
        if "wild_type" not in protocol_text:
            return VerificationReport(False, ("add a wild-type control",))
        return VerificationReport(True)

    def revise(
        self,
        objective: str,
        protocol_text: str,
        issues: tuple[str, ...],
    ) -> str:
        return f"{protocol_text}\nControls: wild_type\nRectified: {'; '.join(issues)}"
