"""Explicit opt-in backend; predictions are ordinary non-authoritative WorkResults."""
from bioma.core.scientific_capabilities import WorkResult
from bioma.backends.base import MethodBinding
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import COMPUTE


def continual_prediction_backend(predictor):
    def execute(state, message, tools):
        del state, tools
        p = message.payload["inputs"]["prediction_request"]
        result = predictor.predict_evidence(hypothesis=p["hypothesis"],
            grounded_action=p["action"], biological_context=p["context"],
            evidence_schema=p["evidence_schema"])
        return WorkResult(family=COMPUTE, kind="ComputeResult", payload={
            "prediction": result, "model_version": predictor.model_version,
            "scope_id": p["context"]["scope_id"], "data_kind": "predictive_likelihood"},
            provenance={"method": "continual_scientific_prediction", "model_version": predictor.model_version})
    return MethodBinding(method(COMPUTE, "continual_scientific_prediction", "ComputeResult"), execute)
