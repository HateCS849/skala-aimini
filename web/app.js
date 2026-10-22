(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {queued:'준비 중',running:'실행 중',completed:'실험 완료',cancelled:'중단됨',error:'실행 실패',interrupted:'서버 종료로 중단'};
  const phaseName = {baseline:'기준',fault:'부하·장애',recovery:'회복'};
  let current = null, source = null, view = 0, busy = false, pollTimer = null, template = null, protectedTemplate = null, presets = [], extra = {};
  async function api(path, options) {
    const response = await fetch(path, options);
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
    return body;
  }
  const number = id => Number($(id).value);
  const rate = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
  const num = value => value == null ? '—' : Number(value).toLocaleString('ko-KR', {maximumFractionDigits:1});
  function error(message) { $('form-error').textContent = message || ''; $('form-error').hidden = !message; }
  function fields() {
    $('comparison-field').hidden = !$('compare').checked;
    $('comparison-url').required = $('compare').checked;
    $('fault-fields').hidden = $('fault-mode').value === 'none';
    $('metrics-fields').hidden = $('metrics-mode').value === 'none';
    $('agent-settings').hidden = $('analysis').value !== 'agents';
  }
  function fill(spec) {
    extra={...template,...spec};
    const mapping = {'experiment-name':'name','target-url':'target_url','comparison-url':'comparison_url','users':'users','pattern':'pattern',
      'baseline-seconds':'baseline_seconds','fault-seconds':'fault_seconds','recovery-seconds':'recovery_seconds','think-ms':'think_ms',
      'timeout':'timeout_seconds','slo-ms':'slo_ms','fault-mode':'fault_mode','control-path':'control_path','delay':'external_delay_seconds',
      'external-status':'external_status','pool-size':'pool_size','pool-wait':'pool_wait_seconds','provider-timeout':'provider_timeout_seconds','analysis':'analysis'};
    for (const [id,key] of Object.entries(mapping)) $(id).value = spec[key] ?? '';
    $('compare').checked = !!spec.comparison_url;
    $('metrics-mode').value = spec.metrics.mode; $('metrics-path').value = spec.metrics.path; $('metric-pool').value = spec.metrics.pool || '';
    $('journeys').value = JSON.stringify(spec.journeys, null, 2);
    protectedTemplate = spec.protected_step;
    $('protected-path').value = spec.protected_step?.path || ''; $('protected-json').value = spec.protected_step?.expect_json || '';
    $('token').value = '';
    if(spec.agent?.enabled)$('analysis').value='agents';
    $('agent-goal').value=spec.agent?.goal||'외부 로그인 지연과 동시 접속으로 DB 풀이 고갈될 때 음식 목록·검색까지 실패하는지 확인해줘.';
    $('agent-context').value=spec.agent?.context_mode||'compact';$('agent-project').value=spec.agent?.project_id||'';$('agent-budget').value=spec.agent?.max_experiments||2;$('agent-backend').value=spec.agent?.backend||'ollama';fields();
    $('experiment-mode').value=spec.mode||'load';
    const faults=spec.faults||{};
    $('external-behavior').value=faults.external_behavior||'normal';$('db-delay').value=faults.db_delay_ms||0;$('db-lock').value=faults.db_lock_seconds||0;$('cache-unavailable').checked=!!faults.cache_unavailable;
    $('integrity-enabled').checked=!!spec.integrity?.enabled;$('integrity-json').value=JSON.stringify(spec.integrity||template?.integrity||{},null,2);
    $('restart-enabled').checked=!!spec.restart?.enabled;$('restart-mode').value=spec.restart?.mode||'abrupt';$('restart-at').value=spec.restart?.at_seconds||1;
    $('memory-kind').value=spec.soak?.enabled?'heap_used_bytes':'process_rss_bytes';$('soak-enabled').checked=!!spec.soak?.enabled;$('soak-min').value=spec.soak?.min_observation_seconds||30;$('rss-growth').value=spec.soak?.rss_growth_mb||16;
    const match=presets.find(p=>p.spec.name===spec.name);$('preset-select').value=match?.id||'custom';$('preset-description').textContent=match?.description||'직접 작성한 실험 계획입니다.';
  }
  function spec() {
    let journeys;
    try { journeys = JSON.parse($('journeys').value); } catch { throw new Error('사용자 행동 JSON 형식을 확인해주세요.'); }
    let integrity;try{integrity=JSON.parse($('integrity-json').value||'{}');}catch{throw new Error('정합성 계획 JSON을 확인해주세요.');}
    return {name:$('experiment-name').value,target_url:$('target-url').value,
      comparison_url:$('compare').checked ? $('comparison-url').value : null,
      users:number('users'),pattern:$('pattern').value,baseline_seconds:number('baseline-seconds'),fault_seconds:number('fault-seconds'),
      recovery_seconds:number('recovery-seconds'),think_ms:number('think-ms'),timeout_seconds:number('timeout'),slo_ms:number('slo-ms'),
      fault_mode:$('fault-mode').value,control_path:$('control-path').value,external_delay_seconds:number('delay'),
      external_status:number('external-status'),pool_size:number('pool-size'),pool_wait_seconds:number('pool-wait'),provider_timeout_seconds:number('provider-timeout'),
      metrics:{mode:$('metrics-mode').value,path:$('metrics-path').value,pool:$('metric-pool').value || null},
      journeys,protected_step:$('protected-path').value ? {...(protectedTemplate || {name:'보호 기능',method:'GET',expected_status:[200]}),
        path:$('protected-path').value,expect_json:$('protected-json').value || null} : null,
      analysis:$('analysis').value==='agents'?'rules':$('analysis').value,
      agent:{enabled:$('analysis').value==='agents',goal:$('agent-goal').value,project_id:$('agent-project').value||null,backend:$('agent-backend').value,max_experiments:number('agent-budget'),context_mode:$('agent-context').value},mode:$('experiment-mode').value,
      faults:{external_behavior:$('external-behavior').value,db_delay_ms:number('db-delay'),db_lock_seconds:number('db-lock'),cache_unavailable:$('cache-unavailable').checked},
      integrity:{...integrity,enabled:$('integrity-enabled').checked},
      restart:{...(extra.restart||{}),enabled:$('restart-enabled').checked,mode:$('restart-mode').value,at_seconds:number('restart-at')},
      soak:{...(extra.soak||{}),enabled:$('soak-enabled').checked,min_observation_seconds:number('soak-min'),rss_growth_mb:number('rss-growth')},
      setup_steps:extra.setup_steps||[],request_budget:extra.request_budget||50000};
  }
  function chart(element, target, kind) {
    const points = [], bands = []; let offset = 0;
    for (const phase of target?.phases || []) {
      const rows = kind === 'latency' ? phase.series || [] : (phase.metrics || []).filter(m => kind==='memory'?m[$('memory-kind').value]!=null:m.pool_active != null);
      for (const row of rows) points.push({x:offset + row.second,y:kind === 'latency' ? row.p95_ms : kind==='memory'?row[$('memory-kind').value]/1048576:row.pool_active,pending:kind==='pool'?row.pool_pending:null,max:kind==='pool'?row.pool_max:null});
      const duration = Math.max(phase.actual_seconds || phase.duration_seconds || 1,...rows.map(r => r.second));
      bands.push({start:offset,end:offset + duration,name:phaseName[phase.name]}); offset += duration;
    }
    if (!points.length) {
      element.innerHTML = `<div class="empty-chart">${kind === 'latency' ? '측정된 요청이 없습니다.' : kind==='memory'?'선택한 메모리 지표 미연결':'DB 풀 지표가 아직 없거나 연결되지 않았습니다.'}</div>`; return;
    }
    const w=720,h=kind === 'latency' ? 160 : 125,left=42,right=8,top=15,bottom=26;
    const maxX=Math.max(1,offset),maxY=Math.max(1,...points.map(p=>Math.max(p.y || 0,p.max || 0,p.pending || 0)))*1.12;
    const x=v=>left+(w-left-right)*v/maxX, y=v=>h-bottom-(h-top-bottom)*v/maxY;
    const path=key=>points.filter(p=>p[key]!=null).map((p,i)=>`${i?'L':'M'}${x(p.x).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
    let svg=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${kind === 'latency'?'응답 시간':kind==='memory'?($('memory-kind').value==='heap_used_bytes'?'힙 사용량':'현재 RSS'):'DB 풀 점유'} 변화 그래프">`;
    for(let i=0;i<4;i++) { const val=maxY*i/3; svg+=`<line x1="${left}" x2="${w-right}" y1="${y(val)}" y2="${y(val)}" stroke="#edf1ee"/><text x="${left-7}" y="${y(val)+3}" text-anchor="end" font-size="9" fill="#8b9e93">${Math.round(val)}</text>`; }
    bands.forEach((b,i)=>{svg+=`<rect x="${x(b.start)}" y="${top}" width="${x(b.end)-x(b.start)}" height="${h-top-bottom}" fill="${i===1?'#faf1e6':'#edf7f1'}" opacity=".45"/><text x="${x((b.start+b.end)/2)}" y="${h-5}" text-anchor="middle" fill="#7c9185" font-size="9">${b.name}</text>`;});
    svg+=`<path d="${path('y')}" fill="none" stroke="${kind === 'latency'?'#0c725c':'#d7803f'}" stroke-width="2.1"/>`;
    if(kind==='pool') svg+=`<path d="${path('max')}" fill="none" stroke="#a2b6a8" stroke-dasharray="4 4"/><path d="${path('pending')}" fill="none" stroke="#b83d42" stroke-width="1.7"/>`;
    element.innerHTML=svg+'</svg>';
  }
  function displayedRecord(){
    const attempt=$('agent-run-view').value;
    return attempt==='primary'?(current?.agent?.runs?.find(r=>r.scenario_id==='requested')||current):(current?.agent?.runs?.[Number(attempt)]||current);
  }
  function renderTarget() {
    const selected=displayedRecord();
    const target=selected?.targets?.[view];
    const phases=target?.phases || [], fault=phases.find(p=>p.name==='fault');
    $('kpi-requests').textContent=num(phases.reduce((n,p)=>n+(p.total?.requests||0),0)+(target?.integrity||[]).reduce((n,b)=>n+(b.requests?.requests||0),0));
    $('kpi-errors').textContent=rate(fault?.total?.error_rate); $('kpi-p95').textContent=num(fault?.total?.p95_ms);
    const pending=phases.flatMap(p=>p.metrics||[]).filter(p=>p.pool_pending!=null).map(p=>p.pool_pending);
    $('kpi-pending').textContent=pending.length?num(Math.max(...pending)):'미연결';
    chart($('latency-chart'),target,'latency'); chart($('pool-chart'),target,'pool');
    chart($('memory-chart'),target,'memory');
    $('phase-rows').innerHTML=phases.length?phases.map(p=>`<tr><td>${esc(phaseName[p.name])}<span class="pill">${p.status==='completed'?'완료':p.status==='running'?'진행':'중단'}</span></td><td>${num(p.total?.requests)}</td><td>${rate(p.total?.error_rate)}</td><td>${num(p.total?.p95_ms)}</td><td>${num(p.total?.rps)}</td></tr>`).join(''):'<tr><td colspan="5" class="empty-cell">아직 측정값이 없습니다.</td></tr>';
    $('journey-results').innerHTML=phases.map(p=>`<h3>${esc(phaseName[p.name])}</h3><div class="table-wrap"><table><thead><tr><th>사용자 행동</th><th>요청</th><th>실패</th><th>P95 · ms</th></tr></thead><tbody>${Object.entries(p.by_journey||{}).map(([name,s])=>`<tr><td>${esc(name)}</td><td>${num(s.requests)}</td><td>${num(s.failures)}</td><td>${num(s.p95_ms)}</td></tr>`).join('')}</tbody></table></div>`).join('');
    const result=selected?.summary?.targets?.[view];
    $('findings').innerHTML=result?(result.findings.length?result.findings.map(f=>`<article class="finding ${esc(f.severity)}"><strong>${esc(f.title)}</strong><p>${esc(f.evidence)}</p></article>`).join(''):`<div class="good">${result.verdict==='insufficient'?'판정할 표본이 부족합니다.':'이번 실험 조건에서 판정 기준을 넘는 문제를 관측하지 않았습니다.'}</div>`)+`<div class="limits"><p>${esc(result.evidence_note)}</p><ul>${result.limits.map(l=>`<li>${esc(l)}</li>`).join('')}</ul></div>`:'<div class="empty-state">단계별 측정을 마치면 판정 근거와 확인하지 못한 범위를 표시합니다.</div>';
    let diagnostics=(target?.integrity||[]).map(batch=>`<h3>${batch.case==='stock'?'재고 동시성':'동일 키 중복 처리'} · ${batch.round}회차</h3>${batch.checks.map(c=>`<article class="finding ${c.status==='failed'?'critical':c.status==='passed'?'passed':''}"><strong>${esc(c.status==='passed'?'통과':c.status==='failed'?'위반':'미확인')} · ${esc(c.title)}</strong><p>${esc(c.evidence)}</p></article>`).join('')}`).join('');
    const d=target?.diagnostics||{};
    if(d.retry)diagnostics+=`<div class="finding"><strong>외부 호출 증폭 ${num(d.retry.amplification)}배</strong><p>논리 로그인 ${num(d.retry.logical_requests)}건 · 외부 호출 ${num(d.retry.attempts)}회</p></div>`;
    if(d.cache_errors!=null)diagnostics+=`<p class="note">Redis 연결 실패 계측 ${num(d.cache_errors)}건 · 응답 영향은 행동별 결과에서 확인</p>`;
    if(d.db_lock_errors!=null)diagnostics+=`<p class="note">실제 DB 쓰기 잠금 충돌 ${num(d.db_lock_errors)}건</p>`;
    diagnostics+=(target?.restarts||[]).map(r=>`<div class="finding ${r.data_status==='failed'?'critical':r.data_status==='passed'?'passed':''}"><strong>실제 프로세스 재시작 · ${esc(r.mode)}</strong><p>부트 ${esc(r.before_boot_id?.slice(0,8))} → ${esc(r.after_boot_id?.slice(0,8))} · ${num(r.restart_seconds)}초</p><p>기준 데이터: ${r.data_status==='passed'?'보존 확인':r.data_status==='failed'?'불일치':'미확인'}</p></div>`).join('');
    diagnostics+=(d.soak||[]).map(s=>`<div class="finding ${s.status==='growth'?'critical':''}"><strong>${esc(s.title)} · ${esc(s.status==='growth'?'지속 증가 징후':s.status==='unverified'?'미확인':'증가 기준 미초과')}</strong><p>${s.window_means?`구간 평균 ${esc(s.window_means.join(' → '))} · 증가 ${esc(s.growth)}`:esc(s.reason)}. 누수 원인 확정은 아닙니다.</p></div>`).join('');
    $('diagnostic-results').innerHTML=diagnostics||'<div class="empty-state">이 실행에는 추가 진단 결과가 없습니다.</div>';
  }
  function renderAgents(record){
    const a=record.agent;$('agent-monitor').hidden=!a;if(!a)return;
    const v=a.validation;
    $('agent-verdict').textContent=v?`근거 ${v.verdict==='complete'?'충분':'일부 부족'} · 원래 조건 ${v.requested_tested?'검사됨':'미검사'} · ${a.runs.length}회 실행 · 서비스에 오류가 없다는 의미는 아닙니다.`:'근거를 검색하고 실험을 계획·검증하고 있습니다.';
    $('download-trace').href=`/api/resilience/experiments/${record.id}/trace`;
    const cloud=a.trace?.cloud,verified=a.trace?.cloud_verified&&cloud?.status==='verified';
    $('agent-cloud-status').textContent=verified?`LangSmith 클라우드 수신 확인 · ${cloud.readback.received}/${cloud.readback.expected}개 · 완료한 합성 실행의 메타데이터`:'LangSmith SDK 로컬 추적 · 이 실행의 클라우드 수신은 미검증';
    $('cloud-trace-link').hidden=!(verified&&/^https:\/\/(?:eu\.)?smith\.langchain\.com\//.test(cloud.url||''));if(!$('cloud-trace-link').hidden)$('cloud-trace-link').href=cloud.url;
    $('agent-roles').innerHTML=a.roles.slice(-8).map(r=>`<span class="pill ${esc(r.status)}">${esc({research:'근거 검색',planner:'계획',gate:'실행 경계',executor:'실행',validator:'검증',replan_check:'재계획 판단',write:'보고서'}[r.name]||r.name)} · ${esc(r.status)}</span>`).join('');
    $('agent-plans').innerHTML=a.plans.map((p,i)=>`<article class="finding"><strong>${i+1}. ${esc(p.title)}</strong><p>${esc(p.backend)} · 근거 ${esc(p.reference_ids.join(', ')||'인용 없음')}</p><p>${esc(a.runs[i]?.validation?.verdict||'검증 대기')}</p></article>`).join('')+(a.contrasts||[]).map(c=>`<p class="note">${esc(c.target)} · ${esc(c.control)} 대조: 보호 기능 영향 ${esc(c.requested_protected_affected)} → ${esc(c.control_protected_affected)} · ${esc(c.status==='observed_change'?'변화 관측':c.status==='unverified'?'근거 부족':'보호 기능 영향 변화 미관측')}</p>`).join('');
    const old=$('agent-run-view').value;
    const options='<option value="primary">원래 조건 · 대표 결과</option>'+a.runs.map((r,i)=>`<option value="${i}">${i+1}. ${esc(a.plans[i]?.title||r.scenario_id)}</option>`).join('');
    if($('agent-run-view').innerHTML!==options){$('agent-run-view').innerHTML=options;$('agent-run-view').value=Array.from($('agent-run-view').options).some(o=>o.value===old)?old:'primary';}
    $('agent-evidence').innerHTML=a.references.map(r=>`<p><strong>${esc(r.id)} · ${esc(r.source)}</strong><br>${esc(r.text.slice(0,350))}</p>`).join('')+`<p>${esc(a.mcp_calls.join(' → '))}</p><p>LLM ${num(a.usage.llm_calls)}회 · 입력 ${num(a.usage.input_tokens)} / 출력 ${num(a.usage.output_tokens)}토큰</p><p>${a.trace?.cloud_verified&&a.trace?.cloud?.status==='verified'?`LangSmith 클라우드 수신 확인 · ${num(a.trace.cloud.readback.received)}/${num(a.trace.cloud.readback.expected)}개 · 완료 실행 메타데이터`: 'LangSmith SDK 로컬 추적 · 이 실행의 클라우드 수신은 미검증'}</p>`+a.limitations.map(l=>`<p class="error">${esc(l)}</p>`).join('');
  }
  function render(record) {
    if(current?.id!==record.id)$('agent-run-view').value='primary';
    current=record;renderAgents(record);const integrityOnly=record.config.mode==='integrity';$('load-charts').hidden=integrityOnly;$('phase-results').hidden=integrityOnly;$('memory-chart').hidden=integrityOnly;$('memory-chart').previousElementSibling.hidden=integrityOnly; const active=['running','queued'].includes(record.status);
    $('run-title').textContent=record.name; $('run-status').textContent=labels[record.status]||record.status; $('run-status').className='badge '+record.status;
    $('start').disabled=active||busy; $('cancel').hidden=!active;
    $('live-message').textContent=record.events.at(-1)?.message||'실험을 준비하고 있습니다.';
    $('run-error').textContent=record.error||''; $('run-error').hidden=!record.error;
    $('elapsed').textContent=record.elapsed_seconds!=null?`실제 경과 ${record.elapsed_seconds}초`:`${record.config.users}명 · ${record.config.pattern} · 실측 중`;
    for(const stage of document.querySelectorAll('[data-stage]')) stage.classList.toggle('current',stage.dataset.stage===record.stage);
    $('download-report').hidden=false; $('download-json').hidden=false;
    $('download-report').href=`/api/resilience/experiments/${record.id}/report`; $('download-json').href=`/api/resilience/experiments/${record.id}/evidence`;
    const choices=displayedRecord().targets.map(t=>t.label);
    if($('target-view').options.length!==choices.length||Array.from($('target-view').options).some((o,i)=>o.text!==choices[i])) $('target-view').innerHTML=choices.map((label,i)=>`<option value="${i}">${esc(label)}</option>`).join('');
    view=Math.min(view,Math.max(0,choices.length-1)); $('target-view').value=String(view);
    $('timeline').innerHTML=record.events.slice(-18).reverse().map(e=>`<li><time>${esc(new Date(e.time).toLocaleTimeString('ko-KR',{hour12:false}))}</time><span>${esc(e.message)}</span></li>`).join('');
    const comparison=displayedRecord().summary?.comparison;
    $('comparison').hidden=!comparison;
    if(comparison){const improved=comparison.isolation_observed||comparison.integrity_improved;$('comparison').className='comparison'+(improved?'':' pending');$('comparison').innerHTML=`<strong>${comparison.integrity_improved?'변경 후, 이번 데이터 정합성 검사를 모두 통과했습니다.':comparison.isolation_observed?'변경 후, 보호 기능으로의 장애 전파가 관측되지 않았습니다.':'변경 전후 결과를 추가로 확인해주세요.'}</strong>${esc(comparison.scope)}`;}
    $('ai-analysis').hidden=!record.ai;
    if(record.ai){$('ai-text').textContent=record.ai.text;$('ai-proposals').innerHTML=(record.ai.proposals||[]).map((p,i)=>`<button type="button" class="secondary" data-proposal="${i}">${esc(p.title)} · 계획에 적용 ↗</button>`).join(' ');}
    renderTarget();
  }
  async function history(){
    const rows=await api('/api/resilience/experiments');
    $('history').innerHTML=rows.length?rows.slice(0,12).map(r=>`<button type="button" data-id="${esc(r.id)}"><strong>${esc(r.name)}</strong><small>${esc(new Date(r.created_at).toLocaleString('ko-KR'))} · ${esc(labels[r.status]||r.status)}</small></button>`).join(''):'<p class="note">아직 실행한 실험이 없습니다.</p>';
    return rows;
  }
  function closeStream(){if(source)source.close();source=null;clearTimeout(pollTimer);}
  function watch(id){
    closeStream();source=new EventSource(`/api/resilience/experiments/${id}/events`);
    source.onopen=()=>{$('stream-status').textContent='LIVE · SSE 연결됨';};
    source.addEventListener('update',event=>render(JSON.parse(event.data)));
    source.addEventListener('done',()=>{closeStream();$('stream-status').textContent='기록 저장됨';history().catch(e=>error(e.message));});
    source.onerror=()=>{ $('stream-status').textContent='연결 재시도 중';clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{try{const r=await api(`/api/resilience/experiments/${id}`);render(r);if(!['running','queued'].includes(r.status)){closeStream();history();}}catch(e){error(e.message);}},1500);};
  }
  async function load(id){closeStream();view=0;const r=await api(`/api/resilience/experiments/${id}`);fill(r.config);render(r);if(['running','queued'].includes(r.status))watch(id);else $('stream-status').textContent='저장된 기록';}
  $('experiment-form').addEventListener('submit',async event=>{
    event.preventDefault(); if(busy)return;busy=true;error('');$('start').disabled=true;
    try{const body=spec();body.token=$('token').value||null;const r=await api('/api/resilience/experiments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});$('token').value='';view=0;render(r);watch(r.id);await history();}
    catch(e){error(e.message);}finally{busy=false;$('start').disabled=['running','queued'].includes(current?.status);}
  });
  $('cancel').addEventListener('click',async()=>{try{$('cancel').disabled=true;const r=await api(`/api/resilience/experiments/${current.id}/cancel`,{method:'POST'});render(r);closeStream();await history();}catch(e){error(e.message);}finally{$('cancel').disabled=false;}});
  $('history').addEventListener('click',event=>{const b=event.target.closest('[data-id]');if(b)load(b.dataset.id).catch(e=>error(e.message));});
  $('refresh-history').addEventListener('click',()=>history().catch(e=>error(e.message)));
  $('target-view').addEventListener('change',()=>{view=number('target-view');renderTarget();});
  $('analysis').addEventListener('change',fields);
  $('agent-run-view').addEventListener('change',()=>render(current));
  $('agent-attachment').addEventListener('change',async()=>{const file=$('agent-attachment').files[0],id=$('agent-project').value;if(!file)return;try{if(!id)throw new Error('자료를 첨부할 프로젝트를 먼저 선택해주세요. 새 프로젝트는 상단 프로젝트 자료에서 만들 수 있습니다.');const data=new FormData();data.append('file',file);data.append('category','auto');await api(`/api/projects/${id}/attachments`,{method:'POST',body:data});$('agent-upload-status').textContent=`${file.name} 첨부됨 · 다음 AI 감독 실행에서 검색합니다.`;}catch(e){$('agent-upload-status').textContent=e.message;}finally{$('agent-attachment').value='';}});
  $('memory-kind').addEventListener('change',()=>renderTarget());
  $('compare').addEventListener('change',fields);$('fault-mode').addEventListener('change',fields);
  $('metrics-mode').addEventListener('change',()=>{if($('metrics-mode').value==='actuator')$('metrics-path').value='/actuator/metrics';else if($('metrics-mode').value==='json')$('metrics-path').value='/__burstguard/metrics';fields();});
  $('demo-preset').addEventListener('click',()=>{const preset=presets.find(p=>p.id===$('preset-select').value);if(preset)fill(preset.spec);else if(template)fill(template);error('');});
  $('preset-select').addEventListener('change',()=>{const preset=presets.find(p=>p.id===$('preset-select').value);if(preset)fill(preset.spec);error('');});
  $('experiment-mode').addEventListener('change',()=>{if($('experiment-mode').value==='integrity')$('integrity-enabled').checked=true;});
  $('ai-proposals').addEventListener('click',event=>{const b=event.target.closest('[data-proposal]');if(!b)return;const p=current?.ai?.proposals?.[Number(b.dataset.proposal)];if(p){fill({...current.config,...p.patch,name:(current.config.name+' · '+p.title).slice(0,100)});$('import-message').textContent='추천 실험을 설계에 적용했습니다. 조건을 확인한 뒤 실험 시작을 누르세요.';$('experiment-form').scrollIntoView({behavior:'smooth',block:'start'});}});
  $('plan-file').addEventListener('change',async()=>{const file=$('plan-file').files[0];if(!file)return;try{const data=new FormData();data.append('file',file);const result=await api('/api/resilience/plans/import',{method:'POST',body:data});if(result.kind==='plan'){fill(result.spec);$('import-message').textContent='실험 계획을 불러왔습니다. 대상 주소와 조건을 확인한 뒤 실행하세요.';}else{$('openapi-preview').hidden=false;$('openapi-steps').textContent=JSON.stringify(result.steps,null,2);$('import-message').textContent=result.notice;}}catch(e){$('import-message').textContent=e.message;}finally{$('plan-file').value='';}});
  $('export-plan').addEventListener('click',()=>{try{const body=spec();const url=URL.createObjectURL(new Blob([JSON.stringify(body,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='burstguard-plan.json';a.click();URL.revokeObjectURL(url);}catch(e){error(e.message);}});
  window.addEventListener('pagehide',closeStream);
  (async()=>{try{template=await api('/api/resilience/template');const projects=await api('/api/projects');$('agent-project').innerHTML='<option value="">기본 장애 Runbook</option>'+projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');presets=await api('/api/resilience/presets');$('preset-select').innerHTML=presets.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('')+'<option value="custom">직접 작성 · 저장된 계획</option>';fill(template);const managed=await api('/api/resilience/fixture');$('fixture-status').textContent=managed.managed?'이 BurstGuard가 소유한 예제 프로세스입니다. 재시작 검사를 사용할 수 있습니다.':'예제를 직접 실행 중이거나 관리 연결이 없습니다. start_all.sh로 실행하면 재시작을 사용할 수 있습니다.';const rows=await history();if(rows.length)await load(rows[0].id);}catch(e){error(e.message);}})();
})();
