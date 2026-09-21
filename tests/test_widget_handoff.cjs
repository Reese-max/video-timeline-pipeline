const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'mcp_server.py'), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];

async function check({ native = true, failedFetch = -1, driveFails = false } = {}) {
  const elements = new Map();
  const element = () => ({ append() {}, replaceChildren() {}, addEventListener() {} });
  const calls = [], prompts = [];
  const segments = [0, 1].map(i => ({ filename: `segment_00${i}.mp4`, url: `https://media.example/${i}` }));
  let fetched = 0, uploaded = 0;
  const host = {
    toolOutput: { job_id: 'job', segments },
    callTool: async (name, args) => {
      calls.push(name);
      if (name === 'upload_prepared_video_to_drive') return { structuredContent: driveFails
        ? { status: 'error', error: 'HTTP 502' }
        : { status: 'drive_ready', files: segments.map(s => ({ file_name: s.filename, file_id: s.filename, size_bytes: 8, url: 'https://drive.google.com/file/d/test/view' })) } };
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
    fetch: async () => { if (fetched++ === failedFetch) throw new TypeError('Failed to fetch'); return { ok: true, blob: async () => new Blob(['video']) }; },
    Blob, File, AbortController, setTimeout: () => 0, clearTimeout() {}, queueMicrotask() {},
  });
  vm.runInContext(script + '\nglobalThis.start = startUpload;', context);
  await context.start();
  assert(!calls.includes('analyze_prepared_video'), 'Never substitute server-side analysis');
  if (driveFails) {
    assert.equal(prompts.length, 0);
    assert.match(elements.get('status').textContent, /502/);
    assert.equal(elements.get('upload').disabled, false);
  } else if (!native || failedFetch >= 0) {
    assert.deepEqual(calls, ['upload_prepared_video_to_drive']);
    assert.equal(prompts.length, 1);
    assert.match(prompts[0], /download_raw_file=true/);
    for (const segment of segments) assert(prompts[0].includes(segment.filename));
  } else {
    assert.deepEqual(calls, ['ingest_video_segments']);
    assert.equal(uploaded, 2);
    assert.equal(prompts.length, 1);
  }
}

(async () => {
  await check();
  await check({ failedFetch: 0 });
  await check({ failedFetch: 1 });
  await check({ native: false });
  await check({ failedFetch: 0, driveFails: true });
  await check({ native: false, driveFails: true });
  console.log('PASS: 6 widget handoff scenarios');
})().catch(error => { console.error(error); process.exitCode = 1; });
