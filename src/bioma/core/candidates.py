"""Versioned candidate design contract; generation and observation are distinct."""
import hashlib
import re
from typing import Literal
from pydantic import Field
from bioma.core.models import StrictModel

AA = 'ACDEFGHIKLMNPQRSTVWY'


class CandidateDesignSpec(StrictModel):
    schema_version: Literal['candidate-design-v1'] = 'candidate-design-v1'
    count: int = Field(ge=1, le=100)
    substitutions_per_candidate: int = Field(ge=1, le=30)
    method: Literal['llm_hypothesis'] = 'llm_hypothesis'


class CandidateProposal(StrictModel):
    mutations: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class CandidateBatch(StrictModel):
    candidates: list[CandidateProposal] = Field(min_length=1)


def construct_candidates(parent, spec, proposal):
    spec = CandidateDesignSpec.model_validate(spec)
    batch = CandidateBatch.model_validate(proposal)
    if not parent or any(a not in AA for a in parent):
        raise ValueError('Parent must be a complete canonical amino-acid sequence')
    if len(batch.candidates) != spec.count:
        raise ValueError('Candidate count does not satisfy contract')
    rows, seen = [], set()
    for index, item in enumerate(batch.candidates, 1):
        if len(item.mutations) != spec.substitutions_per_candidate:
            raise ValueError('Substitution count does not satisfy contract')
        seq, positions = list(parent), set()
        for mutation in item.mutations:
            match = re.fullmatch(r'([ACDEFGHIKLMNPQRSTVWY])([1-9][0-9]*)([ACDEFGHIKLMNPQRSTVWY])', mutation)
            if not match:
                raise ValueError(f'Invalid substitution: {mutation}')
            source, pos, target = match.groups(); pos = int(pos)
            if pos > len(parent) or parent[pos-1] != source or source == target or pos in positions:
                raise ValueError(f'Parent residue/position mismatch or duplicate: {mutation}')
            positions.add(pos); seq[pos-1] = target
        seq = ''.join(seq)
        if seq in seen:
            raise ValueError('Duplicate candidate sequences')
        seen.add(seq)
        rows.append({'id': f'candidate_{index}', 'sequence': seq,
                     'mutations': item.mutations, 'rationale': item.rationale})
    return {'schema_version': 'candidate-design-v1', 'data_kind': 'prediction',
            'method': spec.method, 'parent_sequence': parent,
            'sequence_sha256': hashlib.sha256(parent.encode()).hexdigest(),
            'contract': spec.model_dump(), 'candidates': rows}


def fasta_for(record):
    return '>parent\n'+record['parent_sequence']+'\n'+''.join(
        '>'+row['id']+'\n'+row['sequence']+'\n' for row in record['candidates'])


def verify_candidate_files(record, fasta, parent, spec):
    expected = construct_candidates(parent, spec, {'candidates': [
        {'mutations': r['mutations'], 'rationale': r['rationale']} for r in record['candidates']]})
    if record != expected or fasta != fasta_for(expected):
        raise ValueError('Candidate content differs from the input-bound contract')
