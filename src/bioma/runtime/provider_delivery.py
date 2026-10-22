"""Deterministic delivery of runtime-verified WorkResult records."""
import hashlib
import json
from pathlib import Path

from bioma.core.models import AgentMessage, Artifact, StateDelta
from bioma.core.scientific_capabilities import WorkResult


class ReportRenderer:
    def render(self, ledger, run_id, observation_id, *, markdown=False):
        # Replay independently checks receipts, transition proofs and the chain.
        if ledger is None or not ledger.verify_chain(run_id):
            raise ValueError('Delivery requires a valid runtime ledger')
        state = ledger.replay_state(run_id)
        if state is None:
            raise ValueError('No verified result exists')
        observation = next((o for o in state.observations if o.id == observation_id and o.kind == 'work_result'), None)
        if observation is None:
            raise ValueError('Source is not a verified WorkResult')
        result = WorkResult.model_validate(observation.value)
        text = json.dumps(result.model_dump(mode='json'), ensure_ascii=False, indent=2)
        return '# Scientific work result\n\n```json\n' + text + '\n```\n' if markdown else text

    def render_run(self, ledger, run_id, *, markdown=False):
        if ledger is None or not ledger.verify_chain(run_id):
            raise ValueError('Delivery requires a valid runtime ledger')
        state = ledger.replay_state(run_id)
        rows = [WorkResult.model_validate(row.value).model_dump(mode='json')
                for row in state.observations if row.kind == 'work_result'] if state else []
        if not rows:
            raise ValueError('No verified result exists')
        text = json.dumps({'task_id': run_id, 'work_results': rows}, ensure_ascii=False, indent=2)
        return '# Scientific work results\n\n```json\n' + text + '\n```\n' if markdown else text


class TraceRenderer:
    def render(self, runtime_events):
        return '\n'.join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in runtime_events)


