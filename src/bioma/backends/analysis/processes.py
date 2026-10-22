"""Shared process boundary for generated analysis and plotting scripts."""
from __future__ import annotations

import os
import signal
from pathlib import Path
import subprocess
import sys


def _pidfd_open(pid):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    import platform
    if hasattr(libc, 'pidfd_open'):
        fd = libc.pidfd_open(pid, 0)
    elif platform.machine() in {'x86_64','aarch64'}:
        fd = libc.syscall(434, pid, 0)
    else:
        raise RuntimeError('HOST_EXECUTION_BOUNDARY_UNAVAILABLE: pidfd ABI unsupported')
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return fd


def _pidfd_kill(fd):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    import platform
    if hasattr(libc, 'pidfd_send_signal'):
        result = libc.pidfd_send_signal(fd, signal.SIGKILL, None, 0)
    elif platform.machine() in {'x86_64','aarch64'}:
        result = libc.syscall(424, fd, signal.SIGKILL, None, 0)
    else:
        raise RuntimeError('HOST_EXECUTION_BOUNDARY_UNAVAILABLE: pidfd ABI unsupported')
    if result < 0:
        code = ctypes.get_errno()
        if code != 3:
            raise OSError(code, os.strerror(code))


def analysis_environment() -> dict[str, str]:
    """Generated scientific code must not inherit provider credentials."""
    return {key: value for key, value in os.environ.items()
            if key != "BIOMA_ENV_FILE" and not any(marker in key.upper()
                for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}


def run_python(path: str | Path, *, timeout: float, cwd: str | Path | None = None,
               arguments: tuple[str, ...] = (), executable: str | None = None) -> subprocess.CompletedProcess[str]:
    # Hold the host identity lease until the entire namespace has stopped.
    from contextlib import nullcontext
    lease = sandbox_identity() if _analysis_boundary.get() else nullcontext(None)
    with lease as identity:
        return _run_python(path, timeout=timeout, cwd=cwd, arguments=arguments,
                           executable=executable, identity=identity)


def _run_python(path: str | Path, *, timeout: float, cwd: str | Path | None = None,
               arguments: tuple[str, ...] = (), executable: str | None = None, identity=None) -> subprocess.CompletedProcess[str]:
    executable = executable or sys.executable
    command = [executable, str(path), *arguments]
    boundary = _analysis_boundary.get()
    if boundary:
        workspace, inputs = boundary
        probe_fd = _pidfd_open(os.getpid())
        os.close(probe_fd)
        script = Path(path).resolve()
        if not script.is_relative_to(workspace):
            raise ValueError('Analysis script outside task boundary')
        if cwd is not None and not Path(cwd).resolve().is_relative_to(workspace):
            raise ValueError('Analysis cwd outside task boundary')
        # Workspace is dedicated to this run and must not contain trusted ledger/key files.
        command = ['setpriv','--pdeathsig','KILL','unshare','--mount','--pid','--fork',
            '--net','--mount-proc','--kill-child=KILL',sys.executable,__file__,
            '--sandbox-bootstrap',str(identity),str(workspace),__import__("json").dumps([str(p) for p in inputs] if isinstance(inputs, tuple) else [str(inputs)]),sys.prefix,
            executable,str(script),str(Path(cwd).resolve() if cwd else workspace),str(timeout),*arguments]
    # A timed-out analysis may have spawned workers which still hold stdout open.
    # Own a process group so ordinary descendants are terminated with the script.
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        encoding="utf-8",
        start_new_session=True,
        env={**analysis_environment(), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MPLBACKEND": "Agg"},
    ) as process:
        try:
            import time
            from bioma.runtime.deadline import remaining
            from bioma.runtime.admission import check_execution
            until = time.monotonic() + timeout
            while True:
                check_execution()  # Revocation/cancellation must stop active code too.
                left = until - time.monotonic()
                outer = remaining()
                if outer is not None:left = min(left, outer)
                if left <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(left, .2))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            pidfds = []
            if boundary:
                # Wait for namespace init as well as unshare, including reaping of
                # descendants that deliberately left the launcher's process group.
                children = Path(f'/proc/{process.pid}/task/{process.pid}/children')
                try:
                    for child in children.read_text().split():
                        try:pidfds.append(_pidfd_open(int(child)))
                        except ProcessLookupError:pass
                except FileNotFoundError:
                    pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if pidfds:
                import select
                try:
                    for fd in pidfds:
                        if not select.select([fd], [], [], 5)[0]:
                            _pidfd_kill(fd)
                            if not select.select([fd], [], [], 5)[0]:
                                raise RuntimeError('Namespace cleanup did not complete')
                finally:
                    for fd in pidfds:os.close(fd)
            raise
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


