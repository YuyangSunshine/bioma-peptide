import json
import time
from pathlib import Path
import pytest
from bioma.peptide import target_research as tr
from bioma.peptide import web_jobs as wj
from bioma.peptide.contracts import sha256

CIF='''data_test
_entry.id 1ABC
_exptl.method 'X-RAY DIFFRACTION'
loop_
_atom_site.group_PDB
_atom_site.label_asym_id
_atom_site.type_symbol
_atom_site.label_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
ATOM A C 1 0 0 0
ATOM A N 1 1 0 0
HETATM B O . 2 0 0
#
'''

def test_discovery_preserves_species_and_checks_original(tmp_path,monkeypatch):
    monkeypatch.setattr(tr,'DATA',tmp_path)
    source=tmp_path/'source.json';source.write_text(json.dumps({'results':[{'primaryAccession':'P00001','organism':{'scientificName':'species-a'}},{'primaryAccession':'P00002','organism':{'scientificName':'species-b'}}]}))
    monkeypatch.setattr(tr,'fetch',lambda url:source)
    result=tr.discover('target');assert len(result['targets'])==2
    assert tr.validate_discovery(result,{'query':'target'})
    result['targets'][0]['organism']['scientificName']='invented'
    assert not tr.validate_discovery(result,{'query':'target'})

def test_structure_recomputed_and_input_mutation_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(tr,'DATA',tmp_path)
    source=tmp_path/'source.cif';source.write_text(CIF)
    monkeypatch.setattr(tr,'fetch',lambda url:source)
    result=tr.assess('1ABC');assert tr.validate_assessment(result,{'pdb_id':'1ABC'})
    result['metrics']['atom_count']=100
    assert not tr.validate_assessment(result,{'pdb_id':'1ABC'})
    result=tr.assess('1ABC');source.write_text(CIF.replace('0 0 0','nan 0 0'))
    assert not tr.validate_assessment(result,{'pdb_id':'1ABC'})

def test_structure_identity_and_target_id_injection(tmp_path,monkeypatch):
    monkeypatch.setattr(tr,'DATA',tmp_path)
    source=tmp_path/'source.cif';source.write_text(CIF)
    monkeypatch.setattr(tr,'fetch',lambda url:source)
    with pytest.raises(ValueError,match='identity'):tr.assess('2ABC')
    with pytest.raises(ValueError,match='invalid_pdb'):tr.assess('../secret')

def test_web_job_idempotency_receipt_and_no_self_approval(tmp_path,monkeypatch):
    seed=tmp_path/'seed.txt';seed.write_text('synthetic')
    budget=dict(seconds=5,cpu_seconds=4,memory_bytes=256*1024**2,output_bytes=2*1024**2,cpus=1)
    registry=tmp_path/'registry.json';registry.write_text(json.dumps({'input_root':str(tmp_path),'inputs':{'seed':{'path':seed.name,'sha256':sha256(seed)}},'profiles':{'fixture':{'method':'engineering_canary','approved':True,'review_id':'test','inputs':['seed'],'identity':{'chemical':'synthetic'},'resource_caps':budget}}}))
    monkeypatch.setenv('BIOMA_PEPTIDE_REGISTRY',str(registry));monkeypatch.setenv('BIOMA_PEPTIDE_JOB_ROOT',str(tmp_path/'jobs'))
    args=dict(operation='submit',profile_id='fixture',input_refs=['seed'],idempotency_key='repeat',resources=budget,job_id=None)
    a=wj.operate(args);b=wj.operate(args);assert a['job_id']==b['job_id'];assert wj.validate_operation(a,args)
    a['data']['status']='invented';assert not wj.validate_operation(a,args)
    deadline=time.monotonic()+8
    while time.monotonic()<deadline and wj.broker().status(b['job_id'])['status'] in {'queued','running'}:time.sleep(.05)
    collect=dict(operation='collect',profile_id=None,input_refs=[],idempotency_key=None,resources={},job_id=b['job_id'])
    result=wj.operate(collect);assert wj.validate_operation(result,collect)
    assert not result['data']['deliverable'];assert 'files' not in result
    assert wj.validate_profiles(wj.profiles(),{})

@pytest.mark.parametrize('stage',['failed_topology','failed','unknown','partial_success','interrupted'])
@pytest.mark.parametrize('status_key',['status','stage_status'])
def test_json_failure_never_becomes_ok_receipt(tmp_path,stage,status_key):
    from bioma.tools.base import Tool, ToolInput
    from bioma.tools.registry import ToolRegistry
    from bioma.harness.shared_tools import shared_tools
    from bioma.harness.types import HarnessContext
    class Empty(ToolInput):pass
    t=Tool(name='fixture',description='fixture',category='analysis',version='1',author='test',
           func=lambda:json.dumps({status_key:stage}),args_schema=Empty,
           result_kind='calculation',result_validator=lambda r,a:True,replay_safe=True)
    registry=ToolRegistry([]);registry.add(t)
    observation=shared_tools(registry)[0].call(HarnessContext({},tmp_path),{})
    assert observation.status=='error'
    assert observation.metrics['result'][status_key]==stage


