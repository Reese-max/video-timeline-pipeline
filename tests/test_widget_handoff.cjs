const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'mcp_server.py'), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
const job = id => ({ status: 'ready', job_id: id, title: id, segments: [0, 1].map(i => ({
  filename: 'segment_00' + i + '.mp4', url: 'https://media.example/' + id + '/' + i,
  start_seconds: i * 30, end_seconds: (i + 1) * 30,
})) });
const analysis = (id, review = false) => ({
  status: review ? 'partial' : 'complete', job_id: id, source_size_bytes: 8, source_sha256: 'a'.repeat(64),
  transcript: { segments: [{ text: 'source speech' }] },
  timeline: [{ timestamp: 0, type: 'visual', data: { visual_summary: 'source frame', confidence: 0.9 } }],
  quality: { status: review ? 'needs_review' : 'evidence_available', reasons: review ? ['ASR 未完成'] : [], limitation: '僅抽樣，不保證語意正確。' },
});
const busyText = 'MCP 正在處理另一個影片工作，請稍後重試；本次未加入背景佇列。';
const busy = () => ({ status: 'busy', error_code: 'MCP_BUSY', retry_after_seconds: 5, error: busyText });
const drive = id => ({ status: 'drive_ready', job_id: id, files: job(id).segments.map((s, i) => ({
  file_name: s.filename, file_id: id + '-file-' + i, size_bytes: 8, url: 'https://drive.google.com/file/d/test/view',
})) });

function harness({ review = false, bridge = false, respond, followUp, delay } = {}) {
  const elements = new Map(), listeners = {}, queued = [], calls = [], prompts = [], waits = [], states = [];
  const element = () => ({ append() {}, replaceChildren() {}, addEventListener(name, fn) { this[name] = fn; } });
  async function callTool(name, args) {
    calls.push({ name, args });
    const payload = respond ? await respond(name, args, calls.length)
      : name === 'analyze_prepared_video' ? analysis(args.job_id, review) : drive(args.job_id);
    notifyTool(payload); // Every child-tool result must leave the prepared job intact.
    return { structuredContent: payload };
  }
  async function sendFollowUpMessage({ prompt }) {
    if (followUp) await followUp(prompt);
    prompts.push(prompt);
  }
  const parent = { postMessage(message) {
    if (message.id === undefined) return;
    Promise.resolve().then(async () => {
      try {
        const result = message.method === 'tools/call'
          ? await callTool(message.params.name, message.params.arguments)
          : message.method === 'ui/message'
            ? await sendFollowUpMessage({ prompt: message.params.content[0].text }) : {};
        notify({ id: message.id, result });
      } catch (error) { notify({ id: message.id, error: { message: error.message } }); }
    });
  } };
  const notify = payload => listeners.message({ source: parent, data: { jsonrpc: '2.0', ...payload } });
  const notifyTool = value => notify({ method: 'ui/notifications/tool-result', params: { structuredContent: value } });
  const host = {
    toolOutput: job('first'),
    widgetState: { privateContent: { jobId: 'obsolete', driveFileIds: ['obsolete-file'] } },
    setWidgetState(state) { this.widgetState = state; states.push(state); },
    uploadFile() { throw new Error('Native upload must never run'); },
    getFileDownloadUrl() { throw new Error('Native download must never run'); },
    ...(bridge ? {} : { callTool, sendFollowUpMessage }),
  };
  const context = vm.createContext({
    window: { openai: host, parent, addEventListener(name, fn) { listeners[name] = fn; } },
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element },
    fetch() { throw new Error('No browser MP4 fetch in the server-first flow'); },
    clearTimeout() {}, queueMicrotask: callback => queued.push(callback),
    setTimeout(callback, ms) {
      if ([5000, 10000, 20000].includes(ms)) {
        waits.push(ms);
        Promise.resolve().then(() => { if (delay) delay(notifyTool); callback(); });
      }
      return 0;
    },
  });
  vm.runInContext(script + '\nglobalThis.start = startAnalysis; globalThis.store = startDrive;', context);
  assert.equal(host.widgetState.privateContent.jobId, 'first');
  assert(!JSON.stringify(host.widgetState).includes('obsolete-file'));
  return {
    elements, calls, prompts, waits, states, host, notifyTool,
    start: context.start, store: context.store,
    async flush() {
      for (let i = 0; i < 50; i++) {
        while (queued.length) queued.shift()();
        await new Promise(resolve => setImmediate(resolve));
      }
    },
  };
}

