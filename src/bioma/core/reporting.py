"""Explicit report-delivery intent shared by all scientific methods."""
import re


def assigned_requirements(state, message):
    """Resolve output duties from sealed state, never from a mutable payload copy."""
    from bioma.core.research import contract_for
    assignment = message.payload.get('research_assignment') or {}
    if not assignment:
        return []
    contract = contract_for(state)
    if contract is None or assignment.get('contract_id') != contract.identity:
        raise ValueError('Delivery requires the current sealed contract')
    ids = set(assignment.get('requirement_ids', []))
    # Auxiliary peer work can legitimately have no explicit output obligation.
    # An empty set grants no named-output ownership; unknown IDs still reject.
    if not ids <= {r.id for r in contract.requirements}:
        raise ValueError('Delivery assignment references unknown sealed contract requirements')
    return [r for r in contract.requirements if r.id in ids]


def assignment_needs_report(state) -> bool:
    """Bounded work derives delivery duties from its assignment, not siblings."""
    assignment = state.get('research_assignment') or {}
    if assignment:
        names = [name for row in assignment.get('acceptance', []) for name in row.get('artifact_names', [])]
        if any(str(name).lower().endswith('.md') or name == 'final_report' for name in names):
            return True
        objective = '\n'.join(row.get('description', '') for row in assignment.get('acceptance', []))
        return needs_report(objective, names)
    return needs_report(state.get('current_user_request', ''), state.get('desired_deliverables', []))

def needs_report(objective: str, desired_deliverables=()) -> bool:
    """Respect explicit exclusions; otherwise require a substantial written synthesis."""
    text = objective.casefold()
    if re.search(r"(?:不需要|无需|不要|不用|不必).{0,12}(?:报告|report)|(?:no|without)\s+(?:a\s+)?report|do not.{0,20}report", text):
        return False
    if "final_report" in desired_deliverables:
        return True
    return bool(re.search(r"报告|\b(?:final|markdown|analysis|scientific|evidence|comparative)\s+report\b|\b(?:write|generate|produce|provide|deliver|create|prepare)\b[^.!?\n]{0,60}\breport\b|完整分析总结|综合(?:分析|评估)|全面(?:分析|评估)|comparative analysis|comprehensive (?:analysis|assessment)", text))



def assignment_needs_pdf(state) -> bool:
    """Recognize explicit PDF output duties, not mentions of PDF input data."""
    assignment = state.get('research_assignment') or {}
    if assignment:
        rows = assignment.get('acceptance', [])
        names = [name for row in rows for name in row.get('artifact_names', [])]
        text = '\n'.join(row.get('description', '') for row in rows).casefold()
    else:
        names = state.get('desired_deliverables') or []
        text = state.get('current_user_request', '').casefold()
    if any(str(name).lower().endswith('.pdf') for name in names):
        return True
    if re.search(r'(?:不要|无需|不需要|不用|不必).{0,12}pdf|(?:no|without)\s+(?:a\s+)?pdf|do not.{0,20}pdf', text):
        return False
    return bool(re.search(r'(?:生成|输出|提供|导出|交付|制作)[^。！？\n]{0,25}pdf|\b(?:write|generate|produce|provide|deliver|create|export|prepare)\s+(?:(?:a|an|the|one|final|analysis|scientific|complete|new|updated|summary|report|in|as)\s+){0,6}pdf\b', text))


def validate_written_delivery(state, artifacts, answer_text=None):
    """Check requested presentation form, never scientific sufficiency.

    A generic written report may be the version-bound answer proposal. Explicit
    PDF and named-file duties remain mandatory; finish() checks named outputs
    separately before independent review of the answer and source receipts.
    """
    from pathlib import Path
    suffixes={Path(path).suffix.lower() for path in artifacts}
    if assignment_needs_pdf(state) and '.pdf' not in suffixes:
        raise ValueError('The original task explicitly requires a PDF; text alone is incomplete.')
    if (assignment_needs_report(state) and not suffixes.intersection({'.md','.pdf'})
            and not (isinstance(answer_text,str) and answer_text.strip())):
        raise ValueError('The original task requires a written report; submit a report artifact or an evidence-grounded answer_text for independent review.')