def test_nested_inspection_exposes_keys_and_pagination(tmp_path):
    from bioma.harness.input_inspection import inspect_input_data
    from bioma.harness.types import HarnessContext
    source=tmp_path/'input.json';source.write_text(json.dumps({'results':[{'entryType':'reviewed','primaryAccession':'P12345'}]}))
    ctx=HarnessContext({'raw_data_sources':[str(source)]},tmp_path)
    result=inspect_input_data(ctx,dict(input_index=0,path=['results','0'],offset=0,limit=1))
    assert result.metrics['content']=={'entryType':'reviewed'}
    assert result.metrics['available_keys']==['entryType','primaryAccession']
    assert result.metrics['next_offset']==1


def test_project_material_hash_and_large_reference(tmp_path,monkeypatch):
    from bioma.peptide import project_data as pd
    monkeypatch.setenv('BIOMA_PROJECT_DATA',str(tmp_path))
    source=tmp_path/'large.bin'
    with source.open('wb') as f:f.truncate(33*1024**2)
    (tmp_path/'manifest.json').write_text(json.dumps({'files':[{'local':'large.bin','size':source.stat().st_size,'sha256':sha256(source)}]}))
    result=pd.read('large.bin');assert not result['files'];assert result['external_reference']['sha256']==sha256(source)
    with source.open('r+b') as f:f.write(b'changed')
    with pytest.raises(ValueError,match='identity_mismatch'):pd.read('large.bin')


def test_stalled_read_guard_keeps_autonomous_actions_and_review():
    from bioma.harness.loop import ProgressGuard, _avoid_stalled_reads
    names=['inspect_inputs','inspect_input_data','finish_investigation','invoke_scientific_tool','revise_analysis_code']
    tools=[{'function':{'name':n}} for n in names]
    progress=ProgressGuard();progress.stalled_steps=1;progress.last_tool='inspect_input_data'
    remaining=_avoid_stalled_reads(tools,progress,{'ready_for_review':True},revision_pending=False)
    assert [x['function']['name'] for x in remaining]==names[2:]
    assert _avoid_stalled_reads(tools,progress,{'ready_for_review':False},revision_pending=False)==tools
    assert _avoid_stalled_reads(tools,progress,{'ready_for_review':True},revision_pending=True)==tools


def test_direct_visibility_does_not_create_tool_authority(tmp_path):
    from bioma.config import ScientificInvestigationConfig, LLMConfig
    from bioma.backends.investigation import ScientificInvestigationWorker
    from bioma.tools.base import Tool, ToolInput
    from bioma.tools.registry import ToolRegistry
    class Empty(ToolInput):pass
    registry=ToolRegistry([])
    registry.add(Tool(name='allowed',description='fixture',category='analysis',version='1',author='test',
        func=lambda:{'status':'ok'},args_schema=Empty,result_kind='calculation',
        result_validator=lambda r,a:True,replay_safe=True))
    config=ScientificInvestigationConfig(llm=LLMConfig(model='unused'),direct_tool_names=['allowed','not_authorized'])
    worker=ScientificInvestigationWorker(config,base_dir=tmp_path,tool_registry=registry)
    names=[t.name for t in worker.tools_for_state({})]
    assert names.count('allowed')==1 and 'not_authorized' not in names
    assert 'finish_investigation' in names and 'revise_analysis_code' in names


def test_live_storage_accounting_tolerates_atomic_rename(tmp_path):
    from bioma.peptide.worker import storage_bytes
    class Gone:
        def is_file(self):return True
        def stat(self):raise FileNotFoundError('renamed temporary output')
    good=tmp_path/'result';good.write_bytes(b'abc')
    class Folder:
        def rglob(self,pattern):return iter([Gone(),good])
    assert storage_bytes(Folder())==3


def test_job_defaults_satisfy_actual_grant_schema():
    import jsonschema
    from bioma.tools.registry import ToolRegistry
    registry=ToolRegistry([Path(__file__).resolve().parents[2]/'tools/our_tools']);registry.load()
    tool=registry.get('manage_peptide_job')
    for supplied in [{'operation':'status','job_id':'registered'},
                     {'operation':'submit','profile_id':'registered','idempotency_key':'stable'}]:
        actual=tool.args_schema(**supplied).model_dump()
        jsonschema.validate(actual,tool.parameters_schema())


def test_review_json_fence_is_only_transport_envelope():
    from bioma.harness.delivery_review import _parse_review_response
    raw='{"issues": [], "source_support": null, "method_support": {}}'
    assert _parse_review_response('```json\n'+raw+'\n```')==_parse_review_response(raw)
    for text in ['approved '+raw, '```json\n'+raw, '```json\n'+raw+'\n```\nextra',
                 '```json\n'+raw+'\n```\n```json\n'+raw+'\n```', '{"issues": [']:
        with pytest.raises(ValueError):_parse_review_response(text)