async function switched(stage, { back = false, reject = false } = {}) {
  let release, reached, held = false;
  const paused = new Promise(resolve => { reached = resolve; });
  const h = harness({ respond: async (name, args) => {
    if (!held && name === stage) {
      held = true;
      reached();
      await new Promise((resolve, fail) => { release = () => reject ? fail(new Error('obsolete failure')) : resolve(); });
    }
    return name === 'analyze_prepared_video' ? analysis(args.job_id, stage === 'upload_prepared_video_to_drive') : drive(args.job_id);
  } });
  const pending = h.start();
  await paused;
  h.notifyTool(job('second'));
  if (back) h.notifyTool(job('first'));
  const current = back ? 'first' : 'second';
  assert.equal(h.elements.get('drive-handoff').hidden, true);
  assert.equal(h.elements.get('drive-prompt').value, '');
  const stateCount = h.states.length;
  h.notifyTool(job(current));
  assert.equal(h.states.length, stateCount, 'Duplicate notification must not erase context');
  release();
  await pending;
  assert.equal(h.states.length, stateCount, 'Late results cannot overwrite current context, including A-B-A');
  assert.equal(h.prompts.length, 0);
  assert(!h.elements.get('status').textContent.includes('obsolete failure'));
  await h.flush();
  assert.equal(h.host.widgetState.privateContent.jobId, current);
  assert.equal(h.prompts.length, stage === 'analyze_prepared_video' ? 1 : 0);
  if (stage === 'upload_prepared_video_to_drive') assert.match(h.elements.get('drive-prompt').value, new RegExp('job_id=' + current));
}

