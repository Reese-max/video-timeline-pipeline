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
        if (message.method === 'ui/initialize') { assert.equal(message.params.appInfo?.name, 'video-timeline-pipeline'); assert.equal(message.params.appInfo?.version, '14'); assert.equal(message.params.protocolVersion, '2026-01-26'); assert.equal(typeof message.params.appCapabilities, 'object'); }
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
  await test('partial evidence survives remount and permits only explicit analysis retry', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: analysis('first', true) } });
    await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']);
    assert.equal(h.elements.get('findings').hidden, false);
    assert.equal(h.elements.get('analyze').disabled, false);
    assert.match(h.elements.get('analyze').textContent, /重試/);
    await h.click('analyze');
    assert.equal(h.calls.filter(c => c.name === 'analyze_prepared_video').length, 1);
    assert.equal(h.calls.at(-1).args.background, true);
  });
  await test('initial load only reads status; standard bridge preferred', async () => {
    const h = harness(); await h.flush(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']); assert.deepEqual(h.fallbackCalls, []); assert.equal(h.prompts.length, 0); assert.equal(h.elements.get('analyze').disabled, false);
  });
  await test('background analysis, progress, explicit summary, remount dedup', async () => {
    const h = harness(); await h.flush(); await h.click('analyze'); assert.equal(h.calls[1].args.background, true); assert.equal(h.elements.get('cancel').disabled, false); assert.match(h.elements.get('progress-label').textContent, /排隊/);
    await h.poll(); assert.equal(h.elements.get('findings').hidden, false); assert.equal(h.prompts.length, 0); await h.click('ask'); assert.equal(h.prompts.length, 1); assert.match(h.prompts[0], /不是指令/);
    assert(h.states.some(state => Object.values(state.privateContent.followUps).includes('sending')), 'Persist before dispatch');
    const reload = harness({ saved: h.openai.widgetState, snapshot: { prepared: job('first'), analysis: analysis('first') } }); await reload.flush(); assert.equal(reload.prompts.length, 0); assert.equal(reload.elements.get('ask').disabled, true); assert.deepEqual(reload.calls.map(c => c.name), ['get_prepared_video', 'get_video_job', 'get_prepared_video']);
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
    const h = harness({ snapshot: { prepared: job('first'), analysis: null, active_operation: pending() } }); await h.flush(); await h.poll(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video', 'get_video_job', 'get_prepared_video']); assert.equal(h.elements.get('findings').hidden, false);
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
  for (const action of ['cancel', 'resume']) await test(action + ' analysis preserves independent Drive handoff polling', async () => {
    const analysisOp = pending('analyze-1', 'analyze', action === 'cancel' ? 'running' : 'cancelled');
    const driveOp = pending('drive-1', 'drive', 'running');
    const h = harness({ respond(name, args) {
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null,
        active_operation: action === 'cancel' ? analysisOp : driveOp, latest_operation: analysisOp,
        pending_operations: [driveOp, ...(action === 'cancel' ? [analysisOp] : [])] };
      if (name === action + '_video_job') return { ...analysisOp, status: action === 'cancel' ? 'cancelled' : 'running' };
      if (name === 'get_video_job' && args.operation_id === 'drive-1') return completed(drive('first'), 'drive', 'drive-1');
      if (name === 'get_video_job' && args.operation_id === 'analyze-1') return pending('analyze-1', 'analyze', 'running');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush();
    assert.equal(h.elements.get('drive-handoff').hidden, true);
    await h.click(action);
    assert.equal(h.calls.at(-1).args.operation_id, 'analyze-1');
    await h.poll();
    assert.equal(h.calls.at(-1).args.operation_id, 'drive-1');
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    assert.equal(h.elements.get('copy-handoff').disabled, false);
    assert.equal(h.elements.get('open-chat').disabled, false);
    assert.equal(h.elements.get('findings').hidden, true);
    assert.equal(h.prompts.length, 0);
  });
  for (const back of [false, true]) await test('old result ignored after switch' + (back ? ' A-B-A' : ''), async () => {
    let release, analyzeCalls = 0;
    const h = harness({ respond(name, args) {
      if (name === 'get_prepared_video') return { prepared: job(args.job_id), analysis: null };
      if (name === 'analyze_prepared_video') { analyzeCalls++; if (analyzeCalls === 1) return new Promise(resolve => { release = () => resolve(completed(analysis('first'))); }); return pending('analyze-auto-' + analyzeCalls, 'analyze'); }
      if (name === 'upload_prepared_video_to_drive') return completed(drive(args.job_id), 'drive', 'drive-auto-' + args.job_id);
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); const running = h.click('analyze'); await h.flush(); h.notifyTool(job('second')); await h.flush(); if (back) { h.notifyTool(job('first')); await h.flush(); } release(); await running; assert.equal(h.elements.get('findings').hidden, true); assert.equal(h.prompts.length, 0); assert.equal(h.openai.widgetState.privateContent.jobId, back ? 'first' : 'second');
  });
  await test('timestamp seeks correct local offset; unknown times and source text stay explicit', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: analysis('first') } }); await h.flush(); const rows = h.elements.get('evidence').children; assert.equal(rows.length, 3); await rows[1].children[0].fire('click'); h.elements.get('player').fire('loadedmetadata'); assert.equal(h.elements.get('segment').value, '1'); assert.equal(h.elements.get('player').currentTime, 5); assert.equal(h.elements.get('player').focused, true); assert.equal(rows[2].children[0].tagName, 'SPAN'); assert.match(h.elements.get('gaps').children.map(el => el.textContent).join(' '), /缺少時間碼/); assert.equal(rows[1].children[2].textContent, 'source speech <script>not code</script>');
  });
  await test('unreliable visual timestamp is text without seek; legacy visual still seeks', async () => {
    const result = analysis('first');
    result.timeline[0].data.timestamp_reliable = false;
    result.timeline.push({ timestamp: 5, type: 'visual', data: { visual_summary: 'legacy frame' } });
    const h = harness({ snapshot: { prepared: job('first'), analysis: result } }); await h.flush();
    const rows = h.elements.get('evidence').children;
    assert.equal(rows[0].children[0].tagName, 'SPAN');
    assert.match(rows[0].children[0].textContent, /時間碼不可靠/);
    await rows[0].children[0].fire('click');
    assert.equal(h.elements.get('player').currentTime, 0);
    assert.equal(rows[1].children[0].tagName, 'BUTTON');
    await rows[1].children[0].fire('click'); h.elements.get('player').fire('loadedmetadata');
    assert.equal(h.elements.get('player').currentTime, 5);
  });
  await test('quality warnings appear in gaps without changing evidence status', async () => {
    for (const review of [false, true]) {
      const result = analysis('first');
      result.quality.status = review ? 'needs_review' : 'evidence_available';
      result.quality.reasons = review ? ['逐字稿補轉失敗，可能漏字。'] : [];
      result.quality.warnings = ['1 筆畫面時間碼不可靠，未提供跳轉。', '逐字稿有空白，可能是無人說話或音樂。'];
      const h = harness({ snapshot: { prepared: job('first'), analysis: result } }); await h.flush();
      const gaps = h.elements.get('gaps').children.map(el => el.textContent);
      for (const warning of result.quality.warnings) assert.ok(gaps.includes(warning));
      for (const reason of result.quality.reasons) assert.ok(gaps.includes(reason));
      assert.match(h.elements.get('quality').textContent, review ? /結果需要覆核/ : /已取得伺服器分析證據/);
    }
  });
  await test('unsafe segment URL never reaches player/link', async () => {
    const prepared = job('first'); prepared.segments[0].url = 'javascript:alert(1)'; const h = harness({ output: prepared, snapshot: { prepared, analysis: null } }); await h.flush(); assert.equal(h.elements.get('player').getAttribute('src'), null); assert.equal(h.elements.get('segment-link').hidden, true);
  });
  await test('wrong job result cannot replace evidence', async () => {
    const h = harness({ respond(name, args) { return name === 'get_prepared_video' ? { prepared: job(args.job_id), analysis: null } : completed(analysis('wrong')); } }); await h.flush(); await h.click('analyze'); assert.match(h.elements.get('status').textContent, /工作不符/); assert.equal(h.elements.get('findings').hidden, true);
  });
  await test('compatibility works when bridge unsupported', async () => {
    const h = harness({ bridge: false }); await h.flush(); await h.click('analyze'); await h.poll(); await h.click('ask'); assert.deepEqual(h.fallbackCalls, ['get_prepared_video', 'analyze_prepared_video', 'get_video_job', 'get_prepared_video', 'message']); assert.equal(h.prompts.length, 1);
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
  for (const reprepare of [false, true]) await test('expiry clears existing evidence and Drive before same-job reprepare=' + reprepare, async () => {
    let expired = false, mounted = false;
    const h = harness({ respond(name) {
      if (name === 'get_prepared_video') {
        if (!mounted) { mounted = true; return { prepared: job('first'), analysis: analysis('first'), drive: drive('first'), active_operation: null, latest_operation: null }; }
        if (expired) return { status: 'expired', error_code: 'MEDIA_EXPIRED', job_id: 'first', prepared: null, analysis: null, active_operation: reprepare ? pending('re-after-expiry', 'prepare') : null };
        return { prepared: job('first'), analysis: null, drive: null, drive_configured: true, active_operation: null, latest_operation: null };
      }
      if (name === 'get_video_job') { expired = false; return completed(job('first'), 'prepared', 're-after-expiry'); }
      if (name === 'upload_prepared_video_to_drive') return pending('drive-re', 'drive');
      if (name === 'analyze_prepared_video') return pending('analyze-re', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush();
    assert.equal(h.elements.get('findings').hidden, false);
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    expired = true;
    await h.click('refresh');
    assert.equal(h.elements.get('findings').hidden, true);
    assert.equal(h.elements.get('evidence').children.length, 0);
    assert.equal(h.elements.get('drive-handoff').hidden, true);
    assert.equal(h.elements.get('drive-prompt').value, '');
    assert.equal(h.elements.get('ask').disabled, true);
    assert.equal(h.elements.get('player').getAttribute('src'), null);
    assert.equal(h.openai.widgetState.privateContent.jobId, 'first');
    if (reprepare) {
      await h.poll(); await h.flush();
      assert.equal(h.elements.get('findings').hidden, true);
      assert.equal(h.elements.get('ask').disabled, true);
      // re-prepare 完成會自動重新並行 A＋B，不需手動按鈕。
      assert.deepEqual(h.calls.map(c => c.name).slice(-2), ['upload_prepared_video_to_drive', 'analyze_prepared_video']);
      assert.equal(h.elements.get('analyze').disabled, true);
      assert.equal(h.elements.get('drive').disabled, true);
    }
    assert.equal(h.prompts.length, 0);
  });
  await test('expired snapshot is not an unknown restore failure', async () => {
    const h = harness({ snapshot: { status: 'expired', error_code: 'MEDIA_EXPIRED', error: '影片已過保留期限，請重新準備。', job_id: 'first', prepared: null, analysis: null, active_operation: null, latest_operation: null } });
    await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']);
    assert.equal(h.elements.get('resume').hidden, true);
    assert.equal(h.elements.get('analyze').disabled, true);
    assert.equal(h.elements.get('drive').disabled, true);
    assert.equal(h.elements.get('media').hidden, true);
    assert.equal(h.elements.get('player').getAttribute('src'), null);
    assert.match(h.elements.get('status').textContent, /保留期限/);
    assert.match(h.elements.get('status').textContent, /重新準備/);
  });
  await test('expired state persists across remount without prepared data', async () => {
    const h = harness({ output: null, saved: { privateContent: { jobId: 'first' } }, snapshot: { status: 'expired', error_code: 'MEDIA_EXPIRED', error: 'fixture retention expiry', prepared: null, analysis: null, active_operation: null, latest_operation: null } });
    await h.flush();
    assert.match(h.elements.get('status').textContent, /保留期限/);
    assert.equal(h.openai.widgetState.privateContent.jobId, 'first');
    assert.equal(h.elements.get('analyze').disabled, true);
  });
  await test('expired snapshot follows a live re-prepare operation', async () => {
    let expired = true;
    const h = harness({ respond(name) {
      if (name === 'get_prepared_video') {
        if (expired) { expired = false; return { status: 'expired', error_code: 'MEDIA_EXPIRED', error: 'fixture retention expiry', prepared: null, analysis: null, active_operation: pending('re-1', 'prepare'), latest_operation: null }; }
        return { prepared: job('first'), analysis: null, drive_configured: true };
      }
      if (name === 'get_video_job') return completed(job('first'), 'prepared', 're-1');
      if (name === 'upload_prepared_video_to_drive') return completed(drive('first'), 'drive', 'drive-1');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush();
    await h.poll(); await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video', 'get_video_job', 'get_prepared_video', 'upload_prepared_video_to_drive', 'analyze_prepared_video']);
    assert.equal(h.elements.get('title').textContent, '影片 first');
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    assert.equal(h.elements.get('findings').hidden, true);
  });
  await test('live prepare completion auto-starts drive then analysis without clicks', async () => {
    const h = harness({ output: { ...pending('prepare-1', 'prepare'), job_id: undefined }, respond(name) {
      if (name === 'get_video_job') return completed(job('first'), 'prepared', 'prepare-1');
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: true };
      if (name === 'upload_prepared_video_to_drive') return pending('drive-1', 'drive');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await h.poll(); await h.flush();
    assert.equal(h.elements.get('title').textContent, '影片 first');
    assert.deepEqual(h.calls.map(c => c.name), ['get_video_job', 'get_prepared_video', 'upload_prepared_video_to_drive', 'analyze_prepared_video']);
    assert.equal(h.elements.get('analyze').disabled, true);
    assert.equal(h.elements.get('drive').disabled, true);
    assert.equal(h.prompts.length, 0);
  });
  await test('saved operation-only restore completes preparation and auto-starts parallel work', async () => {
    const h = harness({ output: null, saved: { privateContent: { operationId: 'prepare-old' } }, respond(name, args) {
      if (name === 'get_video_job' && args.operation_id === 'prepare-old') return completed(job('first'), 'prepared', 'prepare-old');
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: true };
      if (name === 'upload_prepared_video_to_drive') return pending('drive-1', 'drive');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name + ' ' + JSON.stringify(args));
    } });
    await h.flush(); await h.flush();
    assert.equal(h.elements.get('title').textContent, '影片 first');
    assert.deepEqual(h.calls.map(c => c.name), ['get_video_job', 'get_prepared_video', 'upload_prepared_video_to_drive', 'analyze_prepared_video']);
    assert.equal(h.elements.get('refresh').disabled, false);
    assert.equal(h.prompts.length, 0);
  });
  await test('authoritative invalidation cannot resurrect cached operation result', async () => {
    const h = harness({ saved: { privateContent: { jobId: 'first', operationId: 'obsolete-analysis', followUps: { ['first:' + 'a'.repeat(64)]: 'sent' } } }, snapshot: { prepared: job('first'), analysis: null, analysis_invalidated: true, active_operation: null, latest_operation: null } });
    await h.flush(); assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']); assert.equal(h.elements.get('analyze').disabled, false); assert.equal(h.elements.get('findings').hidden, true); assert.equal(h.openai.widgetState.privateContent.operationId, null);
    await h.click('refresh'); assert.equal(h.calls.at(-1).name, 'get_prepared_video');
  });
  await test('late prepare A response cannot replace newer prepare B', async () => {
    let release;
    const h = harness({ output: { ...pending('prepare-A', 'prepare'), job_id: undefined }, respond(name, args) {
      if (name === 'get_video_job') {
        if (args.operation_id === 'prepare-A') return new Promise(resolve => { release = () => resolve(completed(job('old'), 'prepared', 'prepare-A')); });
        if (args.operation_id === 'prepare-B') return completed(job('new'), 'prepared', 'prepare-B');
        throw new Error('Unexpected get_video_job ' + args.operation_id);
      }
      if (name === 'get_prepared_video') return { prepared: job('new'), analysis: null, drive_configured: true };
      if (name === 'upload_prepared_video_to_drive') return completed(drive('new'), 'drive', 'drive-new');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await h.poll(); h.notifyTool({ ...pending('prepare-B', 'prepare'), job_id: undefined }); await h.flush();
    release(); await h.flush(); assert.notEqual(h.elements.get('title').textContent, '影片 old');
    await h.poll(); await h.flush();
    assert.equal(h.elements.get('title').textContent, '影片 new');
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    assert.equal(h.elements.get('findings').hidden, true);
    assert.equal(h.prompts.length, 0);
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
  await test('drive handoff appears while analysis still runs, with copy and new-chat actions', async () => {
    let driveDone = false, copied = null, opened = null;
    const h = harness({ output: { ...pending('prepare-1', 'prepare'), job_id: undefined }, respond(name, args) {
      if (name === 'get_video_job') {
        if (args.operation_id === 'prepare-1') return completed(job('first'), 'prepared', 'prepare-1');
        if (args.operation_id === 'drive-1') {
          if (!driveDone) return pending('drive-1', 'drive', 'running');
          return completed(drive('first'), 'drive', 'drive-1');
        }
        if (args.operation_id === 'analyze-1') return pending('analyze-1', 'analyze', 'running');
        throw new Error('Unexpected get_video_job ' + args.operation_id);
      }
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: true };
      if (name === 'upload_prepared_video_to_drive') return pending('drive-1', 'drive');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await h.poll(); await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_video_job', 'get_prepared_video', 'upload_prepared_video_to_drive', 'analyze_prepared_video']);
    assert.equal(h.elements.get('drive-handoff').hidden, true);
    assert.equal(h.elements.get('copy-handoff').disabled, true);
    driveDone = true;
    await h.poll(); await h.flush();
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    assert.equal(h.elements.get('findings').hidden, true);
    const text = h.elements.get('drive-prompt').value;
    for (const required of ['first-file-0', 'first-file-1', '8 bytes', 'segment_000.mp4: 0–30 秒', 'download_raw_file=true', 'SHA-256', '一句話重點', '分段表', '限制說明', '合併', '不要讀取']) assert(text.includes(required), 'missing: ' + required);
    for (const leaked of ['source speech', 'source frame', 'visual_summary', 'transcript']) assert(!text.includes(leaked), 'leaks A result: ' + leaked);
    assert.equal(h.elements.get('copy-handoff').disabled, false);
    assert.equal(h.elements.get('open-chat').disabled, false);
    h.context.navigator = { clipboard: { writeText(value) { copied = value; return Promise.resolve(); } } };
    await h.click('copy-handoff'); assert.equal(copied, text);
    h.context.window.open = url => { opened = url; return {}; };
    await h.click('open-chat'); assert.equal(opened, 'https://chatgpt.com/');
    assert.equal(h.elements.get('findings').hidden, true);
    assert.equal(h.elements.get('drive').disabled, true);
    assert.equal(h.elements.get('analyze').disabled, true);
  });
  await test('drive auto-start failure does not block analysis', async () => {
    const h = harness({ output: { ...pending('prepare-1', 'prepare'), job_id: undefined }, respond(name, args) {
      if (name === 'get_video_job') {
        if (args.operation_id === 'prepare-1') return completed(job('first'), 'prepared', 'prepare-1');
        if (args.operation_id === 'analyze-1') return completed(analysis('first'), 'analysis', 'analyze-1');
        throw new Error('Unexpected get_video_job ' + args.operation_id);
      }
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: true };
      if (name === 'upload_prepared_video_to_drive') throw new Error('Google Drive 未設定');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await h.poll(); await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_video_job', 'get_prepared_video', 'upload_prepared_video_to_drive', 'analyze_prepared_video']);
    await h.poll(); await h.flush();
    assert.equal(h.elements.get('findings').hidden, false);
    assert.equal(h.elements.get('drive-handoff').hidden, true);
    assert.equal(h.elements.get('copy-handoff').disabled, true);
  });
  await test('unconfigured drive skips upload but still auto-starts analysis', async () => {
    const h = harness({ output: { ...pending('prepare-1', 'prepare'), job_id: undefined }, respond(name, args) {
      if (name === 'get_video_job') return completed(job('first'), 'prepared', 'prepare-1');
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: false };
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await h.poll(); await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_video_job', 'get_prepared_video', 'analyze_prepared_video']);
    assert.match(h.elements.get('status').textContent, /Drive 未設定/);
  });
  await test('server handoff text is preferred when present', async () => {
    const h = harness({ output: { ...pending('prepare-1', 'prepare'), job_id: undefined }, respond(name, args) {
      if (name === 'get_video_job') return completed(job('first'), 'prepared', 'prepare-1');
      if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: true };
      if (name === 'upload_prepared_video_to_drive') return completed({ ...drive('first'), handoff_text: 'SERVER HANDOFF 伺服器交接' }, 'drive', 'drive-1');
      if (name === 'analyze_prepared_video') return pending('analyze-1', 'analyze');
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await h.poll(); await h.flush();
    assert.equal(h.elements.get('drive-prompt').value, 'SERVER HANDOFF 伺服器交接');
  });
  await test('remount adopts running drive and analysis from pending operations', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: null, drive_configured: true,
      active_operation: pending('analyze-1', 'analyze', 'running'),
      latest_operation: pending('analyze-1', 'analyze', 'running'),
      pending_operations: [pending('analyze-1', 'analyze', 'running'), pending('drive-1', 'drive', 'running')] },
      respond(name, args) {
        if (name === 'get_prepared_video') return { prepared: job('first'), analysis: null, drive_configured: true,
          active_operation: pending('analyze-1', 'analyze', 'running'),
          latest_operation: pending('analyze-1', 'analyze', 'running'),
          pending_operations: [pending('analyze-1', 'analyze', 'running'), pending('drive-1', 'drive', 'running')] };
        if (name === 'get_video_job') {
          if (args.operation_id === 'drive-1') return completed(drive('first'), 'drive', 'drive-1');
          if (args.operation_id === 'analyze-1') return pending('analyze-1', 'analyze', 'running');
          throw new Error('Unexpected get_video_job ' + args.operation_id);
        }
        throw new Error('Unexpected ' + name);
      } });
    await h.flush(); await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['get_prepared_video']);
    assert.equal(h.elements.get('drive-handoff').hidden, true);
    await h.poll(); await h.flush();
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    assert.equal(h.elements.get('findings').hidden, true);
    assert.equal(h.prompts.length, 0);
  });
  await test('handoff actions degrade to manual steps without clipboard or popup', async () => {
    const h = harness({ snapshot: { prepared: job('first'), analysis: null, drive: drive('first') } });
    await h.flush();
    assert.equal(h.elements.get('copy-handoff').disabled, false);
    await h.click('copy-handoff');
    assert.match(h.elements.get('status').textContent, /手動選取/);
    await h.click('open-chat');
    assert.match(h.elements.get('status').textContent, /手動開啟/);
  });
  await test('evidence filter and bounded pagination', async () => {
    const result = analysis('first'); result.transcript.segments = Array.from({ length: 85 }, (_, i) => ({ start: i / 2, text: 'line ' + i })); const h = harness({ snapshot: { prepared: job('first'), analysis: result } }); await h.flush(); assert.equal(h.elements.get('evidence').children.length, 60); await h.click('more'); assert.equal(h.elements.get('evidence').children.length, 86); h.elements.get('filter').value = 'visual'; h.elements.get('filter').fire('change'); await h.flush(); assert.equal(h.elements.get('evidence').children.length, 1); assert.equal(h.openai.widgetState.privateContent.filter, 'visual'); assert.match(html, /min-height: 44px/); assert.match(html, /:focus-visible/);
  });
  console.log('PASS: ' + count + ' widget background/evidence/recovery scenarios (mocked MCP host; no paid calls).');
})().catch(error => { console.error(error); process.exitCode = 1; });
