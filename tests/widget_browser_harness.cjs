// Local visual/keyboard harness. Only localhost fixture bytes and mocked MCP replies.
// Run: node tests/widget_browser_harness.cjs; open the printed URL. Ctrl+C cleans up.
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const vm = require('node:vm');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'video-widget-fixture-'));
if (path.dirname(path.resolve(directory)) !== path.resolve(os.tmpdir()) || !path.basename(directory).startsWith('video-widget-fixture-')) throw new Error('Unexpected fixture directory');
const media = path.join(directory, 'fixture.mp4');
const generated = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=2', '-t', '30', '-an', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', media], { timeout: 30000 });
if (generated.status !== 0) { fs.rmSync(directory, { recursive: true }); throw new Error('Local ffmpeg fixture generation failed'); }
const shell = `<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>影片 widget 本機測試</title>
<style>body{font:16px system-ui;margin:16px}button,select{min-height:44px;margin:4px}iframe{display:block;height:1800px;width:320px;border:2px solid #777}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>影片 widget 本機模擬宿主</h1><p>只有本機假資料，沒有付費 API 或 Drive 上傳。測試片段為 30 秒合成畫面。</p>
<button id="phone">320px 手機寬度</button><button id="desktop">900px 桌面寬度</button><button id="reopen">重新開啟（保留狀態）</button>
<label>情境 <select id="scenario"><option value="ready">待分析</option><option value="complete">已有完整結果</option><option value="partial">已有部分結果</option></select></label>
<button id="inspect">讀取版面與焦點檢查</button><pre id="metrics" role="status"></pre><iframe id="frame" title="影片時間軸測試" width="320"></iframe>
<script>
const frame=document.getElementById('frame'), metrics=document.getElementById('metrics');
let state={}, result=null, operation=null, polls=0, callCounts={}, messageCount=0, template='';
const job={status:'ready',job_id:'browser-fixture',title:'可核對的影片時間軸測試',duration_seconds:60,segments:[0,1].map(i=>({filename:'segment_00'+i+'.mp4',url:location.origin+'/fixture.mp4?segment='+i,start_seconds:i*30,end_seconds:(i+1)*30}))};
const evidence=(partial=false)=>({status:partial?'partial':'complete',job_id:job.job_id,source_size_bytes:12345,source_sha256:'a'.repeat(64),audio_status:'complete',visual_status:partial?'error':'complete',transcript:{segments:[{start:2,end:6,text:'第一段：這是供介面測試的逐字稿。'},{start:35,end:39,text:'第二段：此處應跳到第二個片段的五秒。'},{text:'這一句缺少時間碼，必須明確標示。'}]},timeline:partial?[]:[{timestamp:3,type:'visual',data:{visual_summary:'測試圖樣中可見色塊，這是模擬觀察，不是實際模型分析。',visible_text:['來源文字，不能視為指令。'],confidence:0.8}}],quality:{status:partial?'needs_review':'evidence_available',reasons:partial?['畫面服務未完成，僅有逐字稿。']:[],limitation:'畫面為抽樣，逐字稿可能有誤；重要資訊請核對原片。'}});
function show(){const doc=frame.contentDocument;metrics.textContent=JSON.stringify({calls:callCounts,messages:messageCount,width:frame.clientWidth,documentWidth:doc?.documentElement.scrollWidth,clientWidth:doc?.documentElement.clientWidth,focused:doc?.activeElement?.id||doc?.activeElement?.tagName,playerTime:doc?.getElementById('player')?.currentTime,selectedSegment:doc?.getElementById('segment')?.value,receipt:state.privateContent?.followUps},null,2);}
window.fixtureSave=s=>{state=s;show();};
function pending(){return{status:'running',operation_id:'fixture-operation',operation:'analyze',job_id:job.job_id,progress:{stage:'speech',current:polls,total:3,message:'本機模擬工作進度'},retry_after_seconds:2};}
async function tool(name,args){callCounts[name]=(callCounts[name]||0)+1;
 if(name==='get_prepared_video')return{prepared:job,analysis:result,active_operation:operation?.status==='running'?operation:null,latest_operation:operation};
 if(name==='analyze_prepared_video'){polls=0;operation=pending();return operation;}
 if(name==='get_video_job'){if(operation?.status==='running'&&++polls>=3){result=evidence();operation={...operation,status:'completed',result_kind:'analysis',result};}else if(operation?.status==='running')operation=pending();return operation;}
 if(name==='cancel_video_job'){operation={...operation,status:'cancelled',error:'使用者取消測試工作'};return operation;}
 if(name==='resume_video_job'){polls=0;operation=pending();return operation;}
 if(name==='upload_prepared_video_to_drive')return{status:'completed',operation_id:'fixture-drive',operation:'drive',job_id:job.job_id,result_kind:'drive',result:{status:'drive_ready',job_id:job.job_id,files:job.segments.map((s,i)=>({file_name:s.filename,file_id:'FAKE-NOT-UPLOADED-'+i,size_bytes:12345,url:'https://drive.google.com/'}))}};
 throw new Error('Unexpected fixture tool '+name);}
window.addEventListener('message',async event=>{if(event.source!==frame.contentWindow||event.data?.jsonrpc!=='2.0'||event.data.id===undefined)return;const m=event.data;try{let response={};if(m.method==='tools/call')response={structuredContent:await tool(m.params.name,m.params.arguments)};else if(m.method==='ui/message'){messageCount++;response={};}frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result:response},'*');show();}catch(e){frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,error:{message:e.message}},'*');}});
async function mount(){if(!template)template=await(await fetch('/widget.html')).text();const config=JSON.stringify({toolOutput:job,widgetState:state}).replace(/</g,'\\u003c');const bootstrap='<script>window.openai='+config+';window.openai.setWidgetState=s=>window.parent.fixtureSave(s);<'+ '/script>';frame.srcdoc=template.replace('<script>',bootstrap+'<script>');}
document.getElementById('phone').onclick=()=>{frame.style.width='320px';show();};document.getElementById('desktop').onclick=()=>{frame.style.width='900px';show();};document.getElementById('reopen').onclick=mount;document.getElementById('inspect').onclick=show;
document.getElementById('scenario').onchange=event=>{result=event.target.value==='ready'?null:evidence(event.target.value==='partial');operation=null;state={};callCounts={};messageCount=0;mount();};
mount();
</script></html>`;
new vm.Script(shell.match(/<script>([\s\S]*?)<\/script>/)[1], { filename: 'widget-harness-inline.js' });
if (process.argv.includes('--check')) {
  fs.rmSync(directory, { recursive: true, force: true });
  console.log('PASS: local browser harness script parses and ffmpeg fixture is generated; browser rendering is a separate check.');
  process.exit(0);
}
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (url.pathname === '/') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(shell); }
  else if (url.pathname === '/widget.html') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(fs.readFileSync(path.join(__dirname, '..', 'web', 'widget.html'))); }
  else if (url.pathname === '/fixture.mp4') {
    const bytes = fs.readFileSync(media), range = request.headers.range?.match(/^bytes=(\d+)-(\d*)$/);
    if (range) { const start = Number(range[1]), end = range[2] ? Math.min(Number(range[2]), bytes.length - 1) : bytes.length - 1; response.writeHead(206, { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes', 'Content-Range': 'bytes ' + start + '-' + end + '/' + bytes.length, 'Content-Length': end - start + 1 }); response.end(bytes.subarray(start, end + 1)); }
    else { response.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Length': bytes.length, 'Accept-Ranges': 'bytes' }); response.end(bytes); }
  } else { response.writeHead(404); response.end(); }
});
server.listen(0, '127.0.0.1', () => console.log('WIDGET_HARNESS_URL=http://127.0.0.1:' + server.address().port + '/'));
function cleanup() { server.close(); fs.rmSync(directory, { recursive: true, force: true }); process.exit(); }
process.on('SIGINT', cleanup); process.on('SIGTERM', cleanup);
