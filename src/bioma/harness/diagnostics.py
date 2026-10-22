"""Structured failure facts for Agent-directed repair without prescribing decisions."""

from __future__ import annotations

import json
import re
from pathlib import Path


_PYTHON_MODULE = re.compile(
    r"ModuleNotFoundError:\s*No module named\s*['\"]([A-Za-z0-9_.-]+)['\"]"
)
_R_PACKAGE = re.compile(
    r"there is no package called\s*[‘'\"]([A-Za-z0-9_.-]+)[’'\"]",
    re.IGNORECASE,
)


def retry_assessment(retryable):
    """Scope backend retry metadata to an immediate unchanged replay."""
    return {
        "automatic_unchanged_replay": "eligible" if retryable else "not_indicated",
        "scope": "Same action identity under the observed request and resource conditions.",
        "future_revisit": (
            "Not decided by this signal. A later Agent-selected action must be evaluated "
            "against its actual purpose, authority, request identity, and current resource state."
        ),
    }


def _process_facts(exc):
    receipt_path = getattr(exc, "receipt_path", None)
    if not receipt_path:
        return [], [], []
    try:
        payload = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        process = payload.get("process") or {}
    except (OSError, ValueError, TypeError):
        return [], [], []
    engine = str(process.get("engine") or "unknown")
    observed = [{"kind": "process_exit", "engine": engine,
                 "exit_code": process.get("exit_code")}]
    confirmed = ["An admitted analysis process executed and returned a non-success exit status."]
    unmet = ["A successful execution receipt is still required before scientific delivery."]
    stderr = str(process.get("stderr") or "")
    match = _PYTHON_MODULE.search(stderr) if engine == "python" else _R_PACKAGE.search(stderr)
    if match:
        observed.append({
            "kind": "process_reported_missing_dependency",
            "engine": engine,
            "dependency": match.group(1),
            "report_source": "stderr",
            "environment_availability": "not_checked",
            "runtime_installation_attempted": False,
        })
        unmet.append(
            "Dependency availability must be checked in the exact execution environment before it can be treated as unavailable."
        )
    return observed, confirmed, unmet


def diagnostic_context(tool_name, exc, *, requested_tool=None):
    """Return bounded observations and constraints, never an automatic repair plan."""
    from bioma.harness.delivery_review import DeliveryReviewRejected
    from bioma.harness.recovery import FailedAnalysisExecution

    domain = "engineering"
    stage = "tool_execution"
    observed = [{"kind": "exception", "type": type(exc).__name__}]
    confirmed = []
    unmet = []
    retained = []
    available = []
    prohibited = [
        "Do not treat a failed action or retained partial file as a scientific result.",
        "Do not fabricate missing inputs, measurements, mappings, or conclusions.",
    ]
    retryable = bool(getattr(exc, "retriable", False))

    from bioma.tools.errors import ToolError
    if isinstance(exc, ToolError):
        observed.append({
            "kind": "tool_error",
            "error_type": exc.error_type,
            "backend": exc.backend,
            "retry_after_seconds": exc.retry_after_seconds,
        })
        confirmed.append(
            "The configured tool invocation failed with a Runtime-normalized backend error."
        )
        unmet.append(
            "The requested resource or operation has not been successfully acquired or completed."
        )

    if isinstance(exc, FailedAnalysisExecution):
        stage = "execution"
        process_observed, process_confirmed, process_unmet = _process_facts(exc)
        observed.extend(process_observed)
        confirmed.extend(process_confirmed)
        unmet.extend(process_unmet)
        if getattr(exc, "retained_outputs", None):
            retained.append(exc.retained_outputs)
        available.extend(["inspect_inputs", "inspect_input_data", "revise_analysis_code"])
        prohibited.append("Do not install packages from generated analysis code or enable arbitrary network access.")
        retryable = True
    elif isinstance(exc, DeliveryReviewRejected):
        issues = list(exc.issues)
        scientific = [row for row in issues if row.get("category") == "scientific_support"]
        domain = "scientific" if scientific else "contract"
        stage = "result_review"
        observed = [{"kind": "review_issue", "issue": row} for row in issues]
        confirmed = ["The computation completed, but the submitted delivery failed independent review."]
        unmet = list(dict.fromkeys(
            str(row.get("support_gap") or row.get("message") or "").strip()
            for row in issues
            if str(row.get("support_gap") or row.get("message") or "").strip()
        ))
        available.extend(["read_workspace", "inspect_input_data", "revise_analysis_code"])
        prohibited.append("Do not weaken, delete, or invent scientific requirements merely to pass review.")
        retryable = True
    else:
        available.append("find_scientific_tools" if tool_name == "invoke_scientific_tool" else tool_name)

    return {
        "failure_domain": domain,
        "failure_stage": stage,
        "failed_action": requested_tool or tool_name,
        "observed_facts": observed,
        "confirmed_facts": confirmed,
        "unmet_conditions": unmet,
        "retained_resources": retained,
        "available_capabilities": list(dict.fromkeys(available)),
        "prohibited_actions": prohibited,
        "retry_assessment": retry_assessment(retryable),
        "agent_decision_required": True,
    }


def input_diagnostic(*, observed, unmet_conditions):
    return {
        "failure_domain": "engineering",
        "failure_stage": "input_inspection",
        "failed_action": "inspect_input_data",
        "observed_facts": list(observed),
        "confirmed_facts": ["The admitted file bytes were inspected without treating them as scientific data."],
        "unmet_conditions": list(unmet_conditions),
        "retained_resources": [],
        "available_capabilities": ["inspect_input_data", "execute_analysis_code"],
        "prohibited_actions": [
            "Do not interpret archive bytes or decoding errors as missing scientific measurements.",
            "Do not extract unsafe absolute or parent-traversal archive paths.",
        ],
        "retry_assessment": retry_assessment(True),
        "agent_decision_required": True,
    }
