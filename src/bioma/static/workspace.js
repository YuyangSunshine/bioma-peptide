/* Presentation and interaction layer over the existing BioMA HTTP contract. */
const {buildEvidenceGraph,activityRows}=BioMAWorkspaceData;
let graphSelection=null,graphZoom=1,graphSignature='',fileSignature='',fileSelected=null,filePreviewToken=0;
const activitySignatures=new Map(),expandedActivities=new Set();
const coreRender=render;
render=async function(d){
  await coreRender(d);
  if(lastRunData!==d)return;
  renderChatActivity(d);
  renderFileBrowser(d);
  if(!d.run_id)renderEvidenceGraphPanel(d);
  $('#graph-count').textContent=d.run_id&&d.state?buildEvidenceGraph(d).nodes.length:'—';
  $('#file-count').textContent=d.state?(d.state.artifacts||[]).length:'—';
};
const coreBubble=bubble;
bubble=function(role,content,runId){
  coreBubble(role,content);
  const row=$('#conversation').lastElementChild;
  if(runId||currentRun)row.dataset.runId=runId||currentRun;
};
conversationAlreadyHas=function(text){return [...$('#conversation').querySelectorAll('.msg.assistant')].some(row=>row.dataset.runId===currentRun&&row.querySelector('.bubble').textContent.trim()===String(text).trim())};
function workspaceMode(home){
  $('.app').classList.toggle('is-home',home);
  $('.brand').classList.remove('history-open');
  $('#mobile-history').setAttribute('aria-expanded','false');
  if(home)$('.app').classList.remove('inspector-open');
  $('#inspector-toggle').setAttribute('aria-expanded',String($('.app').classList.contains('inspector-open')));
}
function goHome(clear=true){
  pollToken++;
  if(clear){sessionId=null;resetResults();$('#conversation').innerHTML='';activitySignatures.clear();}
  workspaceMode(true);history.replaceState(null,'',location.pathname+location.search);
  $('#home-query').focus();loadSessions();
}
$('#mobile-history').onclick=()=>{const open=$('.brand').classList.toggle('history-open');$('#mobile-history').setAttribute('aria-expanded',String(open))};
$('#home-link').onclick=()=>goHome(false);
$('#new-session').onclick=()=>goHome(true);
$('#inspector-toggle').onclick=()=>{
  const open=$('.app').classList.toggle('inspector-open');
  $('#inspector-toggle').setAttribute('aria-expanded',String(open));
};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('.app').classList.remove('inspector-open');$('#inspector-toggle').setAttribute('aria-expanded','false')}});
$('#home-attach').onclick=()=>{
  const hidden=$('#home-file-row').classList.toggle('hidden');
  $('#home-attach').setAttribute('aria-expanded',String(!hidden));
  if(!hidden)$('#home-files').focus();
};
const modes={
  search:'请围绕以下研究主题检索可靠资料，保留来源链接，区分已有证据与待验证假设。研究主题：',
  analyze:'请分析我提供的数据，明确计算方法，保存可复核的结果文件，并说明结论的适用范围。研究问题：',
  design:'请为以下研究目标拟定研究方案，明确对照、重复、测量指标和验收条件。只做方案设计，不执行实验。研究目标：',
  write:'请根据我提供的结果文件整理研究报告，引用具体产物，区分计算结果、证据与局限。报告主题：'
};
$$('[data-mode]').forEach(button=>button.onclick=()=>{$('#home-query').value=modes[button.dataset.mode];$('#home-query').focus()});
$$('[data-home-case]').forEach(button=>button.onclick=()=>{$('#home-query').value=cases[button.dataset.homeCase];$('#home-query').focus();$('#home-query').scrollIntoView({behavior:'smooth',block:'center'})});
$('#home-query').onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){$('#home-form').requestSubmit();e.preventDefault()}};
$('#home-form').onsubmit=e=>{
  e.preventDefault();const query=$('#home-query').value.trim();if(!query){$('#home-query').focus();return}
  // A homepage submission begins a new research session; history remains accessible.
  sessionId=null;currentRun=null;$('#conversation').innerHTML='';
  $('#query').value=query;$('#files').value=$('#home-files').value;
  workspaceMode(false);$('#query-form').requestSubmit();
};
$('#query-form').onsubmit=async e=>{
  e.preventDefault();const query=$('#query').value.trim();if(!query||$('#submit').disabled)return;
  const paths=$('#files').value.split(',').map(v=>v.trim()).filter(Boolean),token=++pollToken;
  resetResults();workspaceMode(false);setPanelCollapsed('result',false);showResultPanel('graph');showRunStatus('running');
  $('#notice').textContent='正在提交研究问题…';bubble('user',query);const userBubble=$('#conversation').lastElementChild;
  try{
    let targetSession=sessionId;
    if(!targetSession){const created=await apiJson('api/sessions',{method:'POST'});if(token!==pollToken)return;targetSession=sessionId=created.session_id;}
    updateSessionMeta();
    const result=await apiJson(`api/sessions/${encodeURIComponent(targetSession)}/query`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,files:paths})});
    if(token!==pollToken)return;
    currentRun=result.run_id;userBubble.dataset.runId=currentRun;$('#query').value='';$('#home-query').value='';$('#notice').textContent='';
    $('#run-meta').textContent=`运行：${currentRun.slice(0,8)}`;$('#session-name').textContent=query.slice(0,65);
    history.replaceState(null,'',`#session/${encodeURIComponent(targetSession)}`);
    await loadSessions();poll(currentRun,token);
  }catch(error){if(token!==pollToken)return;showRunStatus('failed');$('#notice').textContent='提交失败：'+error.message;}
};
loadSession=async function(id,replace=true,requestedRunId=null){
  const token=replace?++pollToken:pollToken;
  if(replace){resetResults();activitySignatures.clear();workspaceMode(false);setPanelCollapsed('result',false);showResultPanel('graph');}
  try{
    const session=await apiJson(`api/sessions/${encodeURIComponent(id)}`);if(token!==pollToken)return;
    sessionId=id;updateSessionMeta();const lastQuery=(session.runs||[]).at(-1)?.query;if(lastQuery)$('#session-name').textContent=lastQuery.slice(0,65);history.replaceState(null,'',`#session/${encodeURIComponent(id)}`);
    if(replace){
      $('#conversation').innerHTML='';
      for(const message of session.messages||[])bubble(message.role,message.content,message.run_id);
      const runs=session.runs||[],selected=requestedRunId?runs.find(run=>run.run_id===requestedRunId):runs.at(-1);
      if(requestedRunId&&!selected)throw new Error('案例对应的运行记录不在该会话中。');
      if(selected){
        currentRun=selected.run_id;$('#session-name').textContent=selected.query?.slice(0,65)||'研究会话';$('#run-meta').textContent=`运行：${currentRun.slice(0,8)}`;
        const run=await apiJson(`api/runs/${encodeURIComponent(currentRun)}`);if(token!==pollToken)return;
        await render(run);if(token!==pollToken)return;requestAnimationFrame(()=>{$('#conversation').scrollTop=$('#conversation').scrollHeight});if(run.status==='running')poll(currentRun,token);
      }else $('#conversation').innerHTML='<div class="welcome"><b>新的研究，从一个问题开始</b><p>描述目标，补充数据，BioMA 将记录每一步实际执行。</p></div>';
    }
    await loadSessions();
    return token===pollToken;
  }catch(error){if(token===pollToken)$('#notice').textContent='加载会话失败：'+error.message;return false;}
};
function renderChatActivity(run){
  if(!run.run_id)return;
  const signature=JSON.stringify([run.events,run.status,run.live_phase,run.state?.artifacts?.map(a=>a.id)]);
  if(activitySignatures.get(run.run_id)===signature)return;
  activitySignatures.set(run.run_id,signature);
  const conversation=$('#conversation'),nearBottom=conversation.scrollHeight-conversation.scrollTop-conversation.clientHeight<100;
  let target=[...conversation.querySelectorAll('.run-activity')].find(el=>el.dataset.runId===run.run_id);
  if(!target){
    target=document.createElement('div');target.className='run-activity';target.dataset.runId=run.run_id;
    const user=[...conversation.querySelectorAll('.msg.user')].find(el=>el.dataset.runId===run.run_id);
    if(user)user.after(target);else conversation.append(target);
  }
  const openRows=new Set([...target.querySelectorAll('details[open]')].map(el=>el.dataset.step));
  const rows=activityRows(run),expanded=expandedActivities.has(run.run_id),visible=expanded?rows:rows.slice(-12),offset=expanded?0:Math.max(0,rows.length-12),artifacts=run.state?.artifacts||[];
  target.innerHTML=`<div class="activity-header"><span>${run.status==='running'?'研究进行中':'本轮执行记录'} · ${rows.length} 个事件步骤</span><button class="activity-detail-link">查看过程 ↗</button></div>${rows.length>12?`<button class="view-btn activity-expand">${expanded?'收起较早步骤':'显示本轮全部 '+rows.length+' 个步骤'}</button>`:''}${visible.map((row,i)=>{
    const stateLabel={running:'进行中',completed:'完成',observed:'调用已返回',failed:'失败',unconfirmed:'未确认结束'}[row.status];
    const detail={阶段:row.stages?.join(" → "),调用编号:row.event.attempt_id,父提案:row.event.proposal_id,输入指纹:row.event.input_hash,事件:row.event.type,工具:row.event.tool,执行者:row.event.agent,模型:row.event.model,时间:row.event.timestamp,说明:row.event.detail,耗时秒:row.event.elapsed_seconds,错误:row.event.error||row.event.error_type,参数:row.event.arguments||row.event.inputs,结果:row.event.observation||row.event.result||row.event.output};
    return `<details class="activity-step ${row.status}" data-step="${offset+i}" ${openRows.has(String(offset+i))?'open':''}><summary><span class="activity-icon">${row.status==='running'?'':row.status==='failed'?'!':row.family==='tool'?'⌘':'✓'}</span><strong>${esc(row.name)}</strong><span class="step-state">${stateLabel}</span><span class="step-chevron">›</span></summary><pre>${esc(Object.entries(detail).filter(([,v])=>v!=null).map(([k,v])=>k+'：'+(typeof v==='object'?JSON.stringify(v,null,2):v)).join('\n'))}</pre></details>`;
  }).join('')}${run.status==='running'?'<div class="activity-now" role="status"><span class="pulse-dot"></span>正在等待后端的下一条执行记录…</div>':''}${artifacts.length?`<button class="run-artifact-link"><span>▤ 已保存 ${artifacts.length} 份本地产物</span><span>在图谱中查看 ↗</span></button>`:''}`;
  target.querySelector('.activity-expand')?.addEventListener('click',()=>{if(expanded)expandedActivities.delete(run.run_id);else expandedActivities.add(run.run_id);activitySignatures.delete(run.run_id);renderChatActivity(run)});
  target.querySelector('.activity-detail-link').onclick=()=>{$('.app').classList.add('inspector-open');$('#inspector-toggle').setAttribute('aria-expanded','true');$('[data-ins="live"]').click()};
  target.querySelector('.run-artifact-link')?.addEventListener('click',()=>{setPanelCollapsed('result',false);showResultPanel('graph');$('#result-panel').scrollIntoView({behavior:'smooth',block:'nearest'})});
  if(nearBottom)conversation.scrollTop=conversation.scrollHeight;
}
function graphLabel(text,limit=23){text=String(text||'');return text.length>limit?text.slice(0,limit-1)+'…':text}
renderEvidenceGraphPanel=function(run){
  const target=$('#graph-view');
  if(!run.run_id){target.innerHTML='<div class="graph-empty-state"><div><div class="empty-orbit" style="margin:auto"></div><strong>让结果之间的关系清晰可见</strong><p>执行开始后，研究问题、工作结果、证据和本地文件<br>将在这里逐步连接。</p></div></div>';graphSignature='';return}
  const graph=buildEvidenceGraph(run),signature=JSON.stringify([run.run_id,graph.nodes.map(n=>[n.id,n.label,n.detail]),graph.edges]);
  if(signature===graphSignature&&target.querySelector('.evidence-svg'))return;
  if(graphSignature&&!graphSignature.includes(run.run_id)){graphSelection=null;graphZoom=1;}
  graphSignature=signature;
  // Lay out reference depth, not one fixed column per record type.
  const depth=new Map(),visiting=new Set();
  function level(id){
    if(depth.has(id))return depth.get(id);
    if(visiting.has(id))return 0; // Reciprocal references remain visible without recursive loops.
    visiting.add(id);
    const parents=graph.edges.filter(e=>e.target===id&&!e.membership);
    const value=parents.length?1+Math.max(...parents.map(e=>level(e.source))):(id==='task'?0:1);
    visiting.delete(id);depth.set(id,value);return value;
  }
  graph.nodes.forEach(n=>level(n.id));
  const kinds=[...new Set(depth.values())].sort((a,b)=>a-b),columns=kinds.map(d=>graph.nodes.filter(n=>depth.get(n.id)===d));
  const height=Math.max(320,...columns.map(col=>col.length*90+80)),width=Math.max(480,kinds.length*222+12),positions=new Map();
  columns.forEach((col,i)=>col.forEach((node,j)=>positions.set(node.id,{x:24+i*222,y:60+(height-90-(col.length-1)*90)/2+j*90})));
  const edgeSVG=graph.edges.map((edge,i)=>{const a=positions.get(edge.source),b=positions.get(edge.target),forward=a.x<=b.x,x1=a.x+(forward?180:0),x2=b.x+(forward?0:180),y1=a.y+29,y2=b.y+29,dx=Math.max(45,Math.abs(x2-x1)*.45);return `<path class="graph-link ${edge.membership?'membership':''}" data-edge="${i}" d="M${x1},${y1} C${x1+(forward?dx:-dx)},${y1} ${x2-(forward?dx:-dx)},${y2} ${x2},${y2}"><title>${esc(edge.label)}</title></path>`}).join('');
  const nodeSVG=graph.nodes.map(node=>{const p=positions.get(node.id);return `<g class="graph-node ${node.kind}" data-node="${esc(node.id)}" transform="translate(${p.x},${p.y})" tabindex="0" role="button" aria-label="${esc(node.label)}"><title>${esc(node.label+' · '+node.detail)}</title><rect width="180" height="60" rx="9"/><text x="13" y="25" class="node-name">${esc(graphLabel(node.label))}</text><text x="13" y="43" class="node-kind">${esc(graphLabel(node.detail,25))}</text></g>`}).join('');
  target.innerHTML=`<div class="evidence-canvas"><div class="graph-topline"><div><h3>研究脉络与产物关联</h3><small>${graph.nodes.length} 个节点 · ${graph.edges.length} 条已记录关系</small></div><div class="graph-tools"><button data-zoom="out" aria-label="缩小图谱">−</button><output id="graph-zoom">100%</output><button data-zoom="in" aria-label="放大图谱">＋</button><button data-zoom="fit" aria-label="适合窗口">⤢</button></div></div><div class="graph-scroll" tabindex="0" aria-label="研究关系图，可拖动或滚动查看"><svg class="evidence-svg" viewBox="0 0 ${width} ${height}" role="group" aria-label="研究问题、工作结果、证据与产物图谱">${kinds.map(level=>level===0?'研究目标':'来源层级 '+level).map((name,i)=>`<text x="${24+i*222}" y="30" class="graph-column-label">${name}</text>`).join('')}${edgeSVG}${nodeSVG}</svg></div><div class="graph-legend">${[['query','研究问题'],['tool','工具调用'],['receipt','执行回执'],['work','工作结果'],['evidence','证据记录'],['claim','主张评估'],['artifact','本地文件']].map(([kind,label])=>`<span><i class="legend-dot ${kind}"></i>${label}</span>`).join('')}<span>虚线：本轮归属</span></div></div><div class="graph-detail" id="graph-detail"><h4>点击一个节点，追溯上游与下游</h4><p>选择工具可追踪回执、工作结果与交付文件；选择产物可反向追溯来源。</p></div><p class="graph-note">层级按已记录的来源引用展开；点击节点高亮完整上下游。工具→回执→工作结果→产物表示间接来源，不证明工具直接生成了每个文件。同一授权的多次调用保留为多个候选关联。尚未形成证据的计算结果会单独保留。只有存在 EvidenceAssessment 记录才展示主张支撑关系；缺少评估不表示反驳。</p>`;
  const viewport=target.querySelector('.graph-scroll'),svg=target.querySelector('svg');
  const applyZoom=()=>{svg.style.width=Math.max(viewport.clientWidth,width)*graphZoom+'px';svg.style.height='auto';$('#graph-zoom').textContent=Math.round(graphZoom*100)+'%'};
  target.querySelectorAll('[data-zoom]').forEach(b=>b.onclick=()=>{graphZoom=b.dataset.zoom==='fit'?Math.min(1,viewport.clientWidth/width):Math.max(.35,Math.min(2,graphZoom+(b.dataset.zoom==='in'?.15:-.15)));applyZoom()});
  target.querySelectorAll('[data-node]').forEach(el=>{el.onclick=()=>selectGraphNode(el.dataset.node,graph,run);el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectGraphNode(el.dataset.node,graph,run)}}});
  let drag=null;
  viewport.onpointerdown=e=>{if(e.target.closest('[data-node]')||e.button!==0)return;drag={x:e.clientX,y:e.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(e.pointerId);viewport.classList.add('dragging')};
  viewport.onpointermove=e=>{if(drag){viewport.scrollLeft=drag.left-(e.clientX-drag.x);viewport.scrollTop=drag.top-(e.clientY-drag.y)}};
  viewport.onpointerup=viewport.onpointercancel=()=>{drag=null;viewport.classList.remove('dragging')};
  requestAnimationFrame(applyZoom);
  if(graphSelection&&graph.nodes.some(n=>n.id===graphSelection))selectGraphNode(graphSelection,graph,run);
};
function selectGraphNode(id,graph,run){
  graphSelection=id;const node=graph.nodes.find(n=>n.id===id);if(!node)return;
  const lineage=new Set([id]);
  for(const direction of ['up','down']){
    const seen=new Set([id]),queue=[id];
    while(queue.length){const current=queue.shift();for(const edge of graph.edges.filter(e=>!e.membership&&(direction==='up'?e.target===current:e.source===current))){const next=direction==='up'?edge.source:edge.target;if(!seen.has(next)){seen.add(next);lineage.add(next);queue.push(next)}}}
  }
  const related=graph.edges.filter(e=>lineage.has(e.source)&&lineage.has(e.target)),neighbors=lineage;
  $$('#graph-view [data-node]').forEach(el=>{el.classList.toggle('selected',el.dataset.node===id);el.classList.toggle('dimmed',!neighbors.has(el.dataset.node))});
  $$('#graph-view [data-edge]').forEach(el=>{const edge=graph.edges[Number(el.dataset.edge)],selected=neighbors.has(edge.source)&&neighbors.has(edge.target);el.classList.toggle('selected',selected);el.classList.toggle('dimmed',!selected)});
  const labels={query:'研究问题',receipt:'工具执行回执',tool:'工具调用',skill:'方法读取',claim:'主张评估',work:'已记录工作结果',evidence:'证据记录',artifact:'本地输出文件'},record=node.record||{};
  $('#graph-detail').innerHTML=`<h4>${esc(node.label)}</h4><p>${labels[node.kind]} · ${esc(node.detail)}</p>${['tool','skill','receipt','evidence','claim'].includes(node.kind)?`<pre class="raw-box">${esc(JSON.stringify(record,null,2))}</pre>`:''}${node.kind==='artifact'?`<code class="node-path">${esc(record.uri)}</code>`:''}${node.kind==='work'?'<p>工作结果不等同于独立核验后的科学证据。</p>':''}<div class="graph-relations">${related.map(edge=>{const other=graph.nodes.find(n=>n.id===(edge.source===id?edge.target:edge.source));return `${esc(edge.label)} · ${esc(other?.label||'')}`}).join('<br>')||'尚无明确关联记录'}</div>${node.kind==='artifact'?`<div class="graph-detail-actions"><button id="graph-open-file">预览文件 ↗</button><a href="${esc(aurl(run,record))}" download="${esc(fileName(record.uri))}">下载原文件 ↓</a></div>`:''}`;
  $('#graph-open-file')?.addEventListener('click',()=>{showResultPanel('files');openWorkspaceFile(record.id,run)});
}
function localFolder(uri){
  let path=String(uri||'').replaceAll('\\','/'),root=String(runtimeInfo.cwd||'').replace(/\/$/,'');
  if(root&&path.startsWith(root+'/'))path=path.slice(root.length+1);
  return path.slice(0,path.lastIndexOf('/'))||'项目目录';
}
function renderFileBrowser(run){
  const artifacts=run.state?.artifacts||[],signature=JSON.stringify([run.run_id,artifacts.map(a=>[a.id,a.uri])]);
  if(signature===fileSignature&&$('#files-view').innerHTML)return;
  fileSignature=signature;filePreviewToken++;
  if(!artifacts.length){$('#files-view').innerHTML='<div class="graph-empty-state"><div><div class="empty-orbit" style="margin:auto"></div><strong>这里收纳研究生成的文件</strong><p>产物保存在项目本地目录，生成后即可在这里<br>查看、预览和下载。</p></div></div>';fileSelected=null;return}
  const folders=new Map();for(const artifact of artifacts){const folder=localFolder(artifact.uri);if(!folders.has(folder))folders.set(folder,[]);folders.get(folder).push(artifact)}
  $('#files-view').innerHTML=`<div class="file-browser"><div class="files-heading"><span>▤ 本地输出文件</span><small>${artifacts.length} 份已注册产物</small></div><div class="file-browser-body"><nav class="file-tree" aria-label="本地产物文件夹">${[...folders].map(([folder,items])=>`<details class="file-folder" open><summary title="${esc(folder)}">${esc(folder.split('/').map(part=>part.length>22?part.slice(0,10)+'…':part).join(' / '))}</summary>${items.map(a=>`<button class="file-item" data-file-id="${esc(a.id)}" title="${esc(a.uri)}"><span class="file-type-icon">${esc(fileName(a.uri).split('.').at(-1).slice(0,5).toUpperCase())}</span><span>${esc(fileName(a.uri))}</span></button>`).join('')}</details>`).join('')}</nav><section id="file-preview-pane" class="file-preview-pane" aria-live="polite"></section></div></div>`;
  $$('#files-view [data-file-id]').forEach(button=>button.onclick=()=>openWorkspaceFile(button.dataset.fileId,run));
  const selected=artifacts.find(a=>a.id===fileSelected)||artifacts[0];openWorkspaceFile(selected.id,run);
}
function parseCSV(text){
  const rows=[];let row=[],field='',quoted=false;
  for(let i=0;i<String(text).length;i++){
    const c=text[i];if(c==='"'){if(quoted&&text[i+1]==='"'){field+='"';i++}else quoted=!quoted}
    else if(c===','&&!quoted){row.push(field);field=''}
    else if(c==='\n'&&!quoted){row.push(field.replace(/\r$/,''));rows.push(row);row=[];field='';if(rows.length>=101)break}
    else field+=c;
  }
  if(field||row.length){row.push(field.replace(/\r$/,''));rows.push(row)}return rows;
}
async function openWorkspaceFile(id,run=lastRunData){
  const artifact=(run?.state?.artifacts||[]).find(a=>a.id===id);if(!artifact||!$('#file-preview-pane'))return;
  fileSelected=id;const token=++filePreviewToken,pane=$('#file-preview-pane'),type=classifyArtifact(artifact),url=aurl(run,artifact);
  $$('#files-view [data-file-id]').forEach(el=>el.classList.toggle('active',el.dataset.fileId===id));
  pane.innerHTML=`<h3>${esc(fileName(artifact.uri))}</h3><div class="file-location">${esc(artifact.uri)}</div><div class="file-preview-actions"><a href="${esc(url)}" download="${esc(fileName(artifact.uri))}">下载原文件 ↓</a><button id="file-show-graph">查看关联图谱</button></div><div id="file-preview-body" class="file-preview-body">正在读取文件…</div>`;
  $('#file-show-graph').onclick=()=>{showResultPanel('graph');selectGraphNode('artifact:'+id,buildEvidenceGraph(run),run)};
  const body=$('#file-preview-body');
  if(type==='image'){body.innerHTML=`<button class="view-btn" id="file-enlarge">放大预览 ↗</button><img src="${esc(url)}" alt="${esc(fileName(artifact.uri))}">`;$('#file-enlarge').onclick=()=>openVisualPreview(artifact,run);body.querySelector('img').onerror=()=>body.textContent='图像预览加载失败，请下载原文件。';return}
  if(type==='pdf'){body.innerHTML=`<iframe title="PDF 报告预览" src="${esc(url)}"></iframe><p>若浏览器无法预览 PDF，请下载原文件。</p>`;return}
  if(type==='html'){body.innerHTML=`<iframe title="HTML 图表 / 报告预览" sandbox="allow-scripts" referrerpolicy="no-referrer" src="${esc(url)}"></iframe><p>独立 HTML 可交互预览；依赖外部文件的报告请下载完整依赖后打开。</p>`;return}
  if(type==='binary'){body.textContent='此文件支持下载保存，请用对应的 R、分析软件或解压工具打开。';return}
  if(type==='structure'){body.innerHTML='<p>已保存三维结构坐标文件。</p><button class="view-btn" id="file-structure">打开交互式 3D 预览 ↗</button>';$('#file-structure').onclick=()=>openVisualPreview(artifact,run);return}
  try{
    const loaded=await loadArtifact(run,artifact);if(token!==filePreviewToken||!body.isConnected)return;
    if(loaded.error)throw Error(loaded.error);
    if(type==='markdown')body.innerHTML=markdownPreview(loaded.raw||'');
    else if(/\.csv$/i.test(artifact.uri)){const rows=parseCSV(loaded.raw||'');body.innerHTML=`<table>${rows.map((row,i)=>`<tr>${row.map(cell=>`<${i?'td':'th'}>${esc(cell)}</${i?'td':'th'}>`).join('')}</tr>`).join('')}</table>${rows.length>=101?'<p>预览前 100 行数据，完整内容请下载。</p>':''}`}
    else body.innerHTML=`<pre>${esc(loaded.parsed?JSON.stringify(loaded.parsed,null,2):(loaded.raw||'空文件'))}</pre>`;
  }catch(error){if(token===filePreviewToken&&body.isConnected)body.textContent='预览失败：'+error.message+'。可下载原文件查看。'}
}
window.addEventListener('hashchange',()=>{
  if(location.hash.startsWith('#session/')){
    try{loadSession(decodeURIComponent(location.hash.slice(9)),true)}catch(error){$('#notice').textContent='会话链接无效：'+error.message}
  }else goHome(false);
});

