'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="radar-token"]').content;
const state = {view:'radar', days:'', changed:false, fresh:false, old:false, page:1, total:0, pageSize:30, detail:null, pendingRun:null};
const labels = {radar:'机会雷达',saved:'我的关注',pending:'待核验',history:'历年归档',coverage:'全国覆盖',sources:'信源与运行',imports:'导入线索'};
const kinds = {date:'日期明确',datetime:'时刻明确',rolling:'招满即止',long_term:'长期招聘',unannounced:'截止未公布',conflict:'日期冲突'};
const statuses = {success:'本轮完成',partial:'待续跑',no_recruitment:'未发现校招内容',error:'采集异常',unverified:'尚未验证',queued:'等待同步',running:'同步中',failed:'同步失败'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const readableError = value => String(value||'').replaceAll('ConnectError','连接中断，请稍后重试或检查网络').replaceAll('ConnectTimeout','连接超时').replaceAll('ReadTimeout','读取超时').replaceAll('HTTP 412','站点拒绝请求（HTTP 412）');
const safeURL = value => /^https?:\/\//i.test(value || '') ? esc(value) : '#';
const when = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '尚未记录';
let toastTimer;
function toast(message,error=false){ const box=$('#toast'); box.textContent=message;box.className='toast'+(error?' error':'');box.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.hidden=true,5000); }
async function api(url,options={}){
  const headers={'X-Radar-Token':token,...options.headers};
  if(options.body && !(options.body instanceof FormData)) headers['Content-Type']='application/json';
  const response=await fetch(url,{...options,headers});
  const content=await response.json();
  if(!response.ok) throw new Error(typeof content.detail==='string'?content.detail:'请求失败，请检查字段');
  return content;
}
function params(){
  const query=new URLSearchParams();
  for(const [key,value] of new FormData($('#filters'))) if(String(value).trim()) query.append(key,String(value).trim());
  query.set('page',state.page);query.set('page_size',state.pageSize);
  if(state.days!=='') query.set('days',state.days);
  if(state.changed) query.set('changed','1');
  if(state.fresh) query.set('new','1');
  if($('#unapplied').checked) query.set('applied','0');
  if(state.view==='saved') query.set('saved','1');
  if(state.view==='pending'){query.set('review','pending');if(state.old)query.set('include_old','1');}
  if(state.view==='history') query.set('history','1');
  return query;
}
function deadlineHTML(row){
  const deadline=row.deadline||{};let display=kinds[deadline.kind]||'待提取';
  if(deadline.value){
    display=row.deadline_display|| (deadline.kind==='datetime'?new Date(deadline.value).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):deadline.value.replaceAll('-','.'));
  }
  let caption=deadline.kind==='date'?'具体时刻未公布':deadline.kind==='datetime'?'北京时间':'';
  const left=row.days_left;
  let countdown=row.status==='closed'?'已截止':left===null?'':left<0?'已截止':left===0?'今天截止':`${left} 天后截止`;
  return `<div class="deadline-date ${deadline.value?'':'deadline-small'}">${esc(display)}</div>${caption?`<div class="deadline-caption">${esc(caption)}</div>`:''}${countdown?`<div class="countdown ${left>=0&&left<=3?'urgent':''}">${esc(countdown)}</div>`:''}`;
}
async function renderList(){
  const query=params();
  const data=await api('/api/opportunities?'+query);
  state.total=data.total;
  $('#result-count').textContent=`${data.total} 条`+(data.hidden_old?` · ${data.hidden_old} 条往届已折叠`:'');
  $('#opportunity-rows').innerHTML=data.items.map(row=>`<tr>
    <td><div class="company-name"><button data-detail="${esc(row.id)}">${esc(row.employer)}</button></div><div class="record-title" title="${esc(row.title)}">${esc(row.title)}</div><div class="record-meta"><span class="tag">${esc(row.industry)}</span><span class="tag gray">${esc(row.batch)}</span>${row.scope!=='本批次'?`<span class="tag amber">${esc(row.scope)}</span>`:''}</div></td>
    <td><div class="location">${esc(row.locations.slice(0,3).join(' · ')||'详见公告')}${row.locations.length>3?' 等':''}</div><div class="record-meta">${row.cohorts.length?row.cohorts.map(y=>`<span class="tag gray">${esc(y)} 届</span>`).join(''):'<span class="tag gray">届别待确认</span>'}</div></td>
    <td>${deadlineHTML(row)}</td><td><span class="tag ${row.review_status==='pending'?'amber':''}">${row.review_status==='pending'?'待核验':row.review_status==='manual'?'人工确认':'自动校验'}</span>${row.data_stale?'<div class="deadline-caption">需要复查</div>':''}<div class="deadline-caption">${row.status==='closed'?'已截止 / 关闭':''}</div></td>
    <td>${state.view==='pending'?`<button class="text-button" data-reject="${esc(row.id)}">排除</button>`:''}<button class="save-button ${row.personal.saved?'saved':''}" data-save="${esc(row.id)}" data-value="${!row.personal.saved}" aria-label="${row.personal.saved?'取消关注':'关注'}${esc(row.employer)}">${row.personal.saved?'★':'☆'}</button><button class="apply-button ${row.personal.applied?'applied':''}" data-apply="${esc(row.id)}" data-value="${!row.personal.applied}">${row.personal.applied?'已投递':'未投递'}</button></td></tr>`).join('');
  $('#empty-state').hidden=data.total!==0;
  const isPending=state.view==='pending', isSaved=state.view==='saved';
  $('#empty-title').textContent=isPending?'暂时没有待核验线索':isSaved?'把值得投的机会收在这里':data.total===0?'当前没有匹配的机会':'';
  $('#empty-message').textContent=isPending?'来源明确、证据完整的公告会自动进入可投列表。':isSaved?'在机会旁点击星标即可关注，投递状态也会保存在本机。':'可以调整筛选、查看历年归档，或同步来源收集新的公告。部分公告需要先核验，才会出现在这里。';
  $('#empty-action').textContent=isPending?'导入一份线索 ↗':isSaved?'浏览机会雷达 ↗':'同步来源 ↗';
  $('#previous').disabled=state.page<=1;$('#next').disabled=state.page*state.pageSize>=data.total;
  $('#page-number').textContent=`${state.page} / ${Math.max(1,Math.ceil(data.total/state.pageSize))}`;
  const exported=new URLSearchParams(query);exported.delete('page');exported.delete('page_size');
  $('#export-csv').href='/exports/opportunities.csv?'+exported;$('#export-ics').href='/exports/deadlines.ics?'+exported;
  $('#list-notice').hidden=!isPending&&state.view!=='history';
  $('#list-notice').textContent=isPending?'这些记录尚未满足发布要求。点击企业查看原因和原文；待核验日期不会导入日历。':'这里保留往届和已截止活动。请检查届别、批次和官方公告，历史记录不代表目前仍可投递。';
}
async function refreshSummary(){
  const [stats,health]=await Promise.all([api('/api/stats'),api('/api/health')]);
  $('#stat-active').textContent=stats.active;$('#stat-due').textContent=stats.due_soon;$('#stat-pending').textContent=stats.pending;
  $('#nav-active').textContent=stats.active;$('#nav-pending').textContent=stats.pending;
  const usable=health.sources.filter(x=>x.usable);
  $('#stat-sources').textContent=`${usable.length} / ${health.sources.filter(x=>x.enabled).length}`;
  $('#stat-sources-note').textContent=`${health.sources.filter(x=>x.enabled&&!x.usable).length} 个未取得校招内容 · ${health.sources.filter(x=>x.enabled&&x.status==='partial').length} 个待续跑`;
  $('#updated-note').textContent=stats.last_checked?'最近核验 '+when(stats.last_checked):'尚未采集 · 数据保存在本机';
  const cohort=$('[name="cohort"]'),current=cohort.value;
  cohort.innerHTML='<option value="">全部届别</option>'+stats.cohorts.map(y=>`<option value="${esc(y)}">${esc(y)} 届</option>`).join('');cohort.value=current;
  const industry=$('[name="industry"]'),previous=industry.value;
  industry.innerHTML='<option value="">全部行业</option>'+stats.industries.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');industry.value=previous;
  $('#sources-list').innerHTML=health.sources.map(source=>`<article class="source-card"><div class="source-top"><h3>${esc(source.name)}</h3><span class="tag ${source.status==='error'?'red':source.status==='partial'?'amber':'gray'}">${esc(statuses[source.status]||source.status)}</span></div><p>网络策略：${esc(source.network_mode)} · 最近读取：${when(source.last_success)} · 已保存 ${source.campus_count} 条校招（含历届） / ${source.verified_count} 条核验通过</p>${source.last_error?`<p class="error-text">${esc(readableError(source.last_error))}</p>`:''}${source.cursor?.backlog?.length?`<p>待续跑 ${source.cursor.backlog.length} 条详情</p>`:''}${source.network_attempts?.length?`<details><summary>最近网络尝试</summary>${source.network_attempts.slice(-6).map(a=>`<p>${esc(a.route)} · ${esc(a.host)} · ${esc(a.status_code||a.error)} · ${a.duration_ms}ms</p>`).join('')}</details>`:''}<a href="${safeURL(source.url)}" target="_blank" rel="noopener noreferrer">官方入口 ↗</a> <button class="button compact ghost" data-sync-source="${esc(source.id)}">重试此来源</button></article>`).join('');
  $('#providers-list').innerHTML=health.providers.map(p=>`<div class="provider-item"><span>${esc(p.name)} <span class="tag ${p.enabled&&!p.configured?'amber':'gray'}">${!p.enabled?'未启用':p.pause?'暂停中':!p.configured?'缺少凭证':p.allowed_models.length?'已配置':'费用策略阻止'}</span></span><code>${esc(p.models.join(' / '))}</code></div>`).join('');
  $('#model-note').textContent=`${health.allow_paid?'已允许付费':'默认禁止额外付费'} · ${health.pending_documents} 份待核验文档 · 共 ${health.model_calls} 次模型请求 · 付费预算预留 $${Number(health.reserved_cost_usd).toFixed(4)}。模型不可用时，采集与规则解析仍可继续。`;
}
async function showView(view){
  state.view=view;state.page=1;state.days='';state.changed=false;state.fresh=false;
  document.querySelectorAll('.nav-item').forEach(item=>item.classList.toggle('active',item.dataset.view===view));
  document.querySelectorAll('[data-days]').forEach(item=>item.classList.toggle('selected',item.dataset.days===''));
  $('#changed-filter').classList.remove('selected');$('#new-filter').classList.remove('selected');$('#old-filter').hidden=view!=='pending';document.querySelector('.hero').hidden=['sources','imports','coverage'].includes(view);document.querySelector('.stats').hidden=['sources','imports','coverage'].includes(view);document.querySelectorAll('[data-days]').forEach(x=>x.hidden=view==='history');
  $('#breadcrumb').textContent=labels[view];
  $('#page-heading').innerHTML=view==='radar'?'下一次机会，<em>提前看见。</em>':esc(labels[view])+'<em>，有迹可循。</em>';
  $('#list-title').textContent={radar:'投递窗口',saved:'我的关注',pending:'待核验线索',history:'历年招聘记录'}[view]||'投递窗口';
  $('#list-panel').hidden=['sources','imports','coverage'].includes(view);$('#sources-panel').hidden=view!=='sources';$('#imports-panel').hidden=view!=='imports';$('#coverage-panel').hidden=view!=='coverage';
  if(!['sources','imports','coverage'].includes(view)) await renderList();
  await refreshSummary();
  if(view==='sources') await renderRuns();
  if(view==='coverage') await renderCoverage();
}
async function renderRuns(){
  const runs=await api('/api/runs');
  $('#runs-list').innerHTML=runs.length?runs.map(run=>`<div class="run-item"><span class="tag ${['partial','failed'].includes(run.status)?'amber':''}">${esc(statuses[run.status]||run.status)}</span><small>${when(run.started_at||run.created_at)}</small><small>${run.summary?.sources?.length||0} 个来源 · ${esc(run.id.slice(0,8))}</small></div>`).join(''):'<p class="muted">尚未执行同步。</p>';
}
async function sync(sourceId=null){
  if(state.pendingRun) return;
  $('#sync-button').disabled=true;
  try{const run=await api('/api/sync',{method:'POST',body:JSON.stringify(sourceId?{source_ids:[sourceId]}:{})});state.pendingRun=run.id;$('#run-banner').hidden=false;$('#run-banner').textContent='同步任务已排队。会独立巡查各个来源，单个来源失败不会影响其他来源。';pollRun();}
  catch(error){toast(error.message,true);$('#sync-button').disabled=false;}
}
async function pollRun(){
  try{
    const run=await api('/api/runs/'+state.pendingRun);
    if(['success','partial','failed'].includes(run.status)){
      state.pendingRun=null;$('#sync-button').disabled=false;
      const failed=(run.summary.sources||[]).filter(s=>['error','failed'].includes(s.status)).length;const partial=(run.summary.sources||[]).filter(s=>s.status==='partial').length;
      const empty=(run.summary.sources||[]).filter(s=>s.status==='no_recruitment').length;$('#run-banner').textContent=run.status==='failed'?'同步失败，详情见信源与运行。':`本轮结束：${failed} 个失败 · ${partial} 个待续跑 · ${empty} 个未发现校招内容。待核验记录不进入临期日历。`;
      await refreshSummary();if(!['sources','imports','coverage'].includes(state.view)) await renderList();if(state.view==='sources') await renderRuns();return;
    }
    $('#run-banner').textContent=run.status==='running'?'正在巡查官方来源、保存原文并校验日期。你可以继续浏览已有数据。':'同步任务等待执行，请保持本地服务运行。';
  }catch(error){toast(error.message,true);}
  setTimeout(pollRun,3000);
}
async function openDetail(id){
  const row=await api('/api/opportunities/'+id);state.detail=row;state.employers=await api('/api/employers');
  state.reviewTargets=[];
  if((row.review_status==='pending'||row.needs_review)&&row.extraction.action!=='recruitment'){
    const params=new URLSearchParams({history:'1',review:'all',employer:row.employer,page_size:'100'});
    if(row.cohorts.length)params.set('cohort',row.cohorts.join(','));
    const candidates=await api('/api/opportunities?'+params);
    state.reviewTargets=candidates.items.filter(x=>x.id!==row.id&&x.employer_id===row.employer_id&&JSON.stringify(x.cohorts)===JSON.stringify(row.cohorts));
  }
  const dl=row.deadline||{};
  const fields=[['企业性质',row.org_type==='soe'?'国有企业（有来源依据）':'尚待确认'],['毕业届别',row.cohorts.map(y=>y+' 届').join(' / ')||'待确认'],['批次',row.batch],['地点',row.locations.join(' / ')||'详见原文'],['学历',row.education.join(' / ')||'详见原文'],['专业',row.majors.join(' / ')||'详见原文']];
  $('#detail-body').innerHTML=`<h2>${esc(row.employer)}</h2><p class="muted">${esc(row.title)}</p><div class="record-meta"><span class="tag ${row.review_status==='pending'?'amber':''}">${row.review_status==='pending'?'待核验':row.review_status==='manual'?'人工确认':'自动校验'}</span><span class="tag gray">${esc(row.scope)}</span></div><div class="detail-grid">${fields.map(([k,v])=>`<div class="detail-field"><span>${esc(k)}</span>${esc(v)}</div>`).join('')}</div><div class="detail-field"><span>投递截止</span>${deadlineHTML(row)}</div>${dl.quote?`<blockquote class="evidence">${esc(dl.quote)}</blockquote>`:''}${dl.year_basis?`<p class="muted">年份判断依据：${esc(dl.year_basis)}</p>`:''}<div class="detail-actions"><a class="button dark" href="${safeURL(row.apply_url||row.official_url)}" target="_blank" rel="noopener noreferrer">${row.apply_url?'前往投递':'查看官方公告'} ↗</a><a class="button ghost" href="${safeURL(row.official_url)}" target="_blank" rel="noopener noreferrer">公告原文 ↗</a></div>${row.review_reasons.length?`<section class="detail-section"><h3>需要确认的内容</h3><ul>${row.review_reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul></section>`:''}<section class="detail-section"><h3>我的投递</h3><div class="detail-actions"><button class="button ghost" data-save="${esc(row.id)}" data-value="${!row.personal.saved}">${row.personal.saved?'★ 已关注':'☆ 关注机会'}</button><button class="button ghost" data-apply="${esc(row.id)}" data-value="${!row.personal.applied}">${row.personal.applied?'✓ 已投递':'标记已投递'}</button></div><textarea id="personal-notes" rows="3" aria-label="个人备注" placeholder="记录你的投递进度和下一步，只保存在本机。">${esc(row.personal.notes)}</textarea><button class="button dark" id="save-notes">保存备注</button></section>${(row.review_status==='pending'||row.needs_review)?reviewHTML(row):''}${reviewHistoryHTML(row)}<section class="detail-section"><h3>日期与变更记录</h3>${row.events.map(event=>`<p>${when(event.created_at)} · ${esc(({new:'首次收录',extend:'延期',reopen:'重新开放',close:'提前关闭',deadline_changed:'日期更新',document_updated:'原文更新'})[event.kind]||event.kind)}${event.new_value?' · '+esc(event.new_value):''}</p>`).join('')||'<p>暂无变更。</p>'}</section><section class="detail-section"><h3>原文版本 · ${row.documents.length} 份</h3>${row.documents.map(doc=>`<details><summary class="muted">${esc(doc.title)} · ${when(doc.fetched_at)}</summary><p><a href="${safeURL(doc.url)}" target="_blank" rel="noopener noreferrer">${esc(doc.url)}</a></p><pre>${esc(doc.text)}</pre></details>`).join('')}</section><p class="muted">最近核验 ${when(row.last_checked)}${row.employer_proof_url?' · 企业依据：<a href="'+safeURL(row.employer_proof_url)+'" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>':''}</p>`;
  const dialog=$('#detail-dialog');if(!dialog.open)dialog.showModal();
}
function reviewHistoryHTML(row){
  if(!row.reviews?.length)return '';
  return `<section class="detail-section"><h3>人工核验与合并记录</h3>${row.reviews.map(r=>`<p>${when(r.created_at)} · ${esc(({approve:'人工确认',reject:'人工排除',merge:'重复合并'})[r.decision]||r.decision)} · ${r.payload.use_snapshot?'已保存快照':'在线原文 / 本地操作'}</p>${r.payload.resolution_note?`<p>${esc(r.payload.resolution_note)}</p>`:''}${r.payload.publication_evidence?`<p>年份依据：${esc(r.payload.publication_evidence)}</p>`:''}${(r.payload.evidence||[]).map(e=>`<p>${esc(e.location)} · ${esc(e.scope)}</p><blockquote class="evidence">${esc(e.quote)}</blockquote>`).join('')}`).join('')}</section>`;
}
function reviewExtraHTML(row){
  const attachments=row.documents.flatMap(d=>(d.attachments||[]).filter(a=>a.url));
  return `<label class="full"><span><input type="checkbox" name="use_snapshot"> 使用已保存快照核验（不表示官网当前仍有效）</span><select name="snapshot_id">${row.documents.map(d=>`<option value="${esc(d.id)}">${esc(d.title)} · 获取于 ${when(d.fetched_at)}</option>`).join('')}</select></label><label>发布日期<input name="published_at" type="date" value="${esc(row.published_at||'')}"></label><label>补充发布日期的依据<input name="publication_evidence" placeholder="如原文落款或海报右上角日期"></label><label>截止适用对象<input name="scope" value="${esc(row.scope)}"></label><label>附件人工转录<select name="attachment_url"><option value="">直接核验正文证据</option>${attachments.map(a=>`<option value="${esc(a.url)}">${esc(a.label||a.url)}</option>`).join('')}</select></label><label class="full">转录位置<input name="location" placeholder="如 PDF 第2页第3段、海报右下角"></label><label class="full">人工判断说明<textarea name="resolution_note" rows="2" placeholder="说明年份、日期冲突或类型修正的判断依据"></textarea></label><label>确认所属母集团<select name="parent"><option value="">不变更集团关系</option>${(state.employers||[]).filter(e=>e.id!==row.employer_id).map(e=>`<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('')}</select></label><label>集团隶属关系依据<input name="relationship_proof_url" type="url" placeholder="官网集团结构或企业介绍地址"></label>${attachments.length?`<div class="full detail-field">${attachments.map(a=>`<a href="${safeURL(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.label||'原始附件')} ↗</a>`).join(' · ')}</div>`:''}`;
}
function reviewHTML(row){
  const dl=row.extraction.deadline||{};
  return `<section class="detail-section"><h3>人工核验</h3><p>默认读取最新原文；也可显式选择已保存快照。图片或扫描件需绑定附件并填写转录位置，补充年份及消解冲突需说明依据。</p><form id="review-form" class="review-form">${reviewExtraHTML(row)}${row.extraction.action!=='recruitment'?`<label class="full">关联原招聘活动<select name="target"><option value="">尝试按公告标题和引用自动关联</option>${state.reviewTargets.map(x=>`<option value="${esc(x.id)}">${esc(x.title)} · ${esc(x.batch)}</option>`).join('')}</select></label>`:''}<label>招聘主体<input name="employer" value="${esc(row.employer)}" required></label><label>毕业届别（逗号分隔）<input name="cohorts" value="${esc(row.cohorts.join(','))}" required></label><label>批次<input name="batch" value="${esc(row.batch)}"></label><label>招聘季<select name="season"><option value="">未明确</option><option ${row.season==='秋招'?'selected':''}>秋招</option><option ${row.season==='春招'?'selected':''}>春招</option></select></label><label>截止类型<select name="kind">${Object.entries(kinds).map(([key,value])=>`<option value="${key}" ${dl.kind===key?'selected':''}>${value}</option>`).join('')}</select></label><label>截止值<input name="value" value="${esc(dl.value||'')}" placeholder="2026-10-31 或带时区的完整时间"></label><label class="full">官方公告地址<input name="official_url" type="url" value="${esc(row.official_url)}" required></label><label class="full">企业国有性质依据地址<input name="proof" type="url" value="${esc(row.employer_proof_url)}" placeholder="官网企业介绍或权威名录地址"></label><label class="full">截止原文证据<textarea name="quote" rows="3">${esc(dl.quote||'')}</textarea></label><div class="detail-actions"><button class="button dark" type="submit">保存人工核验</button><button class="button ghost" type="button" id="reject-record">排除此线索</button></div></form></section>`;
}
document.addEventListener('click',async event=>{
  const target=event.target.closest('button');if(!target)return;
  try{
    if(target.dataset.syncSource)await sync(target.dataset.syncSource);
    if(target.dataset.reject){await api('/api/reviews/'+target.dataset.reject,{method:'POST',body:JSON.stringify({decision:'reject'})});await renderList();await refreshSummary();toast('已排除，后续同步会保留该决定');}
    if(target.dataset.view)await showView(target.dataset.view);
    if(target.dataset.detail)await openDetail(target.dataset.detail);
    if(target.dataset.save||target.dataset.apply){
      const id=target.dataset.save||target.dataset.apply;const key=target.dataset.save?'saved':'applied';
      await api('/api/opportunities/'+id+'/state',{method:'PATCH',body:JSON.stringify({[key]:target.dataset.value==='true'})});
      if($('#detail-dialog').open&&state.detail?.id===id)await openDetail(id);
      if(!['sources','imports','coverage'].includes(state.view))await renderList();await refreshSummary();
    }
    if(target.hasAttribute('data-days')){state.days=target.dataset.days;state.page=1;document.querySelectorAll('[data-days]').forEach(x=>x.classList.toggle('selected',x===target));await renderList();}
  }catch(error){toast(error.message,true);}
});
$('#filters').addEventListener('submit',event=>{event.preventDefault();state.page=1;renderList().catch(e=>toast(e.message,true));});
$('#filters').addEventListener('reset',()=>setTimeout(()=>{state.page=1;renderList().catch(e=>toast(e.message,true));},0));
$('#unapplied').addEventListener('change',()=>{state.page=1;renderList().catch(e=>toast(e.message,true));});
$('#new-filter').addEventListener('click',async()=>{state.fresh=!state.fresh;state.page=1;$('#new-filter').classList.toggle('selected',state.fresh);try{await renderList();}catch(e){toast(e.message,true);}});
$('#changed-filter').addEventListener('click',async()=>{state.changed=!state.changed;state.page=1;$('#changed-filter').classList.toggle('selected',state.changed);try{await renderList();}catch(e){toast(e.message,true);}});
$('#advanced-toggle').addEventListener('click',()=>{$('#advanced').hidden=!$('#advanced').hidden;$('#advanced-toggle').setAttribute('aria-expanded',!$('#advanced').hidden);});
$('#previous').addEventListener('click',()=>{state.page--;renderList().catch(e=>toast(e.message,true));});
$('#next').addEventListener('click',()=>{state.page++;renderList().catch(e=>toast(e.message,true));});
$('#sync-button').addEventListener('click',()=>sync());
$('#old-filter').addEventListener('click',()=>{state.old=!state.old;state.page=1;$('#old-filter').classList.toggle('selected',state.old);renderList().catch(e=>toast(e.message,true));});
$('#empty-action').addEventListener('click',()=>state.view==='pending'?showView('imports'):state.view==='saved'?showView('radar'):sync());
$('#close-detail').addEventListener('click',()=>$('#detail-dialog').close());
$('#detail-dialog').addEventListener('click',async event=>{
  try{
    if(event.target.id==='save-notes'){await api('/api/opportunities/'+state.detail.id+'/state',{method:'PATCH',body:JSON.stringify({notes:$('#personal-notes').value})});toast('备注已保存在本机');}
    if(event.target.id==='reject-record'){await api('/api/reviews/'+state.detail.id,{method:'POST',body:JSON.stringify({decision:'reject'})});$('#detail-dialog').close();await renderList();await refreshSummary();toast('已排除此线索');}
  }catch(e){toast(e.message,true);}
});
$('#detail-dialog').addEventListener('submit',async event=>{
  if(event.target.id!=='review-form')return;event.preventDefault();const form=new FormData(event.target);const button=event.submitter;button.disabled=true;
  try{
    const result=JSON.parse(JSON.stringify(state.detail.extraction));
    result.employer=form.get('employer');result.cohorts=String(form.get('cohorts')).split(/[,，、\s]+/).filter(Boolean).map(Number);result.batch=form.get('batch');result.season=form.get('season');result.recruitment_type='campus';result.review_reasons=[];
    result.deadline={...result.deadline,kind:form.get('kind'),value:form.get('value')||null,quote:form.get('quote'),scope:form.get('scope')||'本批次',start:-1,end:-1};
    const saved=await api('/api/reviews/'+state.detail.id,{method:'POST',body:JSON.stringify({decision:'approve',extraction:result,target_opportunity_id:form.get('target')||null,official_url:form.get('official_url'),employer_proof_url:form.get('proof')||null,use_snapshot:form.get('use_snapshot')==='on',snapshot_id:form.get('snapshot_id')||null,published_at:form.get('published_at')||null,publication_evidence:form.get('publication_evidence')||'',resolution_note:form.get('resolution_note')||'',parent_employer_id:form.get('parent')||null,relationship_proof_url:form.get('relationship_proof_url')||null,evidence:form.get('attachment_url')?[{attachment_url:form.get('attachment_url'),location:form.get('location'),quote:form.get('quote'),scope:result.deadline.scope}]:[]})});
    toast('人工核验已保存');await openDetail(saved.id);await renderList();await refreshSummary();
  }catch(e){toast(e.message,true);}finally{button.disabled=false;}
});
$('#import-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const result=await api('/api/imports',{method:'POST',body:new FormData(event.target)});$('#import-result').hidden=false;$('#import-result').textContent=`已导入 ${result.imported} 条线索，进入待核验区。`+(result.errors.length?'\n'+result.errors.map(e=>`第 ${e.row} 行：${e.error}`).join('\n'):'');await refreshSummary();toast('导入完成');}catch(e){toast(e.message,true);}finally{button.disabled=false;}});
$('#clock').textContent=new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'})+' · Asia/Shanghai';
Promise.all([renderList(),refreshSummary()]).then(async()=>{const runs=await api('/api/runs');const running=runs.find(r=>['running','queued'].includes(r.status));if(running){state.pendingRun=running.id;$('#sync-button').disabled=true;$('#run-banner').hidden=false;pollRun();}}).catch(e=>toast(e.message,true));

let coveragePage=1;
const coverageLabels={missing_entry:'入口待接入',blocked:'访问受阻',not_checked:'尚未检查',stale:'需要复查',not_found:'暂未发现当届校招',campus_found:'已发现当届校招'};
async function renderCoverage(){
  const form=$('#coverage-filters');const query=new URLSearchParams(new FormData(form));
  for(const [key,value] of [...query])if(!value)query.delete(key);
  query.set('page',coveragePage);query.set('page_size',50);
  const report=await api('/api/coverage?'+query),summary=report.summary;
  if(!form.elements.cohort.value)form.elements.cohort.value=report.cohort;
  const registry=form.elements.registry,selected=registry.value;
  registry.innerHTML='<option value="">全部名录 / 已知企业</option>'+report.registries.map(r=>`<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');registry.value=selected;const importSelect=$('#registry-import-form select');const importSelected=importSelect.value;importSelect.innerHTML=report.registries.map(r=>`<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');if(importSelected)importSelect.value=importSelected;
  $('#coverage-summary').innerHTML=[['全国企业总数','未知'],['已知主体（含待核）',summary.known_entities],['有独立监测入口',summary.with_entry],[`${report.fresh_hours} 小时内检查成功`,summary.recently_checked],[`${report.cohort} 届有校招的主体`,summary.with_campus],['底册尚未取得',`${summary.registry_gaps} 项`],['官方索引摘录（待直连复核）',`${summary.bundled_registries} 项`]].map(([label,value])=>`<div class="stat"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
  $('#coverage-total').textContent=`匹配 ${report.total} 家企业。${report.note}`;
  $('#coverage-rows').innerHTML=report.items.map(r=>`<tr><td><strong>${esc(r.name)}</strong>${r.parent_id?'<div class="muted">已关联上级集团；覆盖单独核算</div>':''}${r.proof_url?`<div><a href="${safeURL(r.proof_url)}" target="_blank" rel="noopener noreferrer">企业依据 ↗</a>${r.ownership_verified?'':'<small> · 名单线索，性质待复核</small>'}</div>`:'<div class="muted">企业性质待核验</div>'}</td><td>${esc(r.regions.join(' / ')||'地区待建档')}<div class="muted">${esc(r.registry_ids.join(' / ')||'尚未关联已取得的底册')}</div>${r.registry_evidence.filter(x=>x.declared_domain).map(x=>`<small>官网域名线索（未监测）：${esc(x.declared_domain)}</small>`).join('<br>')}</td><td><span class="tag ${r.status==='blocked'?'red':r.status==='campus_found'?'':'amber'}">${esc(coverageLabels[r.status])}</span>${r.sources.filter(x=>x.error).map(x=>`<p class="error-text">${esc(readableError(x.error))}</p>`).join('')}</td><td>${r.sources.length?r.sources.map(x=>`<p><a href="${safeURL(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.name)} ↗</a><br><small>${when(x.last_success)}</small></p>`).join(''):'待寻找官方招聘入口'}${r.discovered_by.length?`<small>线索发现渠道：${esc(r.discovered_by.join(' / '))}</small>`:''}</td><td>${r.campus_count} 条校招<div class="muted">${r.verified_count} 条已核验 · ${r.pending_count} 条待补证</div>${r.unknown_cohort_count?`<small>另有 ${r.unknown_cohort_count} 条届别待确认</small>`:''}</td></tr>`).join('')||'<tr><td colspan="5">没有匹配企业。名录未读到时分母保持未知，不代表该地区没有国企。</td></tr>';
  $('#coverage-page').textContent=`${coveragePage} / ${Math.max(1,Math.ceil(report.total/50))}`;$('#coverage-prev').disabled=coveragePage<=1;$('#coverage-next').disabled=coveragePage*50>=report.total;
  $('#coverage-export').href='/exports/coverage.csv?cohort='+report.cohort;
  $('#registry-list').innerHTML=report.registries.map(r=>`<article class="source-card"><div class="source-top"><h3>${esc(r.name)}</h3><span class="tag ${r.status==='error'?'red':'gray'}">${r.status==='missing_entry'?'入口待核对':r.status==='error'?'刷新受阻':r.status==='success'?'已取得底册':r.status==='imported'?'已导入（人工来源确认）':r.status==='bundled'?'索引摘录 · 待复核':'尚未读取'}</span></div><p>${esc(r.region)} · ${r.count===null?'企业数未知':`当前版本 ${r.count} 家`} · ${r.versions} 个版本</p><p class="muted">范围：${esc(r.provenance.scope||r.scope)}</p>${r.provenance.acquisition==='official_index_excerpt'?`<p class="muted">官方索引摘录 · 页面发布日期：${esc(r.provenance.published_at||'未标明')} · 整理日期：${esc(r.provenance.retrieved_at)}<br>${esc(r.provenance.note)}<br>名单完整性：尚未确认</p>`:''}<p>本机最近成功读取：${when(r.last_success)}</p>${r.last_error?`<p class="error-text">${esc(readableError(r.last_error))}</p>`:''}${r.url?`<a href="${safeURL(r.url)}" target="_blank" rel="noopener noreferrer">官方名录 ↗</a> ${r.refreshable?`<button class="button compact ghost" data-refresh-registry="${esc(r.id)}">刷新底册</button>`:''}`:'<p class="muted">待核实名录与所属单位，不计入已覆盖。</p>'}${r.authority_url?`<p><a href="${safeURL(r.authority_url)}" target="_blank" rel="noopener noreferrer">国资主管部门入口（名单待核实） ↗</a>${r.authority_proof_url?` · <a href="${safeURL(r.authority_proof_url)}" target="_blank" rel="noopener noreferrer">入口依据</a>`:''}</p>`:''}</article>`).join('');
}
$('#coverage-filters').addEventListener('submit',event=>{event.preventDefault();coveragePage=1;renderCoverage().catch(e=>toast(e.message,true));});
$('#coverage-prev').addEventListener('click',()=>{coveragePage--;renderCoverage().catch(e=>toast(e.message,true));});
$('#coverage-next').addEventListener('click',()=>{coveragePage++;renderCoverage().catch(e=>toast(e.message,true));});
$('#registry-list').addEventListener('click',async event=>{const button=event.target.closest('[data-refresh-registry]');if(!button)return;button.disabled=true;button.textContent='正在读取…';try{const result=await api('/api/registries/'+button.dataset.refreshRegistry+'/refresh',{method:'POST'});const row=result.registries[0];toast(row.status==='success'?`底册已保存：${row.count} 家`:'未取得新底册，已保留已有数据',row.status==='error');await renderCoverage();}catch(error){toast(error.message,true);button.disabled=false;button.textContent='刷新底册';}});

$('#registry-import-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const form=new FormData(event.target);const result=await api('/api/registries/'+encodeURIComponent(form.get('registry_id'))+'/imports',{method:'POST',body:form});toast(`已保存 ${result.count} 家企业，招聘入口将单独核对`);await renderCoverage();}catch(error){toast(error.message,true);}finally{button.disabled=false;}});
