"""Run with the existing scientific Python and PEPTIDE_SOURCE_ROOT explicitly set."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.environ.get('PEPTIDE_SOURCE_ROOT'), 'requires explicit source checkout')
class DomainRegistration(unittest.TestCase):
    def test_all_ten_real_schemas_and_authorization(self):
        # Isolated process avoids namespace contamination of the test runner.
        script = '''
import json,os
from pathlib import Path
from bioma_peptide_bridge.catalog import catalog, source_files
from bioma_peptide_bridge.contracts import sha256
from bioma_peptide_bridge.domain import load_tools
root=Path(os.environ['PEPTIDE_SOURCE_ROOT']); rows=catalog(root)
files=source_files(root)
tools,_=load_tools(root,{p:sha256(root/p) for p in files})
assert len(tools)==10
for name,tool in tools.items():
 assert tool.result_kind=='computed_prediction' and callable(tool.result_validator)
 assert tool.parameters_schema()['type']=='object'
 assert tool.parameters_schema()['properties']
 assert not tool.result_validator({'execution_status':'ok','scientific_validation_status':'passed'}, {})
print(json.dumps({n:t.parameters_schema() for n,t in tools.items()}))
'''
        result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