def test_collection_admits_small_structures_not_large_trajectories(tmp_path):
    from bioma.peptide.web_jobs import collection_files
    report=tmp_path/'web_collection.json';report.write_text('{}')
    small=tmp_path/'topology.sdf';small.write_text('registered structure')
    trajectory=tmp_path/'trajectory.dcd'
    with trajectory.open('wb') as f:f.truncate(33*1024**2)
    data={'artifacts':[{'relative_path':p.name,'size':p.stat().st_size,'sha256':sha256(p)} for p in [small,trajectory]]}
    class Registered:
        def directory(self,job):return tmp_path
    files=collection_files(Registered(),'registered',data)
    assert str(small) in files and str(report) in files and str(trajectory) not in files


def test_unadvertised_action_is_not_dispatched(tmp_path,monkeypatch):
    from bioma.harness import loop
    from bioma.harness.budget import HarnessBudget, HarnessBudgetConfig
    from bioma.harness.types import HarnessContext, HarnessTool
    from bioma.config import ScientificInvestigationConfig, LLMConfig
    from bioma.llm import ChatResponse, ToolCall
    dispatched=[]
    tools=[HarnessTool(name=n,description=n,handler=lambda c,a:dispatched.append(True))
           for n in ['hidden','finish_investigation']]
    monkeypatch.setattr(loop,'investigation_delivery_readiness',lambda context:{
        'ready_for_review':True,'reviewable_support':[{'receipt_ref':'one'},{'receipt_ref':'two'}]})
    class StaleModel:
        def chat_with_tools(self,messages,definitions):
            assert [d['function']['name'] for d in definitions]==['finish_investigation']
            return ChatResponse(content='',tool_calls=(ToolCall(id='stale',name='hidden',arguments='{}'),))
    config=ScientificInvestigationConfig(llm=LLMConfig(model='unused'),max_llm_calls=2)
    context=HarnessContext({'continuous_investigation':True},tmp_path)
    result=loop.DataAnalysisLoop(config,provider_factory=lambda config:StaleModel()).run(
        query='test',context=context,tools=tools,
        budget=HarnessBudget(HarnessBudgetConfig(max_llm_calls=2,max_tool_calls=3)))
    assert result.exit_reason=='computed_pending_review' and not dispatched
    assert context.observations[-1]['status']=='error'


def test_error_feedback_preserves_selected_input_mapping(tmp_path):
    from bioma.harness.action_executor import call_harness_tool
    from bioma.harness.budget import HarnessBudget, HarnessBudgetConfig
    from bioma.harness.types import HarnessContext
    context=HarnessContext({'raw_data_sources':['request.json','data.json']},tmp_path)
    context.analysis_draft={'code':'pass','output_files':[],'input_indices':[1]}
    text,_=call_harness_tool('unknown','{}',{},context,
        HarnessBudget(HarnessBudgetConfig(max_llm_calls=2,max_tool_calls=2)),None,None)
    mapping=json.loads(text)['analysis_draft']['execution_input_map']
    assert mapping==[{'python_index':0,'r_index':1,'catalog_index':1,'path':'data.json'}]


def test_structure_text_is_bounded_observation(tmp_path):
    from bioma.harness.input_inspection import inspect_input_data
    from bioma.harness.types import HarnessContext
    source=tmp_path/'structure.sdf';source.write_text('title\nheader\ncounts\nM END\n$$$$\n')
    context=HarnessContext({'raw_data_sources':[str(source)]},tmp_path)
    result=inspect_input_data(context,dict(input_index=0,path=[],offset=1,limit=2))
    assert result.metrics['content']==['header','counts']
    assert result.metrics['authority']=='input_preview_only'


def test_retrieved_target_alternatives_can_support_clarification(tmp_path):
    from bioma.backends.investigation import request_clarification
    from bioma.harness.types import HarnessContext
    context=HarnessContext({'public_resource_discovery':True,'harness_observations':[
        {'tool_name':'inspect_inputs','status':'ok','metrics':{}},
        {'tool_name':'query_uniprot','status':'ok','metrics':{
            'result_kind':'retrieved_fact','result':{'organisms':['Homo sapiens','Mus musculus']}}}]},tmp_path)
    result=request_clarification(context,dict(kind='semantic_choice',field='organism',
        question='Which observed organism?',reason='Multiple source records',
        alternatives=['Homo sapiens','Mus musculus']))
    assert result.patch['need_clarification']


def test_written_report_can_be_reviewed_without_forcing_a_file():
    from bioma.core.reporting import validate_written_delivery
    validate_written_delivery({'current_user_request':'交付带来源的科学报告'},[], 'A draft report requiring independent review')
    with pytest.raises(ValueError,match='written report'):
        validate_written_delivery({'current_user_request':'交付带来源的科学报告'},[], '')
    with pytest.raises(ValueError,match='PDF'):
        validate_written_delivery({'current_user_request':'生成 PDF 报告'},[], 'Text is not a PDF')
    with pytest.raises(ValueError,match='PDF'):
        validate_written_delivery({'desired_deliverables':['report.pdf']},['report.md'], 'Text is not the requested file')
