"""Execute explicitly requested two-argument Python deliveries before acceptance.

This checks reproducibility, not scientific correctness. The supported interface
is ``python script.py input.ext output_dir``; commands are never sent to a shell.
Only a script owned by this delivery is eligible. Other CLIs need typed adapters.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil

from bioma.backends.analysis.processes import run_python
from bioma.backends.analysis.services.analysis_service import _validate_code_action
from bioma.backends.analysis.services.json_data import load_document
from bioma.runtime.deadline import check, remaining


_COMMAND = re.compile(
    r'\bpython(?:3)?\s+([\w./-]+\.py)\s+([\w./-]+\.[A-Za-z0-9]+)\s+([A-Za-z_][\w./-]*)')


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _difference(expected, actual, path='$'):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return None if type(expected) is type(actual) and expected == actual else path
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return None if (math.isfinite(expected) and math.isfinite(actual)
                        and math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-12)) else path
    if type(expected) is not type(actual):
        return path
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return path + ' (keys)'
        return next((d for k in expected if (d := _difference(expected[k], actual[k], f'{path}.{k}'))), None)
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return path + ' (length)'
        return next((d for i, value in enumerate(expected)
                     if (d := _difference(value, actual[i], f'{path}.{i}'))), None)
    return None if expected == actual else path


def verify_requested_reproduction(request, inputs, names, output_dir, folder):
    """Fail closed on an owned explicit CLI; persist every attempted check.

    Files are copied into a fresh working directory with no harness globals or
    delivered results. This is an independent process, not an OS security sandbox.
    JSON output equality is checked; figures and narrative retain separate review.
    """
    names = {Path(name).as_posix() for name in names}
    commands = sorted(set(_COMMAND.findall(request)))
    commands = [command for command in commands if command[0] in names]
    records = []
    for script_name, input_name, _ in commands:
        record = {'script': script_name, 'passed': False, 'outputs': [],
                  'scope': 'standalone Python execution and delivered JSON equality'}
        records.append(record)
        try:
            check()
            source = (output_dir/script_name).resolve()
            source.relative_to(output_dir.resolve())
            original_inputs = [Path(p).resolve() for p in inputs]
            matching = [p for p in original_inputs if p.name == Path(input_name).name]
            # Content-addressed inputs may have a different basename. A sole
            # explicitly supplied input is unambiguous; never guess among many.
            if not matching and len(original_inputs) == 1:
                matching = original_inputs
            if len(matching) != 1:
                raise ValueError(f'Cannot bind reproduction input {input_name} unambiguously')
            input_path = matching[0]
            record.update(script_sha256=_hash(source), input_sha256=_hash(input_path))
            policy_error = _validate_code_action(source.read_text(encoding='utf-8-sig'))
            if policy_error:
                raise ValueError(f'CodeActionPolicyRejected: {policy_error}')
            json_names = sorted(name for name in names if Path(name).suffix.lower() == '.json')
            if not json_names:
                raise ValueError('Reproduction has no declared JSON output to compare; a typed output adapter is required')
            expected_hashes = {name: _hash(output_dir/name) for name in json_names}
            work = folder/'reproduction'/str(len(records))
            work.mkdir(parents=True)
            script_copy = work/Path(script_name).name
            input_copy = work/Path(input_name).name
            if script_copy == input_copy:
                raise ValueError('Reproduction script and input names collide')
            shutil.copyfile(source, script_copy)
            shutil.copyfile(input_path, input_copy)
            regenerated = work/'regenerated'
            regenerated.mkdir()
            left = remaining()
            timeout = min(60, left) if left is not None else 60
            record['timeout_seconds'] = timeout
            result = run_python(script_copy, arguments=(str(input_copy), str(regenerated)),
                                cwd=work, timeout=timeout)
            record.update(returncode=result.returncode, stdout=(result.stdout or '')[-4000:],
                          stderr=(result.stderr or '')[-4000:])
            check()
            if result.returncode:
                raise ValueError(record['stderr'] or f'Exit code {result.returncode}')
            if (_hash(source) != record['script_sha256'] or _hash(script_copy) != record['script_sha256']
                    or _hash(input_path) != record['input_sha256'] or _hash(input_copy) != record['input_sha256']):
                raise ValueError('Reproduction modified its script or input')
            for name in json_names:
                actual = (regenerated/name).resolve()
                actual.relative_to(regenerated.resolve())
                if not actual.is_file():
                    raise ValueError(f'Missing reproduced output: {name}')
                if _hash(output_dir/name) != expected_hashes[name]:
                    raise ValueError(f'Delivery changed during reproduction: {name}')
                difference = _difference(load_document((output_dir/name).read_bytes()), load_document(actual.read_bytes()))
                if difference:
                    raise ValueError(f'Reproduced output differs: {name}:{difference}')
                record['outputs'].append({'name': name, 'delivered_sha256': expected_hashes[name],
                                          'reproduced_sha256': _hash(actual)})
            record['passed'] = True
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise ValueError(f'Reproduction rejected {script_name}: {record["error"]}. '
                             'Repair the independent script and calculation consistently; do not prewrite expected answers.') from exc
        finally:
            (folder/'reproduction_checks.json').write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    return records