// Persist presentation preferences only; history records remain on the backend.
function historyPref(key,fallback){try{return JSON.parse(localStorage.getItem(key))??fallback}catch{return fallback}}
function saveHistoryPref(key,value){try{localStorage.setItem(key,JSON.stringify(value))}catch{}}
const expandedHistory=new Set(historyPref('bioma.history.expanded',[]));
const sidebarToggle=document.createElement('button');sidebarToggle.id='history-collapse';sidebarToggle.title='收起 / 展开历史侧栏';sidebarToggle.setAttribute('aria-label',sidebarToggle.title);$('.brand').prepend(sidebarToggle);
const sidebarResize=document.createElement('div');sidebarResize.id='history-resize';sidebarResize.tabIndex=0;sidebarResize.setAttribute('role','separator');sidebarResize.setAttribute('aria-label','调节历史侧栏宽度');sidebarResize.setAttribute('aria-orientation','vertical');sidebarResize.setAttribute('aria-valuemin','180');sidebarResize.setAttribute('aria-valuemax','420');$('.brand').append(sidebarResize);
let sidebarWidth=Number(historyPref('bioma.history.width',240))||240;
function resizeHistory(width){sidebarWidth=Math.min(420,Math.max(180,width));$('.app').style.setProperty('--history-width',sidebarWidth+'px');sidebarResize.setAttribute('aria-valuenow',String(sidebarWidth));saveHistoryPref('bioma.history.width',sidebarWidth)}
function collapseHistory(collapsed){$('.app').classList.toggle('history-collapsed',collapsed);sidebarToggle.textContent=collapsed?'☰':'‹';sidebarToggle.setAttribute('aria-expanded',String(!collapsed));saveHistoryPref('bioma.history.collapsed',collapsed)}
resizeHistory(sidebarWidth);collapseHistory(historyPref('bioma.history.collapsed',false));
sidebarToggle.onclick=()=>collapseHistory(!$('.app').classList.contains('history-collapsed'));
let historyDrag=null;
sidebarResize.onpointerdown=e=>{historyDrag={x:e.clientX,width:sidebarWidth};sidebarResize.setPointerCapture(e.pointerId);e.preventDefault()};
sidebarResize.onpointermove=e=>{if(historyDrag)resizeHistory(historyDrag.width+e.clientX-historyDrag.x)};
sidebarResize.onpointerup=sidebarResize.onpointercancel=()=>historyDrag=null;
sidebarResize.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();resizeHistory(e.key==='Home'?180:e.key==='End'?420:sidebarWidth+(e.key==='ArrowRight'?20:-20))}};
loadSessions=async function(){
  const target=$('#history-list');
  try{
    const data=await apiJson('api/sessions');
    target.innerHTML=(data.sessions||[]).map(s=>`<details class="history-card ${s.session_id===sessionId?'active':''}" data-history="${esc(s.session_id)}" ${expandedHistory.has(s.session_id)?'open':''}><summary><span class="history-title">${esc(s.title||'未命名研究')}</span><span class="history-status ${s.status==='completed'?'status-good':s.status==='failed'?'status-bad':''}">${esc(statusText(s.status))}</span></summary><div class="history-body"><p>${esc(s.title||'未命名研究')}</p><small>${esc(s.turn_count==null?'—':s.turn_count)} 轮对话 · ${esc(s.session_id.slice(0,8))}</small><button data-session="${esc(s.session_id)}">打开研究 ↗</button></div></details>`).join('')||'<p>暂无历史研究</p>';
    target.querySelectorAll('[data-history]').forEach(el=>el.ontoggle=()=>{if(el.open)expandedHistory.add(el.dataset.history);else expandedHistory.delete(el.dataset.history);saveHistoryPref('bioma.history.expanded',[...expandedHistory])});
    target.querySelectorAll('[data-session]').forEach(el=>el.onclick=()=>loadSession(el.dataset.session,true));
  }catch(error){target.textContent='历史加载失败：'+error.message}
};

