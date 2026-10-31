"""Discover imported local scientific material without granting arbitrary host access."""
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from bioma.peptide.project_data import listing, read, validate_listing, validate_read
class ListInput(ToolInput):
    query: str = ''
    offset: int = Field(default=0,ge=0)
class ReadInput(ToolInput):
    material_id: str
@bioma_tool(name='list_project_materials',description='Search imported local project evidence by filename terms, e.g. receptor, parameters, ranked candidates. Returns bounded material IDs and hashes, not assumed approvals. Use when the user refers to existing project materials.',category='database',author='BioMA',args_schema=ListInput,result_kind='retrieved_fact',result_validator=validate_listing,replay_safe=True)
def list_project_materials(query='',offset=0):return listing(query,offset)
@bioma_tool(name='read_project_material',description='Read an exact material ID from list_project_materials, verify the imported original hash and expose a bounded preview. Original parameter/receptor review states are preserved. No arbitrary path or generated replacement accepted.',category='database',author='BioMA',args_schema=ReadInput,result_kind='retrieved_fact',result_validator=validate_read,replay_safe=True)
def read_project_material(material_id):return read(material_id)
