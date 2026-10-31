"""Scientific tools for autonomous target discovery and structure assessment."""
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from bioma.peptide.target_research import assess, validate_assessment

class StructureInput(ToolInput):
    pdb_id: str = Field(description='PDB ID selected from actual target metadata; no fixed target or chain is assumed.')

@bioma_tool(name='assess_target_structure',description='Download the selected experimental PDB mmCIF and calculate a checked inventory of chains, residues, missing residues, hetero atoms and finite coordinates. Return source coordinates for 3D preview and a checked per-chain residue-count SVG. Compare candidate structures from query_uniprot before choosing; this does not approve receptor preparation or binding.',category='structure',author='BioMA',args_schema=StructureInput,requires_network=True,result_kind='calculation',result_validator=validate_assessment,replay_safe=True)
def assess_target_structure(pdb_id: str):
    return assess(pdb_id)
