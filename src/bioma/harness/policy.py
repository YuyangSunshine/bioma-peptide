"""Execution prerequisites and receipt-based delivery feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bioma.core.reporting import assignment_needs_pdf

from bioma.harness.types import HarnessContext, ToolObservation


def delivery_revision_pending(context: HarnessContext) -> bool:
    """Return whether the current reviewed version still has active findings."""
    if context.state.get("delivery_complete"):
        return False
    return bool(
        context.state.get("delivery_review_rejected")
        or context.state.get("delivery_review_incomplete")
    )


def release_revision_for_new_candidate(
    context: HarnessContext, receipt_ref: str | None
) -> bool:
    """Release an old review control only for a new contract-complete package."""
    if not receipt_ref or not delivery_revision_pending(context):
        return False
    candidates = investigation_delivery_readiness(context)["reviewable_support"]
    if receipt_ref not in {item["receipt_ref"] for item in candidates}:
        return False
    context.state.pop("delivery_review_rejected", None)
    context.state.pop("delivery_review_incomplete", None)
    context.state.pop("review_budget_insufficient", None)
    return True


def investigation_delivery_readiness(context: HarnessContext) -> dict[str, Any]:
    """Derive whether an executed package can be submitted for review.

    This is deliberately a delivery-contract check, not scientific qualification.
    It only uses admitted receipts, immutable artifact paths, and the work-package
    deliverable names.  ``finish_investigation`` performs the authoritative hash,
    provenance, reproduction, and independent-review checks.
    """
    expected = {
        str(name) for name in context.state.get("desired_deliverables") or []
        if isinstance(name, str) and name
    }
    requirements = context.state.get("execution_requirements") or {}
    summary_required = bool(requirements.get("summary_required")) or not expected
    candidates: list[dict[str, Any]] = []
    from bioma.harness.execution_view import delivery_support_packages, execution_view
    view = execution_view(context)
    eligible = {row['execution_id'] for row in view['records']
                if row['checks'].get('contract_review_candidate')}
    for receipt_ref, raw in delivery_support_packages(context).items():
        if receipt_ref not in eligible:
            continue
        if not isinstance(raw, dict) or raw.get("status") != "ok":
            continue
        artifacts = [str(path) for path in raw.get("artifacts") or []]
        metrics = raw.get("metrics") or {}
        if summary_required and not (
            isinstance(metrics.get("summary"), dict) and metrics["summary"]
        ):
            continue
        receipt = Path(receipt_ref)
        output_dir = receipt.parent / "outputs"
        names: set[str] = set()
        for artifact in artifacts:
            if metrics.get('support_kind') == 'professional_method':
                names.add(metrics['artifact_names'][artifact])
                continue
            try:
                names.add(Path(artifact).relative_to(output_dir).as_posix())
            except ValueError:
                # An artifact outside the receipt output directory is not a
                # work-package deliverable.  finish() will enforce this again.
                continue
        if assignment_needs_pdf(context.state) and not any(name.lower().endswith('.pdf') for name in names):
            continue
        missing = sorted(expected - names)
        if not missing and all(Path(path).is_file() for path in artifacts):
            candidates.append({
                "receipt_ref": str(receipt_ref),
                "support_kind": metrics.get('support_kind', 'computation'),
                "artifact_names": sorted(names),
            })
    return {
        "ready_for_review": bool(candidates),
        "expected_artifacts": sorted(expected),
        "pdf_required": assignment_needs_pdf(context.state),
        "reviewable_support": candidates,
        "reviewable_computations": [item for item in candidates
                                    if item['support_kind'] == 'computation'],
    }


def recommend_next_tools(context: HarnessContext) -> dict[str, Any]:
    """Expose delivery facts without inferring a scientific method from keywords."""
    readiness = investigation_delivery_readiness(context)
    if context.state.get("delivery_complete"):
        next_tools = []
        reason = "Independent review passed; retain the version-bound delivery."
    elif delivery_revision_pending(context):
        next_tools = []
        reason = (
            "Resolve or explicitly investigate the active review findings before "
            "resubmission; choose the appropriate action."
        )
    elif readiness["ready_for_review"]:
        next_tools = ["finish_investigation"]
        reason = "Support is available for review. Explicitly select the applicable receipt version; this is not scientific approval."
    else:
        next_tools = []
        reason = "Delivery requirements remain unresolved; choose actions using observed inputs and the contract."
    return {
        "next_tools": next_tools,
        "delivery_readiness": readiness,
        "reason": reason,
    }


def validate_tool_call(
    tool_name: str, context: HarnessContext
) -> ToolObservation | None:
    """Return a blocking observation when a tool is called before its prerequisites."""
    checks = _POLICY_CHECKS.get(tool_name)
    if not checks:
        return None

    missing = [message for predicate, message in checks if not predicate(context.state)]
    if not missing:
        return None

    return ToolObservation(
        tool_name=tool_name,
        status="error",
        summary="Tool call blocked by execution prerequisites because prerequisites are missing.",
        warnings=missing,
        metrics={"blocked_by_policy": True, "missing_prerequisites": missing},
    )


def _has_raw_files(state: dict[str, Any]) -> bool:
    return bool(state.get("raw_data_sources"))


def _has_transformed_sources(state: dict[str, Any]) -> bool:
    return bool(state.get("transformed_data_sources"))


def _has_dataset_bundle(state: dict[str, Any]) -> bool:
    return bool(state.get("dataset_bundle"))


def _has_data_profile(state: dict[str, Any]) -> bool:
    return bool(state.get("data_profile"))


def _has_analysis_results(state: dict[str, Any]) -> bool:
    return bool(state.get("analysis_results"))


def _has_chart_ready_data(state: dict[str, Any]) -> bool:
    return bool(state.get("chart_data_ready_results"))


def _has_visualizations(state: dict[str, Any]) -> bool:
    return bool(state.get("visualizations"))


def _has_final_report(state: dict[str, Any]) -> bool:
    return bool(state.get("final_report"))


def _has_lightcycler_melt_artifacts(state: dict[str, Any]) -> bool:
    deterministic = (state.get("chart_data_ready_results") or {}).get(
        "deterministic_artifacts"
    ) or {}
    lightcycler = deterministic.get("lightcycler_melt") or {}
    return bool(lightcycler.get("group_tm_statistics") or lightcycler.get("qc_summary"))


def _has_plottable_data(state: dict[str, Any]) -> bool:
    deterministic = (state.get("chart_data_ready_results") or {}).get(
        "deterministic_artifacts"
    ) or {}
    return bool(state.get("chart_plan") or deterministic)


_POLICY_CHECKS = {
    "prepare_data": ((_has_raw_files, "MISSING_RAW_DATA_SOURCES"),),
    "build_dataset_bundle": (
        (_has_transformed_sources, "MISSING_TRANSFORMED_DATA_SOURCES"),
    ),
    "run_analysis": ((_has_dataset_bundle, "MISSING_DATASET_BUNDLE"),),
    "derive_chart_data": ((_has_chart_ready_data, "MISSING_CHART_DATA_READY_RESULTS"),),
    "generate_plots": (
        (_has_dataset_bundle, "MISSING_DATASET_BUNDLE"),
        (_has_data_profile, "MISSING_DATA_PROFILE"),
        (_has_plottable_data, "MISSING_CHART_PLAN_OR_DETERMINISTIC_ARTIFACTS"),
    ),
    "analyze_plots": ((_has_visualizations, "MISSING_VISUALIZATIONS"),),
    "write_report": (
        (_has_data_profile, "MISSING_DATA_PROFILE"),
        (_has_analysis_results, "MISSING_ANALYSIS_RESULTS"),
    ),
    "validate_report": ((_has_final_report, "MISSING_FINAL_REPORT"),),
    "run_lightcycler_melt_qc": ((_has_dataset_bundle, "MISSING_DATASET_BUNDLE"),),
    "run_prediction_review": (
        (_has_lightcycler_melt_artifacts, "MISSING_LIGHTCYCLER_MELT_ARTIFACTS"),
    ),
    "generate_lightcycler_plots": (
        (_has_lightcycler_melt_artifacts, "MISSING_LIGHTCYCLER_MELT_ARTIFACTS"),
    ),
}


__all__ = [
    "delivery_revision_pending",
    "investigation_delivery_readiness",
    "recommend_next_tools",
    "release_revision_for_new_candidate",
    "validate_tool_call",
]
