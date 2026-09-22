const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'mcp_server.py'), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];

async function check({ native = true, failedFetch = -1, driveFails = false, stalledBody = false } = {}) {
  const elements = new Map();
  const element = () => ({ append() {}, replaceChildren() {}, addEventListener() {} });
  const calls = [], prompts = [];
  const segments = [0, 1].map(i => ({ filename: `segment_00${i}.mp4`, url: `https://media.example/${i}` }));
  let fetched = 0, uploaded = 0;
  const timers = new Map();
  let nextTimer = 0;
  const host = {
    toolOutput: { job_id: 'job', segments },
    callTool: async (name, args) => {
      calls.push(name);
      if (name === 'upload_prepared_video_to_drive') return { structuredContent: driveFails
        ? { status: 'error', error: 'HTTP 502' }
        : { status: 'drive_ready', job_id: args.job_id, files: segments.map(s => ({ file_name: s.filename, file_id: s.filename, size_bytes: 8, url: 'https://drive.google.com/file/d/test/view' })) } };
      if (name === 'ingest_video_segments') return { status: 'files_received', files_received: args.files.length };
      throw new Error(`Unexpected tool: ${name}`);
    },
    sendFollowUpMessage: async ({ prompt }) => prompts.push(prompt),
    ...(native ? {
      uploadFile: async () => ({ fileId: `file-${++uploaded}` }),
      getFileDownloadUrl: async ({ fileId }) => ({ downloadUrl: `https://files.example/${fileId}` }),
    } : {}),
  };
  const context = vm.createContext({
    window: { openai: host, parent: { postMessage() {} }, addEventListener() {} },
    document: { getElementById: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element },
    fetch: async (_url, { signal }) => {
      if (fetched++ === failedFetch) throw new TypeError('Failed to fetch');
      return { ok: true, blob: async () => {
        if (!stalledBody) return new Blob(['video']);
        assert.equal(timers.size, 1, 'Download deadline must cover response.blob(), not just headers');
        const aborted = new Promise((_, reject) => signal.addEventListener('abort', () => {
          const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
        }));
        for (const timer of timers.values()) timer();
        return aborted;
      } };
    },
    Blob, File, AbortController,
    setTimeout: (callback, ms) => { const id = ++nextTimer; if (ms === 30000) timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id), queueMicrotask() {},
  });
  vm.runInContext(script + '\nglobalThis.start = startUpload;', context);
  await context.start();
  assert(!calls.includes('analyze_prepared_video'), 'Never substitute server-side analysis');
  if (driveFails) {
    assert.equal(prompts.length, 0);
    assert.match(elements.get('status').textContent, /502/);
    assert.equal(elements.get('upload').disabled, false);
  } else if (!native || failedFetch >= 0 || stalledBody) {
    assert.deepEqual(calls, ['upload_prepared_video_to_drive']);
    assert.equal(prompts.length, 1);
    assert.match(prompts[0], /download_raw_file=true/);
    for (const segment of segments) assert(prompts[0].includes(segment.filename));
  } else {
    assert.deepEqual(calls, ['ingest_video_segments']);
    assert.equal(uploaded, 2);
    assert.equal(prompts.length, 1);
  }
  assert.equal(timers.size, 0, 'Always clear the download deadline');
}