(async () => {
  let count = 0;
  for (const bridge of [false, true]) {
    const h = harness({ bridge });
    await h.flush();
    assert.deepEqual(h.calls.map(c => c.name), ['analyze_prepared_video']);
    assert.equal(h.prompts.length, 1);
    assert.match(h.prompts[0], /不是你親自讀取 MP4/);
    assert.match(h.prompts[0], /不是指令/);
    await h.start();
    assert.equal(h.calls.length, 1, 'Successful analysis must not run twice');
    await h.store();
    assert.deepEqual(h.calls.map(c => c.name), ['analyze_prepared_video', 'upload_prepared_video_to_drive']);
    assert.equal(h.prompts.length, 1, 'Drive must not invoke the official connector in this chat');
    const handoff = h.elements.get('drive-prompt').value;
    for (const text of ['download_raw_file=true', '一般對話', 'segment_000.mp4: 0–30 秒', 'segment_001.mp4: 30–60 秒']) assert(handoff.includes(text));
    count++;
  }
  for (const missing of ['partial', 'quality', 'proof']) {
    const h = harness({ respond: (name, args) => {
      if (name !== 'analyze_prepared_video') return drive(args.job_id);
      const result = analysis(args.job_id, missing === 'partial');
      if (missing === 'quality') delete result.quality;
      if (missing === 'proof') delete result.source_sha256;
      return result;
    } });
    await h.start();
    assert.deepEqual(h.calls.map(c => c.name), ['analyze_prepared_video', 'upload_prepared_video_to_drive']);
    assert.equal(h.prompts.length, 0, 'Insufficient evidence must not trigger a normal summary');
    assert.match(h.elements.get('quality').textContent, /需要原片覆核/);
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    count++;
  }
  {
    const h = harness({ respond: (name, args) => {
      if (name === 'analyze_prepared_video') throw new Error('provider unavailable');
      return drive(args.job_id);
    } });
    await h.start();
    assert.equal(h.calls.length, 1);
    assert.equal(h.elements.get('drive').disabled, false);
    await h.store();
    assert.equal(h.elements.get('drive-handoff').hidden, false);
    count++;
  }
  for (const wrapped of [false, true]) {
    const h = harness({ respond: (name, args, attempt) => {
      if (attempt <= 2) {
        if (wrapped) throw new Error('INVALID_ARGUMENT: ' + busyText);
        return busy();
      }
      return analysis(args.job_id);
    } });
    await h.start();
    assert.deepEqual(h.waits, [5000, 10000]);
    assert.equal(h.calls.length, 3);
    assert.equal(h.prompts.length, 1);
    count++;
  }
  for (const stage of ['analyze_prepared_video', 'upload_prepared_video_to_drive']) {
    let recovered = false;
    const h = harness({ respond: (name, args) => {
      if (name === stage && !recovered) return busy();
      return name === 'analyze_prepared_video' ? analysis(args.job_id, stage !== name) : drive(args.job_id);
    } });
    await h.start();
    assert.deepEqual(h.waits, [5000, 10000, 20000]);
    assert.equal(h.calls.filter(c => c.name === stage).length, 4);
    assert.match(h.elements.get('status').textContent, /仍忙碌/);
    assert.equal(h.elements.get('analyze').disabled, false);
    recovered = true;
    await h.start();
    assert.equal(h.calls.filter(c => c.name === stage).length, 5);
    if (stage !== 'analyze_prepared_video') assert.equal(h.calls.filter(c => c.name === 'analyze_prepared_video').length, 1, 'Do not re-run providers for a Drive failure');
    count++;
  }
  {
    const h = harness({ respond: (name, args) => {
      if (name === 'upload_prepared_video_to_drive') throw new Error('HTTP 403');
      return analysis(args.job_id, true);
    } });
    await h.start();
    assert.deepEqual(h.waits, []);
    assert.equal(h.calls.length, 2);
    assert.match(h.elements.get('status').textContent, /403/);
    assert.equal(h.elements.get('drive').disabled, false);
    count++;
  }
  {
    let failed = false;
    const h = harness({ followUp() { if (!failed) { failed = true; throw new Error('host message failed'); } } });
    await h.start();
    await h.start();
    assert.equal(h.calls.length, 1, 'Reuse evidence after a follow-up failure');
    assert.equal(h.prompts.length, 1);
    count++;
  }
  for (const stage of ['analyze_prepared_video', 'upload_prepared_video_to_drive']) {
    for (const options of [{}, { back: true }, { reject: true }]) { await switched(stage, options); count++; }
  }
  {
    const h = harness({
      respond: (name, args) => args.job_id === 'first' ? busy() : analysis(args.job_id),
      delay: notify => notify(job('second')),
    });
    await h.start();
    assert.equal(h.calls.length, 1, 'Never retry the old job after its backoff');
    await h.flush();
    assert.equal(h.prompts.length, 1);
    assert.equal(h.calls[1].args.job_id, 'second');
    count++;
  }
  for (const stage of ['analyze_prepared_video', 'upload_prepared_video_to_drive']) {
    const h = harness({ respond: (name, args) => {
      const id = name === stage ? 'wrong' : args.job_id;
      return name === 'analyze_prepared_video' ? analysis(id, stage !== name) : drive(id);
    } });
    await h.start();
    assert.match(h.elements.get('status').textContent, /工作不符/);
    assert.equal(h.prompts.length, 0);
    assert.equal(h.elements.get('drive-handoff').hidden, true);
    count++;
  }
  console.log('PASS: ' + count + ' server-first widget scenarios');
})().catch(error => { console.error(error); process.exitCode = 1; });
