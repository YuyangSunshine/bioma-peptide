"""Detached local supervisor and fixed scientific child entry; no shell API."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
from .broker import write_json, process_identity, job_identity
from .contracts import sha256, artifact_reference


def child(folder):
    request = json.loads((folder / 'request.json').read_text())
    for ref, item in request['inputs'].items():
        if sha256(folder / 'inputs' / ref) != item['sha256']:
            raise ValueError('input_hash_mismatch')
    profile = request['profile']
    import importlib.metadata as metadata
    versions = {}
    for package in ['rdkit', 'openmm', 'pydantic', 'PyYAML', 'numpy']:
        try: versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError: versions[package] = None
    write_json(folder / 'output' / 'runtime.json', {'python': sys.version, 'executable': sys.executable, 'packages': versions})
    if profile['method'] == 'engineering_canary':
        # Fixed synthetic workload, used to verify process lifecycle without a model.
        time.sleep(profile.get('delay_seconds', 0))
        write_json(folder / 'output' / 'canary.json', {
            'status': 'ok', 'input_hashes': {r: sha256(folder / 'inputs' / r) for r in request['inputs']},
            'scientific_validation_status': 'not_applicable'})
    else:
        from .domain import execute
        result = execute(folder, request)
        write_json(folder / 'output' / 'result.json', result)


def group_usage(pgid):
    ticks = os.sysconf('SC_CLK_TCK'); page = os.sysconf('SC_PAGE_SIZE')
    cpu, rss, processes = 0, 0, 0
    pids=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) != pgid:
                continue
            cpu += sum(int(fields[i]) for i in (11,12,13,14)) / ticks
            rss += int(fields[21]) * page
            processes += 1; pids.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            pass
    return {'cpu_seconds': cpu, 'rss_bytes': rss, 'processes': processes, 'pids': pids}


def supervise(folder):
    with (folder / '.worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        request = json.loads((folder / 'request.json').read_text())
        prior = json.loads((folder / 'state.json').read_text())
        if prior['status'] == 'completed':
            return
        budget = request['resources']; started = time.time()
        state = {'job_id': request['job_id'], 'status': 'running', 'attempt': prior['attempt'] + 1,
                 'supervisor_pid': os.getpid(), 'supervisor_start': process_identity(os.getpid()),
                 'started_at': started, 'resources': budget}
        write_json(folder / 'state.json', state)
        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (budget['cpu_seconds'], budget['cpu_seconds']))
            address_limit=request['profile'].get('gpu_address_space_bytes',budget['memory_bytes']) if request['profile'].get('gpu_ids') else budget['memory_bytes']
            resource.setrlimit(resource.RLIMIT_AS, (address_limit,address_limit))
            resource.setrlimit(resource.RLIMIT_FSIZE, (budget['output_bytes'], budget['output_bytes']))
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:budget['cpus']])
        gpu_ids=request['profile'].get('gpu_ids',[])
        if gpu_ids:
            # One cooperative project lease; explicit operator allocation remains required.
            gpu_lock=(folder.parent / ('.device-'+gpu_ids[0]+'.lock')).open('a')
            try: fcntl.flock(gpu_lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                write_json(folder/'state.json',dict(state,status='blocked',reason='gpu_lease_busy'))
                gpu_lock.close();return
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=','.join(gpu_ids), OMP_NUM_THREADS=str(budget['cpus']), OPENBLAS_NUM_THREADS=str(budget['cpus']))
        proc = None
        try:
            with (folder / 'child.log').open('ab') as log:
                interpreter = request['profile'].get('interpreter')
                python = sys.executable
                if interpreter:
                    python = interpreter['path']
                    if sha256(python) != interpreter['sha256']:
                        raise ValueError('worker_interpreter_identity_mismatch')
                proc = subprocess.Popen([python, '-m', 'bioma_peptide_bridge.worker', str(folder), '--child'],
                                        cwd=folder, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        env=env, start_new_session=True, preexec_fn=limits, shell=False)
                state.update(child_pid=proc.pid, child_start=process_identity(proc.pid))
                write_json(folder / 'state.json', state)
                deadline = time.monotonic() + budget['seconds']
                while proc.poll() is None:
                    if (folder / 'cancel.json').exists():
                        state.update(status='cancelled', reason='cancel_requested'); break
                    if time.monotonic() >= deadline:
                        state.update(status='timed_out', reason='wall_budget'); break
                    usage = group_usage(proc.pid)
                    state['group_usage_last'] = usage
                    if gpu_ids:
                        monitor=request['profile']['gpu_monitor']
                        if sha256(monitor['path'])!=monitor['sha256']:raise ValueError('gpu_monitor_changed')
                        check=subprocess.run([monitor['path'],'--id='+gpu_ids[0],'--query-compute-apps=pid,used_gpu_memory','--format=csv,noheader,nounits'],
                            capture_output=True,text=True,timeout=min(2,max(0.01,deadline-time.monotonic())),check=True,shell=False)
                        gpu_memory=0
                        for row in check.stdout.splitlines():
                            pid,mem=row.split(',')
                            if int(pid.strip()) in usage['pids']:gpu_memory+=int(mem.strip())
                        state['gpu_memory_mib_last']=gpu_memory
                        if gpu_memory>budget['gpu_memory_mib']:
                            state.update(status='failed',reason='gpu_memory_budget');break
                    if usage['cpu_seconds'] > budget['cpu_seconds'] or usage['rss_bytes'] > budget['memory_bytes'] or usage['processes'] > 64:
                        state.update(status='failed', reason='aggregate_resource_budget'); break
                    total = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
                    if total > budget['output_bytes']:
                        state.update(status='failed', reason='storage_budget'); break
                    time.sleep(0.1)
                if state['status'] != 'running':
                    os.killpg(proc.pid, signal.SIGTERM)
                    try: proc.wait(timeout=1)
                    except subprocess.TimeoutExpired: pass
                # Kill residual descendants even if their parent has already exited.
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait(timeout=5)
                if state['status'] == 'running':
                    state['status'] = 'completed' if proc.returncode == 0 else 'failed'
                state['returncode'] = proc.returncode
        except BaseException as exc:
            if proc:
                try: os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=5)
                except ProcessLookupError: pass
            state.update(status='failed', reason=type(exc).__name__ + ': ' + str(exc))
        refs = []
        try:
            for path in sorted((folder / 'output').rglob('*')):
                if path.is_symlink():
                    raise ValueError('symlink_output')
                if path.is_file():
                    refs.append(artifact_reference(folder, path.relative_to(folder), job_id=request['job_id'],
                        identity=job_identity(request)))
            write_json(folder / 'artifacts.json', refs)
        except (ValueError, OSError) as exc:
            state.update(status='failed', reason=str(exc))
        state.update(finished_at=time.time(), elapsed_seconds=time.time()-started,
                     usage=resource.getrusage(resource.RUSAGE_CHILDREN)[:])
        write_json(folder / 'state.json', state)
        write_json(folder / f'receipt-{state["attempt"]}.json', state)
        if gpu_ids:gpu_lock.close()


if __name__ == '__main__':
    folder = Path(sys.argv[1]).resolve(strict=True)
    child(folder) if '--child' in sys.argv else supervise(folder)
