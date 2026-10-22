from __future__ import annotations

from typing import Any, Dict

from bioma.backends.analysis.workspace import AnalysisState
from bioma.backends.analysis.services.plotting_service import analyze_plots, generate_plots


def run_plotting_node(state: AnalysisState) -> Dict[str, Any]:
    """
    Plotting node in the new architecture:
    Phase A: generate plots
    Phase B: run plot-level analysis (old viz_analysis semantics)
    """
    task_id = str(state.get("task_id") or "task")

    stable_requirement = state.get("stable_requirement") or {}
    data_profile = state.get("data_profile") or {}
    dataset_bundle = state.get("dataset_bundle") or {}
    chart_plan = state.get("chart_plan") or []
    chart_data_ready_results = state.get("chart_data_ready_results") or {}
    analysis_results = state.get("analysis_results") or {}

    if not dataset_bundle:
        return {
            "visualizations": [],
            "visualizations_code": [],
            "visualization_log": [],
            "plotting_issues": [
                {
                    "code": "MISSING_DATASET_BUNDLE",
                    "severity": "high",
                    "message": "run_plotting_node requires dataset_bundle.",
                }
            ],
            "error_code": "MISSING_DATASET_BUNDLE",
            "error_detail": "No dataset_bundle found in state.",
            "status": "failed",
        }

    if not data_profile:
        return {
            "visualizations": [],
            "visualizations_code": [],
            "visualization_log": [],
            "plotting_issues": [
                {
                    "code": "MISSING_DATA_PROFILE",
                    "severity": "high",
                    "message": "run_plotting_node requires data_profile.",
                }
            ],
            "error_code": "MISSING_DATA_PROFILE",
            "error_detail": "No data_profile found in state.",
            "status": "failed",
        }

    deterministic_artifacts = (
        chart_data_ready_results.get("deterministic_artifacts") or {}
    )
    if not chart_plan and not deterministic_artifacts:
        return {
            "visualizations": [],
            "visualizations_code": [],
            "visualization_log": [],
            "plotting_issues": [],
            "error_code": None,
            "error_detail": None,
            "status": "active",
        }

    # Phase A: generate plots
    gen_out = generate_plots(
        task_id=task_id,
        stable_requirement=stable_requirement,
        data_profile=data_profile,
        dataset_bundle=dataset_bundle,
        chart_plan=chart_plan,
        chart_data_ready_results=chart_data_ready_results,
        artifact_root=str(state.get("artifact_root") or "artifacts"),
    )

    visuals = gen_out.get("visualizations") or []
    code_paths = list(gen_out.get("visualizations_code") or [])
    logs = list(gen_out.get("visualization_log") or [])
    issues = list(gen_out.get("issues") or [])

    # Phase B: plot-level analysis
    ana_out = analyze_plots(
        data_profile=data_profile,
        visualizations=visuals,
        task_id=task_id,
        stable_requirement=stable_requirement,
        analysis_results=analysis_results,
        chart_plan=chart_plan,
        artifact_root=str(state.get("artifact_root") or "artifacts"),
    )

    visuals = ana_out.get("visualizations") or visuals
    code_paths.extend(list(ana_out.get("visualizations_code") or []))
    logs.extend(list(ana_out.get("visualization_log") or []))
    issues.extend(list(ana_out.get("issues") or []))

    return {
        "visualizations": visuals,
        "visualizations_code": code_paths,
        "visualization_log": logs,
        "plotting_issues": issues,
        "error_code": None,
        "error_detail": None,
        "status": "active",
    }
