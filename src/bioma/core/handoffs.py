"""Validate committed evidence at the Pro/Oper handoff boundary."""

from __future__ import annotations
from bioma.core.models import Evidence, ScientificState
from dataclasses import dataclass
import hashlib


def dependency_bindings(evidence):
    """Canonical reference fields for explicitly granted prerequisite records.

    Never choose arbitrarily among multiple records of the same kind: those
    assignments require an explicit provider/user binding.
    """
    bindings = {}
    for kind, field in [('prediction', 'prediction_evidence_id'), ('protocol', 'protocol_evidence_id'),
                        ('analysis', 'analysis_evidence_id'), ('execution', 'execution_evidence_id'),
                        ('query_interpretation', 'interpretation_evidence_id'),
                        ('reviewed_protocol', 'protocol_evidence_id')]:
        matches = [e for e in evidence if e.kind == kind]
        if len(matches) == 1:
            bindings[field] = matches[0].id
            if kind == 'execution' and isinstance(matches[0].value, dict) and matches[0].value.get('observation_id'):
                bindings['observation_id'] = matches[0].value['observation_id']
    return bindings


@dataclass(frozen=True)
class ReviewedProtocol:
    """Portable Pro → Oper input, bound to the task and committed evidence.

    Construct at the role boundary with from_state. This value carries no private
    session or live role reference and survives serialization and process restart.
    """

    task_id: str
    objective: str
    protocol_id: str
    text: str
    evidence_id: str
    sha256: str

    def validate(self, task_id: str) -> None:
        if self.task_id != task_id:
            raise ValueError("Reviewed protocol belongs to a different task.")
        if not all(
            (self.protocol_id, self.text.strip(), self.evidence_id, self.sha256)
        ):
            raise ValueError("Reviewed protocol requires content and evidence binding.")
        if hashlib.sha256(self.text.encode()).hexdigest() != self.sha256:
            raise ValueError(
                "Committed protocol content does not match its reviewed evidence."
            )

    @classmethod
    def from_state(cls, state: ScientificState, evidence_id: str) -> ReviewedProtocol:
        qualified = next((e for e in state.evidence if e.id==evidence_id and e.kind=='reviewed_protocol' and e.source=='harness'), None)
        if qualified is not None:
            from bioma.core.research import fingerprint
            from bioma.core.scientific_capabilities import WorkResult
            value = qualified.value
            observation = next((o for o in state.observations if o.id==value.get('work_result_ref') and o.kind=='work_result'),None)
            if observation is None or value.get('qualification_status')!='independently_reviewed' or not value.get('qualification_model'):
                raise ValueError('Protocol lacks a committed independent qualification')
            work = WorkResult.model_validate(observation.value)
            if work.family!='scientific.study_design' or work.kind!='StudyPlan' or fingerprint(work.model_dump(mode='json'))!=value.get('work_result_identity'):
                raise ValueError('Qualified protocol WorkResult identity mismatch')
            if value.get('review')!={'passed':True,'issues':[]} or value.get('execution_scope') not in {'compilation','simulation'}:
                raise ValueError('Independent protocol review/scope rejected')
            result = cls(state.task_id,state.objective,value['protocol_id'],value['protocol_text'],qualified.id,value['protocol_sha256'])
            result.validate(state.task_id)
            return result
        evidence = require_evidence(
            state, evidence_id, kind="protocol", source="proagent"
        )
        value, protocol = evidence.value, state.protocol
        if not isinstance(value, dict) or value.get("protocol_id") != protocol.get(
            "protocol_id"
        ):
            raise ValueError("Protocol state does not match the referenced evidence.")
        review = protocol.get("review") or {}
        if review.get("passed") is not True or review.get("issues"):
            raise ValueError("Automation requires a K_s-verified protocol.")
        result = cls(
            state.task_id,
            state.objective,
            protocol.get("protocol_id", ""),
            protocol.get("text", ""),
            evidence.id,
            value.get("protocol_sha256", ""),
        )
        result.validate(state.task_id)
        return result


def require_evidence(
    state: ScientificState,
    evidence_id: str,
    *,
    kind: str,
    source: str,
) -> Evidence:
    evidence = next((item for item in state.evidence if item.id == evidence_id), None)
    if evidence is None or evidence.kind != kind or evidence.source != source:
        raise ValueError(
            f"Protocol handoff requires the referenced {source} {kind} evidence."
        )
    return evidence
