"""Trusted local broker. Agent requests contain IDs, never commands or paths.

The registry and job root are controller-owned, outside the Agent sandbox.
A detached supervisor owns each process group, budget and cancellation receipt.
"""
from __future__ import annotations
from contextlib import contextmanager
import threading
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from .contracts import TOOLS, digest, sha256, confined, artifact_reference, verify_reference, normalize

ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted", "blocked"}


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x') as f:
        json.dump(value, f, indent=2, allow_nan=False); f.flush(); os.fsync(f.fileno())
    temp.replace(path)


def process_identity(pid):
    try:
        data = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if data[0] == 'Z':
            return None
        return data[19]
    except (OSError, IndexError):
        return None


def job_identity(request):
    return {**request['profile']['identity'], 'config': request['fingerprint'],
            'inputs': {ref: item['sha256'] for ref,item in request['inputs'].items()}}


class Broker:
    def __init__(self, root, registry):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry_path = Path(registry).resolve(strict=True)
        self.registry = json.loads(self.registry_path.read_text())
        self.db = self.root / 'jobs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, idem TEXT UNIQUE, fingerprint TEXT, created REAL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, job_id):
        if not ID.fullmatch(job_id):
            raise ValueError('invalid_job_id')
        with self.connect() as db:
            if not db.execute('SELECT id FROM jobs WHERE id=?', (job_id,)).fetchone():
                raise ValueError('unknown_job_id')
        return self.root / job_id

    def submit(self, *, profile, input_refs, idempotency_key, resources):
        if not ID.fullmatch(idempotency_key):
            raise ValueError('invalid_idempotency_key')
        p = self.registry['profiles'].get(profile)
        if not p:
            raise ValueError('unregistered_profile')
        if p.get('purpose') in {'production_md','md_canary'} or p.get('method') == 'tbmb_md_production':
            from .admission import production_gate
            production_gate(p,self.registry)
        if p.get('approved') is not True or not p.get('review_id'):
            raise ValueError('blocked_unreviewed_profile')
        if p.get('method') in {'prepare_receptor', 'detect_pockets', 'diffdock_screen', 'vina_refine'}:
            policy = p.get('task_profile', {})
            if (policy.get('ligand_charge') != 5 or policy.get('receptor_policy') != 'retain_selected_polymer_only'
                    or not policy.get('target_id') or not policy.get('target_domain') or not policy.get('retain_chains')):
                raise ValueError('blocked_profile_outside_legacy_docking_scope')
        if p['method'] not in {*TOOLS, 'engineering_canary', 'tbmb_md_production'}:
            raise ValueError('method_not_allowed')
        if sorted(input_refs) != sorted(p['inputs']) or len(set(input_refs)) != len(input_refs):
            raise ValueError('registered_inputs_mismatch')
        expected_resources = {'seconds', 'cpu_seconds', 'memory_bytes', 'output_bytes', 'cpus'}
        if p.get('gpu_ids'):
            expected_resources.add('gpu_memory_mib')
        if set(resources) != expected_resources:
            raise ValueError('explicit_resource_budget_required')
        for key, value in resources.items():
            if type(value) is not int or not 0 < value <= p['resource_caps'][key]:
                raise ValueError('resource_budget_outside_profile')
        if p.get('gpu_ids'):
            if p['method'] != 'tbmb_md_production' or len(p['gpu_ids']) != 1 or not re.fullmatch(r'GPU-[a-fA-F0-9-]+',p['gpu_ids'][0]):
                raise ValueError('blocked_gpu_profile_requires_single_registered_device')
            monitor=p.get('gpu_monitor',{})
            if (not Path(monitor.get('path','')).is_absolute() or sha256(monitor['path'])!=monitor.get('sha256')
                    or type(p.get('gpu_address_space_bytes')) is not int or p['gpu_address_space_bytes']<resources['memory_bytes']):
                raise ValueError('gpu_monitor_and_address_space_budget_required')
        interpreter = p.get('interpreter')
        if interpreter:
            binary = Path(interpreter['path'])
            if not binary.is_absolute() or sha256(binary) != interpreter['sha256']:
                raise ValueError('worker_interpreter_identity_mismatch')
        inputs = {}
        for ref in input_refs:
            item = self.registry['inputs'][ref]
            path = confined(self.registry['input_root'], item['path'])
            if sha256(path) != item['sha256']:
                raise ValueError('input_hash_mismatch')
            inputs[ref] = dict(item, source=str(path))
        request = {'profile_id': profile, 'profile': p, 'inputs': inputs,
                   'resources': resources, 'registry_hash': digest(self.registry)}
        fingerprint = digest(request)
        # Cross-process serialization covers database insert, directory and dispatch.
        with (self.root / '.submit.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.connect() as db:
                previous = db.execute('SELECT id,fingerprint FROM jobs WHERE idem=?', (idempotency_key,)).fetchone()
                if previous:
                    if previous[1] != fingerprint:
                        raise ValueError('idempotency_conflict')
                    return self.status(previous[0])
                job_id = 'job-' + uuid.uuid4().hex
                folder = self.root / job_id
                folder.mkdir(mode=0o700)
                (folder / 'inputs').mkdir(); (folder / 'output').mkdir()
                for ref, item in inputs.items():
                    if not ID.fullmatch(ref):
                        raise ValueError('invalid_input_reference')
                    dst = folder / 'inputs' / ref
                    shutil.copyfile(item['source'], dst)
                    if sha256(dst) != item['sha256']:
                        raise ValueError('input_changed_during_registration')
                    dst.chmod(0o400)
                request.update(job_id=job_id, fingerprint=fingerprint)
                write_json(folder / 'request.json', request)
                write_json(folder / 'state.json', {'job_id': job_id, 'status': 'queued', 'attempt': 0})
                db.execute('INSERT INTO jobs VALUES (?,?,?,?)', (job_id, idempotency_key, fingerprint, time.time()))
            self._dispatch(folder)
        return self.status(job_id)

    def _dispatch(self, folder):
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(folder),
               'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
               'PYTHONDONTWRITEBYTECODE': '1', 'CUDA_VISIBLE_DEVICES': ''}
        with (folder / 'supervisor.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'bioma.peptide.worker', str(folder)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                             start_new_session=True, close_fds=True)
            threading.Thread(target=proc.wait, daemon=True).start()

    def status(self, job_id):
        folder = self.directory(job_id)
        state = json.loads((folder / 'state.json').read_text())
        if state['status'] == 'running' and process_identity(state.get('supervisor_pid')) != state.get('supervisor_start'):
            # Never blindly signal a reused PID or claim a dead worker is running.
            state = dict(state, status='interrupted', reason='supervisor_lost; explicit_resume_required')
        return state

    def cancel(self, job_id):
        folder = self.directory(job_id)
        state = self.status(job_id)
        if state['status'] in TERMINAL:
            return dict(state, cancellation='already_terminal')
        write_json(folder / 'cancel.json', {'requested_at': time.time(), 'job_id': job_id})
        # A request is not a confirmed cancellation. The supervisor writes the receipt.
        return {'job_id': job_id, 'cancellation': 'requested', 'status': state['status']}

    def reattach(self, job_id):
        return self.status(job_id)

    def resume(self, job_id):
        folder = self.directory(job_id)
        with (self.root / '.submit.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.status(job_id)
            if state['status'] in {'running', 'completed'}:
                return state
            request = json.loads((folder / 'request.json').read_text())
            # Identity binds parameters, all registered input hashes, and profile review.
            current = self.registry['profiles'].get(request['profile_id'])
            if current != request['profile'] or digest(self.registry) != request['registry_hash']:
                raise ValueError('checkpoint_profile_mismatch')
            for ref, item in request['inputs'].items():
                if sha256(folder / 'inputs' / ref) != item['sha256']:
                    raise ValueError('checkpoint_input_mismatch')
            if state['status'] != 'queued' and request['profile']['method'] not in {'engineering_canary', 'optimize_conformer_ensemble_with_xtb', 'tbmb_md_production'}:
                raise ValueError('resume_not_supported_by_method')
            if state.get('child_pid') and process_identity(state['child_pid']) == state.get('child_start'):
                raise ValueError('orphan_process_requires_operator_reconciliation')
            if (folder / 'cancel.json').exists():
                (folder / 'cancel.json').rename(folder / f'cancel-attempt-{state["attempt"]}.json')
            write_json(folder / 'resume.json', {'job_id': job_id, 'requested_at': time.time(), 'previous': state})
            with (folder / '.worker.lock').open('a') as worker_lock:
                fcntl.flock(worker_lock, fcntl.LOCK_EX)
                write_json(folder / 'state.json', dict(state, status='queued'))
            self._dispatch(folder)
        return {'job_id': job_id, 'resume': 'requested'}

    def collect(self, job_id):
        folder = self.directory(job_id)
        state = self.status(job_id)
        request = json.loads((folder / 'request.json').read_text())
        refs = []
        if state['status'] in TERMINAL and (folder / 'artifacts.json').exists():
            refs = json.loads((folder / 'artifacts.json').read_text())
            for ref in refs:
                verify_reference(folder, ref, job_identity(request))
        raw_path = folder / 'output' / 'result.json'
        raw = {'status': 'ok' if state['status'] == 'completed' else state['status']}
        if raw_path.exists() and raw_path.stat().st_size <= 2 * 1024 * 1024:
            raw = json.loads(raw_path.read_text())
        validator = None
        if request['profile'].get('validator', {}).get('name') == 'graph/1.0':
            from .validators import graph_validator
            validator = graph_validator(request['profile']['validator'])
        identity = job_identity(request)
        result = normalize(raw, tool=request['profile']['method'], task_id=request['profile_id'],
                           job_id=job_id, identity=identity, method_version=request['profile'].get('method_version','1'),
                           root=folder, artifacts=refs, candidates=request['profile'].get('candidates', []),
                           validator=validator)
        result['job_status'] = state['status']
        if state['status'] != 'completed':
            result['execution_status'] = state['status']
            result['deliverable'] = False
        return result
