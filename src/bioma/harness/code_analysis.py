"""Execute a self-contained analysis and verify its declared file delivery."""
from __future__ import annotations

import ast
from copy import deepcopy
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from uuid import uuid4
from typing import Literal

from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from bioma.backends.analysis.services.analysis_service import (
    _exec_python_code,
    _validate_code_action,
)
from bioma.backends.analysis.services.json_data import load_document
from bioma.harness.types import HarnessToolInput, ToolObservation
from bioma.core.reporting import assignment_needs_report
from bioma.runtime.deadline import check, remaining

FORMATS = {'.json', '.csv', '.tsv', '.txt', '.md', '.svg', '.png', '.pdf', '.py', '.r',
           '.pdb', '.cif', '.mmcif', '.html', '.htm', '.rds', '.rdata', '.zip', '.gz', '.h5', '.h5ad'}


def validate_structure_file(path):
    """Check parseable, finite coordinates; this is not biological validation."""
    from Bio.PDB import PDBParser, MMCIFParser
    import numpy as np
    try:
        parser = PDBParser(QUIET=True) if path.suffix.lower() == '.pdb' else MMCIFParser(QUIET=True)
        atoms = list(parser.get_structure('output', str(path)).get_atoms())
        if not atoms or not all(np.isfinite(atom.coord).all() for atom in atoms):
            raise ValueError('missing or nonfinite atom coordinates')
    except Exception as exc:
        raise ValueError(f'Invalid structure file: {path.name}') from exc


class DeclaredOutputMissing(ValueError):
    """A successful process did not satisfy its own artifact declaration."""
    def __init__(self, name, diagnostic):
        self.validation = {"stage": "declared_artifact_check", "execution_status": "succeeded",
                           "missing_output": str(name), "write_failure": "not_established"}
        super().__init__(f'Missing or empty declared output: {name}. '
            'Process exited successfully; declared artifact validation failed. '
            'This does not establish a filesystem write failure. '
            'Inspect the executed write path and conditional branches; an output declaration does not write a file.' + diagnostic)


def analysis_environment():
    from importlib import import_module
    from importlib.metadata import PackageNotFoundError, version
    import platform
    packages = {}
    modules = {'numpy': 'numpy', 'pandas': 'pandas', 'scipy': 'scipy',
               'matplotlib': 'matplotlib', 'biopython': 'Bio',
               'statsmodels': 'statsmodels', 'openpyxl': 'openpyxl'}
    for name, module_name in modules.items():
        try:
            module = import_module(module_name)
            packages[name] = str(getattr(module, '__version__'))
        except (ImportError, AttributeError):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = None
    return {'version': platform.python_version(), 'packages': packages}


class AnalysisReport(HarnessToolInput):
    introduction: list[str] = Field(default_factory=list, description='Background and analysis objective for the requested Introduction section; do not invent citations or prior results.')
    conclusions: list[str] = Field(default_factory=list, description='Deprecated compatibility field. It is not rendered because report conclusions must come from conclusion_text or conclusion in the successfully executed publish_result summary; do not prewrite expected findings.')
    title: str = Field(description='Neutral title naming the analysis in the requested language, not a predicted conclusion.')
    sources: dict[str, str] = Field(default_factory=dict, description='Section title to produced artifact filename. Reference declared non-Markdown outputs; the harness reads their actual data after execution. Computed findings from publish_result are included automatically.')
    methods: list[str] = Field(default_factory=list, description='Explain the method actually implemented, input interpretation and replicate unit in the requested language. No precomputed numeric findings; those come from publish_result and sources. These statements are checked by delivery review.')
    limitations: list[str] = Field(default_factory=list, description='Scientific limitations supported by the inputs and performed analysis, including simulated provenance or insufficient independent replicates when applicable.')

    @field_validator('sources', mode='before')
    @classmethod
    def _unwrap_single_source_lists(cls, value):
        if not isinstance(value, dict):
            return value
        return {
            key: item[0] if isinstance(item, list) and len(item) == 1 else item
            for key, item in value.items()
        }


def _report_mapping(value):
    # Some providers serialize a nested JSON object twice. Decode only this
    # declared mapping field, once, then retain all normal Pydantic validation.
    if isinstance(value, str):
        value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError('reports must encode a JSON object')
    return value


