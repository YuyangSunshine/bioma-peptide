"""Keep scientific data serialization separate from report presentation."""
from __future__ import annotations

import csv
from html import escape
import json
from pathlib import Path

from bioma.backends.analysis.services.json_data import scientific_json_dumps


def _target(output_dir, filename, suffix):
    from bioma.harness.code_analysis import _relative_output
    name = _relative_output(filename)
    if name.suffix.lower() != suffix:
        raise ValueError(f'Expected a {suffix} artifact filename.')
    root = Path(output_dir).resolve()
    target = (root / name).resolve()
    target.relative_to(root)
    return target


def write_json(output_dir, filename, data, *, source_missing=False):
    target = _target(output_dir, filename, '.json')
    text = scientific_json_dumps(data, source_missing=source_missing, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + '\n', encoding='utf-8', newline='\n')
    return str(target)


def _cell(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        return format(value, '.6g')
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return escape(str(value)).replace('|', r'\|').replace('\n', ' ')


def _table(columns, rows):
    lines = ['| ' + ' | '.join(_cell(c) for c in columns) + ' |',
             '| ' + ' | '.join('---' for _ in columns) + ' |']
    for row in rows:
        if len(row) != len(columns):
            raise ValueError('Report table rows must match the header width.')
        lines.append('| ' + ' | '.join(_cell(v) for v in row) + ' |')
    return '\n'.join(lines)


def _markdown(value, depth=2):
    if isinstance(value, dict):
        return '\n\n'.join(
            f"{'#' * min(depth, 6)} {_cell(name)}\n\n{_markdown(item, depth + 1)}"
            if isinstance(item, (dict, list)) else f'- **{_cell(name)}**: {_cell(item)}'
            for name, item in value.items())
    if isinstance(value, list):
        if not value:
            return '[]'
        if all(isinstance(row, dict) for row in value):
            columns = list(dict.fromkeys(key for row in value for key in row))
            if columns:
                return _table(columns, ([row.get(c) for c in columns] for row in value))
        return '\n\n'.join(_markdown(item, depth) if isinstance(item, (dict, list))
                            else '- ' + _cell(item) for item in value)
    return _cell(value)


def render_report(output_dir, filename, *, title, summary, sources, provenance, methods=(), limitations=(), introduction=(), conclusions=()):
    """Render actual computed outputs after the analysis process has exited."""
    from bioma.harness.code_analysis import _relative_output
    from bioma.harness.delivery_review import _MAX_REVIEW_BYTES
    if not isinstance(summary, dict):
        raise ValueError('Reports require a dictionary of computed findings.')
    target = _target(output_dir, filename, '.md')
    root = Path(output_dir).resolve()
    raw_summary = scientific_json_dumps(summary)
    raw_provenance = json.dumps(provenance, ensure_ascii=False, allow_nan=False)
    size = len(raw_summary.encode('utf-8')) + len(raw_provenance.encode('utf-8'))
    size += len(json.dumps(list(introduction), ensure_ascii=False).encode('utf-8'))
    if size > _MAX_REVIEW_BYTES:
        raise ValueError('Report findings exceed the text review budget.')
    parts = [f'# {_cell(title)}']
    if introduction:
        parts.extend(['## Introduction', _markdown(list(introduction))])
    parts.extend(['## Results', _markdown(json.loads(raw_summary))])
    for label, notes in [('Methods', methods), ('Limitations', limitations)]:
        if notes:
            size += len(_encoded_notes := json.dumps(list(notes), ensure_ascii=False).encode('utf-8'))
            if size > _MAX_REVIEW_BYTES:
                raise ValueError('Report notes exceed the text review budget.')
            parts.extend(['## ' + label, _markdown(json.loads(_encoded_notes))])
    for label, source in sources.items():
        path = (root / _relative_output(source)).resolve()
        path.relative_to(root)
        parts.append(f'## {_cell(label)}')
        if path.suffix.lower() in {'.csv', '.tsv'} and path.stat().st_size > 30_000:
            from bioma.backends.analysis.services.review_material import project_material
            import hashlib
            material=project_material(path,hashlib.sha256(path.read_bytes()).hexdigest())
            preview={'full_table':source, 'sha256':material['sha256'],
                'rows':material['rows'],'columns':material['columns'],
                'preview_scope':'First 3 rows and first 8 columns only; full table is a separate output.'}
            rendered=_markdown(preview)+'\n\n'+_table(material['header'][:8],[r[:8] for r in material['first_rows']])
            size+=len(rendered.encode('utf-8'))
            if size>_MAX_REVIEW_BYTES:
                raise ValueError('Projected report sources exceed the text review budget')
            parts.append(rendered)
        elif path.suffix.lower() in {'.json', '.csv', '.tsv'}:
            size += path.stat().st_size
            if size > _MAX_REVIEW_BYTES:
                raise ValueError('Report sources exceed the text review budget; select concise computed sources.')
            if path.suffix.lower() == '.json':
                parts.append(_markdown(json.loads(path.read_text(encoding='utf-8-sig')), 3))
            else:
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    rows = csv.reader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
                    columns = next(rows, [])
                    parts.append(_table(columns, rows))
        elif path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.svg'}:
            parts.append(f'![{_cell(label)}]({source})')
        else:
            parts.append(_cell(source))
    computed_conclusion = summary.get('conclusion_text', summary.get('conclusion'))
    if computed_conclusion not in (None, '', [], {}):
        parts.extend(['## Conclusion', _markdown(computed_conclusion)])
    elif conclusions:
        parts.extend(['## Conclusion',
                      'No executed conclusion was published; pre-execution conclusion text was not used.'])
    if provenance:
        parts.extend(['## Provenance', _markdown(json.loads(raw_provenance), 3)])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n\n'.join(parts) + '\n', encoding='utf-8', newline='\n')
    return str(target)


def stage_reused_inputs(context, args, output_dir, source_catalog=None):
    """Stage trusted copies after code exits, outside its readable input view."""
    import shutil
    from bioma.harness.recovery import sha
    from bioma.harness.code_analysis import _relative_output
    selected=args.get('reuse_inputs') or {}
    files=list(source_catalog if source_catalog is not None else
               context.state.get('artifact_reuse_sources') or
               context.state.get('raw_data_sources',[]))
    records=[]
    for name,index in selected.items():
        if name not in args.get('output_files', []):
            raise ValueError('Reused files must be declared outputs')
        if type(index) is not int or not 0<=index<len(files):
            raise ValueError('reuse_inputs must refer to admitted input indices')
        relative=_relative_output(name)
        if relative.suffix.lower()=='.md':raise ValueError('Declare new Markdown layouts in reports instead of copying them')
        source=Path(files[index]);target=(Path(output_dir)/relative).resolve()
        target.relative_to(Path(output_dir).resolve())
        if target.exists():
            raise ValueError('Generated code must not create a byte-identical reuse target')
        target.parent.mkdir(parents=True,exist_ok=True)
        expected=sha(source);shutil.copyfile(source,target)
        if sha(target)!=expected:raise ValueError('Reused input changed during copy')
        records.append({'input_index':index,'source':str(source),'output':str(target),'sha256':expected,
                        'scope':'byte-identical reuse, not a new measurement or qualified result'})
    return records


def build_artifact_dispositions(args, *, source_catalog, output_dir, output_paths, reused_records):
    """Resolve Agent-selected revision scopes into hash-bound receipt facts."""
    from bioma.harness.recovery import sha
    declared = args.get('artifact_dispositions') or {}
    reused = args.get('reuse_inputs') or {}
    if reused and not declared:
        raise ValueError('artifact_dispositions is required when reuse_inputs is used')
    if not declared:
        return []
    output_root = Path(output_dir).resolve()
    outputs = {Path(path).resolve().relative_to(output_root).as_posix(): Path(path)
               for path in output_paths}
    expected = set(args.get('output_files') or [])
    if set(declared) != expected:
        raise ValueError('artifact_dispositions must classify every declared output exactly once')
    reuse_by_name = {Path(row['output']).resolve().relative_to(output_root).as_posix(): row
                     for row in reused_records}
    catalog = [Path(path) for path in source_catalog]
    readable = args.get('input_indices')
    readable_indices = (set(range(len(catalog))) if readable is None else set(readable))

    def identity(index):
        if type(index) is not int or not 0 <= index < len(catalog):
            raise ValueError('Artifact disposition references an invalid admitted catalog index')
        path = catalog[index]
        if not path.is_file():
            raise ValueError('Artifact disposition source is unavailable')
        return {'input_index': index, 'path': str(path), 'sha256': sha(path)}

    records=[]
    for name, raw in declared.items():
        value = raw.model_dump() if hasattr(raw, 'model_dump') else dict(raw)
        action=value['action'];source_index=value.get('source_input_index')
        dependencies=[]
        for index in value.get('dependency_input_indices') or []:
            item=identity(index)
            if item not in dependencies:dependencies.append(item)
        source=identity(source_index) if source_index is not None else None
        target=outputs.get(name)
        if target is None or not target.is_file():
            raise ValueError('Artifact disposition target is not a declared output')
        target_identity={'path':str(target),'sha256':sha(target)}
        reused_row=reuse_by_name.get(name)
        if action=='byte_identical_reuse':
            if name not in reused or source is None or reused[name]!=source_index or reused_row is None:
                raise ValueError('byte_identical_reuse must match reuse_inputs and its source index')
            if source['sha256']!=target_identity['sha256']:
                raise ValueError('byte_identical_reuse source and target hashes differ')
        elif name in reused or reused_row is not None:
            raise ValueError('Only byte_identical_reuse may name a staged reused output')
        elif source is not None and source['sha256']==target_identity['sha256']:
            raise ValueError('Regenerated or recomputed artifacts must create a new content hash')
        if action in {'artifact_regeneration','scientific_recomputation'} and not dependencies:
            raise ValueError(action+' requires at least one admitted dependency reference')
        if (action=='scientific_recomputation' and
                any(item['input_index'] not in readable_indices for item in dependencies)):
            raise ValueError('scientific_recomputation dependencies must be readable execution inputs')
        records.append({'artifact':name,'action':action,'source':source,
                        'target':target_identity,'dependencies':dependencies,
                        'program_readable_dependency_indices':sorted(
                            item['input_index'] for item in dependencies
                            if item['input_index'] in readable_indices),
                        'authority':'agent_selected_runtime_verified_file_facts_not_scientific_qualification'})
    return records


def verify_reused_inputs(records):
    from bioma.harness.recovery import sha
    for row in records:
        if sha(row['source'])!=row['sha256'] or sha(row['output'])!=row['sha256']:
            raise ValueError('Byte-identical reuse was changed; produce a separately named derived output')


def export_paired_report(output_dir, markdown_name, declared_names):
    pdf_name=Path(markdown_name).with_suffix('.pdf')
    if str(pdf_name) not in {str(p) for p in declared_names}:return
    pdf=(Path(output_dir)/pdf_name).resolve()
    pdf.relative_to(Path(output_dir).resolve())
    if pdf.exists():return  # Code-produced PDFs keep their own independent review.
    from bioma.backends.analysis.services.pdf_service import export_markdown_to_pdf
    ok,_,error=export_markdown_to_pdf(str(Path(output_dir)/markdown_name),str(pdf),project_root=str(output_dir))
    if not ok:raise ValueError('Paired PDF export failed: '+str(error))
