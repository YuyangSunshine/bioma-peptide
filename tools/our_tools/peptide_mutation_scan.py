"""Sequence-level triage for explicitly methionine-crosslinked scaffolds."""
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from bioma.peptide.mutation_scan import scan, validate

class MutationScanInput(ToolInput):
    sequence: str = Field(description='Uppercase canonical parent sequence; exactly three M anchors.')
    fixed_positions: list[int] = Field(description='Explicit one-based positions to retain, including all three M anchors and any fixed scaffold residues.')

@bioma_tool(name='scan_methionine_single_mutants',description='Enumerate every canonical single substitution outside explicit fixed positions for a three-methionine peptide. Produce a checked CSV, linear-mass/GRAVY chart and chemistry flags for extra M or introduced C. No candidate sequence is hardcoded. These are unmodified linear-sequence proxies, NOT affinity, cyclic-product ADMET, docking or MD predictions. Useful independent work while experimental units or production approvals remain unresolved.',category='analysis',author='BioMA',args_schema=MutationScanInput,result_kind='calculation',result_validator=validate,replay_safe=True)
def scan_methionine_single_mutants(sequence: str, fixed_positions: list[int]):
    return scan(sequence, fixed_positions)
