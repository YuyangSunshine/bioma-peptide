"""Thin OptionExecutor compatibility adapter, separate from decision policy.

Execution bindings are persisted before dispatch. Rebuild uses ledger evidence
and those versioned bindings, so restart does not need raw provider results.
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from types import SimpleNamespace

from bioma.scientific_control import GroundedOption, ScientificOption, OptionExecutor, new_execution_record
from .belief import EvidenceBeliefBridge, EvidenceLikelihoodModel
from .bellman import BellmanDecision
from .session import fingerprint
from bioma.core.scientific_decision import PredictorCapability


@dataclass(frozen=True)
class GovernedCandidate:
    action: str
    option: ScientificOption
    grounded: GroundedOption

    @property
    def capability_contracts(self):
        return tuple(s.capability_binding.model_dump(mode='json') if s.capability_binding else {'capability': s.capability_id, 'method': s.method_id,
            'qualification': s.qualification_provenance_json,
            'evidence_schema': s.result_contract_json,
            'implementation': s.implementation_identity,
            'resources': s.required_resources,
            'inputs': s.input_bindings_json} for s in self.grounded.steps)


class GovernedDecisionEpisode:
    """Runtime owns authorization, qualification and commit; this adapter reads.

    ``contract`` and ``plan`` are executor bindings only. Selection consumes
    DecisionSession and capability contracts, never plan utilities or status.
    """
    def __init__(self, *, runtime, contract, plan, candidates, prior, channels,
                 model_id, session, journal_path, predictor_capability, result_qualifier=None):
        if predictor_capability not in (PredictorCapability.L1_HYPOTHESIS_CONDITIONED,
                                        PredictorCapability.L2_INTERVENTIONAL):
            raise ValueError('HYPOTHESIS_CONDITIONED_FORECAST_UNAVAILABLE')
        if runtime.ledger is None:
            raise ValueError('evidence control requires a persistent ledger')
        self.result_qualifier = result_qualifier
        self.runtime, self.contract, self.plan = runtime, contract, plan
        self.candidates = {c.action: c for c in candidates}
        if len(self.candidates) != len(candidates):
            raise ValueError('duplicate grounded action identity')
        if set(self.candidates) - set(channels):
            raise ValueError('grounded actions require declared evidence channels')
        if not model_id or not all(isinstance(z, str) for z in prior.probabilities):
            raise ValueError('persistent inference requires model id and string latent identities')
        self.prior, self.channels, self.model_id = prior, channels, model_id
        self.session = session
        self.journal_path = Path(journal_path)
        self.bindings = []
        if self.journal_path.exists():
            data = json.loads(self.journal_path.read_text())
            if data['model_id'] != model_id or data['prior'] != dict(prior.probabilities):
                raise ValueError('inference model/prior changed on replay')
            if data['spec'] != session.spec.model_dump(mode='json'):
                raise ValueError('decision spec changed on replay')
            if data.get('qualification_model_id') != getattr(result_qualifier, 'model_id', None):
                raise ValueError('qualification model changed on replay')
            self.bindings = data['bindings']
            for key, value in data['session'].items():
                setattr(session, key, value)

    def _save(self):
        belief = self.rebuild_belief()
        data = {'qualification_model_id': getattr(self.result_qualifier, 'model_id', None),
                'model_id': self.model_id, 'prior': dict(self.prior.probabilities),
                'spec': self.session.spec.model_dump(mode='json'),
                'bindings': self.bindings, 'session': self.session.checkpoint(),
                'belief': {'evidence_refs': belief.evidence_refs,
                          'evidence_state_version': belief.evidence_state_version,
                          'inference_model_id': belief.inference_model_id,
                          'belief_version': belief.version}}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, sort_keys=True, indent=2, allow_nan=False))
        temporary.replace(self.journal_path)

    def rebuild_belief(self):
        evidence, likelihoods = {}, {}
        for binding in self.bindings:
            binding_refs = set()
            for task_id in binding['task_ids']:
                state = self.runtime.ledger.replay_state(task_id)
                if state is None:
                    continue
                for item in state.evidence:
                    symbol = (item.value or {}).get('qualified_outcome')
                    if symbol is None:
                        continue
                    binding_refs.add(item.id)
                    if len(binding_refs) > 1:
                        raise ValueError('one action channel requires one jointly qualified outcome')
                    rows = [row for row in binding['likelihoods'] if row['symbol'] == symbol]
                    if len(rows) != 1:
                        raise ValueError('committed evidence outside declared channel')
                    likelihood = rows[0]['values']
                    if item.id in likelihoods and likelihoods[item.id] != likelihood:
                        raise ValueError('conflicting inference bindings')
                    evidence[item.id], likelihoods[item.id] = item, likelihood
        # Scientific evidence revision excludes receipts, WORK and task versions.
        ordered = tuple(evidence[key] for key in sorted(evidence))
        version = int(fingerprint([item.model_dump(mode='json') for item in ordered])[:15], 16)
        projection = SimpleNamespace(version=version, evidence=ordered)
        return EvidenceBeliefBridge().derive(projection, self.prior,
            EvidenceLikelihoodModel(self.model_id, likelihoods))

    def cycle(self, *, context, horizon, execution_instance_id,
              task_class='SCIENTIFIC_CONTROL', unresolved_effects=(), objective_amendments=None):
        if task_class != 'SCIENTIFIC_CONTROL' or unresolved_effects:
            return None
        if any(b.get('execution_record', {}).get('unresolved_effect_refs') for b in self.bindings):
            raise ValueError('persisted unresolved effect requires independent reconciliation')
        before = self.rebuild_belief()
        pending = [b for b in self.bindings if b.get('status') == 'PENDING']
        if len(pending) > 1:
            raise ValueError('multiple pending executions require reconciliation')
        if pending:
            binding = pending[0]
            decision = BellmanDecision(**binding['decision'])
            candidate = self.candidates[binding['action']]
            if candidate.grounded.identity != binding['grounding']:
                raise ValueError('pending execution grounding changed; reconcile first')
        else:
            decision = self.session.decide(task_class=task_class, belief=before,
                operational_state=context,
                qualified_capabilities={a: {'contracts': c.capability_contracts,
                    'grounding': c.grounded.identity, 'target': c.option.scientific_intent}
                    for a, c in sorted(self.candidates.items())},
                unresolved_effects=unresolved_effects, objective_amendments=objective_amendments,
                horizon=horizon)
            if decision is None:
                return None
            if decision.status != 'SELECT':
                self._save()
                return {'decision': decision, 'execution': None, 'before': before, 'after': before,
                        'fingerprint': self.session.previous_fingerprint}
            if decision.action not in self.candidates:
                raise ValueError('selected action has no qualified grounding')
            candidate = self.candidates[decision.action]
            channel = self.channels[decision.action]
            valued_channel = self.session.controller.channel(decision.action, context)
            if any(channel.evidence_distribution(decision.action, z, context) !=
                   valued_channel.evidence_distribution(decision.action, z, context)
                   for z in self.prior.probabilities):
                raise ValueError('valuation and inference evidence channels differ')
            task_ids = ['option.' + hashlib.sha256(
                f'{execution_instance_id}:{step.step_id}'.encode()).hexdigest()
                for step in candidate.grounded.steps]
            if any(b['execution_instance_id'] == execution_instance_id for b in self.bindings):
                raise ValueError('execution identity already consumed')
            binding = {'execution_instance_id': execution_instance_id, 'task_ids': task_ids,
                'action': decision.action, 'grounding': candidate.grounded.identity,
                'context': context, 'status': 'PENDING', 'decision': asdict(decision),
                'likelihoods': [
                    {'symbol': symbol, 'values': channel.evidence_likelihood(symbol,
                        decision.action, tuple(self.prior.probabilities), context)}
                    for symbol in channel.predictive_evidence(self.prior, decision.action, context)]}
            self.bindings.append(binding)
            self._save()  # durable binding and selection precede any real effect
        record = new_execution_record(candidate.option, candidate.grounded,
                                      execution_instance_id=binding['execution_instance_id'])
        result = OptionExecutor(self.runtime).execute(candidate.option, candidate.grounded,
            contract=self.contract, plan=self.plan, record=record)
        if self.result_qualifier is not None and result.lifecycle.value == 'COMPLETED':
            qualified_refs = []
            for task_id in binding['task_ids']:
                qualified_refs.extend(self.result_qualifier.qualify(self.runtime, task_id, result.result_refs))
            result = result.model_copy(update={'evidence_refs': tuple(sorted(set(
                (*result.evidence_refs, *qualified_refs))))})
        binding['status'] = result.lifecycle.value
        binding['execution_record'] = result.model_dump(mode='json')
        after = self.rebuild_belief()
        self._save()
        return {'decision': decision, 'execution': result, 'before': before, 'after': after,
                'fingerprint': self.session.previous_fingerprint}
