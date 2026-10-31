import shlex
import sys
from tools.our_tools.tbmb_bicyclic_docking import _run


def test_external_argument_is_literal_not_shell(tmp_path):
    marker = tmp_path / 'must-not-exist'
    payload = '$(touch ' + str(marker) + ')'
    command = shlex.join([sys.executable, '-c', 'import sys;print(sys.argv[1])', payload])
    log = tmp_path / 'command.log'
    _run(command, log)
    assert log.read_text().strip() == payload
    assert not marker.exists()
