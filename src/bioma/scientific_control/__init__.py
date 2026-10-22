"""Scientific-control semantics and grounded options; no commit authority."""
from .plan import Lifecycle, MethodBinding, NodeKind, PlanNode, ResearchPlan, read_plan
from .revision import PlanChanges, PlanRevision, RevisionTrigger, apply_plan_revision
from .relations import EnablesClause, RequiresClause, SatisfactionLink
from .semantics import ReadinessView, derive_readiness, satisfied_obligations
from .options import OptionIntent, OptionStep, ScientificOption, build_option
from .grounding import (GroundedOption, GroundedStep, GroundingFailure,
                        GroundingResult, ground_option, grounding_is_current)
from .executor import (OptionExecutionRecord, OptionExecutor, OptionLifecycle,
                       StepExecutionOutcome, new_execution_record, run_grounded_step)
from .progress import ContractProgress, ObligationProgress, contract_progress
from .forecast import (DecisionSnapshot, ForecastBranch, ForecastSource, ForecastStaleError,
    HypotheticalControlState, HypotheticalOutcome, OutcomeForecast,
    ProjectedObligationEffect, ProjectedStatus, RuntimeOutcome, decision_snapshot,
    decision_snapshot_is_current, hat_T, hypothetical_state, hypothetical_transition,
    validate_forecast)
from .controller import (BASELINE_HOOKS, CandidateEvaluation, CandidateSelectionPolicy,
    ControllerDecision, ControllerPolicy, FiniteHorizonController,
    OneStepForecastPolicy, SelectionBaseline)
from .generation import (CallableOptionGenerator, ModelOptionGenerator, OptionGenerator,
    PlanDerivedOptionGenerator, RoleOptionGenerator, validate_generated_options)
from .revision_policy import (AUTONOMOUS_CAUSES, BoundedRevisionPolicy, RevisionOperation,
    RevisionOperationKind, RevisionProposal, RevisionTriggerEvent,
    apply_revision_proposal, validate_revision_proposal)
from .forecasting import (CallableOutcomeForecaster, ModelOutcomeForecaster,
                          OutcomeForecaster, RuleOutcomeForecaster)

__all__ = [
    "EnablesClause", "Lifecycle", "NodeKind", "PlanNode", "ReadinessView",
    "RequiresClause", "ResearchPlan", "SatisfactionLink", "derive_readiness",
    "satisfied_obligations",
    "MethodBinding", "PlanChanges", "PlanRevision", "RevisionTrigger", "apply_plan_revision", "read_plan",
    "OptionIntent", "OptionStep", "ScientificOption", "build_option", "GroundedOption", "GroundedStep",
    "GroundingFailure", "GroundingResult", "ground_option", "grounding_is_current",
    "OptionExecutionRecord", "OptionExecutor", "OptionLifecycle", "StepExecutionOutcome",
    "new_execution_record", "run_grounded_step",
    "ContractProgress", "ObligationProgress", "contract_progress",
    "DecisionSnapshot", "ForecastBranch", "ForecastSource", "ForecastStaleError",
    "HypotheticalControlState", "HypotheticalOutcome", "OutcomeForecast",
    "ProjectedObligationEffect", "ProjectedStatus", "RuntimeOutcome", "decision_snapshot",
    "decision_snapshot_is_current", "hat_T", "hypothetical_state", "hypothetical_transition",
    "validate_forecast",
    "BASELINE_HOOKS", "CandidateEvaluation", "CandidateSelectionPolicy",
    "ControllerDecision", "ControllerPolicy", "FiniteHorizonController",
    "OneStepForecastPolicy", "SelectionBaseline",
    "CallableOptionGenerator", "ModelOptionGenerator", "OptionGenerator",
    "PlanDerivedOptionGenerator", "RoleOptionGenerator", "validate_generated_options",
    "AUTONOMOUS_CAUSES", "BoundedRevisionPolicy", "RevisionOperation",
    "RevisionOperationKind", "RevisionProposal", "RevisionTriggerEvent",
    "apply_revision_proposal", "validate_revision_proposal",
    "CallableOutcomeForecaster", "ModelOutcomeForecaster", "OutcomeForecaster",
    "RuleOutcomeForecaster",
]
