/* Pure adapters: every node and edge comes from a recorded state or explicit reference. */
(function(root){
'use strict';
function buildEvidenceGraph(run){
  const state=run.state||{},nodes=[],edges=[],byId=new Map(),aliases=new Map(),grants=new Map();
  function node(id,kind,label,detail,record){const n={id,kind,label,detail,record};nodes.push(n);byId.set(id,n);return id}
  function alias(id,target){if(id)aliases.set(String(id),target)}
  function edge(source,target,label,membership=false){if(source!==target&&byId.has(source)&&byId.has(target)&&!edges.some(e=>e.source===source&&e.target===target&&e.label===label))edges.push({source,target,label,membership})}
  const task=node('task','query','研究问题',run.query||state.objective||'当前研究任务',{query:run.query||state.objective,status:run.status});
  for(const [index,row] of activityRows(run).entries()){
    if(!['tool','skill'].includes(row.family))continue;
    const id=node('execution:'+(row.event.attempt_id||index),row.family,row.name,row.status,row.event);
    edge(task,id,'本轮执行记录',true);
    if(row.event.tool_grant_ref){
      const matches=grants.get(row.event.tool_grant_ref)||[];matches.push(id);grants.set(row.event.tool_grant_ref,matches);
    }
  }
  for(const observation of state.observations||[]){
    const id=node('work:'+observation.id,'work',observation.value?.kind||observation.kind||'工作结果',observation.method||observation.source||'已记录结果',observation);
    alias(observation.id,id);edge(task,id,'本轮工作记录',true);
  }
  for(const evidence of state.evidence||[]){
    const id=node('evidence:'+evidence.id,'evidence',evidence.kind||'证据记录',evidence.claim||evidence.source||'',evidence);
    alias(evidence.id,id);edge(task,id,'本轮证据记录',true);
  }
  for(const artifact of state.artifacts||[]){
    const id=node('artifact:'+artifact.id,'artifact',String(artifact.uri||artifact.id).split(/[\\/]/).pop(),artifact.kind||'文件',artifact);
    alias(artifact.id,id);alias(artifact.uri,id);
  }
  for(const observation of state.observations||[]){
    const source=aliases.get(observation.id);
    for(const id of observation.artifact_ids||[])edge(source,aliases.get(id),'生成产物');
    for(const a of observation.value?.artifacts||[])edge(source,aliases.get(a.id),'生成产物');
    for(const upstream of observation.value?.provenance?.upstream_work_results||[])edge(aliases.get(upstream.observation_id),source,'上游结果引用');
  }
  for(const artifact of state.artifacts||[]){
    const target=aliases.get(artifact.id),ref=artifact.metadata?.work_result_ref;
    if(ref)edge(aliases.get(ref),target,'生成产物');
    for(const id of artifact.evidence_ids||[])edge(aliases.get(id),target,'文件的证据引用');
    for(const id of artifact.metadata?.candidate_result_refs||[])edge(aliases.get(id),target,'结果引用');
    if(!edges.some(e=>e.target===target||e.source===target))edge(task,target,'本轮产物',true);
  }
  for(const observation of state.observations||[]){
    for(const ref of observation.value?.provenance?.tool_grant_refs||[])for(const id of grants.get(ref)||[])edge(id,aliases.get(observation.id),'工作结果引用的工具授权');
    for(const receipt of observation.value?.payload?.tool_receipts||[]){
      if(!receipt.path)continue;
      const id='receipt:'+receipt.path;
      if(!byId.has(id))node(id,'receipt',receipt.tool||'工具回执',receipt.status||'已记录回执',receipt);
      for(const tool of grants.get(receipt.tool_grant_ref)||[])edge(tool,id,'授权关联的工具回执');
      edge(id,aliases.get(observation.id),'工作结果引用的回执');
    }

  }
  // Values containing a known record ID establish a reference, not scientific support.
  function references(value,found=new Set()){
    if(typeof value==='string'&&aliases.has(value))found.add(aliases.get(value));
    else if(Array.isArray(value))value.forEach(v=>references(v,found));
    else if(value&&typeof value==='object')Object.entries(value).forEach(([key,v])=>{if(key!=='id')references(v,found)});
    return found;
  }
  for(const evidence of state.evidence||[]){
    const target=aliases.get(evidence.id);
    for(const ref of references(evidence.value||{}))edge(ref,target,'证据记录引用');
    for(const id of evidence.parent_ids||[])edge(aliases.get(id),target,'上游证据派生');
    for(const id of evidence.artifact_ids||[])edge(aliases.get(id),target,'引用文件');
  }
  for(const observation of state.observations||[]){
    if(observation.value?.kind!=='EvidenceAssessment')continue;
    const assessment=observation.value.payload||{};
    if(!assessment.claim)continue;
    const id=node('claim:'+observation.id,'claim',assessment.claim,assessment.relation||'未评估',assessment);
    edge(aliases.get(observation.id),id,'记录的主张评估');
    for(const ref of assessment.input_refs||[])edge(aliases.get(ref),id,'评估关系：'+(assessment.relation||'未评估'));
  }
  return {nodes,edges};
}
function activityRows(run){
  const rows=[],active=new Map(),attempts=new Map();
  for(const event of run.events||[]){
    const type=event.type||'';
    if(type==='protocol.tool_action'){
      const states={ToolActionProposed:'running',ToolActionPermitted:'running',ToolActionObserved:'observed',ToolActionFailed:'failed',ToolActionUnresolved:'unconfirmed'};
      const status=states[event.stage]||'unconfirmed';
      // A tool observation is not a verified scientific result. Group by attempt,
      // so retries and concurrent invocations of the same tool stay distinct.
      const key=event.attempt_id;
      if(key&&attempts.has(key)){
        const row=attempts.get(key);row.status=status;row.event={...row.event,...event};row.stages.push(event.stage);
      }else{
        const row={name:event.tool||'科学工具',status,family:'tool',event,stages:[event.stage]};
        rows.push(row);if(key)attempts.set(key,row);
      }
      continue;
    }
    let family,name,status;
    if(type.startsWith('llm.')){family='llm';name='模型分析 · '+(event.model||event.agent||'研究任务')}
    else if(type.startsWith('tool.')){family='tool';name=event.tool||'科学工具'}
    else if(type.startsWith('verification.')){family='verification';name='核验执行结果'}
    else if(type==='state.committed'){rows.push({name:'记录结果与产物',status:'completed',family:'commit',event});continue}
    else if(type==='skill.loaded'){rows.push({name:'读取研究方法 · '+(event.skill||'已记录方法'),status:'completed',family:'skill',event});continue}
    else if(type==='run.failed'||type==='runtime.blocked'){rows.push({name:type==='run.failed'?'执行失败':'等待补充条件',status:'failed',family:'status',event});continue}
    else continue;
    status=/failed|rejected/.test(type)?'failed':/completed|approved/.test(type)?'completed':'running';
    const key=family+':'+(event.call_id||event.tool_call_id||event.proposal_id||event.agent||'')+':'+(event.tool||event.model||'');
    if(status==='running'){
      const row={name,status,family,event};rows.push(row);active.set(key,row);
    }else if(active.has(key)){
      const row=active.get(key);row.status=status;row.event={...row.event,...event};active.delete(key);
    }else rows.push({name,status,family,event});
  }
  if(!rows.length&&run.status==='running')rows.push({name:run.live_phase?.detail?.startsWith('Queued')?'等待执行':'解析研究目标',status:'running',family:'gateway',event:run.live_phase||{}});
  if(run.status!=='running')for(const row of rows)if(row.status==='running')row.status='unconfirmed';
  return rows;
}
root.BioMAWorkspaceData={buildEvidenceGraph,activityRows};
if(typeof module!=='undefined'&&module.exports)module.exports=root.BioMAWorkspaceData;
})(typeof window!=='undefined'?window:globalThis);
