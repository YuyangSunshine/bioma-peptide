import subprocess
import sys


def test_lightweight_native_import_preserves_real_package():
    script = """
import sys, bioma
from bioma.peptide import Broker
assert 'bioma.api' not in sys.modules
assert bioma.__file__.endswith('bioma/__init__.py')
assert callable(Broker)
from bioma import BioMA
from bioma.api import BioMA as DirectBioMA
assert BioMA is DirectBioMA
assert 'BioMA' in dir(bioma)
"""
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
