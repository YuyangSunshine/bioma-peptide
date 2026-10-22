"""Persist a validated computation independently of its delivery review."""

from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from bioma.harness.delivery_review import DeliveryReviewRejected, DeliveryReviewIncomplete, review_delivery, review_policy_sha256
from bioma.harness.code_analysis import analysis_environment
from bioma.harness.types import ToolObservation
from bioma.llm import ModelTransportError, ModelOutputBudgetError, ModelBudgetUnavailable, ComputedPendingReview
from bioma.runtime.deadline import ExecutionTimeout, check
from bioma.runtime.telemetry import emit

IMPLEMENTATION_VERSION = "community-4-observable-query-gaps"


_REVIEW_RECEIPT = ContextVar('bioma_review_receipt', default=None)


@contextmanager
def resume_computed_delivery(receipt_path):
    """Trusted caller selects an existing receipt for review only, never execution.

    The normal finish tool revalidates work-package membership, all input/code/
    output hashes and actual receipts. This scope cannot approve a result.
    """
    token = _REVIEW_RECEIPT.set(str(Path(receipt_path).resolve()))
    try:
        yield
    finally:
        _REVIEW_RECEIPT.reset(token)


def selected_review_receipt():
    return _REVIEW_RECEIPT.get()


def _replace_checkpoint(temporary, target):
    # Windows readers/virus scanners may briefly deny atomic replacement.
    # Keep the previous valid checkpoint and bound retries; never truncate it.
    for attempt in range(5):
        try:
            temporary.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in {5, 32} or attempt == 4:
                raise
            check()
            time.sleep(.05 * (2 ** attempt))


