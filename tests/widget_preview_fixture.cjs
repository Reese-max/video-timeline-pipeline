// Test-only preview: serves web/widget.html as a top-level page with a mocked ChatGPT host,
// filled with a real past analysis (analyses/<id>/whisper.json + timeline.json), for screenshots.
// Run: node tests/widget_preview_fixture.cjs [port] [analysis-id]; open http://127.0.0.1:<port>/?scenario=complete|ready&theme=light|dark
// At top level window.parent === window, so the bootstrap answers the widget's JSON-RPC by wrapping postMessage.
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.join(__dirname, '..');
const port = Number(process.argv[2] || 8791);
const id = process.argv[3] || 'fb1GFg77Fady';
const read = name => JSON.parse(fs.readFileSync(path.join(root, 'analyses', id, name), 'utf8'));
const whisper = read('whisper.json'), timeline = read('timeline.json');
const entries = Array.isArray(timeline) ? timeline : timeline.entries;
const duration = Math.round(whisper.duration || entries.at(-1).end);
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'video-widget-preview-'));
const media = path.join(directory, 'fixture.mp4');
const made = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=2', '-t', '10', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', media], { timeout: 30000 });
if (made.status !== 0) throw new Error('ffmpeg fixture generation failed');

const segmentLength = 120;
const job = { status: 'ready', job_id: 'preview-' + id, title: '防套話六招：守住底牌的說話技巧（本機預覽資料）', duration_seconds: duration,
  segments: Array.from({ length: Math.ceil(duration / segmentLength) }, (_, i) => ({ filename: `segment_${String(i).padStart(3, '0')}.mp4`,
    url: `http://127.0.0.1:${port}/fixture.mp4?segment=${i}`, start_seconds: i * segmentLength, end_seconds: Math.min(duration, (i + 1) * segmentLength) })) };
const analysis = { status: 'complete', job_id: job.job_id, cache_signature: 'preview-sig', source_size_bytes: 48213377, source_sha256: 'b'.repeat(64),
  audio_status: 'complete', visual_status: 'complete',
  transcript: { segments: (whisper.segments || []).map(s => ({ start: s.start, end: s.end, text: s.text })) },
  timeline: entries.map(e => ({ timestamp: e.start, type: 'visual', data: { visual_summary: [e.label || e.title, e.detail || e.summary].filter(Boolean).join('：'), visible_text: e.evidence ? [e.evidence] : [], confidence: 0.72 } })),
  quality: { status: 'evidence_available', reasons: [], warnings: ['逐字稿為自動辨識，專有名詞可能有誤。'], limitation: '畫面事件為模型讀完整支影片後的摘要，逐字稿與模型描述可能有誤；重要資訊請核對原片。' } };
const drive = { status: 'drive_ready', job_id: job.job_id, files: job.segments.map((s, i) => ({ file_name: s.filename, file_id: 'FAKE-' + i, size_bytes: 12000000, url: 'https://drive.google.com/' })) };
const report = { status: 'summary_pending', job_id: job.job_id, cache_signature: 'preview-sig', analysis_sha256: 'c'.repeat(64), evidence_status: 'complete', summary_status: 'missing' };
const scenarios = { complete: { prepared: job, analysis, drive, report, active_operation: null, latest_operation: null }, ready: { prepared: job, analysis: null, active_operation: null, latest_operation: null } };

const bootstrap = (scenario, theme) => `<script>
(() => {
  const snapshot = ${JSON.stringify(scenarios[scenario] || scenarios.complete).replace(/</g, '\\u003c')};
  window.openai = { toolOutput: snapshot.prepared, widgetState: { privateContent: { jobId: snapshot.prepared.job_id } }, setWidgetState: async () => {}, theme: ${JSON.stringify(theme === 'dark' ? 'dark' : 'light')} };
  const send = window.postMessage.bind(window);
  const reply = message => {
    if (message.method === 'tools/call') {
      if (message.params.name === 'get_prepared_video') return { structuredContent: snapshot };
      return { structuredContent: { status: 'failed', error: '本機預覽不執行 ' + message.params.name } };
    }
    return {};
  };
  window.postMessage = (message, target) => {
    if (message && message.jsonrpc === '2.0' && message.method && message.id !== undefined) setTimeout(() => send({ jsonrpc: '2.0', id: message.id, result: reply(message) }, '*'), 5);
    else if (!(message && message.jsonrpc === '2.0')) send(message, target);
  };
})();
<` + '/script>';

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1');
  if (url.pathname === '/') {
    const html = fs.readFileSync(path.join(root, 'web', 'widget.html'), 'utf8').replace('<script>', bootstrap(url.searchParams.get('scenario'), url.searchParams.get('theme')) + '<script>');
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(html);
  } else if (url.pathname === '/fixture.mp4') {
    const bytes = fs.readFileSync(media);
    response.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Length': bytes.length }); response.end(bytes);
  } else { response.writeHead(404); response.end(); }
});
server.listen(port, '127.0.0.1', () => console.log(`WIDGET_PREVIEW_URL=http://127.0.0.1:${port}/`));
const cleanup = () => { server.close(); fs.rmSync(directory, { recursive: true, force: true }); process.exit(); };
process.on('SIGINT', cleanup); process.on('SIGTERM', cleanup);
