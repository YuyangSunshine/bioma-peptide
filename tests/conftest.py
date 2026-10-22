"""Opt-in bounded local regression diagnostics; timeouts fail, never skip."""
import faulthandler
import signal
import pytest


def pytest_addoption(parser):
    parser.addoption('--bioma-test-timeout', type=float, default=0,
        help='Fail each test after this many seconds and dump all thread stacks (Linux).')


@pytest.fixture(autouse=True)
def bounded_regression(request):
    seconds = request.config.getoption('--bioma-test-timeout')
    if seconds <= 0:
        yield
        return
    old = signal.getsignal(signal.SIGALRM)
    prior = signal.getitimer(signal.ITIMER_REAL)
    def expired(signum, frame):
        faulthandler.dump_traceback(all_threads=True)
        # Runtime deliberately handles operational TimeoutError as recoverable
        # work feedback. A test watchdog must not enter that recovery path and
        # silently lose its one-shot timer. pytest's failure outcome derives
        # from BaseException, outside ordinary application error handling.
        pytest.fail('Bounded regression timeout: '+request.node.nodeid, pytrace=False)
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,*prior)
        signal.signal(signal.SIGALRM,old)