def _bind_report_outputs(value):
    """Keep a declared report layout and output declaration in lockstep."""
    if not isinstance(value, dict) or not isinstance(value.get('output_files'), list):
        return value
    reports = value.get('reports')
    if isinstance(reports, str):
        try:
            reports = json.loads(reports)
        except json.JSONDecodeError:
            return value
    if not isinstance(reports, dict):
        reports = {}
    normalized = dict(value)
    normalized['output_files'] = list(value['output_files'])
    normalized['output_files'].extend(
        name for name in [*reports, *(value.get('reuse_inputs') or {})] if name not in normalized['output_files'])
    return normalized


class ArtifactDisposition(HarnessToolInput):
    action: Literal[
        'scientific_recomputation',
        'artifact_regeneration',
        'byte_identical_reuse',
    ] = Field(description='Agent-selected revision action. Runtime validates the resulting file and provenance facts but does not select this action.')
    source_input_index: StrictInt | None = Field(default=None, description='Optional zero-based index in the full admitted artifact catalog naming the prior version of this output.')
    dependency_input_indices: list[StrictInt] = Field(default_factory=list, description='Zero-based indices in the full admitted artifact catalog whose identities this revision depends on. Runtime records their hashes; this declaration is not scientific qualification.')


class AnalysisCodeInput(HarnessToolInput):
    reuse_inputs: dict[str, StrictInt] = Field(default_factory=dict, description='Optional byte-identical reuse: map output filenames to zero-based indices in the full admitted artifact catalog. Trusted Runtime copies these files only after generated code exits, so copy-only sources are not program inputs. Copied files become declared outputs and may be report sources; this is not new computation or evidence.')
    artifact_dispositions: dict[str, ArtifactDisposition] = Field(default_factory=dict, description='Required for selective revision using reuse_inputs. Classify every declared output as scientific_recomputation, artifact_regeneration, or byte_identical_reuse and bind its prior source and admitted dependencies. Runtime verifies hashes and action/file consistency but never chooses the classification.')
    skill_refs: list[str] = Field(default_factory=list, description='Optional stable IDs of successfully read skills that informed this method choice. This declaration is recorded, not proof of scientific benefit.')
    input_indices: list[int] | None = Field(default=None, description='Select the exact zero-based entries from the full admitted artifact catalog that generated code may read. Omit to admit the full catalog. When reuse_inputs is present this field is required. A source may explicitly serve both roles, but copy-only sources stay inaccessible when their indices are omitted. INPUT_FILES preserves the selected order (Python index 0, R index 1).')
    engine: Literal['python', 'R'] = Field(default='python', description='Execution language. Use R when requested; never substitute Python for an explicit R requirement.')
    code: str = Field(description='Python or R calculations; choose engine explicitly. The only harness-provided Python bindings are INPUT_FILES (list[str]), OUTPUT_DIR (Path), REQUEST (str), Path, write_json(filename, data), and publish_result(summary); never define, import over, or assign these bindings. Harness tools such as inspect_inputs, inspect_tabular_schema, inspect_input_data, recommend_next_step, execute_analysis_code, and revise_analysis_code are not Python functions and must never appear in generated source; use their returned observations and read INPUT_FILES instead. Normal Python modules require explicit imports allowed by the existing code policy. Compute from actual inputs and reuse computed objects; derive all outputs and published findings from those computations, never prewrite anticipated answers. Use write_json(filename, data, source_missing=True) only for source tables/previews where NaN means a missing source cell (written as null). For computed findings write_json and publish_result reject non-finite numbers; represent undefined quantities as null with an explicit status/reason. Common NumPy scalars/arrays are supported without rounding. Use native plotting libraries for figures. Do not generate Markdown or report-formatting code: declare reports separately and the harness renders them from actual outputs. Use None for uncomputed or undefined quantities; empty containers mean a completed computation found no items. Preserve full numeric precision in machine-readable data unless explicitly requested otherwise. Intermediate exploration may omit publish_result and receives an execution receipt marked not_published; stdout remains unqualified diagnostics. File-only work may be explicitly submitted for review from its actual files and receipt without publish_result. Reports and work packages requiring computed summaries still require a dictionary of actual findings. No network or subprocess calls.')
    output_files: list[str] | None = Field(default=None, description='Optional exact relative output filenames. During continuous investigation, omission means receipt-only exploration; declare every file this step actually produces. The final work-package requirements are checked at finish, not inherited by each step. Outside continuous investigation omission binds the final work-package outputs. Requested reports and reproducibility scripts remain mandatory at final submission. An explicitly requested python script.py input.ext output_dir command is executed independently before acceptance and its JSON outputs must match this delivery. Scripts must read the actual input structure and recompute without harness globals or hardcoded answers. Do not list input files or internal harness files.')
    reports: dict[str, AnalysisReport] = Field(default_factory=dict, description='Layout for each declared .md output, keyed by its exact filename. Reports are rendered after computation; do not write them in code. Declare both report.md and report.pdf to export the paired PDF automatically with Unicode fonts and embedded image sources.')
    _bind_reports_to_outputs = model_validator(mode='before')(_bind_report_outputs)
    _decode_reports = field_validator('reports', mode='before')(_report_mapping)


