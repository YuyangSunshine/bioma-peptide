"""Read-only, state-bound Method prerequisite checks; no scientific scoring."""
from pathlib import Path
import hashlib
import importlib.util


def method_input_reasons(method, state, input_refs, resources=None):
    reasons=[]
    refs=set(input_refs)
    known_evidence={e.id for e in state.evidence}
    if set(method.required_evidence_refs) - (known_evidence & refs):
        reasons.append('Missing required evidence references')
    for environment in method.execution_environment:
        if environment == 'python':
            continue
        if environment.startswith('python_module:'):
            try: ready=importlib.util.find_spec(environment.split(':',1)[1]) is not None
            except (ImportError,ValueError,ModuleNotFoundError): ready=False
        else:
            ready=environment in (resources or ())
        if not ready: reasons.append('Execution environment unavailable: '+environment)
    if not method.required_input_kinds:
        return reasons
    matches=[]
    for row in state.observations:
        if row.id not in refs:
            continue
        value=row.value if isinstance(row.value,dict) else {}
        if not {row.kind,value.get('kind')} & set(method.required_input_kinds):
            continue
        if method.required_input_qualification and value.get('provenance',{}).get('external_import',{}).get('qualification_status') != method.required_input_qualification:
            continue
        if method.required_artifact_suffixes:
            for artifact in value.get('artifacts',[]):
                path=Path(str(artifact.get('uri','')))
                if path.suffix.casefold() not in method.required_artifact_suffixes:
                    continue
                digest=artifact.get('metadata',{}).get('sha256')
                try:
                    with path.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
                except OSError: actual=None
                if not digest or actual != digest:
                    reasons.append('Referenced input artifact is missing, inaccessible or changed')
                else: matches.append(row.id)
        else: matches.append(row.id)
    if 'user_supplied_input' in method.required_input_kinds:
        from bioma.core.source_inputs import verify_source_identities
        try:
            sources = verify_source_identities(state.inputs.get('source_inputs', []))
        except ValueError as exc:
            reasons.append(str(exc))
            sources = []
        matches.extend(s.id for s in sources if s.id in refs and
            (not method.required_artifact_suffixes or Path(s.uri).suffix.casefold() in method.required_artifact_suffixes))
    if len(matches)<method.min_input_count or (method.max_input_count is not None and len(matches)>method.max_input_count):
        reasons.append('Requires '+str(method.min_input_count)+'..'+str(method.max_input_count or 'many')+
                       ' referenced qualified inputs '+','.join(method.required_input_kinds)+
                       '; found '+str(len(matches)))
    return reasons
