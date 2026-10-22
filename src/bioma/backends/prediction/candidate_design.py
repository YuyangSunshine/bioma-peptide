"""Protein substitution proposals and deterministic construction; unqualified outputs."""
import hashlib
import json
from pathlib import Path
from bioma.core.candidates import CandidateBatch, construct_candidates, fasta_for
from bioma.core.models import Artifact
from bioma.core.scientific_capabilities import WorkResult
from bioma.llm import parse_json_object
from bioma.runtime.deadline import bounded_call, check
from bioma.runtime.telemetry import emit


def design_candidates(provider, base_dir, state, message, *, source="scientific-compute"):
    contracts = message.payload.get('delivery_contracts', [])
    candidates = [c for c in contracts if c.get('candidate_spec')]
    if len(candidates) != 1:
        raise ValueError('Candidate construction requires exactly one explicit substitution contract')
    contract = candidates[0]
    if {c['input_hash'] for c in contracts} != {contract['input_hash']}:
        raise ValueError('One execution cannot construct candidates for unrelated input contracts')
    parent = contract['input_bindings']['sequence']
    if hashlib.sha256(parent.encode()).hexdigest() != contract['input_hash']:
        raise ValueError('Candidate contract parent identity does not match its input hash')
    from bioma.backends.prediction.planning import extract_sequence
    if parent != extract_sequence(message.payload.get('inputs', state.inputs)):
        raise ValueError('Candidate contract differs from the granted parent sequence')
    spec = contract['candidate_spec']
    root = Path(base_dir)/'artifacts'/'derived'/state.task_id/contract['input_hash']/'candidates'
    messages = [{'role': 'system', 'content':
        'Propose computational protein substitutions as unverified hypotheses. '
        'Never claim experimental improvement. Return ONLY JSON matching the schema. '
        'Use 1-based positions and exact parent residues. Do not generate full sequences.'},
        {'role': 'user', 'content': json.dumps({'objective': state.objective,
         'parent_sequence': parent, 'contract': spec, 'schema': CandidateBatch.model_json_schema()})}]
    record = None
    checkpoint = root/'candidates.json'
    if checkpoint.exists():
        stored = json.loads(checkpoint.read_text(encoding='utf-8'))
        rebuilt = construct_candidates(parent, spec, {'candidates':[
            {'mutations':r['mutations'],'rationale':r['rationale']} for r in stored['candidates']]})
        if rebuilt != stored:
            raise ValueError('Candidate checkpoint content conflict')
        record = stored
        emit('checkpoint.reused', step_id='candidate_proposal', detail='Reuse validated candidate JSON; no repeated model generation')
    for attempt in range(2):
        if record is not None:
            break
        check()
        emit('plan.step.started', step_id='candidate_proposal', attempt=attempt+1,
             detail='LLM proposes substitutions; no experimental observations')
        raw = bounded_call(lambda: provider.chat(messages, json_mode=True))
        try:
            emit('tool.started', tool='construct_candidate_sequences')
            record = construct_candidates(parent, spec, parse_json_object(raw))
            emit('tool.completed', tool='construct_candidate_sequences', observation={
                'success':True,'count':len(record['candidates']), 'sequence_sha256':record['sequence_sha256']})
            break
        except (ValueError, KeyError, TypeError) as exc:
            emit('tool.failed', tool='construct_candidate_sequences', reason=str(exc))
            emit('verification.rejected', reason=str(exc), step_id='candidate_proposal')
            messages.extend([{'role':'assistant','content':raw},
                             {'role':'user','content':f'Correct this contract error: {exc}'}])
    if record is None:
        raise ValueError('Candidate proposals failed deterministic validation after two attempts')
    check()
    emit('tool.started', tool='write_candidate_artifacts')
    root.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for name, kind, content in [('candidates.json','candidate-design-json',json.dumps(record,indent=2)),
                                ('candidates.fasta','candidate-sequences-fasta',fasta_for(record))]:
        check()
        path = root/name
        # Retries must not silently overwrite a different successful design.
        if path.exists() and path.read_text(encoding='utf-8') != content:
            raise ValueError('Candidate artifact conflict; existing result requires reconciliation')
        if not path.exists():
            path.write_text(content,encoding='utf-8')
        artifacts.append(Artifact(kind=kind, uri=str(path), source=source,
            metadata={'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'input_hash': contract['input_hash']}))
    emit('tool.completed', tool='write_candidate_artifacts', observation={
        'success':True,'artifact_count':len(artifacts), 'sequence_sha256':record['sequence_sha256']})
    return WorkResult(family='scientific.compute', kind='CandidateDesign',
        payload={'candidate_batch': record, 'data_kind': 'hypothesis_proposal',
                 'limitations': ['Constructed substitutions are untested hypotheses; fitness remains unmeasured.']},
        artifacts=[a.model_dump(mode='json') for a in artifacts],
        provenance={'method': 'candidate_substitution_design',
                    'construction': 'llm_hypothesis+deterministic_substitution',
                    'input_hash': contract['input_hash']})