def deliver_named_results(runtime, state, names, directory):
    """Package approved material after scientific methods finish; never call an LLM."""
    from bioma.core.acceptance import protocol_for, current_review_record, completion_status
    policy = protocol_for(state)
    research = bool(policy and policy.mode == 'research')
    if not names:
        return state
    if research:
        if current_review_record(state) is None:
            raise ValueError('Scientific delivery requires a qualified current full-contract review')
    elif state.status != 'completed':
        return state
    existing = {a.metadata.get('relative_path') or Path(a.uri).name for a in state.artifacts}
    names = list(dict.fromkeys(names)) if research else [name for name in dict.fromkeys(names) if name not in existing]
    if not names:
        return state
    if runtime.ledger is None:
        raise ValueError('Named provider delivery requires a verification ledger')
    from bioma.runtime.task_lease import task_lease
    with task_lease(runtime.ledger, state.task_id):
        committed = runtime.ledger.replay_state(state.task_id)
        if committed is None or committed.version != state.version:
            raise ValueError('Delivery source version changed')
        work = [o for o in committed.observations if o.kind == 'work_result']
        if not work:
            raise ValueError('No verified scientific work to deliver')
        if research:
            from uuid import NAMESPACE_URL, uuid5
            from bioma.core.models import Evidence, TaskRequest
            from bioma.core.research import contract_for, fingerprint
            from bioma.runtime.acceptance import stage_output, finalize
            review = current_review_record(committed)
            if review is None:
                raise ValueError('Scientific review changed before delivery')
            obligations = [r for r in contract_for(committed).requirements
                           if r.id in policy.delivery_requirement_ids]
            declared = {name for r in obligations for name in r.artifact_names}
            if set(names) != declared or not declared:
                raise ValueError('Scientific delivery must cover the declared artifact names exactly')
            # The renderer is not another scientific producer: publish the exact
            # bytes reviewed earlier, never a new unreviewed interpretation.
            reviewed_evidence = [e for e in committed.evidence if e.id in review.parent_ids]
            reviewed_artifacts = {ref for e in reviewed_evidence for ref in e.artifact_ids}
            reviewed_work = {e.value['work_result_ref'] for e in reviewed_evidence
                if isinstance(e.value, dict) and isinstance(e.value.get('work_result_ref'), str)}
            reviewed_artifacts.update(a.id for a in committed.artifacts
                if a.metadata.get('work_result_ref') in reviewed_work)
            originals = []
            for name in names:
                if Path(name).name != name:
                    raise ValueError('Invalid delivery basename')
                matches = [a for a in committed.artifacts if Path(a.uri).name == name
                           and not a.metadata.get('delivery_basis')]
                if len(matches) != 1:
                    raise ValueError('Delivery needs one unambiguous reviewed artifact: ' + name)
                original = matches[0]
                if original.id not in reviewed_artifacts:
                    raise ValueError('Delivery artifact is not bound to qualified research evidence: ' + name)
                if hashlib.sha256(Path(original.uri).read_bytes()).hexdigest() != original.metadata.get('sha256'):
                    raise ValueError('Reviewed artifact changed before publication')
                originals.append(original)
            previous = [a for a in committed.artifacts
                        if a.metadata.get('delivery_basis', {}).get('review_evidence_id') == review.id]
            if previous:
                if (set(names) != {Path(a.uri).name for a in previous}
                        or any(not Path(a.uri).is_file() or
                            hashlib.sha256(Path(a.uri).read_bytes()).hexdigest() != a.metadata.get('sha256')
                            for a in previous)):
                    invalidation = AgentMessage(sender='harness', recipient='harness',
                        message_type='runtime.delivery_revalidation', payload={'review_evidence_id':review.id})
                    delta = StateDelta(agent_id='harness', source_message_id=invalidation.id,
                        expected_version=committed.version, outcome='continue',
                        status_reason={'code':'delivery_artifact_changed', 'review_evidence_id':review.id},
                        updates={'metrics':{'acceptance':completion_status(committed)}})
                    check, receipt = runtime.verifier.verify_with_receipt(committed, delta,
                        expected_agent='harness', source_message=invalidation)
                    if not check.ok:
                        raise ValueError('Delivery invalidation rejected: ' + '; '.join(check.reasons))
                    runtime.ledger.commit_transition(committed,
                        delta.model_copy(update={'status':'verified'}), invalidation, receipt)
                    raise ValueError('Previously delivered artifact is missing or changed')
                if not completion_status(committed)['complete']:
                    raise ValueError('Previous delivery no longer meets the contract')
                return committed
            request = TaskRequest(id=str(uuid5(NAMESPACE_URL, fingerprint([state.task_id, review.id, names]))),
                requester='harness', required_capability='scientific.compute', objective=state.objective,
                message_type='runtime.delivery', payload={'accepted_review_evidence_id':review.id,
                    'research_assignment':{'contract_id':contract_for(state).identity,
                        'requirement_ids':list(policy.delivery_requirement_ids)}})
            message = AgentMessage(sender='harness', recipient='harness', message_type='runtime.delivery',
                task_request_id=request.id, payload=request.payload)
            evidence = Evidence(kind='delivery', source='harness',
                claim='Exact reviewed artifact versions delivered; no new scientific assertion.',
                value={'reviewed_artifacts':[{ 'id':a.id, 'sha256':a.metadata['sha256'] } for a in originals]},
                parent_ids=[review.id])
            directory = Path(directory).resolve() / review.id
            directory.mkdir(parents=True, exist_ok=True)
            artifacts = []
            for original in originals:
                path = directory / Path(original.uri).name
                data = Path(original.uri).read_bytes()
                if hashlib.sha256(data).hexdigest() != original.metadata['sha256']:
                    raise ValueError('Reviewed artifact changed during publication')
                if path.exists() and path.read_bytes() != data:
                    raise ValueError('Publication would overwrite a different artifact version')
                path.write_bytes(data)
                artifact = Artifact(source='harness', kind=original.kind, uri=str(path),
                    evidence_ids=[evidence.id], metadata={'sha256':original.metadata['sha256'],
                        'reviewed_artifact_id':original.id, 'reviewed_state_identity':review.value['reviewed_state_identity']})
                artifacts.append(artifact)
                evidence.artifact_ids.append(artifact.id)
            delta = StateDelta(agent_id='harness', source_message_id=message.id, expected_version=state.version,
                evidence=[evidence], artifacts=artifacts, task_requests=[request], outcome='completed')
            delta = stage_output(committed, delta, message)
            delta = finalize(committed, delta, message, (), delta.outcome)
            check, receipt = runtime.verifier.verify_with_receipt(committed, delta,
                expected_agent='harness', source_message=message)
            if not check.ok:
                raise ValueError('Scientific delivery rejected: ' + '; '.join(check.reasons))
            return runtime.ledger.commit_transition(committed, delta.model_copy(update={'status':'verified'}), message, receipt)
        artifacts = []
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            if Path(name).name != name or Path(name).suffix.lower() not in {'.md', '.json', '.txt'}:
                raise ValueError('No deterministic renderer for named output: ' + name)
            if 'trace' in Path(name).stem.casefold():
                events = [{'event_id': event.event_id, 'kind': event.kind, 'actor': event.actor,
                           'payload': event.payload} for event in runtime.ledger.events(state.task_id)]
                text = (json.dumps(events, ensure_ascii=False, indent=2) if Path(name).suffix.lower() == '.json'
                        else TraceRenderer().render(events))
            else:
                text = ReportRenderer().render_run(runtime.ledger, state.task_id,
                                                   markdown=Path(name).suffix.lower() == '.md')
            path = directory / name
            path.write_text(text, encoding='utf-8')
            artifacts.append(Artifact(source='harness', kind='verified-work-report', uri=str(path),
                metadata={'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'work_result_ref': work[-1].id}))
        message = AgentMessage(sender='harness', recipient='harness', message_type='runtime.delivery')
        delta = StateDelta(agent_id='harness', source_message_id=message.id, expected_version=state.version,
                           artifacts=artifacts, outcome='completed')
        check, receipt = runtime.verifier.verify_with_receipt(state, delta,
            expected_agent='harness', source_message=message)
        if not check.ok:
            raise ValueError('Delivery verification failed: ' + '; '.join(check.reasons))
        return runtime.ledger.commit_transition(state, delta.model_copy(update={'status': 'verified'}), message, receipt)