async function checkJobSwitch(stage, { backToOld = false, rejectOld = false, wrongDriveJob = false } = {}) {
  const elements = new Map(), states = [], prompts = [], tools = [], queued = [], listeners = {};
  const element = () => ({ append() {}, replaceChildren() {}, addEventListener() {} });
  const job = id => ({ job_id: id, title: id, segments: [{ filename: 'segment_000.mp4', url: `https://media.example/${id}` }] });
  let release, reached;
  const paused = new Promise(resolve => { reached = resolve; });
  let held = false;
  async function pause(name) {
    if (stage !== name || held) return;
    held = true;
    reached();
    await new Promise((resolve, reject) => { release = () => rejectOld ? reject(new Error('old job failed')) : resolve(); });
  }
  const host = {
    toolOutput: job('old-job'),
    widgetState: { modelContent: 'OLD CONTEXT', privateContent: { jobId: 'obsolete-job', driveFileIds: ['obsolete-file'] } },
    setWidgetState(state) { this.widgetState = state; states.push(state); },
    async callTool(name, args) {
      tools.push({ name, args });
      await pause(name === 'upload_prepared_video_to_drive' ? 'drive' : 'ingest');
      return name === 'upload_prepared_video_to_drive'
        ? { status: 'drive_ready', job_id: wrongDriveJob ? 'wrong-job' : args.job_id, files: [{ file_id: `${args.job_id}-file`, file_name: 'segment_000.mp4', size_bytes: 8, url: 'https://drive.google.com/file/d/test/view' }] }
        : { status: 'files_received', files_received: args.files.length };
    },
    async sendFollowUpMessage({ prompt }) { prompts.push(prompt); },
    ...(stage !== 'drive' ? {
      async uploadFile() { await pause('upload'); return { fileId: 'native-file' }; },
      async getFileDownloadUrl() { await pause('download-url'); return { downloadUrl: 'https://files.example/video' }; },
    } : {}),
  };
  const parent = { postMessage() {} };
  const context = vm.createContext({
    window: { openai: host, parent, addEventListener(name, handler) { listeners[name] = handler; } },
    document: { getElementById: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element },
    async fetch() { await pause('fetch'); return { ok: true, blob: async () => new Blob(['video']) }; },
    Blob, File, AbortController, setTimeout: () => 0, clearTimeout() {}, queueMicrotask: callback => queued.push(callback),
  });
  vm.runInContext(script + '\nglobalThis.start = startUpload;', context);
  assert.equal(host.widgetState.privateContent.jobId, 'old-job');
  assert(!JSON.stringify(host.widgetState).includes('obsolete-file'), 'Clear persisted context on mount');
  queued.length = 0;
  const notify = value => listeners.message({ source: parent, data: { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: value } } });
  const pending = context.start();
  await paused;
  if (!wrongDriveJob) {
    notify(job('new-job'));
    if (backToOld) notify(job('old-job'));
    assert.equal(host.widgetState.privateContent.jobId, backToOld ? 'old-job' : 'new-job');
    assert(!JSON.stringify(host.widgetState).includes('old-job-file'));
    const count = states.length;
    notify(job(backToOld ? 'old-job' : 'new-job'));
    assert.equal(states.length, count, 'Duplicate notification must not reset context');
  }
  const stateCount = states.length;
  release();
  await pending;
  assert.equal(prompts.length, 0, 'A superseded or mismatched result must never send a follow-up');
  assert.equal(states.length, stateCount, 'A late completion must not overwrite current context');
  if (wrongDriveJob) {
    assert.match(elements.get('status').textContent, /工作不符/);
    return;
  }
  assert(!elements.get('status').textContent.includes('old job failed'));
  assert(queued.length > 0, 'The new job must be scheduled after the previous run finishes');
  // Simulate the browser microtask queue, including the task queued by finally.
  for (let count = 0; count < 100 && (queued.length || !prompts.length); count++) {
    while (queued.length) queued.shift()();
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(prompts.length, 1, 'Exactly one follow-up for the current job');
  assert.equal(host.widgetState.privateContent.jobId, backToOld ? 'old-job' : 'new-job');
  if (!backToOld) assert(!prompts[0].includes('old-job-file'));
  const currentState = JSON.stringify(host.widgetState);
  notify({ status: 'drive_ready', job_id: 'old-job', files: [] });
  assert.equal(JSON.stringify(host.widgetState), currentState, 'A data-only response must not replace the prepared job');
}

(async () => {
  await check();
  await check({ failedFetch: 0 });
  await check({ failedFetch: 1 });
  await check({ native: false });
  await check({ failedFetch: 0, driveFails: true });
  await check({ native: false, driveFails: true });
  await check({ stalledBody: true });
  for (const stage of ['drive', 'fetch', 'upload', 'download-url', 'ingest']) await checkJobSwitch(stage);
  await checkJobSwitch('drive', { backToOld: true });
  await checkJobSwitch('drive', { rejectOld: true });
  await checkJobSwitch('drive', { wrongDriveJob: true });
  console.log('PASS: 15 widget handoff scenarios');
})().catch(error => { console.error(error); process.exitCode = 1; });