// Reports are deliverables, independent of the older evidence-report kind.
const legacyRenderReport=renderReport;
renderReport=function(run){
  const reports=loadedArtifacts.filter(o=>o.type==='pdf'||(o.type==='markdown'&&(/report/i.test(o.a.uri)||o.a.kind==='evidence-report-markdown')));
  if(!reports.length){legacyRenderReport(run);return}
  const target=$('#report-view');
  target.innerHTML='<h2>详细报告</h2><div class="report-choices"></div><div class="report-body"></div>';
  for(const o of reports){
    const b=document.createElement('button');b.className='view-btn';b.textContent=fileName(o.a.uri);target.querySelector('.report-choices').append(b);
    b.onclick=()=>{target.querySelector('.report-body').innerHTML=`<p><a href="${esc(o.u)}" download="${esc(fileName(o.a.uri))}">下载报告 ↓</a></p>${o.type==='pdf'?`<iframe class="report-pdf" title="PDF 详细报告" src="${esc(o.u)}"></iframe>`:`<article class="markdown">${o.error?'正文读取失败，请下载报告。':markdownPreview(o.raw||'')}</article>`}`};
  }
  target.querySelector('button').click();
};
const legacySummary=renderSummaryPanel;
renderSummaryPanel=function(run,graph){
  legacySummary(run,graph);
  if(!run.state)return;
  const observations=run.state.observations||[],evidence=run.state.evidence||[],arts=run.state.artifacts||[];
  const box=document.createElement('section');box.className='block research-delivery';
  const summaries=loadedArtifacts.filter(o=>o.parsed&&/\/(summary|confidence)\.json$/i.test(o.a.uri));
  const attribution=run.execution_attribution||{record_status:'legacy_run_without_start_snapshot'};
  box.innerHTML=`<h3>计算结果与交付</h3><p>${observations.length} 条工作记录 · ${arts.length} 份已登记产物 · ${evidence.length} 条正式证据。</p><p>${evidence.length?'证据内容与评估记录见研究图谱。':'当前产物已随工作结果持久化；尚无正式证据入库及 claim 支撑评估。交付审阅通过不等于科学主张成立。'}</p><div class="delivery-links"></div><details><summary>运行归因与宿主输入</summary><pre class="raw-box">${esc(JSON.stringify(attribution,null,2))}</pre></details>${summaries.map(o=>`<details open><summary>${esc(fileName(o.a.uri))} · 原始计算结果</summary><pre class="raw-box">${esc(JSON.stringify(o.parsed,null,2))}</pre></details>`).join('')}<details><summary>工作结果的审阅与适用范围</summary><pre class="raw-box">${esc(JSON.stringify(observations.map(o=>({id:o.id,authority:o.value?.authority,method:o.value?.provenance?.method_id,limitations:o.value?.payload?.limitations,delivery_review:o.value?.payload?.delivery_review})),null,2))}</pre></details>`;
  $('#summary-view').prepend(box);
  for(const a of arts.filter(a=>['structure','pdf'].includes(classifyArtifact(a))||/report\.md$/i.test(a.uri))){
    const b=document.createElement('button');b.className='view-btn';b.textContent=classifyArtifact(a)==='structure'?'打开三维结构 ↗':fileName(a.uri)+' ↗';box.querySelector('.delivery-links').append(b);
    b.onclick=()=>{if(classifyArtifact(a)==='structure')openVisualPreview(a,run);else showResultPanel('report')};
  }
};
async function loadFeaturedCases(){
  const target=$('#featured-cases');
  try{
    const catalog=await apiJson('api/featured-cases'),entries=catalog.cases||[];
    target.innerHTML='';
    if(!entries.length){target.innerHTML='<div class="empty">当前没有可查看或可重新运行的案例。</div>';return}
    await Promise.all(entries.map(async entry=>{
      const card=document.createElement('button');card.type='button';card.className='example-card featured-case';card.dataset.case=entry.key;
      const count=entry.artifact_count==null?'产物待同步':`${entry.artifact_count} 份已登记产物`;
      const fallback=entry.prompt?`${entry.reason||'本机执行记录不可用'} · 载入示例任务 ↗`:entry.reason||'执行记录不可用';
      card.innerHTML=`<div class="example-visual real-cover"></div><div class="example-copy"><span class="example-tag">已执行案例 · ${esc(statusText(entry.status))}</span><h3>${esc(entry.title)}</h3><p>${esc(entry.scope)}</p><small>${esc(entry.available?count+' · 查看结果 ↗':fallback)}</small></div>`;target.append(card);
      const coverBox=card.querySelector('.real-cover');
      if(!entry.available){
        coverBox.classList.add('cover-unavailable');coverBox.textContent=entry.prompt?'执行记录暂不可用 · 可载入任务重新运行':'历史案例记录暂不可用';
        if(entry.prompt){card.onclick=()=>{$('#home-query').value=entry.prompt;$('#home-query').focus();$('#home-query').scrollIntoView({behavior:'smooth',block:'center'})}}
        else{card.disabled=true;card.title=entry.reason||'本机没有该历史运行记录'}
        return
      }
      const run={run_id:entry.run_id},cover=entry.cover_artifact,artifact=entry.target_artifact;
      if(cover){
        const image=document.createElement('img');image.alt=entry.title+' · '+entry.cover;image.loading='lazy';
        image.onerror=()=>{coverBox.classList.add('cover-unavailable');coverBox.textContent='封面暂时无法加载'};
        image.src=aurl(run,cover);coverBox.append(image);
      }else{coverBox.classList.add('cover-unavailable');coverBox.textContent='暂无封面预览'}
      card.onclick=async()=>{
        card.disabled=true;card.setAttribute('aria-busy','true');$('#notice').textContent='正在打开案例记录…';
        const loaded=await loadSession(entry.session_id,true,entry.run_id);
        card.disabled=false;card.removeAttribute('aria-busy');
        if(!loaded||currentRun!==entry.run_id)return;
        $('#notice').textContent='';
        const artifactType=classifyArtifact(artifact);
        if(artifactType==='structure')openVisualPreview(artifact,lastRunData);
        else if(artifactType==='pdf')showResultPanel('report');
        else if(artifact)selectArtifact(artifact.id);
        else showResultPanel('summary');
      };
    }));
  }catch(error){target.textContent='案例目录加载失败：'+error.message}
}
(async function initializeWorkspace(){
  resetResults();showResultPanel('graph');workspaceMode(true);
  try{
    await loadRuntime();await loadSessions();await loadFeaturedCases();
    if(location.hash.startsWith('#session/'))await loadSession(decodeURIComponent(location.hash.slice(9)),true);
  }catch(error){$('#backend-health').textContent='● 后端连接失败';$('#backend-chip').textContent='BACKEND · ERROR';$('#notice').textContent='接口连接失败：'+error.message;}
})();
