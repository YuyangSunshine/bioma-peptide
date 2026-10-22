"""Constrained evidence-conditioned local ResearchPlan revision."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from bioma.core.research_contract import ResearchContract
from .forecast import DecisionSnapshot
from .plan import Dependency, Lifecycle, MethodBinding, NodeKind, PlanNode, ResearchPlan
from .relations import FrozenModel, Reference
from .revision import PlanChanges, PlanRevision, RevisionTrigger, apply_plan_revision


class RevisionOperationKind(str, Enum):
    ADD_NODE = "ADD_NODE"
    ADD_DEPENDENCY = "ADD_DEPENDENCY"
    ADD_BINDING = "ADD_BINDING"
    REPLACE_BINDING = "REPLACE_BINDING"
    BLOCK_NODE = "BLOCK_NODE"
    UNBLOCK_NODE = "UNBLOCK_NODE"
    SUPERSEDE_NODE = "SUPERSEDE_NODE"
    RETAIN_NODE = "RETAIN_NODE"


AUTONOMOUS_CAUSES = frozenset({RevisionTrigger.EVIDENCE_UPDATE, RevisionTrigger.CONTRADICTION,
    RevisionTrigger.EXECUTION_FAILURE, RevisionTrigger.CAPABILITY_CHANGE,
    RevisionTrigger.RESOURCE_CHANGE, RevisionTrigger.EFFECT_RECONCILED,
    RevisionTrigger.OPTION_COMPLETED, RevisionTrigger.OPTION_BLOCKED})


class RevisionTriggerEvent(FrozenModel):
    schema_version: Literal["revision-trigger-v1"] = "revision-trigger-v1"
    trigger_id: Reference
    cause: RevisionTrigger
    contract_ref: Reference
    plan_identity: tuple[str, int, str]
    decision_snapshot_identity: Reference
    authoritative_refs: tuple[Reference, ...] = Field(min_length=1)
    affected_node_refs: tuple[Reference, ...] = ()
    details_json: str = "{}"
    created_at: datetime
    provenance_refs: tuple[Reference, ...] = ()

    @model_validator(mode="after")
    def valid_trigger(self):
        if self.cause not in AUTONOMOUS_CAUSES:
            raise ValueError("cause is not an autonomous revision trigger")
        details = json.loads(self.details_json)
        if not isinstance(details, dict):
            raise ValueError("trigger details must be an object")
        if details.get("authority") in {"forecast", "hypothetical"} or any(
                ref.startswith(("forecast:", "hypothetical:")) for ref in self.authoritative_refs):
            raise ValueError("forecast output is not authoritative revision evidence")
        object.__setattr__(self, "details_json", json.dumps(
            details, sort_keys=True, separators=(",", ":"), allow_nan=False))
        object.__setattr__(self, "created_at", self.created_at.astimezone(timezone.utc))
        return self

    @property
    def details(self):
        return json.loads(self.details_json)


class RevisionOperation(FrozenModel):
    kind: RevisionOperationKind
    node_ref: Reference | None = None
    node: PlanNode | None = None
    dependency: Dependency | None = None
    binding: MethodBinding | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def exact_payload(self):
        required = {
            RevisionOperationKind.ADD_NODE: (self.node is not None),
            RevisionOperationKind.ADD_DEPENDENCY: (self.dependency is not None),
            RevisionOperationKind.ADD_BINDING: (self.node_ref is not None and self.binding is not None),
            RevisionOperationKind.REPLACE_BINDING: (self.node_ref is not None and self.binding is not None),
            RevisionOperationKind.BLOCK_NODE: self.node_ref is not None,
            RevisionOperationKind.UNBLOCK_NODE: self.node_ref is not None,
            RevisionOperationKind.SUPERSEDE_NODE: self.node_ref is not None,
            RevisionOperationKind.RETAIN_NODE: self.node_ref is not None,
        }[self.kind]
        if not required:
            raise ValueError("revision operation lacks its typed payload")
        return self


class RevisionProposal(FrozenModel):
    schema_version: Literal["revision-proposal-v1"] = "revision-proposal-v1"
    proposal_id: Reference
    contract_ref: Reference
    source_plan_identity: tuple[str, int, str]
    decision_snapshot_identity: Reference
    trigger_ref: Reference
    operations: tuple[RevisionOperation, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    provenance_refs: tuple[Reference, ...] = Field(min_length=1)


def _compile_changes(plan: ResearchPlan, operations, *, evidence_snapshot_ref=None):
    nodes = {node.node_id: node for node in plan.nodes}
    added, updated, superseded = [], {}, []
    dependencies = list(plan.dependencies)
    dependencies_changed = False
    for operation in operations:
        if operation.kind is RevisionOperationKind.ADD_NODE:
            added.append(operation.node)
        elif operation.kind is RevisionOperationKind.ADD_DEPENDENCY:
            dependencies.append(operation.dependency)
            dependencies_changed = True
        elif operation.kind in {RevisionOperationKind.ADD_BINDING,
                                RevisionOperationKind.REPLACE_BINDING}:
            current = updated.get(operation.node_ref, nodes.get(operation.node_ref))
            if current is None:
                raise ValueError("binding operation names unknown node")
            bindings = {item.binding_id: item for item in current.candidate_bindings}
            bindings[operation.binding.binding_id] = operation.binding
            selected = (operation.binding.binding_id if operation.kind is
                        RevisionOperationKind.REPLACE_BINDING else current.selected_binding_id)
            updated[current.node_id] = current.model_copy(update={
                "candidate_bindings": tuple(bindings.values()), "selected_binding_id": selected})
        elif operation.kind in {RevisionOperationKind.BLOCK_NODE,
                                RevisionOperationKind.UNBLOCK_NODE}:
            current = updated.get(operation.node_ref, nodes.get(operation.node_ref))
            if current is None:
                raise ValueError("lifecycle operation names unknown node")
            lifecycle = (Lifecycle.BLOCKED if operation.kind is RevisionOperationKind.BLOCK_NODE
                         else Lifecycle.PENDING)
            updated[current.node_id] = current.model_copy(update={"lifecycle": lifecycle})
        elif operation.kind is RevisionOperationKind.SUPERSEDE_NODE:
            superseded.append(operation.node_ref)
        elif operation.node_ref not in nodes:
            raise ValueError("retain operation names unknown node")
    return PlanChanges(add_nodes=tuple(added), update_nodes=tuple(updated.values()),
        supersede_node_ids=tuple(superseded),
        dependencies=tuple(dependencies) if dependencies_changed else None,
        evidence_snapshot_ref=evidence_snapshot_ref)


def validate_revision_proposal(proposal: RevisionProposal, *, trigger: RevisionTriggerEvent,
                               snapshot: DecisionSnapshot, contract: ResearchContract,
                               plan: ResearchPlan) -> PlanChanges:
    if (proposal.contract_ref != contract.identity or trigger.contract_ref != contract.identity or
            proposal.source_plan_identity != plan.version_identity or
            trigger.plan_identity != plan.version_identity or
            proposal.decision_snapshot_identity != snapshot.identity or
            trigger.decision_snapshot_identity != snapshot.identity or
            proposal.trigger_ref != trigger.trigger_id):
        raise ValueError("stale revision proposal or trigger")
    incompatible = {RevisionOperationKind.ADD_NODE, RevisionOperationKind.ADD_DEPENDENCY,
                    RevisionOperationKind.ADD_BINDING, RevisionOperationKind.REPLACE_BINDING,
                    RevisionOperationKind.UNBLOCK_NODE, RevisionOperationKind.SUPERSEDE_NODE}
    if snapshot.unresolved_effect_refs and any(item.kind in incompatible for item in proposal.operations):
        raise ValueError("EFFECT_UNKNOWN blocks incompatible plan revision")
    changes = _compile_changes(plan, proposal.operations,
        evidence_snapshot_ref=trigger.details.get("after_evidence_snapshot_ref"))
    revision = PlanRevision(revision_id="validation:" + proposal.proposal_id,
        contract_ref=contract.identity, before_plan_version=plan.version,
        after_plan_version=plan.version + 1, before_plan_identity=plan.version_identity,
        trigger_type=trigger.cause, trigger_refs=trigger.authoritative_refs,
        changes=changes, rationale=proposal.rationale, created_at=trigger.created_at)
    candidate = apply_plan_revision(contract, plan, revision)
    live = [node for node in candidate.nodes if node.lifecycle is not Lifecycle.SUPERSEDED]
    covered = {ref for node in live for ref in node.contract_obligation_refs}
    mandatory = {item.id for item in contract.requirements if item.mandatory}
    if not mandatory <= covered:
        raise ValueError("revision removes mandatory-obligation coverage")
    accepted = {row.obligation_ref for row in snapshot.obligation_progress if row.progress_credit}
    if not accepted <= covered:
        raise ValueError("revision deletes accepted scientific result coverage")
    return changes


def apply_revision_proposal(proposal, *, trigger, snapshot, contract, plan):
    changes = validate_revision_proposal(proposal, trigger=trigger, snapshot=snapshot,
        contract=contract, plan=plan)
    revision = PlanRevision(revision_id="revision:" + hashlib.sha256(
        proposal.model_dump_json().encode()).hexdigest(), contract_ref=contract.identity,
        before_plan_version=plan.version, after_plan_version=plan.version + 1,
        before_plan_identity=plan.version_identity, trigger_type=trigger.cause,
        trigger_refs=trigger.authoritative_refs, changes=changes,
        rationale=proposal.rationale, created_at=trigger.created_at)
    return revision, apply_plan_revision(contract, plan, revision)


class BoundedRevisionPolicy:
    """Deterministic local repair; it never treats forecast as authority."""

    def propose(self, trigger: RevisionTriggerEvent, snapshot: DecisionSnapshot,
                contract: ResearchContract, plan: ResearchPlan) -> RevisionProposal | None:
        if (trigger.plan_identity != plan.version_identity or
                trigger.decision_snapshot_identity != snapshot.identity):
            raise ValueError("stale revision trigger")
        affected = [node for node in plan.nodes if node.node_id in trigger.affected_node_refs]
        operations = []
        details = trigger.details
        if trigger.cause in {RevisionTrigger.CONTRADICTION, RevisionTrigger.EVIDENCE_UPDATE}:
            if not affected:
                return None
            source = affected[0]
            after = details.get("after_obligations", {})
            statuses = {after.get(ref, {}).get("status") for ref in source.contract_obligation_refs}
            needs_discrimination = (trigger.cause is RevisionTrigger.CONTRADICTION or
                                    bool(statuses & {"CONFLICTED", "INSUFFICIENT", "UNRESOLVED"}))
            if needs_discrimination:
                node_id = source.node_id + ":discriminate:" + trigger.trigger_id[-8:]
                operations.append(RevisionOperation(kind=RevisionOperationKind.ADD_NODE,
                    node=PlanNode(node_id=node_id, kind=NodeKind.WORK_ITEM,
                        description="Discriminate the changed claim state with new evidence.",
                        contract_obligation_refs=source.contract_obligation_refs,
                        question_refs=source.question_refs,
                        candidate_bindings=source.candidate_bindings),
                    rationale="Committed scientific evidence leaves a conflict or unresolved obligation."))
            else:
                operations.append(RevisionOperation(kind=RevisionOperationKind.RETAIN_NODE,
                    node_ref=source.node_id,
                    rationale="Bind the strategy lineage to newly committed qualifying evidence."))
        elif trigger.cause in {RevisionTrigger.EXECUTION_FAILURE,
                              RevisionTrigger.CAPABILITY_CHANGE, RevisionTrigger.RESOURCE_CHANGE}:
            if not affected or not details.get("replacement_binding"):
                return None
            binding = MethodBinding.model_validate(details["replacement_binding"])
            operations.append(RevisionOperation(kind=RevisionOperationKind.REPLACE_BINDING,
                node_ref=affected[0].node_id, binding=binding,
                rationale="Operational failure changes route feasibility, not scientific truth."))
        elif trigger.cause is RevisionTrigger.OPTION_BLOCKED and affected:
            operations.append(RevisionOperation(kind=RevisionOperationKind.BLOCK_NODE,
                node_ref=affected[0].node_id, rationale="Selected option is authoritatively blocked."))
        elif trigger.cause is RevisionTrigger.EFFECT_RECONCILED and affected:
            operations.append(RevisionOperation(kind=RevisionOperationKind.UNBLOCK_NODE,
                node_ref=affected[0].node_id, rationale="Authoritative reconciliation permits reconsideration."))
        if not operations:
            return None
        proposal_id = "revision-proposal:" + hashlib.sha256((
            trigger.trigger_id + plan.version_identity[2] + "".join(item.kind for item in operations)).encode()).hexdigest()
        return RevisionProposal(proposal_id=proposal_id, contract_ref=contract.identity,
            source_plan_identity=plan.version_identity,
            decision_snapshot_identity=snapshot.identity, trigger_ref=trigger.trigger_id,
            operations=tuple(operations), rationale="Bounded local response to " + trigger.cause.value,
            provenance_refs=(trigger.trigger_id, *trigger.authoritative_refs))
