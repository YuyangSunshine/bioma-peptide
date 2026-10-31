"""Host-configured job tools. The Agent supplies IDs, never registry paths or approvals."""
import json
import os
import uuid
from pathlib import Path
from .broker import Broker, write_json
from .contracts import digest, sha256


def broker():
    registry=os.environ.get('BIOMA_PEPTIDE_REGISTRY');root=os.environ.get('BIOMA_PEPTIDE_JOB_ROOT')
    if not registry or not root:raise ValueError('blocked_peptide_registry_not_configured')
    return Broker(root,registry)


def profiles():
    b=broker()
    return {'status':'ok','profiles':[{'profile_id':key,'method':p['method'],
        'input_refs':p.get('inputs',[]),'purpose':p.get('purpose'),'approved':p.get('approved',False),
        'resource_caps':p.get('resource_caps'),'identity':p.get('identity',{}),
        'requires_independent_review':p.get('purpose') in {'production_md','md_canary'}}
        for key,p in b.registry['profiles'].items()],
        'scope':'Registered capabilities only; profile availability is not scientific qualification'}


def validate_profiles(result,args):
    try:return result==profiles()
    except (ValueError,OSError):return False


def operate(arguments):
    b=broker();a=dict(arguments);op=a['operation']
    if op not in {'submit','status','collect','cancel','resume','reattach'}:raise ValueError('unsupported_job_operation')
    if op=='submit':
        if a.get('job_id'):raise ValueError('submit_does_not_accept_job_id')
        data=b.submit(profile=a['profile_id'],input_refs=a['input_refs'],idempotency_key=a['idempotency_key'],resources=a['resources'])
    else:
        if any(a.get(k) for k in ('profile_id','input_refs','idempotency_key','resources')):raise ValueError('lifecycle_requires_only_job_id')
        if not a.get('job_id'):raise ValueError('job_id_required')
        data=getattr(b,op)(a['job_id'])
    result={'status':'ok','operation':op,'job_id':data.get('job_id',a.get('job_id')),'data':data,
            'scientific_validation_status':data.get('scientific_validation_status','not_run'),
            'qualification':'A job operation receipt is not scientific evidence'}
    if op=='collect' and data.get('deliverable'):
        folder=b.directory(result['job_id']);report=folder/'web_collection.json';write_json(report,data)
        # Only the small report is admitted to the conversation; artifacts remain references.
        result['files']={str(report):sha256(report)};result['analysis_inputs']=[str(report)]
    result['arguments_sha256']=digest(a)
    receipt=b.root/'web-receipts'/ (uuid.uuid4().hex+'.json')
    receipt.parent.mkdir(exist_ok=True)
    write_json(receipt,result)
    result['operation_receipt']={'path':str(receipt),'sha256':sha256(receipt)}
    return result


def validate_operation(result,args):
    try:
        if result['status']!='ok' or result['arguments_sha256']!=digest(args):return False
        b=broker();job=result['job_id'];b.directory(job)
        ref=result['operation_receipt'];path=Path(ref['path']).resolve()
        if path.parent!=b.root/'web-receipts' or sha256(path)!=ref['sha256']:return False
        if json.loads(path.read_text())!={k:v for k,v in result.items() if k!='operation_receipt'}:return False
        if args['operation']=='collect':
            actual=b.collect(job)
            if result['data']!=actual:return False
            for p,h in result.get('files',{}).items():
                if Path(p).resolve()!=b.directory(job)/'web_collection.json' or sha256(p)!=h or json.loads(Path(p).read_text())!=actual:return False
        elif args['operation']=='submit':
            request=json.loads((b.directory(job)/'request.json').read_text())
            if request['profile_id']!=args['profile_id'] or sorted(request['inputs'])!=sorted(args['input_refs']) or request['resources']!=args['resources']:return False
        return True
    except (OSError,ValueError,KeyError,TypeError):return False