def _digest(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def _review_candidate_identity(observation, context):
    output_names = observation.patch.get('delivery_output_names') or {
        path: Path(path).name for path in observation.metrics.get('sha256', {})
    }
    outputs = sorted(
        (output_names.get(path, Path(path).name), digest)
        for path, digest in observation.metrics.get('sha256', {}).items()
    )
    candidate = observation.metrics.get('answer_candidate_ref')
    candidate_digest = observation.metrics.get('execution_sha256', {}).get(candidate)
    observations = context.state.get('harness_observations') or context.observations or []
    observations = [item for item in observations
                    if item.get('tool_name') != 'finish_investigation']
    observations = sorted(set(
        json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        for item in observations
    ))
    review_basis = ({
        'tool_receipts': context.state.get('tool_receipts') or [],
        'observations': observations,
        'admitted_tool_inputs': context.state.get('admitted_tool_inputs') or {},
        'admitted_execution_inputs': context.state.get('admitted_execution_inputs') or {},
    } if context.state.get('continuous_investigation') else {})
    material = {
        'selected_support_receipt_refs': observation.metrics.get('selected_support_receipt_refs') or [],
        'selected_input_manifest': observation.patch.get('selected_input_manifest') or {},
        'native_receipt_hashes': observation.metrics.get('native_receipt_hashes') or {},
        'outputs': outputs,
        'answer_candidate_sha256': candidate_digest,
        'review_basis': review_basis,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


class ReviewContinuationUnavailable(ValueError):
    """No admitted computation can be restored for this exact invocation."""


class DeliveryCheckpoint:
    def __init__(self, context, config):
        self.context = context
        self.inputs = {
            str(Path(p).resolve()): _digest(p)
            for p in context.state.get("raw_data_sources", [])
        }
        contract = {
            "implementation": IMPLEMENTATION_VERSION,
            "runtime": analysis_environment(),
            "config": config.model_dump(mode="json"),
            "request": context.state.get("current_user_request", ""),
            "original_request": context.state.get("original_user_request", ""),
            "analysis_work":context.state.get("analysis_work"),
            "query_input_questions":context.state.get("query_input_questions", []),
            "research_intake":context.state.get("research_intake", {}),
            "deliverables": context.state.get("desired_deliverables", []),
            "execution_requirements": context.state.get("execution_requirements", {}),
            "inputs": self.inputs,
        }
        if context.state.get("continuous_investigation"):
            contract["continuous_investigation"] = True
        if context.state.get("research_assignment"):
            contract["research_assignment"] = context.state["research_assignment"]
        if context.state.get("repair_request_id"):
            contract["repair_request_id"] = context.state["repair_request_id"]
        self.contract = contract
        key = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        self.path = context.artifact_dir("delivery_checkpoints") / (key + ".json")
        self.draft_path = self.path.with_suffix(".draft.json")

    def _clarification_parent(self):
        """Find the one prior checkpoint whose only config change is added budget."""
        if not self.context.state.get('clarification_reply') or self.draft_path.exists():
            return None
        matches = []
        current = json.loads(json.dumps(self.contract))
        current_config = current.pop('config')
        for candidate in self.draft_path.parent.glob('*.draft.json'):
            payload = json.loads(candidate.read_text(encoding='utf-8'))
            prior = json.loads(json.dumps(payload.get('checkpoint_contract') or {}))
            prior_config = prior.pop('config', None)
            if prior != current or not prior_config:
                continue
            prior_limits = (prior_config.pop('max_llm_calls', None), prior_config.pop('max_tool_calls', None))
            current_copy = dict(current_config)
            current_limits = (current_copy.pop('max_llm_calls', None), current_copy.pop('max_tool_calls', None))
            if prior_config != current_copy:
                continue
            if (not all(isinstance(value, int) for value in (*prior_limits, *current_limits))
                    or prior_limits[0] > current_limits[0] or prior_limits[1] > current_limits[1]):
                continue
            matches.append(candidate)
        if len(matches) > 1:
            raise ReviewContinuationUnavailable('CLARIFICATION_CHECKPOINT_AMBIGUOUS: multiple prior work checkpoints match')
        return matches[0] if matches else None

    def save_work(self, budget):
        from bioma.harness.execution_view import computation_packages
        observations = list(self.context.observations or self.context.state.get('harness_observations', []))
        recorded = {item['execution_package']['metrics']['receipt_path']
                    for item in observations if item.get('execution_package')}
        for ref, package in computation_packages(self.context).items():
            if ref not in recorded:
                observations.append({'tool_name':package.get('tool_name'),
                    'status':package.get('status', 'unknown'),
                    'summary':'Legacy checkpoint native execution observation',
                    'metrics':package['metrics'], 'execution_package':package})
        payload = {
            "checkpoint_contract": self.contract,
            "draft": self.context.analysis_draft,
            "investigation_state": {key: self.context.state[key] for key in
                ("unresolved_tool_action", "investigation_notes", "admitted_tool_inputs", "admitted_execution_inputs", "discovered_tool_schemas", "final_answer_candidates", "review_budget_insufficient") if key in self.context.state},
            "observations": observations,
            "tool_receipts": self.context.state.get("tool_receipts") or [],
            "budget": budget.telemetry.flush(),
            "inputs": self.inputs,
        }
        temporary = self.draft_path.parent / (uuid4().hex + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            _replace_checkpoint(temporary, self.draft_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify(self, fingerprints):
        check()
        for filename, digest in {**self.inputs, **fingerprints}.items():
            if _digest(filename) != digest:
                raise ValueError(
                    f"Analysis checkpoint file changed: {Path(filename).name}"
                )

    def require_review_checkpoint(self, receipt):
        # A receipt file alone is not work-package membership. Never scan/adopt
        # another checkpoint or start a fresh Worker when exact recovery fails.
        if not self.draft_path.is_file():
            alternatives = sorted(p.name for p in self.draft_path.parent.glob('*.draft.json'))
            raise ReviewContinuationUnavailable(
                'REVIEW_CHECKPOINT_NOT_FOUND: no exact work checkpoint for the current '
                'input, contract, configuration and execution environment. '
                f'Expected {self.draft_path.name}; existing checkpoints: {alternatives}. '
                'Restore the original invocation environment; do not recompute or '
                'substitute a receipt. No review/tool dispatch occurred.')
        work = json.loads(self.draft_path.read_text(encoding='utf-8'))
        recorded = work.get('checkpoint_contract')
        if recorded is not None and recorded != self.contract:
            raise ReviewContinuationUnavailable('REVIEW_CHECKPOINT_CONTRACT_MISMATCH: recorded invocation changed')
        state = {**work.get('investigation_state', {}), 'tool_receipts':work.get('tool_receipts', []),
                 'task_id':self.context.task_id,
                 'artifact_scope_id':self.context.state.get('artifact_scope_id')}
        from bioma.harness.execution_view import delivery_support_packages
        from bioma.harness.types import HarnessContext
        catalog = delivery_support_packages(HarnessContext(state, self.context.base_dir,
            observations=work.get('observations', [])))
        saved = json.loads(self.path.read_text()).get('observation', {}) if self.path.is_file() else {}
        metrics = saved.get('metrics', {})
        selected = metrics.get('selected_support_receipt_refs') or []
        package_member = (metrics.get('receipt_path') == receipt and selected
                          and all(ref in catalog for ref in selected))
        if receipt not in catalog and not package_member:
            raise ReviewContinuationUnavailable(
                'REVIEW_RECEIPT_NOT_REGISTERED: selected receipt is not part of this '
                'checkpoint; no review/tool dispatch occurred')

    def load(self, budget):
        saved = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        if saved is not None:
            # Reject a foreign review before restoring any work state or budget.
            self._require_saved_contract(saved)
        draft = self.draft_path if self.draft_path.exists() else self._clarification_parent()
        if draft is not None:
            work = json.loads(draft.read_text(encoding="utf-8"))
            # A filename is not proof of session identity. In particular, copying
            # a draft under a new model's key must not restore its judgments,
            # conversation state or spending as if it were the original session.
            # The separately checked clarification parent permits only the
            # existing explicit reply/budget extension rule.
            if draft == self.draft_path and work.get("checkpoint_contract") != self.contract:
                raise ReviewContinuationUnavailable(
                    'SESSION_CONTRACT_MISMATCH: model, environment or research contract changed. '
                    'Cross-model result reuse requires a separately authorized handoff; '
                    'it is not session restoration. Original artifacts remain retained.')
            self._verify(work["inputs"])
            receipts = work.get("tool_receipts") or []
            self._verify({item["path"]: item["sha256"] for item in receipts})
            self.context.analysis_draft = work.get("draft")
            investigation = work.get("investigation_state", {})
            self._verify(investigation.get('final_answer_candidates', {}))
            if investigation.get('unresolved_tool_action'):
                raise ReviewContinuationUnavailable(
                    'UNRESOLVED_TOOL_ACTION: prior dispatch has no confirmed outcome; '
                    'retain artifacts and reconcile before any model call or replay')
            admitted = investigation.get('admitted_tool_inputs') or {}
            receipt_hashes = {item['path']: item['sha256'] for item in receipts}
            for filename, binding in admitted.items():
                if receipt_hashes.get(binding['receipt_path']) != binding['receipt_sha256']:
                    raise ValueError('Retrieved input has no bound tool receipt')
                recorded = json.loads(Path(binding['receipt_path']).read_text())
                if (not recorded.get('result_validated') or recorded.get('status') != 'ok'
                        or recorded.get('result', {}).get('files', {}).get(filename) != binding['sha256']):
                    raise ValueError('Retrieved input differs from tool result')
                self._verify({filename: binding['sha256']})
            from bioma.harness.recovery import verify_partial_inputs
            recovered = investigation.get('admitted_execution_inputs', {})
            verify_partial_inputs(recovered)
            self.context.state.update(investigation)
            self.context.state['raw_data_sources'] = list(dict.fromkeys([
                *self.context.state.get('raw_data_sources', []), *admitted, *recovered]))
            self.context.observations = work.get("observations") or []
            # Read old checkpoint catalogs once as historical observations.
            # New saves persist only native observations, never a second catalog.
            from bioma.harness.execution_view import computation_packages
            packages = computation_packages(self.context)
            existing = {item['execution_package']['metrics']['receipt_path']
                        for item in self.context.observations if item.get('execution_package')}
            for ref, package in packages.items():
                if ref not in existing:
                    self.context.observations.append({'tool_name':package['tool_name'],
                        'status':package['status'], 'summary':'Historical native execution observation',
                        'metrics':package['metrics'], 'execution_package':package})
            self.context.state.pop('investigation_completed', None)
            self.context.state.pop('investigation_latest', None)
            self.context.state.update(
                harness_observations=self.context.observations, tool_receipts=receipts
            )
            for name, value in work["budget"].items():
                setattr(budget.telemetry, name, value)
        if saved is None:
            return None
        self._verify(saved["sha256"])
        for name, value in saved["budget"].items():
            setattr(budget.telemetry, name, max(getattr(budget.telemetry, name), value))
        observation = ToolObservation(**saved["observation"])
        from bioma.runtime.capability_requests import delivery_result
        for ref, identity in observation.metrics.get('native_receipt_hashes', {}).items():
            delivery_result(ref, identity)
        if ("checkpoint_contract" not in saved or
                saved.get('review_policy_sha256') != review_policy_sha256()):
            # Preserve the original assessment on disk. Its decision is not an
            # active blocker or approval under a changed admission policy.
            for key in ('delivery_complete', 'delivery_review', 'delivery_review_rejected'):
                observation.patch.pop(key, None)
            if observation.patch.get('delivery_review_incomplete'):
                observation.patch['delivery_review_incomplete']['review_policy_changed'] = True
                observation.patch['delivery_review_incomplete']['requires_fresh_adjudication'] = True
            observation.metrics.pop('consistency_review', None)
            observation.metrics.pop('specialized_review', None)
            observation.metrics['review_policy_changed'] = True
            if "checkpoint_contract" not in saved:
                observation.metrics['review_contract_unbound'] = True
        emit(
            "analysis.resumed",
            stage=(
                "reviewed" if observation.patch.get("delivery_complete") else "computed"
            ),
        )
        return observation

    def _require_saved_contract(self, saved):
        recorded = saved.get("checkpoint_contract")
        if recorded is None and self.draft_path.is_file():
            # Older review checkpoints used the adjacent work checkpoint as
            # their contract binding. Preserve that migration path, not an
            # unchecked fallback based on a caller-supplied filename.
            recorded = json.loads(self.draft_path.read_text(encoding="utf-8")).get("checkpoint_contract")
        if recorded != self.contract:
            raise ReviewContinuationUnavailable(
                'SESSION_CONTRACT_MISMATCH: delivery checkpoint is not bound to '
                'this model, environment and research contract; no approval restored.')

    def _save(self, observation, budget):
        if self.path.exists():
            previous = json.loads(self.path.read_text(encoding="utf-8"))
            self._require_saved_contract(previous)
            self._verify(previous["sha256"])
        fingerprints = dict(observation.metrics["sha256"])
        candidate = observation.metrics.get('answer_candidate_ref')
        if candidate:
            fingerprints[candidate] = observation.metrics['execution_sha256'][candidate]
        files = [
            observation.metrics["receipt_path"],
            *observation.patch["analysis_code_paths"],
        ]
        if observation.patch.get("delivery_complete"):
            files.append(observation.patch["delivery_review"]["path"])
        pending_review = (observation.patch.get("delivery_review_incomplete")
                          or observation.patch.get("delivery_review_rejected") or {})
        if pending_review.get("path"):
            files.append(pending_review["path"])
        fingerprints.update({p: _digest(p) for p in files})
        self._verify(fingerprints)
        payload = {
            "checkpoint_contract": self.contract,
            "review_policy_sha256": review_policy_sha256(),
            "execution_status": ("REVIEWED" if observation.patch.get("delivery_complete")
                                 else "COMPUTED_PENDING_REVIEW"),
            "observation": asdict(observation),
            "sha256": fingerprints,
            "budget": budget.telemetry.flush(),
        }
        self._write(payload)

    def _write(self, payload):
        # Same-directory atomic replacement without repeating the 64-character key.
        temporary = self.path.parent / (uuid4().hex + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            _replace_checkpoint(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def review(self, observation, llm, budget):
        candidate_identity = _review_candidate_identity(observation, self.context)
        if self.path.exists():
            previous = json.loads(self.path.read_text(encoding='utf-8'))
            self._require_saved_contract(previous)
            if previous.get('review_policy_sha256') == review_policy_sha256():
                prior_observation = ToolObservation(**previous['observation'])
                pending = (prior_observation.patch.get('delivery_review_incomplete')
                           or prior_observation.patch.get('delivery_review_rejected') or {})
                prior_identity = pending.get('review_candidate_identity')
                if pending and not prior_identity:
                    prior_identity = _review_candidate_identity(prior_observation, self.context)
                if pending and prior_identity == candidate_identity:
                    raise ComputedPendingReview(
                        self.path,
                        'UNCHANGED_REVIEW_MATERIAL: the same result, sources and checks remain '
                        'unresolved; preserve the package and obtain new review basis before resubmission.',
                    )
        self._save(observation, budget)
        # The calculation is durably identified before its intent is cleared.
        # Subsequent model-review failure must not be confused with unknown
        # execution, and a crash before this point must never permit replay.
        if self.context.state.pop('unresolved_tool_action', None) is not None:
            self.save_work(budget)
        try:
            feedback = None
            attempts = 1 if self.context.state.get('harness_mode', 'current') == 'current' else 2
            for attempt in range(attempts):
                try:
                    kwargs = {'reviewer_feedback': feedback} if feedback else {}
                    result = review_delivery(self.context, observation, llm, budget,
                        on_request=lambda: self._save(observation, budget), **kwargs)
                    break
                except DeliveryReviewIncomplete:
                    if attempt + 1 == attempts or budget.remaining_llm_calls < 1:
                        raise
                    review_path = Path(observation.metrics['receipt_path']).parent / 'delivery_review.json'
                    feedback = json.loads(review_path.read_text())['unresolved_findings']
                    # One bounded clarification on unchanged bytes, never an
                    # actor repair or an unbounded retry-until-approved loop.

        except ModelBudgetUnavailable as exc:
            # Preserve bytes and qualification boundary; a new grant may resume review.
            raise ComputedPendingReview(self.path, str(exc)) from exc
        except DeliveryReviewIncomplete as exc:
            # Preserve the exact review observation as non-evidentiary guidance.
            # A later authorized Agent turn may inspect the cited data and choose
            # whether to repair, clarify, or resubmit; Runtime chooses none of them.
            review_path = Path(observation.metrics['receipt_path']).parent / 'delivery_review.json'
            review_record = json.loads(review_path.read_text(encoding='utf-8'))
            response = review_record.get('response') or '{}'
            try:
                reported_issues = json.loads(response).get('issues', []) if isinstance(response, str) else response.get('issues', [])
            except (ValueError, TypeError):
                reported_issues = []
            observation.patch['delivery_review_incomplete'] = {
                'path': str(review_path),
                'sha256': _digest(review_path),
                'assessment_status': review_record.get('assessment_status'),
                'reported_issues': reported_issues,
                'active_issues': review_record.get('active_issues') or [],
                'unresolved_findings': review_record.get('unresolved_findings') or [],
                'evidence_status': 'not_evidence',
                'decision_authority': 'agent',
                'review_candidate_identity': candidate_identity,
                'scope': ('Hash-bound independent review observation for investigation; '
                          'not a verified scientific fact, code-edit instruction, execution grant, or approval.'),
            }
            self._save(observation, budget)
            raise ComputedPendingReview(self.path, str(exc)) from exc
        except DeliveryReviewRejected as exc:
            digest = hashlib.sha256(json.dumps(
                observation.metrics['sha256'], sort_keys=True).encode()).hexdigest()
            exc.computed_delivery_sha256 = digest
            observation.patch['delivery_review_rejected'] = {
                'issues': exc.issues,
                'dismissed_non_issues': exc.dismissed_non_issues,
                'computed_delivery_sha256': digest,
                'path': str(Path(observation.metrics['receipt_path']).parent / 'delivery_review.json'),
                'sha256': _digest(Path(observation.metrics['receipt_path']).parent / 'delivery_review.json'),
                'evidence_status': 'not_evidence',
                'review_candidate_identity': candidate_identity,
            }
            # Keep hash-bound executed work for targeted repair and audit. It is
            # still unapproved and cannot be published as a completed delivery.
            self._save(observation, budget)
            raise
        except (ModelTransportError, ModelOutputBudgetError, ExecutionTimeout, OSError):
            # The call budget was persisted before dispatch, including interrupted calls.
            raise
        except Exception:
            # Invalid packages without a structured scientific review cannot be
            # safely resumed as computed work.
            self.path.unlink(missing_ok=True)
            raise
        self._save(result, budget)
        return result