# Trusted runtime configuration; generated code cannot set this context.
from contextvars import ContextVar
from contextlib import contextmanager
_analysis_boundary = ContextVar('analysis_boundary', default=None)


def _subordinate_ids(filename):
    """Only the deployment administrator may reserve a host identity range."""
    import pwd
    owner = pwd.getpwuid(os.geteuid()).pw_name
    result = set()
    for line in Path(filename).read_text().splitlines():
        fields = line.split(':')
        if len(fields) == 3 and fields[0] in {owner, str(os.geteuid())}:
            start, count = map(int, fields[1:])
            result.update(range(start, start + count))
    return result


@contextmanager
def sandbox_identity():
    """Lease a reserved host UID/GID; never share RLIMIT_NPROC with another run.

    flock handles concurrent launchers. Scanning /proc prevents recycling after
    launcher death while its namespace is still terminating. No user credentials
    or identity configuration are accepted from generated code.
    """
    import fcntl
    import pwd
    import grp
    import stat
    if os.geteuid() != 0:
        raise PermissionError('Sandbox bootstrap requires the privileged Runtime')
    reserved = _subordinate_ids('/etc/subuid') & _subordinate_ids('/etc/subgid')
    reserved -= {entry.pw_uid for entry in pwd.getpwall()}
    reserved -= {entry.gr_gid for entry in grp.getgrall()}
    if not reserved:
        raise RuntimeError('SANDBOX_IDENTITY_UNAVAILABLE: provision matching subordinate UID/GID ranges for the Runtime user')
    directory = Path('/run/bioma-sandbox-identities')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise PermissionError('Untrusted sandbox identity lease directory')
    for identity in sorted(reserved):
        fd = os.open(directory / str(identity), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            occupied = False
            for status in Path('/proc').glob('[0-9]*/status'):
                try:
                    fields = status.read_text().split('Uid:', 1)[1].splitlines()[0].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if str(identity) in fields:
                    occupied = True
                    break
            if occupied:
                continue
            yield identity
            return
        finally:
            os.close(fd)
    raise RuntimeError('SANDBOX_IDENTITY_UNAVAILABLE: reserved identities exhausted')


@contextmanager
def analysis_boundary(workspace: Path, inputs: Path):
    workspace, inputs = workspace.resolve(), inputs.resolve()
    if not inputs.is_relative_to(workspace):
        raise ValueError('Inputs must be inside the isolated workspace')
    token = _analysis_boundary.set((workspace, inputs))
    try:
        yield
    finally:
        _analysis_boundary.reset(token)



@contextmanager
def code_action_boundary(workspace, files):
    """Reuse the production namespace boundary with exact read-only input mounts."""
    workspace = Path(workspace).resolve()
    admitted = tuple(Path(p).resolve() for p in files)
    if not all(p.is_file() for p in admitted):
        raise ValueError('Code action inputs must be existing regular files')
    token = _analysis_boundary.set((workspace, admitted))
    try:
        yield
    finally:
        _analysis_boundary.reset(token)


def check_analysis_paths(arguments):
    boundary = _analysis_boundary.get()
    if not boundary:
        return
    workspace, _ = boundary
    def visit(value, key=''):
        if isinstance(value, dict):
            for k,v in value.items():visit(v,k)
        elif isinstance(value, (tuple,list)):
            for v in value:visit(v,key)
        elif isinstance(value, (str,Path)):
            text = str(value)
            if text.startswith('/') or any(x in key.lower() for x in ('path','file','directory')):
                candidate = Path(text)
                candidate = (candidate if candidate.is_absolute() else workspace/candidate).resolve()
                if not candidate.is_relative_to(workspace):
                    raise PermissionError('Tool file argument outside task boundary')
    visit(arguments)


def _sandbox_bootstrap(identity, workspace, inputs, prefix, executable, script, working_directory, timeout, *arguments):
    """Linux mount/PID/net namespace setup, followed by capability-free exec."""
    identity = int(identity)
    import tempfile
    import resource
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
    import json
    workspace, inputs = Path(workspace), [Path(p) for p in json.loads(inputs)]
    root = Path(tempfile.mkdtemp(prefix='bioma-boundary-'))
    def run(*cmd):
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    run('mount', '--make-rprivate', '/')
    run('mount', '-t', 'tmpfs', '-o', 'size=16m', 'tmpfs', str(root))
    def bind(source, target=None, readonly=True):
        source = Path(source)
        target = root / str(target or source).lstrip('/')
        if source.is_dir():target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True);target.touch()
        run('mount', '--bind', str(source), str(target))
        if readonly:run('mount', '-o', 'remount,bind,ro,nosuid', str(target))
    for source in ['/usr','/lib','/lib64','/bin','/opt/conda/lib','/opt/conda/bin','/etc/R','/etc/fonts','/etc/alternatives',prefix]:
        if Path(source).exists():bind(source)
    # Production generated scripts import the shipped serialization helpers.
    # Mount only package source, never repository data, tests, ledgers or .env.
    package_source = Path(__file__).resolve().parents[3]
    bind(package_source / 'bioma', '/opt/bioma-runtime/bioma')
    bind('/proc') # Already private to this PID namespace, never host /proc.
    for source in ['/dev/null','/dev/zero','/dev/urandom','/dev/random']:
        bind(source, readonly=False)
    for item in workspace.rglob('*'):
        if item.is_symlink() or any(item == p or item.is_relative_to(p) for p in inputs):
            continue
        os.chown(item, identity, identity)
    os.chown(workspace, identity, identity)
    os.chmod(workspace, 0o700)
    temporary = workspace / '.sandbox-tmp'
    temporary.mkdir(exist_ok=True)
    os.chown(temporary, identity, identity)
    os.chmod(temporary, 0o700)
    bind(temporary, '/tmp', readonly=False)
    bind(workspace, readonly=False)
    for path in inputs:
        bind(path)
    run('mount','-o','remount,ro',str(root))
    os.chroot(root)
    os.chdir(working_directory)
    # Drop all capabilities and set no_new_privs before executing user code.
    resource.setrlimit(resource.RLIMIT_FSIZE,(256*1024**2,256*1024**2))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NOFILE,(256,256))
    resource.setrlimit(resource.RLIMIT_NPROC,(128,128))
    resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    cpu_limit = max(1, min(1800, int(float(timeout)) + 1))
    resource.setrlimit(resource.RLIMIT_CPU,(cpu_limit,cpu_limit))
    os.environ.clear()
    # The runtime prefix is already mounted read-only. Resolve ordinary shell
    # Python commands against the same installation as scientific execution,
    # without importing the host PATH or its credentials/environment.
    os.environ.update(PATH=f'{prefix}/bin:/usr/bin:/bin',HOME=str(workspace),TMPDIR='/tmp',
        PYTHONIOENCODING='utf-8',PYTHONUTF8='1',MPLBACKEND='Agg',
        LANG='C.UTF-8',LC_ALL='C.UTF-8',
        OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',
        PYTHONPATH='/opt/bioma-runtime',PYTHONDONTWRITEBYTECODE='1')
    os.execv('/usr/bin/setpriv',['setpriv',f'--reuid={identity}',f'--regid={identity}',
        '--clear-groups','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all',
        '--no-new-privs','--pdeathsig','KILL',executable,
        *(['--vanilla'] if Path(executable).name == 'Rscript' else []), script,*arguments])


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--sandbox-bootstrap':
        _sandbox_bootstrap(*sys.argv[2:])
    else:
        raise SystemExit('Trusted sandbox bootstrap only')
