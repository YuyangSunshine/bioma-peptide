"""BioMA peptide research API, with lazy imports for lightweight scientific workers."""
from importlib import import_module

_EXPORTS = {
    "BioMA": "bioma.api",
    **{name: "bioma.core.models" for name in ["CapabilitySpec", "Commitment", "ScientificState", "ScientificTask", "TaskOffer", "TaskRequest"]},
    **{name: "bioma.core.external_tasks" for name in ["ArtifactObligation", "CoverageDecision", "CoverageSufficiency", "DeliveryState", "DeliveryStatus", "ExternalTaskContract"]},
    "ArtifactGenerationPolicy": "bioma.artifact_generation",
    **{name: "bioma.execution_context" for name in ["ExecutionResult", "TaskExecutionContext", "execute_python"]},
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
