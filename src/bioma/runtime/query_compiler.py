"""Bind a verified intake to the existing agent execution service, never a method plan."""
from __future__ import annotations
import re
from pathlib import PurePath
from bioma.core.models import ControlRecord


class QueryContractCompiler:
    @staticmethod
    def validate_intake(task, spec):
        """Recheck the intake binding at Runtime admission, including outputs."""
        from bioma.core.query_contract import QueryInterpretationError
        from bioma.core.scientific_capabilities import CapabilityRequirement
        allowed = {'intake_version', 'query_spec', 'provider_requirement',
                   'query_control_record', 'desired_deliverables'}
        if (task.gateway.get('intake_version') != 'research-contract-intake-v1'
                or set(task.gateway) != allowed
                or task.initial_capability != 'scientific.compute'
                or task.initial_method_id != 'continuous_scientific_investigation'
                or task.initial_message_type != 'task.submitted'):
            raise QueryInterpretationError('Intake cannot inject a method plan or scientific authority')
        expected = _deliverable_names(spec.explicit_deliverables,
            source_text=task.objective, input_names=_input_filenames(task.inputs))
        if task.gateway['desired_deliverables'] != expected:
            raise QueryInterpretationError('Intake deliverables differ from the user contract')
        expected_requirement = CapabilityRequirement(
            capability_family='scientific.compute', objective=task.objective,
            constraints={'method_id':'continuous_scientific_investigation',
                         'message_type':'task.submitted'}).model_dump(mode='json')
        if task.gateway['provider_requirement'] != expected_requirement:
            raise QueryInterpretationError('Intake execution binding cannot add method or resource authority')

    def compile_task(self, task, spec, capabilities):
        from bioma.core.query_contract import verify_query_spec, QueryInterpretationError
        from bioma.core.research import fingerprint
        from bioma.core.scientific_capabilities import COMPUTE
        spec = verify_query_spec(task.objective, spec.model_dump(mode='json'))
        # This is an execution host binding, not a scientific method selection.
        # Input type, scientific keywords and proposed missing data never alter it.
        host = 'continuous_scientific_investigation'
        installed = any(c.enabled and c.family == COMPUTE and any(
            m.id == host and m.available and m.entrypoint for m in c.methods)
            for c in capabilities)
        if not installed:
            raise QueryInterpretationError('Scientific agent execution service is unavailable: ' + host)
        requirement = self.compile_requirement(capability_family=COMPUTE,
            objective=task.objective, constraints={'method_id':host, 'message_type':'task.submitted'})
        control = ControlRecord(kind='query_spec', subject_hash=fingerprint(spec.model_dump(mode='json')),
                                source='query_contract_compiler')
        compiled = task.model_copy(update={'initial_capability':COMPUTE,
            'initial_method_id':host, 'initial_message_type':'task.submitted',
            'intent':'capability_task', 'gateway':{
                'intake_version':'research-contract-intake-v1',
                'query_spec':spec.model_dump(mode='json'),
                'provider_requirement':requirement.model_dump(mode='json'),
                'query_control_record':control.model_dump(mode='json'),
                'desired_deliverables':_deliverable_names(spec.explicit_deliverables,
                    source_text=task.objective, input_names=_input_filenames(task.inputs))}})
        self.validate_intake(compiled, spec)
        return compiled

    def compile_requirement(self, *, capability_family, objective, input_refs=(), constraints=None):
        from bioma.core.scientific_capabilities import (
            CapabilityRequirement,
            canonical_family,
        )
        constraints = dict(constraints or {})
        return CapabilityRequirement(
            capability_family=canonical_family(capability_family, constraints.get('method_id')),
            objective=objective, input_refs=list(input_refs), constraints=constraints)



def _input_filenames(inputs):
    """Known user-source identities, used only to prevent invented obligations."""
    from pathlib import PurePosixPath
    paths = [row.get('uri','') for row in inputs.get('source_inputs',[]) if isinstance(row,dict)]
    paths += [p for p in inputs.get('files',[]) if isinstance(p,str)]
    return {PurePosixPath(p).name for p in paths if p}


