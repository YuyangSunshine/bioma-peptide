"""Local AlphaFold2 inference through an explicitly configured ColabFold executable."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Literal
from uuid import uuid4

from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from bioma.runtime.deadline import remaining, check


class LocalAlphaFoldInput(ToolInput):
    sequence: str = Field(min_length=16, max_length=1000, pattern='^[ACDEFGHIKLMNPQRSTVWY]+$')
    msa_mode: Literal['mmseqs2_uniref_env', 'single_sequence'] = 'mmseqs2_uniref_env'


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_prediction(result, arguments):
    """Validate file identity and residue coverage, not biological correctness."""
    try:
        if result['status'] != 'ok' or result['method'] != 'AlphaFold2 via ColabFold':
            return False
        if result['sequence_sha256'] != hashlib.sha256(arguments['sequence'].encode()).hexdigest():
            return False
        if result['msa_mode'] != arguments.get('msa_mode', 'mmseqs2_uniref_env'):
            return False
        if not result['files'] or any(_sha(p) != h for p, h in result['files'].items()):
            return False
        if result['structure_path'] not in result['files'] or result['scores_path'] not in result['files']:
            return False
        from Bio.PDB import PDBParser
        from Bio.SeqUtils import seq1
        residues = list(PDBParser(QUIET=True).get_structure('prediction', result['structure_path'])[0].get_residues())
        if ''.join(seq1(r.resname) for r in residues) != arguments['sequence']:
            return False
        scores = json.loads(Path(result['scores_path']).read_text())
        import math
        return (len(scores['plddt']) == len(residues) and
                all(math.isfinite(v) and 0 <= v <= 100 for v in scores['plddt']))
    except (OSError, KeyError, TypeError, ValueError):
        return False


from contextlib import contextmanager


@contextmanager
def _gpu_slot(device):
    """Serialize this tool across worktrees/processes on the configured device."""
    import fcntl
    import time
    from bioma.runtime.admission import check_execution
    key = hashlib.sha256(str(device).encode()).hexdigest()[:16]
    lock = Path('/tmp') / ('bioma-colabfold-gpu-' + key + '.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            check()
            check_execution()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.1)
        yield
    finally:
        os.close(descriptor)


@bioma_tool(name='run_local_alphafold2', category='prediction', version='1.0.0', author='BioMA',
            description='Run real local AlphaFold2 inference using configured ColabFold, one monomer model (model 1), 3 recycles, seed 0. Returns hash-bound PDB, confidence scores and actual output paths. Default MSA uses the public MMseqs2 server; single_sequence is an explicitly limited alternative. Does not retrieve AFDB structures or perform experimental validation.',
            args_schema=LocalAlphaFoldInput, result_kind='computed_prediction',
            result_validator=validate_prediction, requires_network=True,
            tags=['protein', 'structure', 'alphafold', 'local-inference'])
def run_local_alphafold2(sequence: str, msa_mode: str = 'mmseqs2_uniref_env'):
    args = LocalAlphaFoldInput(sequence=sequence, msa_mode=msa_mode)
    binary = os.environ.get('BIOMA_COLABFOLD_BIN') or shutil.which('colabfold_batch')
    data = os.environ.get('BIOMA_COLABFOLD_DATA')
    if not binary or not Path(binary).is_file() or not data:
        raise ValueError('Configure BIOMA_COLABFOLD_BIN and BIOMA_COLABFOLD_DATA with installed ColabFold and downloaded AlphaFold2 parameters')
    root = Path(__file__).resolve().parents[2] / 'artifacts' / 'local_alphafold'
    folder = root / uuid4().hex
    folder.mkdir(parents=True)
    fasta = folder / 'input.fasta'
    fasta.write_text('>query\n' + args.sequence + '\n')
    output = folder / 'prediction'
    command = [binary, str(fasta), str(output), '--data', data,
               '--model-type', 'alphafold2_ptm', '--num-models', '1', '--model-order', '1',
               '--num-recycle', '3', '--random-seed', '0', '--msa-mode', args.msa_mode]
    from bioma.backends.analysis.processes import analysis_environment
    env = analysis_environment()
    env['CUDA_VISIBLE_DEVICES'] = os.environ.get('BIOMA_COLABFOLD_GPU', '0')
    env['XLA_PYTHON_CLIENT_PREALLOCATE'] = 'false'
    budget = remaining()
    check()
    with _gpu_slot(env['CUDA_VISIBLE_DEVICES']):
        budget = remaining()
        with (folder / 'process.log').open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            try:
                code = process.wait(timeout=min(900, budget) if budget is not None else 900)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise TimeoutError(f'AlphaFold2 inference timed out; diagnostics: {folder / "process.log"}')
    check()
    if code:
        raise ValueError(f'AlphaFold2 inference failed ({code}); diagnostics: {folder / "process.log"}')
    structures = list(output.glob('*unrelaxed_rank_001*.pdb'))
    scores = list(output.glob('*scores_rank_001*.json'))
    if len(structures) != 1 or len(scores) != 1:
        raise ValueError('AlphaFold2 did not produce exactly one ranked structure and score file. '
                         'Actual process diagnostics: ' + (folder / 'process.log').read_text(errors='replace')[-4000:])
    result = {'status': 'ok', 'method': 'AlphaFold2 via ColabFold',
              'msa_mode': args.msa_mode, 'model': 'alphafold2_ptm model 1', 'seed': 0, 'recycles': 3,
              'command': command,
              'parameter_sha256': _sha(Path(data) / 'params' / 'params_model_1_ptm.npz'),
              'sequence_sha256': hashlib.sha256(sequence.encode()).hexdigest(),
              'structure_path': str(structures[0]), 'scores_path': str(scores[0]),
              'analysis_inputs': [str(structures[0]), str(scores[0])],
              'validation_scope': 'file identity, residue sequence and confidence range only; computed prediction, not experimental evidence',
              'files': {str(p): _sha(p) for p in folder.rglob('*') if p.is_file()}}
    if not validate_prediction(result, args.model_dump()):
        raise ValueError('AlphaFold2 output failed sequence/score/file validation')
    return result
