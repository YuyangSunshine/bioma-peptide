"""Native asynchronous job actions exposed through BioMA's existing tool budget."""
from typing import Literal
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from bioma.peptide.web_jobs import profiles, validate_profiles, operate, validate_operation

class Empty(ToolInput):pass
class JobInput(ToolInput):
    operation: Literal['submit','status','collect','cancel','resume','reattach']
    profile_id: str = ''
    input_refs: list[str] = Field(default_factory=list)
    idempotency_key: str = ''
    resources: dict[str,int] = Field(default_factory=dict)
    job_id: str = ''

@bioma_tool(name='list_peptide_profiles',description='List host-registered peptide methods, input IDs, resource caps and purpose gates. Discover before submission; no path or scientific approval can be supplied by the Agent.',category='computation',author='BioMA',args_schema=Empty,result_kind='calculation',result_validator=validate_profiles,replay_safe=True)
def list_peptide_profiles():return profiles()

@bioma_tool(name='manage_peptide_job',description='Submit or manage a registered persistent scientific job. Submit needs exact profile/input IDs, explicit resource caps and a stable idempotency key. status/collect/cancel/resume/reattach need only job_id. Submission is not completion or scientific approval. Collect validates artifacts and reruns the registered checker. Reattach existing jobs rather than submitting again.',category='computation',author='BioMA',args_schema=JobInput,result_kind='calculation',result_validator=validate_operation,replay_safe=True)
def manage_peptide_job(operation,profile_id='',input_refs=None,idempotency_key='',resources=None,job_id=''):
    return operate(dict(operation=operation,profile_id=profile_id,input_refs=input_refs or [],idempotency_key=idempotency_key,resources=resources or {},job_id=job_id))
