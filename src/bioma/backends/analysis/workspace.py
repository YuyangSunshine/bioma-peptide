from __future__ import annotations

from typing import TypedDict, Annotated, Literal, Optional, List, Dict, Any
import operator


# =========================================================
# Basic enums / literals
# =========================================================

Role = Literal["system", "user", "assistant"]

InstrumentFamily = Literal[
    "qpcr",
    "plate_reader",
    "capillary_electrophoresis",
    "unknown",
]

TurnType = Literal[
    "new_task",
    "add_files",
    "clarification_reply",
    "modify_requirement",
    "modify_plot",
    "modify_report",
    "ask_explanation",
    "rerun_analysis",
    "export_artifact",
    "unknown",
]


# =========================================================
# Small structured objects
# =========================================================


class Message(TypedDict, total=False):
    role: Role
    content: str
    ts: str


class Issue(TypedDict, total=False):
    code: str
    severity: Literal["low", "medium", "high"]
    message: str
    detail: str


class TurnIntent(TypedDict, total=False):
    type: TurnType
    summary: str
    target_artifact_id: Optional[str]
    user_requested_outputs: List[str]
    requires_data_change: bool
    requires_report_refresh: bool
    requires_plot_refresh: bool
    confidence: float
    evidence: Dict[str, Any]


class ParserTarget(TypedDict, total=False):
    instrument_family: InstrumentFamily
    experiment_kind: str
    confidence: float
    need_clarification: bool
    source: str
    evidence: Dict[str, Any]


class ReportVersion(TypedDict, total=False):
    report_id: str
    created_at: str
    report_path: str
    note: str
    based_on_analysis_version: str
    based_on_plot_version: str


# =========================================================
# Main task state
# =========================================================


class AnalysisState(TypedDict, total=False):
    # -----------------------------------------------------
    # 0) Task / thread meta
    # -----------------------------------------------------
    task_id: str
    thread_id: str
    created_at: str
    updated_at: str
    status: str  # active / waiting_user / completed / failed
    artifact_root: str

    # -----------------------------------------------------
    # 1) Conversation memory
    # -----------------------------------------------------
    messages: List[Message]
    current_user_request: str
    turn_intent: TurnIntent
    pending_question: Optional[str]
    # Routing marker written by respond/ask_user nodes (not used for graph routing)
    next_action: Optional[str]
    # Rolling summary for long conversations
    context_summary: str

    # -----------------------------------------------------
    # 2) Raw input
    # -----------------------------------------------------
    raw_data_sources: List[str]
    has_new_files: bool

    # -----------------------------------------------------
    # 3) Data prep outputs
    # -----------------------------------------------------
    table_count: int
    transformed_data_sources: List[str]
    data_prep_meta: Dict[str, Any]
    data_artifact: Dict[str, Any]

    # -----------------------------------------------------
    # 4) Requirement understanding
    # -----------------------------------------------------
    stable_requirement: Dict[str, Any]
    requirement_history: List[Dict[str, Any]]  # audit trail of updates
    experiment_config: Dict[str, Any]
    experiment_background: Dict[str, Any]
    instrument_family: InstrumentFamily  # convenience alias from resolved_parser_target
    resolved_parser_target: ParserTarget
    requirement_issues: List[Issue]
    need_clarification: bool

    # -----------------------------------------------------
    # 5) Parser outputs
    # -----------------------------------------------------
    dataset_bundle: Dict[str, Any]
    bundle_issues: List[Dict[str, Any]]

    # -----------------------------------------------------
    # 6) Planning outputs
    # -----------------------------------------------------
    data_profile: Dict[str, Any]
    chart_plan: List[Dict[str, Any]]
    analysis_plan: Dict[str, Any]
    planning_issues: List[Issue]

    # -----------------------------------------------------
    # 7) Execution outputs
    # -----------------------------------------------------
    analysis_results: Dict[str, Any]
    chart_data_ready_results: Dict[str, Any]
    analysis_log: List[Dict[str, Any]]
    analysis_code_paths: List[str]
    analysis_execution_issues: List[Dict[str, Any]]

    # -----------------------------------------------------
    # 8) Visualization outputs
    # -----------------------------------------------------
    visualizations: List[Dict[str, Any]]
    visualizations_code: List[str]
    visualization_log: List[str]
    plotting_issues: List[Dict[str, Any]]

    # -----------------------------------------------------
    # 9) Report outputs
    # -----------------------------------------------------
    report_outline: Dict[str, Any]
    final_report: str
    final_report_pdf: Optional[str]
    report_versions: List[ReportVersion]
    active_report_id: Optional[str]
    report_validation: Dict[str, Any]

    # -----------------------------------------------------
    # 10) Control / errors
    # -----------------------------------------------------
    recoverable_error: Optional[Dict[str, Any]]
    error_code: Optional[str]
    error_detail: Optional[str]

    # -----------------------------------------------------
    # 11) Harness execution log
    # -----------------------------------------------------
    # Append-only log of each stage execution.
    stage_log: Annotated[List[Dict[str, Any]], operator.add]


__all__ = [
    "Role",
    "InstrumentFamily",
    "TurnType",
    "Message",
    "Issue",
    "TurnIntent",
    "ParserTarget",
    "ReportVersion",
    "AnalysisState",
]