def bind_contract_outputs(context, args):
    """Resolve an omitted declaration from the already sealed work package.

    This binds names only. The program must still create every non-report file,
    report layouts remain model-authored, and normal execution/hash/review
    checks retain authority.
    """
    normalized = deepcopy(args)
    if normalized.get('output_files') is None:
        deliverables = context.state.get('desired_deliverables') or []
        if not deliverables:
            raise ValueError('Declare output_files when no sealed work-package deliverables are available.')
        normalized['output_files'] = list(deliverables)
    return normalized


def resolve_analysis_inputs(context, args):
    """Return the immutable copy catalog and exact generated-code read view."""
    catalog = list(context.state.get('artifact_reuse_sources') or
                   context.state.get('raw_data_sources', []))
    indices = args.get('input_indices')
    if indices is not None:
        if any(type(index) is not int or index < 0 or index >= len(catalog)
               for index in indices):
            raise ValueError('Input selection must reference existing admitted input indices')
        if len(set(indices)) != len(indices):
            raise ValueError('Input selection must not contain duplicate indices')
        inputs = [catalog[index] for index in indices]
    else:
        inputs = list(catalog)
    if args.get('reuse_inputs') and indices is None:
        raise ValueError('input_indices is required with reuse_inputs to separate copy sources from program inputs')
    return catalog, inputs


class SourceEdit(HarnessToolInput):
    start_line: int = Field(ge=1, description='First line to replace in the numbered current draft (inclusive, 1-based).')
    end_line: int = Field(ge=1, description='Last line to replace (inclusive). All edits refer to the same current draft.')
    new_lines: list[str] = Field(description='Replacement lines, preserving Python indentation. Each item is one line without newline characters; empty list deletes the range.')


class AnalysisRevisionInput(HarnessToolInput):
    reuse_inputs: dict[str, StrictInt] | None = None
    artifact_dispositions: dict[str, ArtifactDisposition] | None = None
    edits: list[SourceEdit] = Field(default_factory=list, description='Non-overlapping ranges in the latest numbered draft. Preserve unrelated calculations.')
    output_files: list[str] | None = Field(default=None, description='Replace the output list only if its declaration needs correction; otherwise retain it.')
    reports: dict[str, AnalysisReport] | None = Field(default=None, description='Replace report layouts only if their declarations need correction; otherwise retain them.')
    _bind_reports_to_outputs = model_validator(mode='before')(_bind_report_outputs)
    _decode_reports = field_validator('reports', mode='before')(_report_mapping)


def retain_report_repair_draft(context, args, error):
    """Keep typed, unexecuted source when only the report layout is malformed.

    No source validation, receipt, output or scientific approval is implied.
    Revision must still pass the entire normal execution and review pipeline.
    """
    if not isinstance(error, ValidationError) or not error.errors():
        return False
    if any(not row['loc'] or row['loc'][0] != 'reports' for row in error.errors()):
        return False
    try:
        draft = AnalysisCodeInput.model_validate({**args, 'reports': {}})
    except ValidationError:
        return False
    context.analysis_draft = draft.model_dump()
    return True


