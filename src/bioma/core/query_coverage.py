"""Independent query coverage gate; no routing, tools, or scientific execution.

The inventory is elicited without the proposed contract or recalled memory.
Semantic correspondence is reviewed separately; identifiers, source quotations,
mandatory result kinds and content binding are checked by deterministic code.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from bioma.core.research import ResearchModel, fingerprint


COVERAGE_METHOD = 'coverage_gated_query_interpreter_v5'
SUBJECT_FIELDS = ('acceptance_mode', 'goals', 'claims', 'constraints',
                  'missing_information', 'evidence_requirements',
                  'desired_deliverables', 'execution_requirements', 'sequence_sources',
                  'candidate_design')

# Scientific outcomes, not file formats or Agent identities. Serializing a
# candidate does not create a new data-analysis responsibility.
RESULT_KINDS = {
    'candidate_design': {'prediction'},
    'study_design': {'protocol'},
    'calculation': {'prediction', 'analysis'},
    'data_analysis': {'analysis', 'prediction'},
    'observation': {'execution'},
    'evidence_evaluation': {'evidence_graph_synthesis', 'analysis'},
    'retrieval': {'structure_resolution', 'analysis', 'prediction'},
    'artifact': None,
    'other': None,
}


class QueryObligation(ResearchModel):
    id: str = Field(min_length=1)
    source_id: str = 'query'
    quote: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: Literal['result', 'constraint', 'exclusion']
    result_category: Literal['candidate_design', 'study_design', 'calculation',
        'data_analysis', 'observation', 'evidence_evaluation', 'retrieval', 'artifact', 'other'] | None = Field(default=None,
        description='Scientific outcome, not an Agent or internal evidence kind. artifact means saving/packaging an already requested result, owned by its producer; it does not mean data_analysis. Use null for constraints/exclusions. A planned comparison with controls is study_design.')


class ObligationInventory(ResearchModel):
    obligations: list[QueryObligation] = Field(min_length=1)


class CoverageRow(ResearchModel):
    inventory_obligation_id: str = Field(description='Exact ID from VALID_INVENTORY_IDS; never a contract ID.')
    status: Literal['covered', 'missing', 'contradictory']
    contract_requirement_ids: list[str] = Field(default_factory=list,
        description='Exact IDs from VALID_CONTRACT_REQUIREMENT_IDS; never inventory IDs.')
    contract_constraint_ids: list[str] = Field(default_factory=list,
        description='Exact IDs from VALID_CONTRACT_CONSTRAINT_IDS; never inventory IDs.')
    contract_missing_information_fields: list[str] = Field(default_factory=list,
        description='For constraints asking to obtain missing user input, the exact blocking missing_information field may cover the request. Never use this to replace a future scientific result or an execution prohibition.')
    rationale: str = Field(min_length=1)


class CoverageReview(ResearchModel):
    rows: list[CoverageRow] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list,
        description='Unrequested experiments, missing dependency support, weakened criteria or other scope contradictions. Empty only when none.')
    inventory_issues: list[str] = Field(default_factory=list,
        description='Misread original intentions in the independent inventory itself, e.g. reversing a positive conditional request into a prohibition. Do not force the contract to satisfy an incorrect inventory.')


class CoverageError(ValueError):
    pass


def coverage_subject(query, gateway):
    return {'query': query, **{key: gateway.get(key) for key in SUBJECT_FIELDS}}


def bind_source_constraints(inventory, sources, constraints):
    """Carry verified user restrictions into the contract without paraphrasing them.

    This does not mark them satisfied or map scientific results. The independent
    reviewer still checks the inventory, scope and semantic correspondence.
    """
    validate_inventory(inventory, sources)
    rows = [dict(row) for row in constraints]
    for item in inventory.obligations:
        if item.kind == 'result':
            continue
        expected = source_constraint(item)
        identity = expected['id']
        existing = next((row for row in rows if row['id'] == identity), None)
        if existing is not None and existing != expected:
            raise CoverageError('Source constraint identity collision: ' + identity)
        if existing is None:
            rows.append(expected)
    return rows


def source_constraint(item):
    return {'id': 'source:' + fingerprint([item.source_id, item.quote, item.kind])[:24],
            'expression': item.quote, 'severity': 'error', 'source': item.source_id, 'status': 'proposed'}


def bind_coverage_references(inventory, review, subject):
    """Bind exact source identities, without supplying a semantic verdict.

    Only the framework's unchanged verbatim constraint can be bound this way.
    Scientific result mappings, missing/contradictory statuses and reviewer
    issues remain untouched. Certificate verification still requires the IDs.
    """
    obligations = {item.id: item for item in inventory.obligations}
    constraints = {row['id']: row for row in subject.get('constraints') or []}
    result = review.model_copy(deep=True)
    for row in result.rows:
        item = obligations.get(row.inventory_obligation_id)
        if item is None or item.kind == 'result':
            continue
        expected = source_constraint(item)
        if constraints.get(expected['id']) == expected and expected['id'] not in row.contract_constraint_ids:
            row.contract_constraint_ids.append(expected['id'])
    return result


def validate_inventory(inventory, sources):
    ids = [o.id for o in inventory.obligations]
    if len(ids) != len(set(ids)):
        raise CoverageError('Duplicate original-query obligation id')
    for item in inventory.obligations:
        if item.quote not in sources.get(item.source_id, ''):
            raise CoverageError('Obligation quotation is not verbatim in its user source: ' + item.id)
        if (item.kind == 'result') != (item.result_category is not None):
            raise CoverageError('Only result obligations require a scientific result category: ' + item.id)


def coverage_errors(inventory, review, subject):
    obligations = {o.id: o for o in inventory.obligations}
    row_ids = [r.inventory_obligation_id for r in review.rows]
    rows = {r.inventory_obligation_id: r for r in review.rows}
    duplicated = sorted({identity for identity in row_ids if row_ids.count(identity) > 1})
    missing_rows = sorted(set(obligations) - set(rows))
    unknown_rows = sorted(set(rows) - set(obligations))
    if duplicated or missing_rows or unknown_rows:
        raise CoverageError(
            'Coverage rows must cover every independent obligation exactly once; '
            f'field=inventory_obligation_id; namespace=VALID_INVENTORY_IDS; '
            f'duplicated={duplicated}; missing={missing_rows}; unknown={unknown_rows}; '
            f'valid={sorted(obligations)}')
    needs = {n['id']: n for n in subject['evidence_requirements'] or []}
    constraints = {c['id'] for c in subject['constraints'] or []}
    missing = {m['field'] for m in subject['missing_information'] or []
               if m.get('blocking') and m.get('verification') == 'verified_missing'}
    errors = [*review.inventory_issues, *review.issues]
    from pathlib import Path
    for name in subject.get('desired_deliverables') or []:
        if not isinstance(name, str) or not Path(name).suffix:
            continue  # Logical delivery IDs have their separate typed protocol.
        owners = [n['id'] for n in needs.values() if n.get('mandatory', True) and name in n.get('artifact_names', [])]
        if len(owners) != 1:
            errors.append(f'Named deliverable {name!r} requires exactly one mandatory producer in artifact_names; found {owners}. Bind it to the appropriate evidence node, not just a description or constraint.')
    for key, row in rows.items():
        reference_checks = (
            ('contract_requirement_ids', row.contract_requirement_ids, set(needs), 'VALID_CONTRACT_REQUIREMENT_IDS'),
            ('contract_constraint_ids', row.contract_constraint_ids, constraints, 'VALID_CONTRACT_CONSTRAINT_IDS'),
            ('contract_missing_information_fields', row.contract_missing_information_fields, missing,
             'VALID_CONTRACT_MISSING_INFORMATION_FIELDS'),
        )
        for field, values, valid, namespace in reference_checks:
            invalid = sorted(set(values) - valid)
            if invalid:
                raise CoverageError(
                    f'Invalid coverage reference; inventory_obligation_id={key}; field={field}; '
                    f'namespace={namespace}; invalid={invalid}; valid={sorted(valid)}')
        if row.status != 'covered':
            errors.append(f'{key}: {row.status}: {row.rationale}')
            continue
        obligation = obligations[key]
        selected = [needs[k] for k in row.contract_requirement_ids]
        if obligation.kind == 'result':
            if not selected or any(not n.get('mandatory', True) for n in selected):
                errors.append(f'{key}: required result must map to mandatory evidence obligations')
            kinds = RESULT_KINDS[obligation.result_category]
            if kinds is not None and not any(set(n.get('evidence_kinds', [])) & kinds for n in selected):
                errors.append(f'{key}: requested result responsibility is not covered')
        elif not row.contract_constraint_ids and not (
                obligation.kind == 'constraint' and row.contract_missing_information_fields):
            errors.append(f'{key}: constraint/exclusion must have an explicit contract constraint')
    return errors


def verify_coverage_certificate(query, gateway):
    certificate = gateway.get('coverage_audit')
    if gateway.get('interpretation_method') != COVERAGE_METHOD and certificate is None:
        return  # Explicit structured tasks and historical contracts retain their own admission.
    if not certificate:
        raise CoverageError('Independent query coverage certificate is required')
    subject = coverage_subject(query, gateway)
    if certificate.get('subject_hash') != fingerprint(subject):
        raise CoverageError('Query/obligations changed after independent coverage review')
    if certificate.get('subject') != subject:
        raise CoverageError('Coverage audit must retain the exact reviewed contract')
    sources = certificate.get('sources', {})
    if sources.get('query') != query:
        raise CoverageError('Coverage source query mismatch')
    inventory = ObligationInventory.model_validate(certificate['inventory'])
    review = CoverageReview.model_validate(certificate['review'])
    validate_inventory(inventory, sources)
    errors = coverage_errors(inventory, review, subject)
    if errors:
        raise CoverageError('Query coverage is incomplete: ' + ' | '.join(errors))
