"""Controlled scientific tools for the shared harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field

from bioma.harness.policy import recommend_next_tools
from bioma.harness.types import (
    HarnessContext,
    HarnessTool,
    HarnessToolInput,
    ToolObservation,
)


class EmptyInput(HarnessToolInput):
    pass


class InputSlice(HarnessToolInput):
    input_index: int = Field(ge=0, description='Index in the admitted INPUT_FILES list.')
    path: list[str] = Field(default_factory=list, description='Nested JSON keys or array indices, e.g. ["measurements", "32"]. CSV/TSV expose header and rows. Empty selects the root.')
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=30)


class TabularSchemaInput(HarnessToolInput):
    input_index: int = Field(ge=0, description='Index in the admitted INPUT_FILES list.')


class FilePathInput(HarnessToolInput):
    file_path: str = Field(..., description="Path to the input file.")


class OptionalTextInput(HarnessToolInput):
    text: str = Field(
        "",
        description="Optional text to parse. If empty, the current research request/state is used.",
    )


def build_default_tools() -> list[HarnessTool]:
    from bioma.harness.input_inspection import inspect_input_data, inspect_tabular_schema
    from bioma.harness.code_analysis import (
        AnalysisCodeInput, AnalysisRevisionInput, execute_analysis_code, revise_analysis_code,
    )
    return [
        HarnessTool(name='inspect_input_data', description='Read selected keys or rows from JSON/CSV/TSV/text inputs; gzip text supports path=[] for lines, ["header"] or ["rows"] for delimited data, and ["lines", "exact prefix"] to find metadata lines; XLSX/XLS uses the existing bounded workbook schema/sample (path=[], offset=0). For complete workbook calculations use pandas.read_excel on INPUT_FILES. No code execution, output files, report or publish_result required. Use for exploration before analysis or to diagnose a schema error; computations must still read original inputs.', handler=inspect_input_data, args_schema=InputSlice),
        HarnessTool(name='inspect_tabular_schema', description='Read bounded schema metadata and sample rows from an admitted XLSX, XLS, CSV or TSV input. This is a harness tool, not a Python global. It performs no analysis and never replaces reading the original INPUT_FILES in execution code.', handler=inspect_tabular_schema, args_schema=TabularSchemaInput),
        HarnessTool(name="execute_analysis_code", description="Compute scientific analyses, statistics and plots from real input files using Python or R (engine). input_indices is the exact generated-code read view; reuse_inputs is a separate trusted copy catalog staged only after code exits. Selective revisions must classify every output in artifact_dispositions. Runtime verifies hashes and dependency identities, never the scientific choice. Python receives only INPUT_FILES, OUTPUT_DIR, REQUEST, Path, write_json and publish_result as harness bindings. R receives analogous bindings. Declare report layouts separately. All declared files and execution receipts are checked. Never substitute engines or invent scientific results.", handler=execute_analysis_code, args_schema=AnalysisCodeInput),
        HarnessTool(name="revise_analysis_code", description="Repair the latest analysis draft using small edits to the numbered code lines in the last tool feedback, retaining its output filenames and report layouts unless explicitly changed. Re-executes the revised program and requires the same delivery checks. Use after execution or delivery-review errors; avoid regenerating unrelated code.", handler=revise_analysis_code, args_schema=AnalysisRevisionInput),
        HarnessTool(
            name="inspect_inputs",
            description="Inspect admitted input files, current state and actual execution environments.",
            args_schema=EmptyInput,
            handler=_inspect_inputs,
            tags=["entry", "general"],
        ),
        HarnessTool(
            name="recommend_next_step",
            description="Return receipt-based delivery readiness and unresolved review status.",
            args_schema=EmptyInput,
            handler=_recommend_next_step,
            tags=["entry", "general"],
        ),
        HarnessTool(
            name="extract_requirement",
            description="Extract or update the stable analysis requirement and parser target.",
            args_schema=EmptyInput,
            handler=_analysis_tool("extract_requirement", "update_requirement"),
            tags=["general"],
        ),
        HarnessTool(
            name="prepare_data",
            description="Preprocess raw input files into transformed CSV/table artifacts.",
            args_schema=EmptyInput,
            handler=_analysis_tool("prepare_data", "normalize"),
            tags=["general"],
        ),
        HarnessTool(
            name="build_dataset_bundle",
            description="Build the dataset bundle from transformed data sources.",
            args_schema=EmptyInput,
            handler=_analysis_tool("build_dataset_bundle", "parse"),
            tags=["general"],
        ),
        HarnessTool(
            name="run_analysis",
            description="Plan and execute deterministic analysis for the current dataset bundle.",
            args_schema=EmptyInput,
            handler=_run_analysis,
            tags=["general"],
        ),
        HarnessTool(
            name="derive_chart_data",
            description="Summarize chart-ready data created by analysis.",
            args_schema=EmptyInput,
            handler=_derive_chart_data,
            tags=["general"],
        ),
        HarnessTool(
            name="generate_plots",
            description="Generate plots and plot-level analysis from chart-ready data.",
            args_schema=EmptyInput,
            handler=_analysis_tool("generate_plots", "run_plotting"),
            tags=["general"],
        ),
        HarnessTool(
            name="analyze_plots",
            description="Summarize generated plot artifacts and their plot-level analysis.",
            args_schema=EmptyInput,
            handler=_analyze_plots,
            tags=["general"],
        ),
        HarnessTool(
            name="write_report",
            description="Generate the final Markdown report from analysis and visualization artifacts.",
            args_schema=EmptyInput,
            handler=_analysis_tool("write_report", "run_report"),
            tags=["general"],
        ),
        HarnessTool(
            name="validate_report",
            description="Inspect the current report validation result and report artifact paths.",
            args_schema=EmptyInput,
            handler=_validate_report,
            tags=["general"],
        ),
        HarnessTool(
            name="parse_lightcycler_txt",
            description="Parse a strongly matched LightCycler melting txt export into a long CSV table.",
            args_schema=FilePathInput,
            handler=_parse_lightcycler_txt,
            tags=["protein", "lightcycler"],
        ),
        HarnessTool(
            name="resolve_plate_layout",
            description="Resolve a 96-well layout from requirement text and write it as CSV.",
            args_schema=OptionalTextInput,
            handler=_resolve_plate_layout,
            tags=["protein", "lightcycler"],
        ),
        HarnessTool(
            name="run_lightcycler_melt_qc",
            description="Run deterministic LightCycler melt Tm/QC analysis on the dataset bundle.",
            args_schema=EmptyInput,
            handler=_run_lightcycler_melt_qc,
            tags=["protein", "lightcycler"],
        ),
        HarnessTool(
            name="run_prediction_review",
            description="Compare predicted Top candidates against observed LightCycler group Tm results.",
            args_schema=OptionalTextInput,
            handler=_run_prediction_review,
            tags=["protein", "lightcycler"],
        ),
        HarnessTool(
            name="generate_lightcycler_plots",
            description="Generate fixed LightCycler plots from deterministic melt-analysis artifacts.",
            args_schema=EmptyInput,
            handler=_generate_lightcycler_plots,
            tags=["protein", "lightcycler"],
        ),
    ]


def _inspect_inputs(context: HarnessContext, args: dict[str, Any]) -> ToolObservation:
    from bioma.backends.analysis.operations.intake import intake_node
    from bioma.harness.code_analysis import analysis_environment
    from bioma.harness.input_inspection import preview_inputs
    from bioma.harness.r_analysis import r_environment

    patch = intake_node(context.state)
    probe_state = {**context.state, **patch}
    probe_context = HarnessContext(
        state=probe_state,
        base_dir=context.base_dir,
        observations=list(context.observations),
    )
    recommendation = recommend_next_tools(probe_context)
    patch["harness_workflow_recommendation"] = recommendation
    files = [
        str(path)
        for path in patch.get("raw_data_sources")
        or context.state.get("raw_data_sources")
        or []
    ]
    metrics = {
        "raw_file_count": len(files),
        "has_transformed_data": bool(context.state.get("transformed_data_sources")),
        "has_dataset_bundle": bool(context.state.get("dataset_bundle")),
        "has_analysis_results": bool(context.state.get("analysis_results")),
        "has_visualizations": bool(context.state.get("visualizations")),
    }
    return ToolObservation(
        tool_name="inspect_inputs",
        status="ok",
        summary="Inspected input files and execution environments.",
        artifacts=files,
        metrics={
            "state": metrics,
            "input_previews": preview_inputs(files),
            "python_runtime": analysis_environment(),
            "r_runtime": r_environment(),
            "execution_requirements": context.state.get('execution_requirements') or {},
            "desired_deliverables": context.state.get('desired_deliverables') or [],
            "report_contract": 'Declare every requested .md in output_files and reports. Include actual methods and limitations in its layout; computed findings come from publish_result. INPUT_FILES and OUTPUT_DIR are already bound, do not assign them.',
            "workflow_recommendation": recommendation,
        },
        patch=patch,
    )


def _recommend_next_step(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    recommendation = recommend_next_tools(context)
    return ToolObservation(
        tool_name="recommend_next_step",
        status="ok",
        summary=recommendation.get("reason")
        or "Computed receipt-based delivery readiness.",
        metrics={"workflow_recommendation": recommendation},
        patch={"harness_workflow_recommendation": recommendation},
    )


def _analysis_tool(tool_name: str, stage_name: str):
    def _handler(context: HarnessContext, args: dict[str, Any]) -> ToolObservation:
        result = _execute_analysis_operation(context, stage_name)
        return _observation_from_analysis(tool_name, stage_name, result)

    return _handler


def _execute_analysis_operation(context: HarnessContext, stage_name: str):
    # The registered HarnessTool is the action boundary. No secondary registry
    # or Stage object owns dispatch, permissions or completion.
    from bioma.backends.analysis.operations.normalize import normalize_node
    from bioma.backends.analysis.operations.requirement import update_requirement_node
    from bioma.backends.analysis.operations.parsing import parse_node
    from bioma.backends.analysis.operations.analysis import plan_analysis_node, run_analysis_node
    from bioma.backends.analysis.operations.plotting import run_plotting_node
    from bioma.backends.analysis.operations.reporting import run_report_node
    operations = {
        'normalize': normalize_node, 'update_requirement': update_requirement_node,
        'parse': parse_node, 'plan_analysis': plan_analysis_node,
        'run_analysis': run_analysis_node, 'run_plotting': run_plotting_node,
        'run_report': run_report_node,
    }
    operation = operations.get(stage_name)
    if operation is None:
        return {'status': 'error', 'message': f"Unknown analysis operation: {stage_name}",
                'patch': {'error_code': 'UNKNOWN_STAGE', 'error_detail': stage_name, 'status': 'failed'}}
    patch = dict(operation(context.state))
    status, message = 'success', ''
    if patch.get('error_code'):
        status, message = 'failed', patch.get('error_detail') or ''
    elif stage_name in {'update_requirement', 'parse', 'plan_analysis'} and patch.get('need_clarification'):
        status, message = 'needs_clarification', patch.get('pending_question') or ''
    patch['stage_log'] = [{'stage': stage_name, 'status': status,
                           'message': message, 'source': 'scientific_harness'}]
    context.merge_patch(patch)
    return {'status': status, 'message': message, 'patch': patch}


def _run_analysis(context: HarnessContext, args: dict[str, Any]) -> ToolObservation:
    warnings: list[str] = []
    if not context.state.get("data_profile") or not context.state.get("analysis_plan"):
        plan_result = _execute_analysis_operation(context, "plan_analysis")
        if plan_result["status"] not in {"success", "ok"}:
            warnings.append(
                plan_result.get("message")
                or "plan_analysis did not complete successfully."
            )
            return _observation_from_analysis(
                "run_analysis", "plan_analysis", plan_result, warnings=warnings
            )
    run_result = _execute_analysis_operation(context, "run_analysis")
    return _observation_from_analysis(
        "run_analysis", "run_analysis", run_result, warnings=warnings
    )


def _derive_chart_data(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    chart_ready = context.state.get("chart_data_ready_results") or {}
    artifacts = _collect_paths(chart_ready)
    metrics = {
        "chart_plan_count": len(context.state.get("chart_plan") or []),
        "chart_ready_keys": sorted(chart_ready.keys()),
        "artifact_count": len(artifacts),
    }
    status = "ok" if chart_ready else "error"
    summary = (
        "Chart-ready data is available."
        if chart_ready
        else "No chart-ready data found; run run_analysis first."
    )
    return ToolObservation(
        tool_name="derive_chart_data",
        status=status,
        summary=summary,
        artifacts=artifacts,
        tables=[path for path in artifacts if path.lower().endswith(".csv")],
        metrics=metrics,
        warnings=[] if chart_ready else ["MISSING_CHART_DATA_READY_RESULTS"],
    )


def _analyze_plots(context: HarnessContext, args: dict[str, Any]) -> ToolObservation:
    visualizations = context.state.get("visualizations") or []
    figures = _collect_paths(visualizations)
    metrics = {
        "visualization_count": len(visualizations),
        "code_path_count": len(context.state.get("visualizations_code") or []),
        "log_count": len(context.state.get("visualization_log") or []),
    }
    return ToolObservation(
        tool_name="analyze_plots",
        status="ok" if visualizations else "error",
        summary="Summarized generated plot artifacts."
        if visualizations
        else "No plots found; run generate_plots first.",
        artifacts=figures + list(context.state.get("visualizations_code") or []),
        figures=figures,
        metrics=metrics,
        warnings=[] if visualizations else ["MISSING_VISUALIZATIONS"],
    )


def _validate_report(context: HarnessContext, args: dict[str, Any]) -> ToolObservation:
    validation = context.state.get("report_validation") or {}
    report_path = str(context.state.get("final_report") or "")
    pdf_path = str(context.state.get("final_report_pdf") or "")
    artifacts = [path for path in [report_path, pdf_path] if path]
    status = "ok" if report_path else "error"
    return ToolObservation(
        tool_name="validate_report",
        status=status,
        summary="Report validation inspected."
        if report_path
        else "No report artifact found; run write_report first.",
        artifacts=artifacts,
        metrics={"report_validation": validation},
        warnings=[] if report_path else ["MISSING_FINAL_REPORT"],
    )


def _parse_lightcycler_txt(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    from bioma.backends.analysis.services.lightcycler_parser import parse_lightcycler_txt

    file_path = _resolve_path(context.base_dir, args["file_path"])
    parsed = parse_lightcycler_txt(file_path)
    if parsed is None:
        return ToolObservation(
            tool_name="parse_lightcycler_txt",
            status="error",
            summary="Input file did not strongly match a LightCycler melting txt export.",
            artifacts=[str(file_path)],
            warnings=["NOT_LIGHTCYCLER_TXT"],
        )
    out_dir = context.artifact_dir("lightcycler_parsed")
    value_name = "peaks" if parsed.value_column == "negative_derivative" else "curves"
    out_path = out_dir / f"{file_path.stem}_{value_name}_long.csv"
    parsed.dataframe.to_csv(out_path, index=False, encoding="utf-8-sig")
    return ToolObservation(
        tool_name="parse_lightcycler_txt",
        status="ok",
        summary=f"Parsed LightCycler {value_name} txt into long CSV.",
        artifacts=[str(out_path)],
        tables=[str(out_path)],
        metrics={
            "value_column": parsed.value_column,
            "encoding": parsed.encoding,
            "well_count": parsed.well_count,
            "row_count": parsed.row_count,
        },
    )


def _resolve_plate_layout(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    from bioma.backends.analysis.services.plate_layout_resolver import (
        requirement_text_from_state,
        resolve_plate_layout_from_text,
    )

    text = str(args.get("text") or "").strip() or requirement_text_from_state(
        context.state
    )
    result = resolve_plate_layout_from_text(text)
    if result.dataframe.empty:
        return ToolObservation(
            tool_name="resolve_plate_layout",
            status="error",
            summary="No parseable 96-well layout rules were found.",
            warnings=[issue.get("code", "LAYOUT_NOT_FOUND") for issue in result.issues],
            metrics={"issues": result.issues},
        )
    out_path = context.artifact_dir("layout") / "resolved_plate_layout.csv"
    result.dataframe.to_csv(out_path, index=False, encoding="utf-8-sig")
    group_counts = result.dataframe["group"].value_counts(dropna=False).to_dict()
    return ToolObservation(
        tool_name="resolve_plate_layout",
        status="ok",
        summary="Resolved 96-well layout from requirement text.",
        artifacts=[str(out_path)],
        tables=[str(out_path)],
        metrics={
            "row_count": int(result.dataframe.shape[0]),
            "group_counts": group_counts,
            "issues": result.issues,
        },
        warnings=[issue.get("code", "") for issue in result.issues],
        patch={
            "harness_layout_path": str(out_path),
            "harness_layout_summary": group_counts,
        },
    )


def _run_lightcycler_melt_qc(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    from bioma.backends.analysis.services.lightcycler_melt_analysis import (
        run_lightcycler_melt_analysis,
    )

    dataset_bundle = context.state.get("dataset_bundle") or {}
    if not dataset_bundle:
        return ToolObservation(
            tool_name="run_lightcycler_melt_qc",
            status="error",
            summary="No dataset_bundle found; run build_dataset_bundle first.",
            warnings=["MISSING_DATASET_BUNDLE"],
        )
    out = run_lightcycler_melt_analysis(
        dataset_bundle=dataset_bundle,
        outputs_dir=str(context.artifact_dir("lightcycler_melt")),
    )
    patch = _merge_deterministic_artifact(context.state, "lightcycler_melt", out)
    status = "ok" if out.get("status") == "success" else "error"
    artifacts = _collect_paths(out.get("artifacts") or out.get("output_files") or [])
    return ToolObservation(
        tool_name="run_lightcycler_melt_qc",
        status=status,
        summary="Ran LightCycler melt Tm/QC analysis."
        if status == "ok"
        else str(out.get("reason") or "LightCycler melt QC skipped."),
        artifacts=artifacts,
        tables=[path for path in artifacts if path.lower().endswith(".csv")],
        metrics=out.get("summary") or {},
        warnings=[]
        if status == "ok"
        else [str(out.get("reason") or "LIGHTCYCLER_MELT_QC_SKIPPED")],
        patch=patch,
    )


def _run_prediction_review(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    from bioma.backends.analysis.services.plate_layout_resolver import (
        requirement_text_from_state,
    )
    from bioma.backends.analysis.services.prediction_review_service import run_prediction_review

    context_text = str(args.get("text") or "").strip() or "\n\n".join(
        [
            str(context.state.get("current_user_request") or ""),
            json.dumps(
                context.state.get("stable_requirement") or {}, ensure_ascii=False
            ),
            json.dumps(
                context.state.get("experiment_background") or {}, ensure_ascii=False
            ),
            requirement_text_from_state(context.state),
        ]
    )
    deterministic = (context.state.get("chart_data_ready_results") or {}).get(
        "deterministic_artifacts"
    ) or {}
    out = run_prediction_review(
        context_text=context_text,
        analysis_artifacts=deterministic,
        outputs_dir=str(context.artifact_dir("prediction_review")),
    )
    patch = _merge_deterministic_artifact(context.state, "prediction_review", out)
    status = "ok" if out.get("status") == "success" else "error"
    artifacts = _collect_paths(out.get("artifacts") or out.get("output_files") or [])
    return ToolObservation(
        tool_name="run_prediction_review",
        status=status,
        summary="Compared predicted candidates against observed Tm results."
        if status == "ok"
        else str(out.get("reason") or "Prediction review skipped."),
        artifacts=artifacts,
        tables=[path for path in artifacts if path.lower().endswith(".csv")],
        metrics=out.get("summary") or {},
        warnings=[]
        if status == "ok"
        else [str(out.get("reason") or "PREDICTION_REVIEW_SKIPPED")],
        patch=patch,
    )


def _generate_lightcycler_plots(
    context: HarnessContext, args: dict[str, Any]
) -> ToolObservation:
    from bioma.backends.analysis.services.lightcycler_fixed_plots import (
        generate_lightcycler_fixed_plots,
    )

    deterministic = (context.state.get("chart_data_ready_results") or {}).get(
        "deterministic_artifacts"
    ) or {}
    out = generate_lightcycler_fixed_plots(
        deterministic_artifacts=deterministic,
        output_dir=str(context.artifact_dir("lightcycler_plots")),
    )
    plots = [
        _normalize_lightcycler_plot_record(plot) for plot in (out.get("plots") or [])
    ]
    patch = {"harness_lightcycler_plots": plots}
    existing = list(context.state.get("visualizations") or [])
    if plots:
        patch["visualizations"] = existing + plots
    figures = _collect_paths(plots)
    return ToolObservation(
        tool_name="generate_lightcycler_plots",
        status="ok" if plots else "error",
        summary="Generated fixed LightCycler plots."
        if plots
        else "No fixed LightCycler plots were generated.",
        artifacts=figures,
        figures=figures,
        metrics={"plot_count": len(plots), "issues": out.get("issues") or []},
        warnings=[issue.get("code", "") for issue in out.get("issues") or []]
        if not plots
        else [],
        patch=patch,
    )


def _normalize_lightcycler_plot_record(plot: dict[str, Any]) -> dict[str, Any]:
    chart_id = str(plot.get("chart_id") or "").strip()
    image_path = str(plot.get("path") or plot.get("image_path") or "").strip()
    data_path = str(plot.get("data_path") or "").strip()
    title = str(
        plot.get("title") or chart_id.replace("_", " ").title() or "LightCycler Plot"
    ).strip()
    item = dict(plot)
    item.update(
        {
            "chart_id": chart_id,
            "chart_key": str(plot.get("chart_key") or chart_id),
            "scope": str(plot.get("scope") or "canonical_visualization"),
            "trust_level": str(plot.get("trust_level") or "canonical"),
            "title": title,
            "image_paths": [image_path] if image_path else [],
            "code_paths": list(plot.get("code_paths") or []),
            "parsed_data_path": str(plot.get("parsed_data_path") or data_path),
            "source_table_id": str(plot.get("source_table_id") or ""),
            "status": str(plot.get("status") or ("success" if image_path else "error")),
            "notes": str(plot.get("notes") or "Deterministic LightCycler fixed plot."),
            "stdout": str(plot.get("stdout") or ""),
            "stderr": str(plot.get("stderr") or ""),
        }
    )
    return item


def _observation_from_analysis(
    tool_name: str,
    stage_name: str,
    result: dict[str, Any],
    warnings: list[str] | None = None,
) -> ToolObservation:
    patch = result.get("patch") or {}
    status = (
        "ok"
        if result.get("status") == "success" and not patch.get("error_code")
        else "error"
    )
    artifacts = _collect_paths(patch)
    metrics = {
        "stage": stage_name,
        "status": result.get("status"),
        "patch_keys": sorted(patch.keys()),
        "error_code": patch.get("error_code"),
        "table_count": patch.get("table_count"),
        "chart_plan_count": len(patch.get("chart_plan") or []),
        "visualization_count": len(patch.get("visualizations") or []),
    }
    return ToolObservation(
        tool_name=tool_name,
        status=status,
        summary=result.get("message") or f"Executed stage '{stage_name}'.",
        artifacts=artifacts,
        tables=[
            path
            for path in artifacts
            if path.lower().endswith((".csv", ".xlsx", ".xls"))
        ],
        figures=[
            path
            for path in artifacts
            if path.lower().endswith((".png", ".jpg", ".jpeg", ".pdf", ".svg"))
        ],
        metrics=metrics,
        warnings=list(warnings or [])
        + ([str(patch.get("error_code"))] if patch.get("error_code") else []),
    )


def _merge_deterministic_artifact(
    state: dict[str, Any], key: str, out: dict[str, Any]
) -> dict[str, Any]:
    current = dict(state.get("chart_data_ready_results") or {})
    deterministic = dict(current.get("deterministic_artifacts") or {})
    if out.get("status") == "success":
        deterministic[key] = out.get("artifacts") or {}
        current["deterministic_artifacts"] = deterministic
        return {"chart_data_ready_results": current}
    return {}


def _collect_paths(value: Any) -> list[str]:
    out: list[str] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            if _looks_like_path(item):
                out.append(item)
            return
        if isinstance(item, dict):
            for val in item.values():
                visit(val)
            return
        if isinstance(item, (list, tuple, set)):
            for val in item:
                visit(val)

    visit(value)
    seen: set[str] = set()
    unique: list[str] = []
    for path in out:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _looks_like_path(text: str) -> bool:
    low = text.lower()
    return any(
        low.endswith(ext)
        for ext in (
            ".csv",
            ".tsv",
            ".txt",
            ".xlsx",
            ".xls",
            ".png",
            ".jpg",
            ".jpeg",
            ".pdf",
            ".svg",
            ".md",
            ".json",
        )
    )


def _resolve_path(base_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


__all__ = ["build_default_tools"]
