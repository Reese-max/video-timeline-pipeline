// Real widget script with fake media and host APIs; no external network.
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
function harness({ output = job('first'), saved = {}, snapshot, respond, bridge = true, persistence = true, sendError, messageResult = {}, upload = async () => ({ fileId: 'file-library' }), download = async () => ({ downloadUrl: 'https://download.example/private?sig=DOWNLOAD' }), fetchImpl, subtle = true } = {}) {
  const fetches = [], uploads = [], downloads = [];
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
  if (upload) openai.uploadFile = async (file, options) => { uploads.push({ file, options }); return upload(file, options); };
  if (download) openai.getFileDownloadUrl = async args => { downloads.push(args); return download(args); };
  const context = vm.createContext({ URL, Blob, Uint8Array, performance, crypto: subtle ? require('node:crypto').webcrypto : undefined,
    File: class extends Blob { constructor(parts, name, options) { super(parts, options); this.name = name; } }, window: { openai, parent, addEventListener(name, listener) { listeners[name] = listener; } },
    document: { getElementById(id) { assert(elements.has(id), 'Missing HTML id: ' + id); return elements.get(id); }, createElement(tag) { return new Element(tag); } },
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; }, clearTimeout(id) { timers.delete(id); }, async fetch(url, options) { fetches.push({ url, options }); return fetchImpl ? fetchImpl(url, options) : { ok: true, blob: async () => new Blob(['fake-mp4']) }; } });
  vm.runInContext(script, context, { filename: 'widget.html' });
  return { fetches, uploads, downloads, elements, calls, prompts, states, openai, fallbackCalls, methods, notifyTool, context, timers, teardown() { notify({ method: 'ui/resource-teardown', id: 'teardown' }); },
    async flush() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); },
    async click(id) { const element = elements.get(id); assert(!element.disabled, id + ' is disabled'); await element.fire('click'); await this.flush(); },
    async poll() { const next = [...timers.entries()].find(([, timer]) => timer.ms <= 10000); assert(next, 'No poll timer'); timers.delete(next[0]); next[1].callback(); await this.flush(); } };
}