def _deliverable_names(deliverables, *, source_text=None, input_names=()):
    """Compile source-bound output descriptions, preserving relative identity.

    This is a bounded filename grammar, not a scientific intent classifier.
    Exclusions, optional clauses and input operands do not create mandatory
    outputs. An unsafe or unresolved explicit filename fails closed.
    """
    from pathlib import PurePosixPath, PureWindowsPath
    from bioma.core.query_contract import QueryInterpretationError
    names, explicit_names, inferred_names = [], set(), set()
    filename = re.compile(r"`([^`]+\.[A-Za-z0-9]+)`|\"([^\"\n]+\.[A-Za-z0-9]+)\"|'([^'\n]+\.[A-Za-z0-9]+)'|(?<![\w./\\])((?:[A-Za-z]:)?(?:\.{1,2}[/\\]|[/\\])?(?:[\w.-]+[/\\])*[\w.-]*[^\W\d][\w.-]*\.[A-Za-z][A-Za-z0-9]*)(?![\w/])")
    optional = re.compile(r"\b(?:optional(?:ly)?|if available|if appropriate|not required|need not)\b|可选|如可用|酌情", re.I)
    excluded = re.compile(r"\b(?:do not|don't|must not|never)\s+(?:\w+\s+){0,2}(?:write|save|create|generate|output|deliver|produce|return)|不要|不得|无需", re.I)
    actions = re.compile(r"\b(read|load|from|using|input|analyze|analyse|inspect|attached|save|write|create|generate|deliver|output|produce|return|as|named)\b|读取|输入|保存|输出|写入|生成|交付", re.I)
    def clauses(value):
        # Chinese sentence punctuation is not part of a bare filename. Separate
        # verbs from adjacent ASCII operands without changing quoted identities.
        pieces = re.split(r'''(`[^`]*`|"[^"\n]*"|'[^'\n]*')''', str(value))
        for i in range(0, len(pieces), 2):
            pieces[i] = re.sub(r'(读取|输入|保存|输出|写入|生成|交付)(?=[A-Za-z_])', r'\1 ', pieces[i])
            pieces[i] = re.sub(r'(\.[A-Za-z][A-Za-z0-9]*)(?:以及|和|与|及)(?=[A-Za-z_])', r'\1、', pieces[i])
        return re.split(r"[;；。！？\n]|(?<=[.!?])\s+|\bbut\b", ''.join(pieces))
    descriptions = list(deliverables)
    if source_text is not None:
        # Preserve literal output operands in the original request even when
        # semantic extraction omitted them. Only explicit output clauses qualify;
        # attached input paths or arbitrary scientific prose confer no obligation.
        for clause in clauses(source_text):
            if re.search(
                    r"\b(?:save|write|create|generate|deliver|output|produce|return)\b|保存|输出|写入|生成|交付", clause, re.I):
                descriptions.append(clause)
    for value in descriptions:
        # Sentence boundaries require whitespace; dots inside filenames survive.
        for text in clauses(str(value).strip()):
            lowered = text.casefold()
            candidates = list(filename.finditer(text))
            if not candidates and (optional.search(text) or excluded.search(text)):
                continue
            if (re.search(r'pdf', text, re.I)
                    and re.search(r'\b(?:report|deliver|save|write|create|generate|output|produce|return)\b|报告|保存|输出|写入|生成|交付', text, re.I)
                    and not optional.search(text)
                    and not excluded.search(text) and not any(
                        next(v for v in m.groups() if v is not None).lower().endswith('.pdf')
                        for m in candidates)):
                names.append('report.pdf')
                inferred_names.add('report.pdf')
            for index, match in enumerate(candidates):
                prefix = text[:match.start()]
                # A new action resets modifier scope (e.g. read x.csv, save y.csv).
                verbs = list(actions.finditer(prefix))
                verb = verbs[-1] if verbs else None
                local_start = candidates[index-1].end() if index else 0
                modifier_start = verb.start() if index and verb and verb.start() >= local_start else 0
                local = text[modifier_start:match.start()]
                tail = text[match.end():candidates[index+1].start() if index+1<len(candidates) else len(text)]
                if excluded.search(prefix) or optional.search(local) or re.match(r"\s*(?:\(\s*optional\s*\)|is optional|is not required)",tail,re.I):
                    continue
                if verb and verb.group().casefold() in {'read','load','from','using','input','analyze','analyse','inspect','attached','读取','输入'}:
                    continue
                candidate = next(value for value in match.groups() if value is not None)
                # The historical /app convention is an explicit workspace root,
                # not authority to flatten arbitrary host paths or subdirectories.
                if candidate.startswith('/app/'):
                    candidate = candidate[len('/app/'):]
                path = PurePosixPath(candidate)
                if path.is_absolute() or '..' in path.parts or '\\' in candidate or PureWindowsPath(candidate).drive:
                    raise QueryInterpretationError('Unsafe output path in explicit deliverable: ' + candidate)
                # Detect a rejected prefix instead of accidentally admitting its basename.
                before = text[:match.start()]
                if before.endswith(('..', '\\', ':')):
                    raise QueryInterpretationError('Unresolved output path in explicit deliverable')
                name = path.as_posix()
                names.append(name); explicit_names.add(name)
            if candidates:
                continue
            if not re.search(r"\bwithout (?:a )?filename\b", text, re.I) and re.search(r"\b(?:filename|file named|file called|save as)\b|文件名", text, re.I):
                raise QueryInterpretationError('Explicit output filename could not be resolved: ' + text)
            if not candidates:
                if 'markdown' in lowered and 'report' in lowered:
                    names.append('report.md')
                if ('png' in lowered or 'svg' in lowered or 'plot' in lowered or 'figure' in lowered) and (
                        'plot' in lowered or 'figure' in lowered):
                    names.append('analysis_plot.png')
                    inferred_names.add('analysis_plot.png')
                if ('csv' in lowered or 'json' in lowered) and any(
                        term in lowered for term in ('table', 'result', 'comparison', 'statistic')):
                    names.append('analysis_results.json' if 'json' in lowered and 'csv' not in lowered else 'comparison.csv')
                if 'execution trace' in lowered:
                    names.append('execution_trace.json')
    if source_text is not None:
        scoped = [clause for clause in clauses(source_text)
                  if filename.search(clause) and actions.search(clause)]
        grounded_required = set(_deliverable_names(scoped))
        mentioned = set()
        for clause in scoped:
            for match in filename.finditer(clause):
                candidate = next(v for v in match.groups() if v is not None)
                if candidate.startswith('/app/'):
                    candidate = candidate[len('/app/'):]
                mentioned.add(PurePosixPath(candidate).as_posix())
        # Source exclusions, optionality and input roles cannot be upgraded by
        # a lossy semantic paraphrase into mandatory output files.
        names = [name for name in names if name not in mentioned or name in grounded_required]
        explicit_names.intersection_update(names)
    if input_names:
        # An attached file is not an output merely because a semantic extraction
        # lists it. It needs an explicit output operand in the original request.
        output_clauses = [clause for clause in clauses(source_text or '') if re.search(
            r"\b(?:save|write|create|generate|deliver|output|produce|return)\b|保存|输出|写入|生成|交付",clause,re.I)]
        grounded_outputs = set(_deliverable_names(output_clauses))
        input_basenames = {PurePosixPath(str(name)).name for name in input_names}
        names = [name for name in names if PurePosixPath(name).name not in input_basenames
                 or name in grounded_outputs]
        explicit_names.intersection_update(names)
    # A generic plot and its explicit filename may describe the same obligation.
    if any(PurePath(name).suffix.casefold() in {'.png', '.svg'} for name in explicit_names):
        names = [name for name in names
                 if name != 'analysis_plot.png' or name in explicit_names or name not in inferred_names]
    if any(PurePath(name).suffix.casefold() == '.pdf' for name in explicit_names):
        names = [name for name in names if name != 'report.pdf'
                 or name in explicit_names or name not in inferred_names]
    return list(dict.fromkeys(names))
