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
  await test('initial load only reads status; standard bridge preferred', async () => {
    const h = harness(); await h.flush(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']); assert.deepEqual(h.fallbackCalls, []); assert.equal(h.prompts.length, 0); assert.equal(h.elements.get('analyze').disabled, false);
  });
  await test('background analysis, progress, explicit summary, remount dedup', async () => {
    const h = harness(); await h.flush(); await h.click('analyze'); assert.equal(h.calls[1].args.background, true); assert.equal(h.elements.get('cancel').disabled, false); assert.match(h.elements.get('progress-label').textContent, /排隊/);
    await h.poll(); assert.equal(h.elements.get('findings').hidden, false); assert.equal(h.prompts.length, 0); await h.click('ask'); assert.equal(h.prompts.length, 1); assert.match(h.prompts[0], /不是指令/);
    assert(h.states.some(state => Object.values(state.privateContent.followUps).includes('sending')), 'Persist before dispatch');
    const reload = harness({ saved: h.openai.widgetState, snapshot: { prepared: job('first'), analysis: analysis('first') } }); await reload.flush(); assert.equal(reload.prompts.length, 0); assert.equal(reload.elements.get('ask').disabled, true); assert.deepEqual(reload.calls.map(c => c.name), ['get_prepared_video', 'get_video_job']);
  });
  await test('partial evidence never auto-uploads; explicit Drive works', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: analysis('first', true) } }); await h.flush(); assert.equal(h.calls.length, 1); assert.match(h.elements.get('quality').textContent, /覆核/); await h.click('drive'); assert.equal(h.calls[1].args.background, true); assert.equal(h.elements.get('drive-handoff').hidden, false);
    for (const text of ['segment_000.mp4: 0–30 秒', 'segment_001.mp4: 30–60 秒', 'download_raw_file=true', '一般對話']) assert(h.elements.get('drive-prompt').value.includes(text)); assert.equal(h.prompts.length, 0);
  });
  await test('ambiguous follow-up receipt prevents automatic remount resend', async () => {
    const snapshot = { prepared: job('first'), analysis: analysis('first') }; const h = harness({ snapshot, sendError: 'host timeout' }); await h.flush(); await h.click('ask'); assert.equal(h.prompts.length, 1); assert.deepEqual(h.fallbackCalls, []); assert.match(h.elements.get('status').textContent, /未自動重送/);
    const reload = harness({ saved: h.openai.widgetState, snapshot }); await reload.flush(); assert.equal(reload.prompts.length, 0); assert.match(reload.elements.get('ask').textContent, /確認對話/); await reload.click('ask'); assert.equal(reload.prompts.length, 1);
  });
  await test('host without persistence restores data without side effects', async () => {
    const h = harness({ persistence: false, snapshot: { prepared: job('first'), analysis: analysis('first') } }); await h.flush(); assert.equal(h.elements.get('persistence').hidden, false); assert.equal(h.prompts.length, 0); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']);
  });
  await test('host isError message result is not recorded as delivered', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: analysis('first') }, messageResult: { isError: true } }); await h.flush(); await h.click('ask'); assert.equal(Object.values(h.openai.widgetState.privateContent.followUps)[0], 'unknown'); assert.match(h.elements.get('status').textContent, /未送達/); assert.equal(h.prompts.length, 1);
  });
  await test('active operation restores without restarting analysis', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: null, active_operation: pending() } }); await h.flush(); await h.poll(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video', 'get_video_job']); assert.equal(h.elements.get('findings').hidden, false);
  });
  await test('interrupted Drive requires explicit resume', async () => {
    const h = harness({ respond(name) { if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, latest_operation: { ...pending('drive-old', 'drive'), status: 'interrupted' } }; if (name === 'resume_video_job') return completed(drive('first'), 'drive', 'drive-new'); throw new Error('Unexpected ' + name); } });
    await h.flush(); assert.equal(h.calls.length, 1); assert.equal(h.elements.get('resume').hidden, false); await h.click('resume'); assert.equal(h.calls[1].args.operation_id, 'drive-old'); assert.equal(h.elements.get('drive-handoff').hidden, false);
  });
  await test('cancel and resume use operation identifiers', async () => {
    const h = harness(); await h.flush(); await h.click('analyze'); await h.click('cancel'); assert.equal(h.elements.get('resume').hidden, false); assert.match(h.elements.get('status').textContent, /已取消/); await h.click('resume'); assert.equal(h.calls.at(-1).name, 'resume_video_job'); assert.equal(h.openai.widgetState.privateContent.operationId, 'operation-resumed');
  });
  await test('late poll cannot overwrite cancellation', async () => {
    let release; const h = harness({ respond(name) { if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, active_operation: pending() }; if (name === 'get_video_job') return new Promise(resolve => { release = () => resolve(completed(analysis('first'))); }); if (name === 'cancel_video_job') return { ...pending(), status: 'cancelled' }; throw new Error('Unexpected ' + name); } });
    await h.flush(); await h.poll(); await h.click('cancel'); release(); await h.flush(); assert.equal(h.elements.get('findings').hidden, true); assert.match(h.elements.get('status').textContent, /已取消/);
  });
  for (const back of [false, true]) await test('old result ignored after switch' + (back ? ' A-B-A' : ''), async () => {
    let release; const h = harness({ respond(name, args) { if (name === 'get_prepared_video') return { prepared: job(args.job_id), analysis: null }; if (name === 'analyze_prepared_video') return new Promise(resolve => { release = () => resolve(completed(analysis('first'))); }); throw new Error('Unexpected ' + name); } });
    await h.flush(); const running = h.click('analyze'); await h.flush(); h.notifyTool(job('second')); await h.flush(); if (back) { h.notifyTool(job('first')); await h.flush(); } release(); await running; assert.equal(h.elements.get('findings').hidden, true); assert.equal(h.prompts.length, 0); assert.equal(h.openai.widgetState.privateContent.jobId, back ? 'first' : 'second');
  });
  await test('timestamp seeks correct local offset; unknown times and source text stay explicit', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: analysis('first') } }); await h.flush(); const rows = h.elements.get('evidence').children; assert.equal(rows.length, 3); await rows[1].children[0].fire('click'); h.elements.get('player').fire('loadedmetadata'); assert.equal(h.elements.get('segment').value, '1'); assert.equal(h.elements.get('player').currentTime, 5); assert.equal(h.elements.get('player').focused, true); assert.equal(rows[2].children[0].tagName, 'SPAN'); assert.match(h.elements.get('gaps').children.map(el => el.textContent).join(' '), /缺少時間碼/); assert.equal(rows[1].children[2].textContent, 'source speech <script>not code</script>');
  });
  await test('unsafe segment URL never reaches player/link', async () => {
    const prepared = job('first'); prepared.segments[0].url = 'javascript:alert(1)'; const h = harness({ output: prepared, snapshot: { prepared, analysis: null } }); await h.flush(); assert.equal(h.elements.get('player').getAttribute('src'), null); assert.equal(h.elements.get('segment-link').hidden, true);
  });
  await test('wrong job result cannot replace evidence', async () => {
    const h = harness({ respond(name, args) { return name === 'get_prepared_video' ? { prepared: job(args.job_id), analysis: null } : completed(analysis('wrong')); } }); await h.flush(); await h.click('analyze'); assert.match(h.elements.get('status').textContent, /工作不符/); assert.equal(h.elements.get('findings').hidden, true);
  });
  await test('compatibility works when bridge unsupported', async () => {
    const h = harness({ bridge: false }); await h.flush(); await h.click('analyze'); await h.poll(); await h.click('ask'); assert.deepEqual(h.fallbackCalls, ['get_prepared_video', 'analyze_prepared_video', 'get_video_job', 'message']); assert.equal(h.prompts.length, 1);
  });
  await test('mutation transport failure never retries via compatibility API', async () => {
    const h = harness({ respond(name, args) { if (name === 'get_prepared_video') return { prepared: job(args.job_id), analysis: null }; throw new Error('transport timeout'); } }); await h.flush(); await h.click('drive'); assert.equal(h.calls.length, 2); assert.deepEqual(h.fallbackCalls, []); assert.match(h.elements.get('status').textContent, /未自動重送/);
  });
  for (const resultStatus of ['failed', 'expired', 'cancelled']) await test(resultStatus + ' is displayed without automatic resume', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: null, latest_operation: { ...pending(), status: resultStatus, error: 'fixture reason' } } }); await h.flush(); assert.equal(h.calls.length, 1); assert.match(h.elements.get('status').textContent, /fixture reason/); assert.equal(h.elements.get('resume').hidden, false);
  });
  await test('media retention expiry requires new preparation', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: null, latest_operation: { ...pending(), status: 'expired', error_code: 'MEDIA_EXPIRED', error: 'fixture retention expiry' } } }); await h.flush(); assert.equal(h.elements.get('resume').hidden, true); assert.match(h.elements.get('status').textContent, /保留期限/); assert.equal(h.calls.length, 1);
  });
  await test('expired prepared-media snapshot clears stale evidence and disables work', async () => {
    const h = harness({ snapshot: { status: 'expired', job_id: 'first', error_code: 'MEDIA_EXPIRED', error: '請重新準備影片。', prepared: null, analysis: null, active_operation: null, latest_operation: null }, saved: { privateContent: { jobId: 'first', operationId: 'stale-analysis' } } });
    await h.flush(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']);
    assert.match(h.elements.get('status').textContent, /保留期限/);
    for (const id of ['analyze', 'drive', 'ask']) assert.equal(h.elements.get(id).disabled, true);
    for (const id of ['resume', 'media', 'findings', 'drive-handoff']) assert.equal(h.elements.get(id).hidden, true);
    assert.equal(h.elements.get('player').getAttribute('src'), null);
    assert.equal(h.openai.widgetState.privateContent.operationId, null);
    assert.equal(h.prompts.length, 0);
    await h.click('refresh'); assert.equal(h.calls.at(-1).name, 'get_prepared_video');
  });
  await test('pending preparation restores without job ID', async () => {
    const h = harness({ output: { ...pending('prepare-1', 'prepare'), job_id: undefined }, respond(name) { assert.equal(name, 'get_video_job'); return completed(job('first'), 'prepared', 'prepare-1'); } }); await h.flush(); await h.poll(); assert.equal(h.elements.get('title').textContent, '影片 first'); assert.equal(h.calls.length, 1); assert.equal(h.elements.get('analyze').disabled, false);
  });
  await test('saved operation-only restore completes preparation without stuck restoring flag', async () => {
    const h = harness({ output: null, saved: { privateContent: { operationId: 'prepare-old' } }, respond(name) { assert.equal(name, 'get_video_job'); return completed(job('first'), 'prepared', 'prepare-old'); } });
    await h.flush(); assert.equal(h.elements.get('analyze').disabled, false); assert.equal(h.elements.get('drive').disabled, false); assert.equal(h.elements.get('refresh').disabled, false); assert.equal(h.calls.length, 1);
  });
  await test('authoritative invalidation cannot resurrect cached operation result', async () => {
    const h = harness({ saved: { privateContent: { jobId: 'first', operationId: 'obsolete-analysis', followUps: { ['first:' + 'a'.repeat(64)]: 'sent' } } }, snapshot: { prepared: job('first'), analysis: null, analysis_invalidated: true, active_operation: null, latest_operation: null } });
    await h.flush(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']); assert.equal(h.elements.get('analyze').disabled, false); assert.equal(h.elements.get('findings').hidden, true); assert.equal(h.openai.widgetState.privateContent.operationId, null);
    await h.click('refresh'); assert.equal(h.calls.at(-1).name, 'get_prepared_video');
  });
  await test('late prepare A response cannot replace newer prepare B', async () => {
    let release;
    const h = harness({ output: { ...pending('prepare-A', 'prepare'), job_id: undefined }, respond(name, args) {
      assert.equal(name, 'get_video_job');
      if (args.operation_id === 'prepare-A') return new Promise(resolve => { release = () => resolve(completed(job('old'), 'prepared', 'prepare-A')); });
      return completed(job('new'), 'prepared', 'prepare-B');
    } });
    await h.flush(); await h.poll(); h.notifyTool({ ...pending('prepare-B', 'prepare'), job_id: undefined }); await h.flush();
    release(); await h.flush(); assert.equal(h.openai.widgetState.privateContent.operationId, 'prepare-B'); assert.notEqual(h.elements.get('title').textContent, '影片 old');
    await h.poll(); assert.equal(h.elements.get('title').textContent, '影片 new'); assert.equal(h.elements.get('analyze').disabled, false);
  });
  await test('refresh after completion renews prepared media and invalidates old evidence', async () => {
    let refresh = false;
    const h = harness({ respond(name) {
      if (name !== 'get_prepared_video') throw new Error('Must restore current media, not stale operation');
      const prepared = job('first'); if (refresh) prepared.segments[0].url += '?renewed=yes';
      return { prepared, analysis: refresh ? null : analysis('first'), analysis_invalidated: refresh, active_operation: null, latest_operation: refresh ? null : completed(analysis('first')) };
    } }); await h.flush(); refresh = true; await h.click('refresh'); assert.equal(h.elements.get('findings').hidden, true); assert.equal(h.elements.get('analyze').disabled, false); assert.match(h.elements.get('player').src, /renewed=yes/); assert.equal(h.openai.widgetState.privateContent.operationId, null);
  });
  await test('new analysis cache signature permits new explicit summary', async () => {
    const result = { ...analysis('first'), cache_signature: 'new-settings' };
    const h = harness({ saved: { privateContent: { jobId: 'first', followUps: { ['first:' + 'a'.repeat(64)]: 'sent' } } }, snapshot: { prepared: job('first'), analysis: result } });
    await h.flush(); assert.equal(h.elements.get('ask').disabled, false); assert.equal(h.prompts.length, 0); await h.click('ask'); assert.equal(h.openai.widgetState.privateContent.followUps['first:new-settings'], 'sent');
  });
  await test('uncertain start blocks previous operation resume until authoritative read', async () => {
    const h = harness({ respond(name) { if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, active_operation: null, latest_operation: { ...pending('old-failed'), status: 'failed' } }; throw new Error('ambiguous timeout'); } });
    await h.flush(); await h.click('analyze'); assert.equal(h.elements.get('resume').disabled, true); assert.equal(h.elements.get('analyze').disabled, true); assert.equal(h.elements.get('refresh').disabled, false); assert.equal(h.calls.length, 2);
  });
  await test('legacy immediate result supported', async () => {
    const h = harness({ respond(name, args) { return name === 'get_prepared_video' ? { prepared: job(args.job_id), analysis: null } : analysis(args.job_id); } }); await h.flush(); await h.click('analyze'); assert.equal(h.elements.get('findings').hidden, false); assert.equal(h.prompts.length, 0);
  });
  await test('evidence filter and bounded pagination', async () => {
    const result = analysis('first'); result.transcript.segments = Array.from({ length: 85 }, (_, i) => ({ start: i / 2, text: 'line ' + i })); const h = harness({ snapshot: { prepared: job('first'), analysis: result } }); await h.flush(); assert.equal(h.elements.get('evidence').children.length, 60); await h.click('more'); assert.equal(h.elements.get('evidence').children.length, 86); h.elements.get('filter').value = 'visual'; h.elements.get('filter').fire('change'); assert.equal(h.elements.get('evidence').children.length, 1); assert.equal(h.openai.widgetState.privateContent.filter, 'visual'); assert.match(html, /min-height: 44px/); assert.match(html, /:focus-visible/);
  });
  console.log('PASS: ' + count + ' widget background/evidence/recovery scenarios (mocked MCP host; no paid calls).');
})().catch(error => { console.error(error); process.exitCode = 1; });