const row = (h, index = 0) => h.elements.get('library-list').children[index];
const buttons = (h, index = 0) => row(h, index).children.filter(e => e.tagName === 'BUTTON');
const text = element => [element.textContent, element.value, ...element.children.map(text)].join('\n');
const visible = h => [...h.elements.values()].map(text).join('\n');
const uploadClick = async (h, fallback = false, index = 0) => { const button = buttons(h, index)[fallback ? 1 : 0]; assert(!button.disabled); await button.fire('click'); await h.flush(); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const waitUntil = async ready => { for (let i = 0; i < 200 && !ready(); i++) await new Promise(resolve => setTimeout(resolve, 1)); assert(ready(), 'Expected async stage was not reached'); };
const privateJob = () => { const value = job('first'); value.segments[0].url += '?expires=1&sig=SECRET'; value.segments[0].size_bytes = 8; return value; };
const noLeaks = h => {
  const exposed = visible(h) + JSON.stringify(h.states) + h.prompts.join('\n');
  for (const secret of ['SECRET', 'DOWNLOAD', 'sig=', 'expires=', 'https://media.example/', 'https://download.example/']) assert(!exposed.includes(secret), 'Leaked ' + secret);
};
let count = 0, failures = 0;
async function test(name, run) { try { await run(); count++; console.log('ok ' + count + ' - ' + name); } catch (error) { failures++; console.error('FAIL - ' + name, error); } }
(async () => {
  await test('no click means no fetch upload download or prompt, even after refresh', async () => {
    const h = harness(); await h.flush(); await h.click('refresh');
    assert(h.elements.has('library-experiment')); assert.equal(h.elements.get('library-experiment').hidden, false);
    assert.equal(h.fetches.length + h.uploads.length + h.downloads.length + h.prompts.length, 0); assert.deepEqual(h.fallbackCalls, []);
    assert.equal(buttons(h).length, 2); assert(buttons(h)[1].hidden);
  });
  await test('missing uploadFile shows capability diagnosis and disables upload', async () => {
    const h = harness({ upload: null }); await h.flush(); assert(buttons(h).every(b => b.disabled));
    assert.match(h.elements.get('library-caps').textContent, /uploadFile.*缺少/);
    for (const api of ['getFileDownloadUrl', 'selectFiles', 'setWidgetState', 'sendFollowUpMessage']) assert(h.elements.get('library-caps').textContent.includes(api));
  });
  await test('explicit click fetches private media and uploads MP4 with library true and SHA-256', async () => {
    const h = harness({ output: privateJob(), snapshot: { prepared: privateJob() } }); await h.flush(); await uploadClick(h);
    assert.equal(h.fetches.length, 1); assert.equal(h.uploads.length, 1);
    assert.equal(h.fetches[0].url, privateJob().segments[0].url);
    assert.deepEqual(clone(h.fetches[0].options), { credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
    assert.deepEqual(clone(h.uploads[0].options), { library: true }); assert.equal(h.uploads[0].file.name, 'segment_000.mp4'); assert.equal(h.uploads[0].file.type, 'video/mp4');
    const output = text(row(h)); assert.match(output, /file-library/); assert.match(output, /8 bytes/); assert.match(output, /\d+ ms/);
    assert(output.includes(require('node:crypto').createHash('sha256').update('fake-mp4').digest('hex'))); assert.match(output, /下載網址：已取得/);
    assert(!JSON.stringify(h.states).includes('file-library')); noLeaks(h); assert.equal(h.prompts.length, 0);
  });
  await test('string fileId and unavailable crypto are supported', async () => {
    const h = harness({ upload: async () => 'file-string', subtle: false }); await h.flush(); await uploadClick(h); assert.match(text(row(h)), /file-string/); assert.match(text(row(h)), /SHA-256：不可用/);
  });
  await test('bad fileId shape is explicit and does not retry', async () => {
    const h = harness({ upload: async () => ({ other: 'value' }) }); await h.flush(); await uploadClick(h); assert.match(text(row(h)), /uploadFile 未回傳 fileId.*other/); assert.equal(h.uploads.length, 1);
  });
  await test('HTTP errors are complete and never upload', async () => {
    const h = harness({ fetchImpl: async () => ({ ok: false, status: 403 }) }); await h.flush(); await uploadClick(h); assert.match(text(row(h)), /Error: 下載失敗 HTTP 403/); assert.equal(h.uploads.length, 0);
  });
  await test('network failure explains refresh without hiding the complete error', async () => {
    const h = harness({ fetchImpl: async () => { throw new TypeError('Failed to fetch: full detail END'); } }); await h.flush(); await uploadClick(h); assert.match(text(row(h)), /TypeError: Failed to fetch: full detail END/); assert.match(text(row(h)), /重新取得狀態/); assert.equal(h.uploads.length, 0);
  });
  await test('upload errors are complete and redacted; normal fallback requires another click', async () => {
    const h = harness({ upload: async (_, options) => { throw new Error((options ? 'library' : 'normal') + ' full detail\nEND https://media.example/private?sig=SECRET expires=123 sig=ABC'); } });
    await h.flush(); await uploadClick(h); assert.equal(h.uploads.length, 1); assert.match(text(row(h)), /library full detail\nEND/); assert(!buttons(h)[1].hidden); noLeaks(h);
    await uploadClick(h, true); assert.equal(h.uploads.length, 2); assert.equal(h.uploads[1].options, undefined);
    assert.match(text(row(h)), /library full detail\nEND/); assert.match(text(row(h)), /normal full detail\nEND/); noLeaks(h);
  });
  await test('normal fallback success retains library error and marks library false', async () => {
    const h = harness({ upload: async (_, options) => { if (options) throw new Error('library original failure'); return 'file-normal'; } });
    await h.flush(); await uploadClick(h); await uploadClick(h, true); assert.match(text(row(h)), /library original failure/); assert.match(text(row(h)), /library:false/); assert.match(text(row(h)), /file-normal/);
  });
  await test('download URL failure is nonfatal with full redacted error', async () => {
    const h = harness({ download: async () => { throw new Error('download complete detail END https://download.example/private?sig=DOWNLOAD'); } }); await h.flush(); await uploadClick(h);
    assert.match(text(row(h)), /file-library/); assert.match(text(row(h)), /getFileDownloadUrl.*download complete detail END/); assert.match(text(row(h)), /下載網址：未取得/); assert(!h.elements.get('library-ask').disabled); noLeaks(h);
  });
  await test('missing download API is nonfatal and size mismatch is shown', async () => {
    const value = job('first'); value.segments[0].size_bytes = 99;
    const h = harness({ output: value, snapshot: { prepared: value }, download: null }); await h.flush(); await uploadClick(h); assert.match(text(row(h)), /大小不符/); assert.match(text(row(h)), /下載網址：未取得/);
  });
  await test('double click during upload only uploads once', async () => {
    const wait = deferred(), h = harness({ upload: () => wait.promise }); await h.flush(); const b = buttons(h)[0]; const first = b.fire('click'); const second = b.fire('click'); await waitUntil(() => h.uploads.length === 1); wait.resolve('file-once'); await Promise.all([first, second]); await h.flush(); assert.equal(h.uploads.length, 1);
  });
  for (const stage of ['fetch', 'blob', 'upload', 'download']) {
    await test('A to B to A discards stale ' + stage + ' result', async () => {
      const wait = deferred();
      let reached = false; const pause = () => { reached = true; return wait.promise; };
      const h = harness({ fetchImpl: stage === 'fetch' ? pause : stage === 'blob' ? async () => ({ ok: true, blob: pause }) : undefined,
        upload: stage === 'upload' ? pause : async () => 'file-stale', download: stage === 'download' ? pause : undefined });
      await h.flush(); const first = buttons(h)[0].fire('click'); await waitUntil(() => reached); h.notifyTool(job('second')); await h.flush(); h.notifyTool(job('first')); await h.flush();
      wait.resolve(stage === 'fetch' ? { ok: true, blob: async () => new Blob(['fake-mp4']) } : stage === 'blob' ? new Blob(['fake-mp4']) : stage === 'upload' ? 'file-stale' : { downloadUrl: 'https://download.example/private?sig=DOWNLOAD' });
      await first; await h.flush(); assert(!visible(h).includes('file-stale')); assert(h.elements.get('library-ask').disabled); assert.equal(h.prompts.length, 0);
      if (['fetch', 'blob'].includes(stage)) assert.equal(h.uploads.length, 0);
    });
  }
  await test('ask explicitly saves model IDs through unified state and standard bridge', async () => {
    const h = harness(); await h.flush(); await uploadClick(h); assert.equal(h.prompts.length, 0); await h.click('library-ask'); assert.equal(h.prompts.length, 1);
    assert.match(h.prompts[0], /file-library/); assert.match(h.prompts[0], /看不到/); assert.match(h.prompts[0], /不要呼叫任何工具/);
    assert.match(h.openai.widgetState.modelContent, /uploaded_file_ids.*file-library/); assert(!JSON.stringify(h.openai.widgetState.privateContent).includes('file-library')); assert.deepEqual(h.fallbackCalls, []);
    h.elements.get('segment').value = '1'; h.elements.get('segment').fire('change'); assert.match(h.openai.widgetState.modelContent, /uploaded_file_ids.*file-library/); noLeaks(h);
  });
  await test('ask uses host follow-up when standard bridge is unsupported', async () => {
    const h = harness({ bridge: false }); await h.flush(); await uploadClick(h); await h.click('library-ask'); assert.equal(h.prompts.length, 1); assert(h.fallbackCalls.includes('message')); noLeaks(h);
  });
  await test('experimental state rejection preserves normal state saving and does not send', async () => {
    const h = harness(); await h.flush(); await uploadClick(h); const save = h.openai.setWidgetState;
    h.openai.setWidgetState = function(state) { if (state.modelContent.includes('uploaded_file_ids')) throw new Error('Rejected experimental shape FULL END'); return save.call(this, state); };
    await h.click('library-ask'); assert.match(visible(h), /Rejected experimental shape FULL END/); assert.equal(h.prompts.length, 0);
    const before = h.states.length; h.elements.get('segment').value = '1'; h.elements.get('segment').fire('change'); assert.equal(h.states.length, before + 1); assert(!h.openai.widgetState.modelContent.includes('uploaded_file_ids'));
    assert(h.elements.get('persistence').hidden);
  });
  await test('follow-up ambiguity does not retry and retains error; job switch clears model IDs', async () => {
    const h = harness({ sendError: 'send failed FULL END https://download.example/private?sig=DOWNLOAD' }); await h.flush(); await uploadClick(h); await h.click('library-ask'); assert.equal(h.prompts.length, 1); assert.match(visible(h), /send failed FULL END/); noLeaks(h);
    h.notifyTool(job('second')); await h.flush(); assert(!h.openai.widgetState.modelContent.includes('file-library'));
  });
  await test('new preparation resets library state and stale buttons', async () => {
    const wait = deferred(), h = harness({ upload: () => wait.promise }); await h.flush(); const b = buttons(h)[0], first = b.fire('click'); await waitUntil(() => h.uploads.length === 1);
    h.notifyTool({ operation: 'prepare', operation_id: 'new-prepare', status: 'queued' }); await h.flush(); wait.resolve('file-expired'); await first;
    assert(h.elements.get('library-experiment').hidden); assert.equal(h.elements.get('library-list').children.length, 0); await b.fire('click'); assert.equal(h.uploads.length, 1);
  });
  await test('same-job media expiry discards a pending upload and clears model state', async () => {
    let expired = false;
    const wait = deferred(), h = harness({ upload: () => wait.promise, respond(name, args) {
      if (name === 'get_prepared_video') return expired ? { status: 'expired', error_code: 'MEDIA_EXPIRED', job_id: 'first', prepared: null } : { prepared: job(args.job_id) };
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); const first = buttons(h)[0].fire('click'); await waitUntil(() => h.uploads.length === 1);
    expired = true; await h.click('refresh'); wait.resolve('file-expired'); await first; await h.flush();
    assert(h.elements.get('library-experiment').hidden); assert(h.elements.get('library-ask').disabled); assert(!visible(h).includes('file-expired')); assert.equal(h.prompts.length, 0);
  });
  await test('SEC-1 async experimental rejection blocks prompt and preserves retry and normal saving', async () => {
    const unhandled = [], onUnhandled = error => unhandled.push(error);
    process.on('unhandledRejection', onUnhandled);
    try {
      const h = harness(); await h.flush(); await uploadClick(h);
      const save = h.openai.setWidgetState, persisted = vm.runInContext('lastPersisted', h.context), wait = deferred(); let attempts = 0;
      h.openai.setWidgetState = function(state) {
        if (state.modelContent.includes('uploaded_file_ids')) {
          if (++attempts === 1) return Promise.reject(new Error('Async rejection FULL END'));
          return wait.promise.then(() => save.call(this, state));
        }
        return Promise.resolve(save.call(this, state));
      };
      await h.click('library-ask');
      assert.equal(h.prompts.length, 0); assert.match(visible(h), /Async rejection FULL END/);
      assert(!h.openai.widgetState.modelContent.includes('uploaded_file_ids'));
      assert.equal(vm.runInContext('libraryModel', h.context), null);
      assert.equal(vm.runInContext('lastPersisted', h.context), persisted);
      const before = h.states.length;
      h.elements.get('segment').value = '1'; h.elements.get('segment').fire('change');
      assert.equal(h.states.length, before + 1); assert(h.elements.get('persistence').hidden);
      h.elements.get('segment').value = '0'; h.elements.get('segment').fire('change');
      const retry = h.elements.get('library-ask').fire('click'); await h.flush();
      try { assert.equal(attempts, 2); assert.equal(h.prompts.length, 0); }
      finally { wait.resolve(); await retry; await h.flush(); }
      assert.equal(h.prompts.length, 1);
      h.openai.setWidgetState = () => Promise.reject(new Error('Normal async rejection'));
      h.elements.get('segment').value = '1'; h.elements.get('segment').fire('change'); await h.flush();
      assert(h.elements.get('persistence').hidden); assert.deepEqual(unhandled, []);
    } finally { process.off('unhandledRejection', onUnhandled); }
  });
  await test('SEC-2 unprintable upload exceptions release busy state and show a safe error', async () => {
    const unhandled = [], onUnhandled = error => unhandled.push(error);
    process.on('unhandledRejection', onUnhandled);
    try {
      for (const error of [Object.create(null), { toString() { throw new Error('conversion failed'); } }, { name: Object.create(null), message: 'original detail END' }]) {
        const h = harness({ upload: async () => { throw error; } }); await h.flush(); await uploadClick(h);
        assert.match(text(row(h)), error.message ? /Error: original detail END/ : /無法轉成文字的例外/);
        assert(!text(row(h)).includes('上傳中…')); assert.equal(vm.runInContext('libraryBusy.size', h.context), 0);
        assert(!buttons(h)[0].disabled); assert.deepEqual(unhandled, []);
      }
    } finally { process.off('unhandledRejection', onUnhandled); }
  });
  await test('SEC-3 encoded escaped and protocol-relative signed URLs are fully redacted', async () => {
    for (const url of [
      'https%3A%2F%2Fmedia.example%2Ffirst%2F0%3Fexpires%3D1%26sig%3DSECRET',
      String.raw`https:\/\/media.example/first/0?expires\u003d1&sig\u003dSECRET`,
      '//media.example/first/0?expires=1&sig=SECRET',
      'HTTPS%3a//media.example/first/0?SIG%3dSECRET',
      String.raw`https%3a\/\/media.example/first/0?SIG\u003DSECRET`,
      String.raw`sig%3dSECRET expires\u003dSECRET`,
      'https://media.example/first/0?sig=SECRET',
    ]) {
      const h = harness({ upload: async () => { throw new Error('detail BEFORE ' + url + ' AFTER END'); } });
      await h.flush(); await uploadClick(h); noLeaks(h);
      assert.match(text(row(h)), /detail BEFORE \[URL\].* AFTER END/);
      assert(!visible(h).includes('media.example')); assert(!visible(h).includes('first/0'));
    }
  });
  await test('F3 polled MEDIA_EXPIRED removes persisted uploaded IDs after model request', async () => {
    const h = harness({ respond(name, args) {
      if (name === 'get_prepared_video') return { prepared: job(args.job_id) };
      if (name === 'analyze_prepared_video') return pending();
      if (name === 'get_video_job') return { ...pending(), status: 'failed', error_code: 'MEDIA_EXPIRED', error: 'media expired' };
      throw new Error('Unexpected ' + name);
    } });
    await h.flush(); await uploadClick(h); await h.click('library-ask');
    assert.match(h.states.at(-1).modelContent, /uploaded_file_ids/);
    await h.click('analyze'); await h.poll();
    assert(h.elements.get('library-experiment').hidden);
    assert(!h.states.at(-1).modelContent.includes('uploaded_file_ids'));
    assert.equal(vm.runInContext('libraryModel', h.context), null);
    h.notifyTool(job('second')); await h.flush();
    h.notifyTool({ ...job('third'), segments: [] }); await h.flush();
    assert(!h.states.at(-1).modelContent.includes('uploaded_file_ids'));
    assert.equal(h.states.at(-1).privateContent.jobId, 'third');
  });
  await test('F4 new library attempt clears normal fallback error and button before success', async () => {
    const wait = deferred(); let attempt = 0;
    const h = harness({ upload: async (_, options) => {
      if (++attempt === 1) throw new Error('old library failure');
      if (!options) throw new Error('old normal failure');
      return wait.promise;
    } });
    await h.flush(); await uploadClick(h); await uploadClick(h, true);
    assert.match(text(row(h)), /old normal failure/); assert(!buttons(h)[1].hidden);
    const retry = buttons(h)[0].fire('click'); await waitUntil(() => attempt === 3);
    try {
      assert.equal(row(h).children[4].textContent, ''); assert(buttons(h)[1].hidden);
    } finally { wait.resolve('file-retry'); await retry; await h.flush(); }
    assert.match(text(row(h)), /file-retry/); assert(!text(row(h)).includes('old normal failure'));
    assert(!text(row(h)).includes('old library failure')); assert(buttons(h)[1].hidden);
  });
  await test('spec-F2 double model request sends once and uploading blocks model requests', async () => {
    const h = harness(); await h.flush(); await uploadClick(h);
    const ask = h.elements.get('library-ask');
    await Promise.all([ask.fire('click'), ask.fire('click')]); await h.flush();
    assert.equal(h.prompts.length, 1);
    const wait = deferred(); h.openai.uploadFile = () => wait.promise;
    const uploading = buttons(h, 1)[0].fire('click');
    await waitUntil(() => text(row(h, 1)).includes('上傳中…'));
    try { await ask.fire('click'); await h.flush(); assert.equal(h.prompts.length, 1); }
    finally { wait.resolve('file-second'); await uploading; await h.flush(); }
  });
  if (failures) { console.error(failures + ' FAIL; ' + count + ' PASS'); process.exitCode = 1; } else console.log(count + ' PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
