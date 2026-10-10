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
  focus() { this.focused = true; } load() { this.loads = (this.loads || 0) + 1; } fire(event) { return this.listeners[event]?.({ target: this }); }
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
        if (message.method === 'ui/initialize') { assert.equal(message.params.appInfo?.name, 'video-timeline-pipeline'); assert.equal(message.params.protocolVersion, '2026-01-26'); assert.equal(typeof message.params.appCapabilities, 'object'); }
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
  return { elements, calls, prompts, states, openai, fallbackCalls, methods, notifyTool, context,
    async flush() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); },
    async click(id) { const element = elements.get(id); assert(!element.disabled, id + ' is disabled'); await element.fire('click'); await this.flush(); },
    async poll() { const next = [...timers.entries()].find(([, timer]) => timer.ms <= 10000); assert(next, 'No poll timer'); timers.delete(next[0]); next[1].callback(); await this.flush(); } };
}
let count = 0;
async function test(name, run) { await run(); count++; console.log('ok ' + count + ' - ' + name); }
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
  });
  await test('only explicit summary+provenance submission sends exact current analysis binding', async () => {
    const h=setup(r(),(name,args)=>name==='get_prepared_video'?{prepared:job('first'),analysis:a(),report:r()}:({...r('queued'),summary_status:'caller_submitted'}));
    await h.flush();h.elements.get('report-summary').value='user source text';h.elements.get('report-provenance').value='unknown author';await h.click('finalize-report');
    const sent=h.calls.find(x=>x.name==='finalize_analysis_report');assert.deepEqual(sent.args,{job_id:'first',cache_signature:'a'.repeat(64),analysis_sha256:'b'.repeat(64),summary_text:'user source text',provenance_note:'unknown author'});
    assert.equal(h.calls.filter(x=>x.name==='finalize_analysis_report').length,1);assert.match(h.elements.get('report-state').textContent,/queued/);
  });
  await test('unverified or stale report never enables merge copy', async () => {
    const h=setup({...r('report_ready'),verified:false,file_id:'fixture',merge_text:'must not copy'});await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
    h.notifyTool({...r('report_ready'),cache_signature:'f'.repeat(64),verified:true,file_id:'fixture',merge_text:'stale'});await h.flush();assert.equal(h.elements.get('copy-merge').disabled,true);
  });
  await test('verified same-analysis report copies literal conflict-aware instruction only on click', async () => {
    const merge='語音以 A；畫面文字以 B；衝突與未知明列';const h=setup({...r('report_ready'),verified:true,file_id:'fixture-report',merge_text:merge});await h.flush();
    const copies=[];h.context.navigator={clipboard:{writeText:async text=>copies.push(text)}};assert.equal(h.elements.get('copy-merge').disabled,false);assert.deepEqual(copies,[]);
    await h.click('copy-merge');assert.deepEqual(copies,[merge]);assert.equal(h.prompts.length,0);
  });
  await test('new preparation clears old report and never injects A text into initial B handoff', async () => {
    const h=setup({...r('report_ready'),verified:true,file_id:'fixture-report',merge_text:'private A result'});await h.flush();
    h.notifyTool(pending('next-prepare','prepare'));await h.flush();assert.equal(h.elements.get('analysis-report').hidden,true);
    assert(!h.elements.get('drive-prompt').value.includes('private A result'));assert.equal(h.prompts.length,0);
  });
  console.log('PASS: '+count+' A report inert widget controls; no actual ChatGPT/Google/provider acceptance.');
})().catch(error=>{console.error(error);process.exitCode=1;});
