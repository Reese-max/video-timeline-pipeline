// Real widget script with a mocked MCP host; no providers, uploads or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'widget.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const clone = value => JSON.parse(JSON.stringify(value));
const job = id => ({ status: 'ready', job_id: id, title: '影片 ' + id, duration_seconds: 60, segments: [0, 1].map(i => ({ filename: 'segment_00' + i + '.mp4', url: 'https://media.example/' + id + '/' + i, start_seconds: i * 30, end_seconds: (i + 1) * 30 })) });
const analysis = (id, review = false) => ({ status: review ? 'partial' : 'complete', job_id: id, source_size_bytes: 8, source_sha256: 'a'.repeat(64),
  transcript: { segments: [{ start: 35, end: 38, text: 'source speech <script>not code</script>' }, { text: 'unknown time' }] },
  timeline: [{ timestamp: 2, type: 'visual', data: { visual_summary: 'source frame', visible_text: ['source text'], confidence: 0.9 } }],
  quality: { status: review ? 'needs_review' : 'evidence_available', reasons: review ? ['ASR 未完成'] : [], limitation: '僅抽樣，不保證語意正確。' } });
const drive = id => ({ status: 'drive_ready', job_id: id, files: job(id).segments.map((s, i) => ({ file_name: s.filename, file_id: id + '-file-' + i, size_bytes: 8, url: 'https://drive.google.com/file/d/test/view' })) });
const pending = (id = 'operation-1', operation = 'analyze', status = 'queued') => ({ status, operation_id: id, operation, job_id: 'first', progress: { stage: 'queued', current: 0, total: 2, message: '等待開始' }, retry_after_seconds: 2 });
const completed = (result, kind = 'analysis', id = 'operation-1') => ({ status: 'completed', operation_id: id, operation: kind === 'analysis' ? 'analyze' : kind === 'prepared' ? 'prepare' : 'drive', job_id: result.job_id, result_kind: kind, result });
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.attributes = {}; this.hidden = false; this.disabled = false; this.value = ''; this.textContent = ''; this.currentTime = 0; }
  append(...items) { this.children.push(...items); } replaceChildren(...items) { this.children = [...items]; }
  addEventListener(event, listener) { this.listeners[event] = listener; }
  setAttribute(name, value) { this.attributes[name] = String(value); } getAttribute(name) { return this.attributes[name] ?? null; } removeAttribute(name) { delete this.attributes[name]; }
  set src(value) { this.attributes.src = value; } get src() { return this.attributes.src; }
  focus() { this.focused = true; } select() { this.selected = true; } load() { this.loads = (this.loads || 0) + 1; } fire(event) { return this.listeners[event]?.({ target: this }); }
}
function harness({ output = job('first'), saved = {}, snapshot, respond, bridge = true, persistence = true, sendError, messageResult = {} } = {}) {
  const elements = new Map(), listeners = {}, calls = [], prompts = [], states = [], timers = new Map(), methods = [], fallbackCalls = [];
  let timerId = 0;
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*?)\bid="([^"]+)"([^>]*?)>/g)) {
    const element = new Element(match[1]); element.hidden = /\bhidden\b/.test(match[2] + match[4]); element.disabled = /\bdisabled\b/.test(match[2] + match[4]); elements.set(match[3], element);
  }
  elements.get('filter').value = 'all';
  const notify = payload => listeners.message?.({ source: parent, data: { jsonrpc: '2.0', ...payload } });
  const notifyTool = payload => notify({ method: 'ui/notifications/tool-result', params: { structuredContent: payload } });
  async function callTool(name, args) {
    calls.push({ name, args: clone(args) });
    const payload = respond ? await respond(name, args, calls.length) : name === 'get_prepared_video' ? (snapshot || { prepared: job(args.job_id), analysis: null })
      : name === 'analyze_prepared_video' ? pending() : name === 'get_video_job' ? completed(analysis('first'))
      : name === 'upload_prepared_video_to_drive' ? completed(drive(args.job_id), 'drive', 'drive-1')
      : name === 'cancel_video_job' ? { ...pending(args.operation_id), status: 'cancelled' } : pending('operation-resumed');
    notifyTool(payload);
    return { structuredContent: payload };
  }
  async function send(prompt) { prompts.push(prompt); if (sendError) throw new Error(sendError); return messageResult; }
  const parent = { postMessage(message) {
    methods.push(message.method); if (message.id === undefined) return;
    Promise.resolve().then(async () => {
      try {
        if (!bridge) throw Object.assign(new Error('Unsupported bridge'), { code: -32601 });
        if (message.method === 'ui/initialize') { assert.equal(message.params.appInfo?.name, 'video-timeline-pipeline'); assert.equal(message.params.appInfo?.version, '20'); assert.equal(message.params.protocolVersion, '2026-01-26'); assert.equal(typeof message.params.appCapabilities, 'object'); }
        if (message.method === 'ui/message') { assert.equal(message.params.role, 'user'); assert(Array.isArray(message.params.content)); }
        const result = message.method === 'tools/call' ? await callTool(message.params.name, message.params.arguments) : message.method === 'ui/message' ? await send(message.params.content[0].text) : {};
        notify({ id: message.id, result });
      } catch (error) { notify({ id: message.id, error: { message: error.message, code: error.code } }); }
    });
  } };
  const openai = { toolOutput: output, widgetState: clone(saved), ...(persistence ? { setWidgetState(state) { this.widgetState = clone(state); states.push(clone(state)); } } : {}),
    callTool(name, args) { fallbackCalls.push(name); return callTool(name, args); }, sendFollowUpMessage({ prompt }) { fallbackCalls.push('message'); return send(prompt); } };
  const context = vm.createContext({ URL, window: { openai, parent, addEventListener(name, listener) { listeners[name] = listener; } },
    document: { getElementById(id) { assert(elements.has(id), 'Missing HTML id: ' + id); return elements.get(id); }, createElement(tag) { return new Element(tag); } },
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; }, clearTimeout(id) { timers.delete(id); }, fetch() { throw new Error('Must use MCP'); } });
  vm.runInContext(script, context, { filename: 'widget.html' });
  return { elements, calls, prompts, states, openai, fallbackCalls, methods, notifyTool, context, timers, teardown() { notify({ method: 'ui/resource-teardown', id: 'teardown' }); },
    async flush() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); },
    async click(id) { const element = elements.get(id); assert(!element.disabled, id + ' is disabled'); await element.fire('click'); await this.flush(); },
    async poll() { const next = [...timers.entries()].find(([, timer]) => timer.ms <= 10000); assert(next, 'No poll timer'); timers.delete(next[0]); next[1].callback(); await this.flush(); } };
}
let count = 0;
let failures = 0;
async function test(name, run) { try { await run(); count++; console.log('ok ' + count + ' - ' + name); } catch (error) { failures++; console.error('FAIL - ' + name, error); } }
(async () => {
  function a() { return { ...analysis('first'), cache_signature: 'a'.repeat(64) }; }
  function r(status = 'summary_pending') { return { job_id:'first', cache_signature:'a'.repeat(64), analysis_sha256:'b'.repeat(64),
    report_sha256:'c'.repeat(64), evidence_status:'complete', summary_status:'pending_chatgpt', status,
    download_url:'https://media.example/reports/current/A_server_analysis.md' }; }
  function setup(report = r(), respond) { return harness({ snapshot:{ prepared:job('first'), analysis:a(), report }, respond }); }
  await test('actual widget distinguishes evidence, submitted summary and report; no implicit submission', async () => {
    const h=setup();await h.flush();assert(h.elements.has('analysis-report'),'Required A report UI is absent');
    assert.equal(h.elements.get('analysis-report').hidden,false);assert.match(h.elements.get('report-state').textContent,/pending_chatgpt/);
    assert.match(h.elements.get('report-state').textContent,/未獨立驗證/);assert.equal(h.elements.get('copy-merge').disabled,true);
    assert.deepEqual(h.calls.map(x=>x.name),['get_prepared_video']);assert.equal(h.prompts.length,0);
    assert.deepEqual(h.fallbackCalls,[],'standard MCP bridge, not the window.openai fallback');
  });
  await test('only explicit summary+provenance submission sends exact current analysis binding', async () => {
    let stored=r();
    const h=setup(stored,name=>name==='get_prepared_video'?{prepared:job('first'),analysis:a(),report:stored}:(stored={...r('queued'),summary_status:'caller_submitted'}));
    await h.flush();h.elements.get('report-summary').value='user source text';h.elements.get('report-provenance').value='unknown author';await h.click('finalize-report');
    const sent=h.calls.find(x=>x.name==='finalize_analysis_report');assert.deepEqual(sent.args,{job_id:'first',cache_signature:'a'.repeat(64),analysis_sha256:'b'.repeat(64),summary_text:'user source text',provenance_note:'unknown author'});
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,1);assert.match(h.elements.get('report-state').textContent,/queued/);
  });
  await test('unverified or stale report never enables merge copy', async () => {
    const h=setup({...r('report_ready'),verified:false,file_id:'fixture',merge_text:'must not copy'});await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
    h.notifyTool({...r('report_ready'),cache_signature:'f'.repeat(64),verified:true,file_id:'fixture',merge_text:'stale'});await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
  });
  await test('a delayed older report notification cannot replace the current queued summary', async () => {
    const latest={...r('queued'),report_sha256:'d'.repeat(64),summary_status:'caller_submitted',operation:pending('new-report','report')};
    const h=setup(latest,name=>{
      assert.equal(name,'get_prepared_video');return {prepared:job('first'),analysis:a(),report:latest};
    });
    await h.flush();
    h.notifyTool({...r('report_ready'),verified:true,file_id:'old-report',merge_text:'OLD SUMMARY MERGE'});
    await h.flush();
    assert.equal(h.elements.get('copy-merge').disabled,true);
    assert.match(h.elements.get('report-state').textContent,/queued/);
    assert.equal(h.calls.filter(x=>x.name==='get_prepared_video').length,2);
    assert(h.calls.every(x=>x.name==='get_prepared_video'));assert.equal(h.prompts.length,0);
  });
  await test('a new ChatGPT report notification is reconciled with the saved report before enabling copy', async () => {
    let latest=r();
    const h=setup(latest,name=>{
      assert.equal(name,'get_prepared_video');return {prepared:job('first'),analysis:a(),report:latest};
    });
    await h.flush();
    latest={...r('queued'),report_sha256:'d'.repeat(64),summary_status:'caller_submitted',operation:pending('new-report','report')};
    h.notifyTool(latest);await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
    latest={...latest,status:'report_ready',verified:true,file_id:'new-report',merge_text:'CURRENT SUMMARY MERGE'};
    h.notifyTool(latest);await h.flush();assert.equal(h.elements.get('copy-merge').disabled,false);
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};
    await h.click('copy-merge');assert.deepEqual(copies,['CURRENT SUMMARY MERGE']);
    assert.equal(h.calls.filter(x=>x.name==='get_prepared_video').length,3);
    assert(h.calls.every(x=>x.name==='get_prepared_video'));
  });
  await test('an older in-flight report refresh cannot overwrite a newer authoritative refresh', async () => {
    const queued=(digest,id)=>({...r('queued'),report_sha256:digest.repeat(64),operation:pending(id,'report')});
    const first=queued('d','report-d'),latest=queued('e','report-e');let reads=0,release;
    const h=setup(first,name=>{
      assert.equal(name,'get_prepared_video');reads++;
      if(reads===2) return new Promise(resolve=>{release=()=>resolve({prepared:job('first'),analysis:a(),report:{...first,status:'report_ready',verified:true,file_id:'old-report',merge_text:'OLD MERGE'}});});
      return {prepared:job('first'),analysis:a(),report:reads>=3?latest:first};
    });
    await h.flush();h.notifyTool({...first,status:'report_ready',verified:true,file_id:'old-report',merge_text:'OLD MERGE'});await h.flush();
    assert.equal(typeof release,'function');
    h.notifyTool(latest);await h.flush();release();await h.flush();
    assert.equal(h.elements.get('copy-merge').disabled,true);assert.match(h.elements.get('report-state').textContent,/queued/);
    assert.equal(reads,3);assert.equal(h.prompts.length,0);
  });
  for(const oldOperation of [false,true]) await test('a stale manual restore cannot revive an old report'+(oldOperation?' through a completed operation':''), async () => {
    const old={...r('report_ready'),verified:true,file_id:'old-report',merge_text:'OLD C MERGE'};
    const latest={...r('queued'),report_sha256:'d'.repeat(64),operation:pending('report-d','report')};let reads=0,release;
    const oldSnapshot={prepared:job('first'),analysis:a(),report:old,
      ...(oldOperation?{latest_operation:reportCompleted(old)}:{})};
    const h=setup(old,name=>{
      assert.equal(name,'get_prepared_video');reads++;
      if(reads===2) return new Promise(resolve=>{release=()=>resolve(oldSnapshot);});
      return {prepared:job('first'),analysis:a(),report:reads>=3?latest:old};
    });
    await h.flush();const restoring=h.elements.get('refresh').fire('click');await h.flush();
    h.notifyTool(latest);await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
    release();await restoring;await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
    assert.match(h.elements.get('report-state').textContent,/queued/);
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};
    await h.elements.get('copy-merge').fire('click');assert.deepEqual(copies,[]);
    assert.equal(reads,3);assert(h.calls.every(x=>x.name==='get_prepared_video'));
  });
  await test('the saved current report wins over an older latest completed queue operation', async () => {
    const old={...r('report_ready'),verified:true,file_id:'old-report',merge_text:'OLD C MERGE'};
    const latest={...r('awaiting_segments'),report_sha256:'d'.repeat(64),summary_status:'caller_submitted'};
    const h=setup(latest,()=>({prepared:job('first'),analysis:a(),report:latest,latest_operation:reportCompleted(old)}));
    await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
    assert.match(h.elements.get('report-state').textContent,/awaiting_segments/);
    assert(h.calls.every(x=>x.name==='get_prepared_video'));assert.equal(h.prompts.length,0);
  });
  await test('a newer notification during the post-submission read supersedes that older response', async () => {
    const own={...r('queued'),report_sha256:'d'.repeat(64),summary_status:'caller_submitted',operation:pending('report-d','report')};
    const ready={...own,status:'report_ready',verified:true,file_id:'old-d',merge_text:'OLD D MERGE'};
    const latest={...own,report_sha256:'e'.repeat(64),operation:pending('report-e','report')};let reads=0,release;
    const h=setup(r(),name=>{
      if(name==='finalize_analysis_report') return own;
      assert.equal(name,'get_prepared_video');reads++;
      if(reads===2) return new Promise(resolve=>{release=()=>resolve({prepared:job('first'),analysis:a(),report:ready});});
      return {prepared:job('first'),analysis:a(),report:reads>2?latest:r()};
    });
    await h.flush();h.elements.get('report-summary').value='explicit text';h.elements.get('report-provenance').value='owned fixture';
    const submission=h.elements.get('finalize-report').fire('click');await h.flush();assert.equal(typeof release,'function');
    h.notifyTool(latest);await h.flush();release();await submission;await h.flush();
    assert.equal(h.elements.get('copy-merge').disabled,true);assert.match(h.elements.get('report-state').textContent,/queued/);
    assert.equal(reads,3);assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,1);
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};
    await h.elements.get('copy-merge').fire('click');assert.deepEqual(copies,[]);assert.equal(h.prompts.length,0);
  });
  await test('notifications during the pending write are reconciled by the mandatory post-acknowledgment read', async () => {
    const own={...r('queued'),report_sha256:'d'.repeat(64),summary_status:'caller_submitted'};
    const latest={...own,report_sha256:'e'.repeat(64)};let stored=r(),reads=0,release;
    const h=setup(stored,name=>{
      if(name==='finalize_analysis_report') return new Promise(resolve=>{release=()=>resolve(own);});
      assert.equal(name,'get_prepared_video');reads++;return {prepared:job('first'),analysis:a(),report:stored};
    });
    await h.flush();h.elements.get('report-summary').value='explicit text';h.elements.get('report-provenance').value='owned fixture';
    const submission=h.elements.get('finalize-report').fire('click');await h.flush();
    stored=latest;h.notifyTool(latest);await h.flush();assert.equal(reads,1);
    release();await submission;await h.flush();assert.equal(reads,2);
    assert.equal(h.elements.get('copy-merge').disabled,true);assert.match(h.elements.get('report-state').textContent,/queued/);
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,1);
  });
  await test('an old job submission cannot release a new job pending-write guard', async () => {
    let releaseFirst,releaseSecond,secondSubmitted=false,reads=0;
    const secondReport=()=>({...r(secondSubmitted?'queued':'summary_pending'),job_id:'second'});
    const h=setup(r(),(name,args)=>{
      if(name==='get_prepared_video') {reads++;return {prepared:job(args.job_id),analysis:{...a(),job_id:args.job_id},report:args.job_id==='second'?secondReport():r()};}
      assert.equal(name,'finalize_analysis_report');
      return new Promise(resolve=>{
        if(args.job_id==='first') releaseFirst=()=>resolve({...r('queued'),summary_status:'caller_submitted'});
        else {secondSubmitted=true;releaseSecond=()=>resolve(secondReport());}
      });
    });
    await h.flush();h.elements.get('report-summary').value='first';h.elements.get('report-provenance').value='fixture';
    const first=h.elements.get('finalize-report').fire('click');await h.flush();
    h.notifyTool(job('second'));await h.flush();h.elements.get('report-summary').value='second';
    const second=h.elements.get('finalize-report').fire('click');await h.flush();
    releaseFirst();await first;await h.flush();const before=reads;
    h.notifyTool(secondReport());await h.flush();assert.equal(reads,before);
    assert.equal(h.elements.get('finalize-report').disabled,true);
    releaseSecond();await second;await h.flush();
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,2);
    assert.equal(h.elements.get('copy-merge').disabled,true);
  });
  await test('an absent authoritative report clears a previously enabled merge instruction', async () => {
    const ready={...r('report_ready'),verified:true,file_id:'old-report',merge_text:'OLD MERGE'};let reads=0;
    const h=setup(ready,name=>{
      assert.equal(name,'get_prepared_video');return {prepared:job('first'),analysis:a(),report:++reads===1?ready:null};
    });
    await h.flush();assert.equal(h.elements.get('copy-merge').disabled,false);
    h.notifyTool(ready);await h.flush();
    assert.equal(h.elements.get('copy-merge').disabled,true);assert.equal(h.elements.get('analysis-report').hidden,true);
    assert.equal(reads,2);
  });
  await test('a failed authoritative notification refresh blocks old merge copy without replaying a write', async () => {
    const ready={...r('report_ready'),verified:true,file_id:'old-report',merge_text:'OLD MERGE'};let reads=0;
    const h=setup(ready,name=>{
      assert.equal(name,'get_prepared_video');
      if(++reads>1) throw new Error('REPORT_READ: offline fixture');
      return {prepared:job('first'),analysis:a(),report:ready};
    });
    await h.flush();h.notifyTool(ready);await h.flush();
    assert.equal(h.elements.get('copy-merge').disabled,true);assert.match(h.elements.get('status').textContent,/REPORT_READ: offline fixture/);
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};
    await h.elements.get('copy-merge').fire('click');assert.deepEqual(copies,[]);
    assert(h.calls.every(x=>x.name==='get_prepared_video'));assert.equal(h.prompts.length,0);
  });
  await test('verified same-analysis report copies literal conflict-aware instruction only on click', async () => {
    const merge='合併：讀取同資料夾的 A_server_analysis.md，和你剛才的分析合併成一份摘要。講話內容以 A 為主，畫面上的文字和數字以你讀到的為主；兩邊不一致的地方列成衝突清單，不要自己選一邊。';const h=setup({...r('report_ready'),verified:true,file_id:'fixture-report',merge_text:merge});await h.flush();
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};assert.equal(h.elements.get('copy-merge').disabled,false);assert.deepEqual(copies,[]);
    await h.click('copy-merge');assert.deepEqual(copies,[merge]);assert.equal(h.prompts.length,0);
  });
  await test('new preparation clears old report and never injects A text into initial B handoff', async () => {
    const h=setup({...r('report_ready'),verified:true,file_id:'fixture-report',merge_text:'private A result'});await h.flush();
    h.notifyTool(pending('next-prepare','prepare'));await h.flush();assert.equal(h.elements.get('analysis-report').hidden,true);
    assert(!h.elements.get('drive-prompt').value.includes('private A result'));assert.equal(h.prompts.length,0);
  });
  function queuedReport(status = 'running') { return { ...r(status),summary_status:'caller_submitted',operation:pending('report-1','report',status) }; }
  function readyReport() { return { ...r('report_ready'),summary_status:'caller_submitted',verified:true,file_id:'fixture-report',merge_text:'合併：讀取同資料夾的 A_server_analysis.md，和你剛才的分析合併成一份摘要。' }; }
  function reportCompleted(result = readyReport()) { return { ...completed(result,'report','report-1'),operation:'report' }; }
  function fillSummary(h) { h.elements.get('report-summary').value='explicit summary';h.elements.get('report-provenance').value='unknown author'; }
  function awaitingReport() { return { ...r('awaiting_segments'),summary_status:'caller_submitted' }; }
  for(const origin of ['submission','notification','restore']) await test(origin+' follows a submitted report while its Drive prerequisites finish', async () => {
    let reads=0;
    const h=setup(origin==='restore'?awaitingReport():r(),name=> {
      if(name==='get_prepared_video') return {prepared:job('first'),analysis:a(),report:++reads===1?(origin==='restore'?awaitingReport():r()):reads<=(origin==='restore'?2:3)?awaitingReport():queuedReport()};
      if(name==='finalize_analysis_report') return awaitingReport();
      if(name==='get_video_job') return reportCompleted();
      throw new Error('Unexpected '+name);
    });
    await h.flush();
    if(origin==='submission') { fillSummary(h);await h.click('finalize-report'); }
    if(origin==='notification') { h.notifyTool(awaitingReport());await h.flush(); }
    for(let i=0;i<3;i++) {
      assert.equal(h.elements.get('copy-merge').disabled,true);
      assert(!h.elements.get('report-state').textContent.includes('已放進 Drive'));
      await h.poll();
    }
    assert.equal(reads,origin==='restore'?3:4);assert.equal(h.elements.get('copy-merge').disabled,false);
    assert.match(h.elements.get('report-state').textContent,/A 已完成/);
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,origin==='submission'?1:0);
    assert(h.calls.every(x=>['get_prepared_video','get_video_job','finalize_analysis_report'].includes(x.name)));
  });
  await test('waiting report snapshot errors and mismatched bindings stop without resubmission', async () => {
    const message='REPORT_WAIT: 完整快照錯誤';
    for(const changed of [null,{prepared:job('other')},{report:null},{report:{...awaitingReport(),job_id:'other'}},
                         ...['cache_signature','analysis_sha256','report_sha256'].map(key=>({report:{...awaitingReport(),[key]:'f'.repeat(64)}}))]) {
      let reads=0;
      const snapshot={prepared:job('first'),analysis:a(),report:awaitingReport()};
      const h=setup(awaitingReport(),()=> {
        if(++reads===1) return snapshot;
        if(changed===null) throw new Error(message);
        return {...snapshot,...changed};
      });
      await h.flush();await h.poll();
      assert.equal(h.elements.get('copy-merge').disabled,true);
      assert.match(h.elements.get('status').textContent,changed===null?/REPORT_WAIT: 完整快照錯誤/:/不符/);
      assert(![...h.timers.values()].some(x=>x.ms<=10000));
      assert(h.calls.every(x=>x.name==='get_prepared_video'));
    }
  });
  await test('waiting snapshot polling is bounded and explicit refresh can start a new read-only wait', async () => {
    const snapshot={prepared:job('first'),analysis:a(),report:awaitingReport()};
    const waiting=setup(awaitingReport(),()=>snapshot);await waiting.flush();
    for(let i=0;i<100&&[...waiting.timers.values()].some(x=>x.ms<=10000);i++) await waiting.poll();
    assert(![...waiting.timers.values()].some(x=>x.ms<=10000));
    assert.match(waiting.elements.get('status').textContent,/重新取得狀態/);
    assert(waiting.calls.length<=61);assert(waiting.calls.every(x=>x.name==='get_prepared_video'));
    await waiting.click('refresh');assert([...waiting.timers.values()].some(x=>x.ms<=10000));
    waiting.teardown();
  });
  for(const action of ['switch','teardown']) await test(action+' discards an in-flight waiting report snapshot', async () => {
    let reads=0,release;
    const h=setup(awaitingReport(),(name,args)=> {
      assert.equal(name,'get_prepared_video');
      if(args.job_id!=='first') return {prepared:job(args.job_id),analysis:{...a(),job_id:args.job_id},report:null};
      if(++reads===1) return {prepared:job('first'),analysis:a(),report:awaitingReport()};
      return new Promise(resolve=>{release=()=>resolve({prepared:job('first'),analysis:a(),report:readyReport()});});
    });
    await h.flush();await h.poll();assert.equal(typeof release,'function');
    if(action==='switch') h.notifyTool(job('second'));else h.teardown();await h.flush();
    release();await h.flush();
    assert.equal(h.elements.get('copy-merge').disabled,true);
    assert(![...h.timers.values()].some(x=>x.ms<=10000));
  });
  await test('A completion automatically reads report once and honestly waits for ChatGPT summary', async () => {
    let reads=0;
    const h=harness({respond(name,args) {
      if(name==='get_prepared_video') return {prepared:job('first'),analysis:++reads===1?null:a(),report:reads===1?null:r()};
      if(name==='analyze_prepared_video') return pending();
      if(name==='get_video_job') return completed(a());
      throw new Error('Unexpected '+name);
    }});
    await h.flush();await h.click('analyze');await h.poll();
    assert.equal(reads,2);assert.equal(h.elements.get('analysis-report').hidden,false);
    assert.match(h.elements.get('report-state').textContent,/A 分析已完成.*等待 ChatGPT 整理摘要/);
    assert(!h.elements.get('report-state').textContent.includes('已放進 Drive'));assert.equal(h.elements.get('copy-merge').disabled,true);
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,0);
  });
  await test('complete evidence completion does not tell the user to wait for an optional summary', async () => {
    let reads=0;
    const h=harness({respond(name,args) {
      if(name==='get_prepared_video') return {prepared:job('first'),analysis:++reads===1?null:a(),report:reads===1?null:{...r('awaiting_segments'),evidence_scope:'complete'}};
      if(name==='analyze_prepared_video') return pending();
      if(name==='get_video_job') return completed(a());
      throw new Error('Unexpected '+name);
    }});
    await h.flush();await h.click('analyze');await h.poll();
    assert.match(h.elements.get('report-state').textContent,/自動放進 Drive（不需摘要）/);
    assert(!h.elements.get('status').textContent.includes('等待 ChatGPT 整理摘要後才會上傳'));
    assert.equal(h.calls.filter(x=>x.name==='analyze_prepared_video').length,1);
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,0);
  });
  await test('report snapshot read failure displays full error without replaying A', async () => {
    let reads=0;
    const h=harness({respond(name) {
      if(name==='get_prepared_video') { if(++reads>1) throw new Error('REPORT_READ: 完整快照錯誤');return {prepared:job('first'),analysis:null}; }
      if(name==='analyze_prepared_video') return pending();
      if(name==='get_video_job') return completed(a());
      throw new Error('Unexpected '+name);
    }});
    await h.flush();await h.click('analyze');await h.poll();
    assert.match(h.elements.get('status').textContent,/REPORT_READ: 完整快照錯誤/);
    assert.equal(h.elements.get('findings').hidden,false);assert.equal(h.calls.filter(x=>x.name==='analyze_prepared_video').length,1);
  });
  await test('summary submission polls queued and running report with bounded backoff then enables spec merge', async () => {
    let polls=0,stored=r();
    const h=setup(stored,name=>name==='get_prepared_video'?{prepared:job('first'),analysis:a(),report:stored}:name==='finalize_analysis_report'?(stored=queuedReport('queued')):name==='get_video_job'?(++polls<4?pending('report-1','report','running'):reportCompleted()):null);
    await h.flush();fillSummary(h);await h.click('finalize-report');
    const delays=[];
    for(let i=0;i<4;i++) {
      const timer=[...h.timers.values()].find(x=>x.ms<=10000);assert(timer,'Report must schedule a poll');delays.push(timer.ms);
      assert.equal(h.elements.get('copy-merge').disabled,true);assert(!h.elements.get('report-state').textContent.includes('已放進 Drive'));
      await h.poll();
    }
    assert(delays.every(x=>x>=1000&&x<=10000));assert(delays[1]>delays[0]);assert(delays[3]>=delays[2]);
    assert.equal(h.elements.get('copy-merge').disabled,false);assert.match(h.elements.get('report-state').textContent,/A 已完成：A_server_analysis.md 已放進 Drive/);
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};await h.click('copy-merge');assert(copies[0].startsWith('合併：'));
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,1);assert(h.calls.filter(x=>x.name==='get_video_job').every(x=>x.args.operation_id==='report-1'));
    assert(![...h.timers.values()].some(x=>x.ms<=10000));
  });
  await test('ChatGPT finalize notification and restored running report adopt polling without resubmission', async () => {
    for(const restored of [false,true]) {
      let stored=restored?queuedReport():r();
      const h=setup(stored,name=>name==='get_prepared_video'?{prepared:job('first'),analysis:a(),report:stored}:reportCompleted());
      await h.flush();if(!restored) {stored=queuedReport();h.notifyTool(stored);}await h.flush();await h.poll();
      assert.equal(h.elements.get('copy-merge').disabled,false);assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,0);
    }
  });
  for(const failure of ['operation','transport']) await test('report polling '+failure+' failure displays complete error and stops', async () => {
    const message='REPORT_UPLOAD: 完整錯誤，驗證失敗 detail=checksum';
    const h=setup(queuedReport(),name=> {
      if(name==='get_prepared_video') return {prepared:job('first'),analysis:a(),report:queuedReport()};
      if(failure==='transport') throw new Error(message);
      return {...pending('report-1','report','failed'),error:{message}};
    });
    await h.flush();await h.poll();assert(h.elements.get('status').textContent.includes(message));assert.equal(h.elements.get('copy-merge').disabled,true);
    assert(![...h.timers.values()].some(x=>x.ms<=10000));assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,0);
  });
  for(const action of ['switch','teardown']) await test(action+' stops scheduled and in-flight report polls', async () => {
    for(const inFlight of [false,true]) {
      let release;
      const h=setup(queuedReport(),(name,args)=> {
        if(name==='get_prepared_video') return {prepared:job(args.job_id),analysis:{...a(),job_id:args.job_id},report:args.job_id==='first'?queuedReport():null};
        return new Promise(resolve=>{release=()=>resolve(reportCompleted());});
      });
      await h.flush();if(inFlight) await h.poll();
      if(action==='switch') h.notifyTool(job('second'));else h.teardown();await h.flush();
      if(release) {release();await h.flush();}
      assert(![...h.timers.values()].some(x=>x.ms<=10000));assert.equal(h.elements.get('copy-merge').disabled,true);
      assert.equal(h.calls.filter(x=>x.name==='get_video_job').length,inFlight?1:0);
    }
  });
  await test('mismatched report poll cannot enable merge and exposes binding error', async () => {
    for(const changed of [{operation_id:'wrong'},{job_id:'wrong'},{result:{...readyReport(),report_sha256:'d'.repeat(64)}}]) {
      const h=setup(queuedReport(),name=>name==='get_prepared_video'?{prepared:job('first'),analysis:a(),report:queuedReport()}:{...reportCompleted(),...changed});
      await h.flush();await h.poll();assert.equal(h.elements.get('copy-merge').disabled,true);assert.match(h.elements.get('status').textContent,/不符/);
      assert(![...h.timers.values()].some(x=>x.ms<=10000));
    }
  });
  await test('invalid and unverified reports never claim a Drive upload', async () => {
    for(const report of [{...r('report_ready'),verified:false},{...r('report_invalid'),error:'REPORT_STATE_INVALID: 完整核對錯誤'}]) {
      const h=setup(report);await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);assert(!h.elements.get('report-state').textContent.includes('已放進 Drive'));
      if(report.error) assert(h.elements.get('report-state').textContent.includes(report.error));
    }
  });
  await test('authoritative active report restores a single polling loop', async () => {
    const h=setup(queuedReport(),name=>name==='get_prepared_video'?{prepared:job('first'),analysis:a(),report:queuedReport(),active_operation:queuedReport().operation,latest_operation:queuedReport().operation}:pending('report-1','report','running'));
    await h.flush();assert.equal([...h.timers.values()].filter(x=>x.ms<=10000).length,1);
    await h.poll();assert.equal(h.calls.filter(x=>x.name==='get_video_job').length,1);
    assert.equal([...h.timers.values()].filter(x=>x.ms<=10000).length,1);
  });
  for(const scope of ['speech_only','complete']) await test(scope+' evidence report uploads on its own and the widget follows it without a summary', async () => {
    // Field test c9289f71a482: the widget told the user to wait for a ChatGPT summary that was never needed.
    const auto=(status='awaiting_segments',extra={})=>({...r(status),evidence_status:scope==='complete'?'complete':'partial',evidence_scope:scope,...extra});
    let reads=0;
    const h=setup(auto(),name=> {
      if(name==='get_prepared_video') return {prepared:job('first'),analysis:a(),report:++reads===1?auto():auto('queued',{operation:pending('report-1','report','queued')})};
      if(name==='get_video_job') return reportCompleted({...readyReport(),summary_status:'pending_chatgpt',evidence_scope:scope});
      throw new Error('Unexpected '+name);
    });
    await h.flush();
    const text=h.elements.get('report-state').textContent;
    assert.match(text,scope==='speech_only'?/只含語音.*自動放進 Drive（不需摘要）/:/A 證據完整.*自動放進 Drive（不需摘要）/);
    assert(!text.includes('等待 ChatGPT 整理摘要'));
    await h.poll();assert.match(h.elements.get('report-state').textContent,/A 報告排隊／上傳中/);
    await h.poll();assert.equal(h.elements.get('copy-merge').disabled,false);assert.match(h.elements.get('report-state').textContent,/A 已完成/);
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,0);assert.equal(h.prompts.length,0);
  });
  await test('unattended report without Drive explains instead of polling forever', async () => {
    const h=harness({snapshot:{prepared:job('first'),analysis:a(),report:{...r('awaiting_segments'),evidence_scope:'complete'},drive_configured:false}});
    await h.flush();
    assert.match(h.elements.get('report-state').textContent,/未設定 Google Drive/);
    assert(![...h.timers.values()].some(x=>x.ms<=10000));assert.deepEqual(h.calls.map(x=>x.name),['get_prepared_video']);
  });
  await test('failed unattended upload reports the failure instead of asking for a summary', async () => {
    const failed={...r('failed'),evidence_status:'partial',evidence_scope:'speech_only',operation:{...pending('report-1','report','failed'),error:{message:'REPORT_UPLOAD: 完整錯誤'}}};
    const h=setup(failed);await h.flush();
    const text=h.elements.get('report-state').textContent;
    assert(!text.includes('等待 ChatGPT 整理摘要'));assert.match(text,/尚未確認上傳至 Drive/);assert(text.includes('REPORT_UPLOAD: 完整錯誤'));
    assert(![...h.timers.values()].some(x=>x.ms<=10000));
  });
  await test('analysis that lacks a half recommends route B with the reason', async () => {
    const lacking={...a(),status:'partial',quality:{...a().quality,status:'needs_review',recommend_route_b:true,route_b_reasons:['伺服器畫面分析失敗或不完整']}};
    const h=harness({snapshot:{prepared:job('first'),analysis:lacking,report:null}});await h.flush();
    assert.match(h.elements.get('quality').textContent,/建議加跑 B（伺服器畫面分析失敗或不完整）/);
    const fine=harness({snapshot:{prepared:job('first'),analysis:a(),report:null}});await fine.flush();
    assert(!fine.elements.get('quality').textContent.includes('建議加跑 B'));
  });
  await test('merge copy falls back to a selected textarea when the host refuses the clipboard', async () => {
    const merge='合併：讀取同資料夾的 A_server_analysis.md。';
    for(const exec of [undefined,()=>true]) {
      const h=setup({...r('report_ready'),verified:true,file_id:'fixture-report',merge_text:merge});await h.flush();
      h.context.navigator={clipboard:{writeText:async()=>{throw new Error('NotAllowedError');}}};
      if(exec) h.context.document.execCommand=exec;
      await h.click('copy-merge');
      const box=h.elements.get('merge-prompt');assert.equal(box.value,merge);assert(box.selected);
      assert.match(h.elements.get('status').textContent,exec?/已複製已核對報告的合併指令/:/Ctrl\+C/);
    }
  });
  if(failures) throw new Error(failures+' widget report scenarios failed');
  console.log('PASS: '+count+' A report inert widget controls; no actual ChatGPT/Google/provider acceptance.');
})().catch(error=>{console.error(error);process.exitCode=1;});
