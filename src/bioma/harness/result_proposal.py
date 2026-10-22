"""Unverified delivery declarations bound to selected native records.

Availability is never support selection. Rebinding creates a new immutable
proposal; archived drafts and their interpretation remain untouched.
"""
import json
from pathlib import Path
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field

from .execution_view import digest


class ResultProposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    answer_text: str | None = None
    support_receipt_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_requirements: list[str] = Field(default_factory=list)


def retain(context, proposal, *, available=(), parent=None, selection='unselected'):
    checkpoint = getattr(context, 'investigation_checkpoint', None)
    if checkpoint is None:
        raise ValueError('A result proposal requires a bound work-package contract')
    path = context.artifact_dir('final_answer_candidates') / (uuid4().hex + '.json')
    value = {'kind':'ResultProposal', **proposal.model_dump(),
        'text':proposal.answer_text,  # archive reader compatibility; never an independent value
        'status':'unqualified', 'selection':selection, 'parent_proposal_ref':parent,
        'available_receipt_refs':list(available),
        'input_sha256':{str(Path(p).resolve()):digest(p)
                        for p in context.state.get('raw_data_sources', [])},
        'contract':checkpoint.contract}
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    context.state.setdefault('final_answer_candidates', {})[str(path)] = digest(path)
    return str(path)


def load(context, ref):
    path = Path(ref)
    path.resolve().relative_to(context.artifact_dir('final_answer_candidates').resolve())
    expected = context.state.get('final_answer_candidates', {}).get(ref)
    if not expected or digest(path) != expected:
        raise ValueError('Answer candidate is not a current hash-bound work-package proposal')
    value = json.loads(path.read_text())
    checkpoint = getattr(context, 'investigation_checkpoint', None)
    if checkpoint is None or value['contract'] != checkpoint.contract:
        raise ValueError('Answer candidate contract changed')
    if value['input_sha256'] != {str(Path(p).resolve()):digest(p)
                                for p in context.state.get('raw_data_sources', [])}:
        raise ValueError('Answer candidate input scope/version changed')
    return value


def bind(context, ref, selected, artifacts, *, answer=None, assumptions=(), unresolved=(), available=(), selection):
    prior = load(context, ref) if ref else None
    if prior:
        if not set(selected) <= set(prior.get('available_receipt_refs', [])):
            raise ValueError('Selected support did not exist for this answer candidate; propose a new answer')
        if prior.get('support_receipt_refs') and prior['support_receipt_refs'] != selected:
            raise ValueError('Proposal support selection changed; propose a new answer')
        if prior.get('artifact_refs') and prior['artifact_refs'] != artifacts:
            raise ValueError('Proposal artifact selection changed; propose a new answer')
        if answer is not None and answer != prior.get('answer_text', prior.get('text')):
            raise ValueError('Proposal answer changed; propose a new answer')
        answer = prior.get('answer_text', prior.get('text'))
        assumptions = list(prior.get('assumptions', [])) + list(assumptions)
        unresolved = list(prior.get('unresolved_requirements', [])) + list(unresolved)
    proposal = ResultProposal(answer_text=answer, support_receipt_refs=selected,
        artifact_refs=artifacts, assumptions=list(dict.fromkeys(assumptions)),
        unresolved_requirements=list(dict.fromkeys(unresolved)))
    if proposal.unresolved_requirements:
        raise ValueError('Unresolved proposal requirements prevent delivery: ' + '; '.join(proposal.unresolved_requirements))
    # A resume of the exact selected declaration needs no new identity.
    if prior and prior.get('kind') == 'ResultProposal' and all(
            prior.get(k) == v for k, v in proposal.model_dump().items()):
        return ref
    return retain(context, proposal, available=available, parent=ref, selection=selection)


def reviewed_result_basis(result):
    """Bind delivery meaning to selected native records, never the receiving host.

    Native Method returns retain their family and result semantics in the Ledger.
    Parent contract acceptance never qualifies their scientific claims.
    """
    candidate = result.get('accepted_answer_candidate') or {}
    review = result.get('delivery_review') or {}
    hashes = review.get('sha256', {})
    ref = candidate.get('path')
    if not ref or not hashes.get(ref) or candidate.get('sha256') != hashes[ref] or digest(ref) != hashes[ref]:
        raise ValueError('Investigation result requires its reviewed ResultProposal')
    proposal = json.loads(Path(ref).read_text())
    if proposal.get('kind') != 'ResultProposal' or proposal.get('unresolved_requirements'):
        raise ValueError('Result origins require a resolved, reviewed ResultProposal')
    selected = proposal.get('support_receipt_refs', [])
    basis = result.get('result_basis', [])
    if (not selected or len(set(selected)) != len(selected)
            or [row.get('receipt_ref') for row in basis] != selected):
        raise ValueError('Result origins must match explicitly selected support receipts')
    for row in basis:
        receipt = row['receipt_ref']
        if row.get('acquisition') == 'professional_method':
            from bioma.runtime.capability_requests import delivery_result
            expected = review.get('native_receipt_hashes', {}).get(receipt)
            if (not expected or expected != row.get('receipt_sha256')
                    or row.get('scientific_support') != 'not_assessed'):
                raise ValueError('Native result was not covered by parent contract review')
            event = delivery_result(receipt, expected)
            from bioma.core.research import fingerprint
            origins = [{'id':item['id'], 'family':item['value']['family'],
                        'kind':item['value']['kind'], 'identity':fingerprint(item['value'])}
                       for item in event.payload['observation']['work_results']
                       if item['kind']=='work_result']
            if row.get('native_work_results') != origins:
                raise ValueError('Selected native WorkResult identity changed')
            continue
        native = json.loads(Path(receipt).read_text())
        if native.get('tool'):
            kind = native.get('result_kind')
            acquisition = ('retrieval' if kind in {'retrieved_fact', 'source', 'source_set'}
                else 'computation' if kind in {'calculation', 'computed_prediction'} else None)
            if native.get('status') != 'ok' or not native.get('result_validated'):
                acquisition = None
        else:
            acquisition = ('computation' if native.get('execution', {}).get('exit_code') == 0
                           and native.get('execution', {}).get('script_sha256') else None)
        if (row.get('acquisition') != acquisition or acquisition is None
                or row.get('scientific_support') != 'not_assessed'
                or not hashes.get(receipt)
                or row.get('receipt_sha256') != hashes[receipt]
                or digest(receipt) != hashes[receipt]):
            raise ValueError('Result origin is unsupported or changed since review')
    return basis