def revise_analysis_code(context, args):
    if context.analysis_draft is None:
        raise ValueError('No analysis draft exists. Call execute_analysis_code first.')
    draft = deepcopy(context.analysis_draft)
    lines = draft['code'].splitlines()
    edits = sorted(args['edits'], key=lambda edit: edit['start_line'])
    previous_end = 0
    for edit in edits:
        if not previous_end < edit['start_line'] <= edit['end_line'] <= len(lines):
            raise ValueError('Edits must have non-overlapping, valid ranges in the current draft. No edits applied.')
        if any('\n' in line or '\r' in line for line in edit['new_lines']):
            raise ValueError('Each new_lines item must be one source line. No edits applied.')
        previous_end = edit['end_line']
    for edit in reversed(edits):
        lines[edit['start_line'] - 1:edit['end_line']] = edit['new_lines']
    if edits:
        draft['code'] = '\n'.join(lines) + '\n'
    for field in ('output_files', 'reports', 'reuse_inputs', 'artifact_dispositions'):
        if args.get(field) is not None:
            draft[field] = args[field]
    if draft == context.analysis_draft:
        raise ValueError('Revision makes no change to the analysis draft.')
    previous_draft = context.analysis_draft
    previous_stage = context.state.get('code_validation_stage')
    previous_complete = context.state.get('delivery_complete')
    try:
        observation = execute_analysis_code(context, draft)
    except Exception:
        # A malformed partial edit must not become the next repair baseline.
        context.analysis_draft = previous_draft
        if previous_stage is None:
            context.state.pop('code_validation_stage', None)
        else:
            context.state['code_validation_stage'] = previous_stage
        if previous_complete is None:
            context.state.pop('delivery_complete', None)
        else:
            context.state['delivery_complete'] = previous_complete
        raise
    observation.tool_name = 'revise_analysis_code'
    return observation


def _relative_output(name: str) -> Path:
    path = Path(name)
    windows = PureWindowsPath(name)
    if (not name or path.is_absolute() or windows.drive or '\\' in name
            or '..' in path.parts or path.suffix.lower() not in FORMATS):
        raise ValueError(f'Invalid output path or unsupported format: {name}')
    return path


def _declared_outputs(context, declared, reports):
    names = [_relative_output(name) for name in declared]
    required = {item for item in context.state.get('desired_deliverables', [])
                if isinstance(item, str) and Path(item).suffix}
    if (not names and not context.state.get('investigation_intermediate')) or not required <= set(declared):
        raise ValueError(f'Declare all requested files before execution; missing: {sorted(required - set(declared))}')
    if not context.state.get('investigation_intermediate') and assignment_needs_report(context.state) and not any(p.suffix.lower() in {'.md', '.pdf'} for p in names):
        raise ValueError('The request requires a report; include a Markdown or PDF report in output_files.')
    report_names = {str(name) for name in names if name.suffix.lower() == '.md'}
    if set(reports) != report_names:
        raise ValueError(f'reports must contain exactly the declared Markdown (.md) filenames: {sorted(report_names)}; '
                         f'received: {sorted(reports)}. Use a .md layout key and declare its paired .pdf for automatic export; '
                         'PDF filenames are not reports layout keys.')
    allowed_sources = set(declared) - report_names
    for report_name, report in reports.items():
        invalid = {title: source for title, source in report.sources.items() if source not in allowed_sources}
        if invalid:
            raise ValueError('Report sources must reference declared non-Markdown output files. '
                f'{report_name}: sources maps section titles TO output filenames, not filenames to descriptions. '
                f'Invalid entries: {json.dumps(invalid, ensure_ascii=False)}. '
                f'Allowed filenames: {sorted(allowed_sources)}. '
                'Use sources={} to rely on publish_result findings, including values read from authorized INPUT_FILES. '
                'To reuse saved computed tables or figures, set reuse_inputs={output_filename: input_index}; byte-identical copying is recorded and reviewed, not a new calculation. '
                'Repair only reports using revise_analysis_code; the program draft is retained.')
    return names


