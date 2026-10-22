"""Explicit evidence-gated runtime entry point, separate from the legacy policy."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from bioma.core.models import ScientificState


@dataclass
class EvidenceEpisodeBinding:
    episode: object
    read_operational_state: Callable[[], dict]
    horizon: int = 2
    max_decisions: int = 8


class EvidenceControlRuntime:
    """Factory resolves domain semantics and grounded capabilities, not roles.

    read_operational_state must read actual resource/effect state. Hypothetical
    Bellman transitions are never applied to production operational state.
    """
    def __init__(self, runtime, episode_factory=None, completion_policy=None):
        self.runtime = runtime
        self.episode_factory = episode_factory
        self.completion_policy = completion_policy
        self.controller_calls = 0

    def run(self, task, *, event_sink=None):
        # Only an explicitly constructed decision task enters this controller.
        if task.intent != 'scientific_control':
            return self.runtime.run(task, event_sink=event_sink)
        state = ScientificState.from_task(task)
        if self.episode_factory is None:
            return state.model_copy(update={'status': 'blocked', 'status_reason': {
                'control_outcome': 'DECISION_SPEC_UNRESOLVED',
                'message': 'Explicit decision, inference and grounded capability bindings required'}})
        binding = self.episode_factory(task, self.runtime)
        episode = binding.episode
        outcome, terminal = 'BUDGET_EXHAUSTED', None
        if episode.session.terminal_resolved:
            outcome = 'STOP'
            terminal = episode.session.controller.problem.best_decision(episode.rebuild_belief().state)
        for _ in range(0 if episode.session.terminal_resolved else binding.max_decisions):
            context = binding.read_operational_state()
            unresolved = context.get('unresolved_effect_refs', ())
            if unresolved:
                outcome = 'SUSPENDED'
                break
            calls_before = episode.session.controller_calls
            row = episode.cycle(context=context, horizon=binding.horizon,
                execution_instance_id=f'{task.id}:{calls_before}', unresolved_effects=unresolved)
            self.controller_calls += episode.session.controller_calls - calls_before
            if row is None:
                outcome = 'QUIESCENT'
                break
            if row['decision'].status == 'STOP':
                terminal = episode.session.controller.problem.best_decision(row['after'].state)
                episode.session.terminal_resolved = True
                episode._save()
                outcome = 'STOP'
                break
            if row['execution'].lifecycle.value != 'COMPLETED':
                outcome = row['execution'].lifecycle.value
                break
        from bioma.runtime.projections import merge_committed_child
        for item in episode.bindings:
            for child_id in item['task_ids']:
                child = self.runtime.ledger.replay_state(child_id)
                if child is not None:
                    state = merge_committed_child(state, child)
        belief = episode.rebuild_belief()
        gateway = {**state.gateway, 'evidence_control': {
            'outcome': outcome, 'terminal_decision': terminal,
            'controller_calls': episode.session.controller_calls,
            'belief_version': belief.version, 'evidence_refs': belief.evidence_refs,
            'bayes_risk': episode.session.controller.problem.bayes_risk(belief.state)}}
        from bioma.core.acceptance import completion_status
        from bioma.core.research import contract_for
        acceptance = completion_status(state)
        complete = outcome == 'STOP' and acceptance['complete']
        if complete:
            complete = (bool(self.completion_policy.allow_completion(state, contract_for(state)))
                        if self.completion_policy is not None else
                        not state.gateway.get('goal_completion_policy_required', False))
        # STOP ends the strategy, not the scientific contract. This shares the
        # existing acceptance semantics rather than inventing an EG-VoI success.
        return state.model_copy(update={'gateway': gateway,
            'status': 'completed' if complete else 'blocked',
            'status_reason': {**state.status_reason, 'control_outcome': outcome,
                'contract_complete': bool(complete), 'acceptance': acceptance}})