def _source_diagnostics(source, *, runtime_bindings=True):
    check()
    left = remaining()
    builtins = '["INPUT_FILES", "OUTPUT_DIR", "REQUEST", "publish_result", "Path", "write_json"]' if runtime_bindings else '[]'
    result = subprocess.run(
        [sys.executable, '-m', 'ruff', 'check', '--isolated', '--no-cache', '--no-fix',
         '--select', 'F821', '--output-format', 'json',
         '--target-version', f'py{sys.version_info.major}{sys.version_info.minor}',
         '--config', 'builtins = ' + builtins,
         '--stdin-filename', 'analysis.py', '-'],
        input=source, capture_output=True, check=False, encoding='utf-8', timeout=min(10, left) if left is not None else 10)
    check()
    if result.returncode not in {0, 1}:
        raise RuntimeError('Source diagnostics failed: ' + result.stderr)
    try:
        diagnostics = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Source diagnostics returned no valid JSON; verify the Ruff dependency. '
                           + result.stderr.strip()) from exc
    return [f"line {item['location']['row']}, column {item['location']['column']}: {item['code']} {item['message']}"
            for item in diagnostics]


def execute_analysis_code(context, args):
    args = bind_contract_outputs(context, args)
    context.analysis_draft = deepcopy(args)
    context.state.pop('delivery_complete', None)
    engine = args.get('engine', 'python')
    required = (context.state.get('execution_requirements') or {}).get('engine')
    if required and required.casefold() != engine.casefold():
        raise ValueError(f'Requested engine {required} cannot be substituted with {engine}.')
    if engine == 'R':
        from bioma.harness.r_analysis import execute_r_analysis
        return execute_r_analysis(context, args)
    diagnostics = _source_diagnostics(args['code'])
    try:
        tree = ast.parse(args['code'])
    except SyntaxError:
        if diagnostics:
            raise ValueError('Source validation failed:\n' + '\n'.join(diagnostics))
        raise
    context.state['code_validation_stage'] = max(context.state.get('code_validation_stage', 0), 1)
    replaced_bindings = set()
    binding_lines = set()
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            name = node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.split('.')[0]
        if name in {'INPUT_FILES', 'OUTPUT_DIR', 'REQUEST', 'publish_result', 'write_json'}:
            replaced_bindings.add(name)
            binding_lines.add(node.lineno)
    if replaced_bindings:
        diagnostics.append(f'{", ".join(sorted(replaced_bindings))} is provided by the harness. Delete the assignments, definitions or import aliases on source lines {sorted(binding_lines)}. Do not add replacement assignments; keep only reads or calls to these existing names.')
    if diagnostics:
        raise ValueError('Source validation failed:\n' + '\n'.join(diagnostics))
    reports = {name: AnalysisReport.model_validate(value) for name, value in args.get('reports', {}).items()}
    names = _declared_outputs(context, args['output_files'], reports)
    reuse_catalog, selected_inputs = resolve_analysis_inputs(context, args)
    inputs = [str(Path(p).resolve()) for p in selected_inputs]
    if not inputs or not all(Path(p).is_file() for p in inputs):
        raise ValueError('Analysis requires existing input files.')
    input_sha256 = {}
    for filename in inputs:
        digest = hashlib.sha256()
        with Path(filename).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        input_sha256[filename] = digest.hexdigest()
    folder = context.artifact_dir('code_analysis', uuid4().hex[:12]).resolve()
    output_dir = folder/'outputs'
    output_dir.mkdir()
    from bioma.harness.output_writer import (stage_reused_inputs, verify_reused_inputs,
        export_paired_report, build_artifact_dispositions)
    receipt_path = folder/'receipt.json'
    script = folder/'analysis.py'
    import bioma
    package_root = Path(bioma.__file__).resolve().parent.parent
    header = ('from pathlib import Path\n'
              'import sys\n'
              # The child changes cwd; relative PYTHONPATH must not select another installation.
              f'sys.path.insert(0, {str(package_root)!r})\n'
              f'INPUT_FILES = {inputs!r}\nOUTPUT_DIR = Path({str(output_dir)!r})\n'
              f'REQUEST = {context.state.get("current_user_request", "")!r}\n'
              'from functools import partial\n'
              'from bioma.harness.output_writer import write_json as _write_json\n'
              'from bioma.backends.analysis.services.json_data import scientific_json_dumps\n'
              'write_json = partial(_write_json, OUTPUT_DIR)\n')
    publisher = (
        'def publish_result(summary):\n'
        '    import json\n'
        '    from pathlib import Path\n'
        '    if not isinstance(summary, dict):\n'
        '        raise TypeError("publish_result expects a dictionary of computed findings.")\n'
        f'    path = Path({str(receipt_path)!r})\n'
        '    if path.exists():\n'
        '        raise ValueError("Publish the final result only once.")\n'
        '    payload = scientific_json_dumps({"summary": summary})\n'
        '    path.write_text(payload, encoding="utf-8")\n'
    )
    policy_error = _validate_code_action(args['code'])
    if policy_error:
        raise ValueError(f'CodeActionPolicyRejected: {policy_error}')
    context.state['code_validation_stage'] = max(context.state.get('code_validation_stage', 0), 2)
    script.write_text(args['code'], encoding='utf-8', newline='\n')
    driver = folder/'run.py'
    driver.write_text(header+publisher+'import runpy\n'+
        f'runpy.run_path({str(script)!r}, run_name="__main__", init_globals={{'
        '"Path": Path, "INPUT_FILES": INPUT_FILES, "OUTPUT_DIR": OUTPUT_DIR, '
        '"REQUEST": REQUEST, "publish_result": publish_result, "write_json": write_json})\n', encoding='utf-8', newline='\n')
    check()
    budget = remaining()
    from bioma.backends.analysis.processes import code_action_boundary
    with code_action_boundary(folder, inputs):
        result = _exec_python_code(str(driver), timeout=min(180, budget) if budget is not None else 180, cwd=output_dir,
            **({'log_dir': folder} if context.state.get('continuous_investigation') else {}))
    check()
    diagnostic = ''
    if context.state.get('continuous_investigation'):
        diagnostic = ('\nUntrusted diagnostic log, not a result or evidence: ' +
                      str(folder/'stdout.txt'))
    if not result['ok']:
        if context.state.get('continuous_investigation'):
            # Exception values and source excerpts can contain input records.
            # Keep them in local logs; never embed them in model feedback.
            if result.get('exit_code') is not None:
                # Record the observed process, independently of any program-written
                # summary. Failed outputs remain provisional, just as for R.
                process = {'engine':'python','exit_code':result['exit_code'],
                    'script_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),
                    'input_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in inputs},
                    'stdout':result['stdout'],'stderr':result['stderr']}
                receipt_path.write_text(json.dumps({'process':process}),encoding='utf-8')
                from bioma.harness.recovery import FailedAnalysisExecution
                raise FailedAnalysisExecution('Analysis script failed; local diagnostics: ' +
                    str(folder/'stderr.txt'), receipt_path, names)
            raise ValueError('Analysis did not produce an observed process exit; ' + result['stderr'])
        raise ValueError((result['stderr'] or 'Analysis script failed.') + diagnostic)
    if not receipt_path.is_file():
        if not context.state.get('investigation_intermediate') or reports:
            raise ValueError('Missing analysis result: call publish_result(summary). stdout is only a diagnostic log.' + diagnostic)
        # This receipt is issued only after a successful real process, never from
        # stdout. Absent findings stay absent and cannot satisfy final submission.
        receipt = {'execution_status': 'succeeded', 'summary_status': 'not_published', 'summary': None}
    else:
        receipt = load_document(receipt_path.read_bytes())
        if not isinstance(receipt, dict) or not isinstance(receipt.get('summary'), dict):
            raise ValueError('Invalid analysis result: summary must be a dictionary of computed findings.')
        receipt.update(execution_status='succeeded', summary_status='published')
    reused = stage_reused_inputs(context, args, output_dir, reuse_catalog)
    paths = []
    for name in names:
        path = (output_dir/name).resolve()
        path.relative_to(output_dir)
        if path.suffix.lower() == '.md':
            if path.exists():
                raise ValueError('Do not write Markdown in code; declare its layout in reports.')
            paths.append(str(path))
            continue
        if path.suffix.lower()=='.pdf' and str(name.with_suffix('.md')) in reports and not path.exists():
            paths.append(str(path))
            continue
        if not path.is_file() or not path.stat().st_size:
            raise DeclaredOutputMissing(name, diagnostic)
        if path.suffix.lower()=='.json':
            load_document(path.read_bytes())
        elif path.suffix.lower() in {'.csv','.tsv'}:
            with path.open(encoding='utf-8-sig', newline='') as handle:
                if not next(csv.reader(handle, delimiter='\t' if path.suffix.lower()=='.tsv' else ','), []):
                    raise ValueError(f'Empty table header: {name}')
        elif path.suffix.lower()=='.py':
            diagnostics = _source_diagnostics(path.read_text(encoding='utf-8-sig'), runtime_bindings=False)
            if diagnostics:
                raise ValueError(f'Invalid Python artifact {name}:\n' + '\n'.join(diagnostics))
        elif path.suffix.lower()=='.svg':
            import xml.etree.ElementTree as ET
            if ET.parse(path).getroot().tag.split('}')[-1] != 'svg':
                raise ValueError(f'Invalid SVG: {name}')
        elif path.suffix.lower()=='.png':
            from PIL import Image
            with Image.open(path) as img:
                img.verify()
        elif path.suffix.lower()=='.pdf':
            from pypdf import PdfReader
            if not PdfReader(path).pages:
                raise ValueError(f'Empty PDF: {name}')
        elif path.suffix.lower() in {'.pdb', '.cif', '.mmcif'}:
            validate_structure_file(path)
        paths.append(str(path))
    from bioma.harness.reproduction import verify_requested_reproduction
    receipt['reproduction_checks'] = ([] if context.state.get('investigation_intermediate') else
        verify_requested_reproduction(context.state.get('current_user_request', ''), inputs, names, output_dir, folder))
    from bioma.harness.output_writer import render_report
    runtime = analysis_environment()
    receipt['execution'] = {'exit_code': 0, 'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
                            'input_sha256': input_sha256}
    verify_reused_inputs(reused)
    receipt['provenance'] = {
        'reused_inputs':reused,
        'input_versions': [{'file': Path(name).name, 'sha256': digest}
                           for name, digest in input_sha256.items()],
        'execution_environment': runtime,
    }
    for name, report in reports.items():
        check()
        render_report(output_dir, name, title=report.title, summary=receipt['summary'], sources=report.sources,
                      provenance=receipt['provenance'], methods=report.methods, limitations=report.limitations, introduction=report.introduction, conclusions=report.conclusions)
        export_paired_report(output_dir, name, names)
    receipt['provenance']['artifact_dispositions'] = build_artifact_dispositions(
        args, source_catalog=reuse_catalog, output_dir=output_dir,
        output_paths=paths, reused_records=reused)
    receipt['reports'] = {name: report.model_dump() for name, report in reports.items()}
    from bioma.runtime.tool_actions import current_tool_action
    receipt['dispatch'] = current_tool_action()
    receipt['execution_arguments'] = {key:value for key,value in args.items() if key != 'code'}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    context.state['code_validation_stage'] = max(context.state.get('code_validation_stage', 0), 3)
    metadata = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
    report = next((p for p in paths if Path(p).suffix.lower()=='.md'), '')
    figures = [p for p in paths if Path(p).suffix.lower() in {'.png','.svg'}]
    return ToolObservation(tool_name='execute_analysis_code', status='ok',
        summary='Executed analysis and checked declared files; delivery consistency review is pending.', artifacts=paths,
        metrics={'summary':receipt['summary'], 'summary_status':receipt['summary_status'], 'sha256':metadata, 'input_sha256':input_sha256, 'runtime':runtime, 'receipt_path':str(receipt_path),
                 'execution_sha256':{str(receipt_path):hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                                     str(script):hashlib.sha256(script.read_bytes()).hexdigest()}},
        patch={'analysis_results':{'findings':([{'summary':receipt['summary']}] if receipt['summary_status']=='published' else []),
                                  'analysis_outputs':{'code_analysis':paths}},
               'analysis_code_paths':[str(script)],
               'execution_receipts':[str(receipt_path)],
               'visualizations':[{'path':p} for p in figures],
               'final_report':report, 'error_code':None, 'error_detail':None})
