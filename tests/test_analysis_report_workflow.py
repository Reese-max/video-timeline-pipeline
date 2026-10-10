"""Actual server/queue boundaries with owned files/workers and explicitly inert transports."""
import asyncio
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import mcp_server as server
from job_queue import JobQueue, atomic_json

class AnalysisReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve(); self.job_id = '0123456789ab'
        self.job = self.root / 'mcp_jobs' / self.job_id
        (self.job / 'segments').mkdir(parents=True)
        shutil.copyfile(Path(__file__).parent / 'fixtures/cutspec/cfr_30.mp4', self.job / 'source.mp4')
        (self.job / 'segments/segment_000.mp4').write_bytes(b'01234567')
        self.manifest = {'status':'ready','version':2,'job_id':self.job_id,'segment_seconds':30,
            'source':{'title':'owned fixture','fingerprint':server.pipeline.file_fingerprint(self.job/'source.mp4')},
            'segments':[{'filename':'segment_000.mp4','start_seconds':0,'end_seconds':2,'size_bytes':8}]}
        atomic_json(self.job/'manifest.json',self.manifest)
        self.stack=ExitStack(); self.addCleanup(self.stack.close); self.addCleanup(self.temp.cleanup)
        for p in (patch.object(server,'MCP_ROOT',self.root),patch.object(server,'MCP_JOBS_ROOT',self.root/'mcp_jobs'),
                  patch.object(server,'MCP_MIN_FREE_BYTES',0),patch.dict(os.environ,{'TRANSCRIBE_BACKEND':'local','MINIMAX_MODEL':'fixture-model','MINIMAX_BASE_URL':'https://api.minimax.io/v1'}),
                  patch.object(server.pipeline,'visual_mode',return_value='video_mp4'),
                  patch('urllib.request.urlopen',side_effect=AssertionError('No network permitted'))):self.stack.enter_context(p)

    def analyze(self):
        # The real analyze_prepared_sync, source hash, timeline merge and write_json run.
        # Only paid/external evidence/probe bodies are inert; no credentials or FFmpeg spawned here.
        with ExitStack() as s:
            for p in (patch.object(server.pipeline,'minimax_subscription_key',return_value='inert-not-real'),
                      patch.object(server.pipeline,'visual_mode',return_value='video_mp4'),
                      patch.object(server.pipeline,'probe_video',return_value={'format':{'duration':'2'},'streams':[{'codec_type':'video'},{'codec_type':'audio'}]}),
                      patch.object(server.pipeline,'extract_audio',return_value=[{'path':str(self.job/'source.mp4')}]),
                      patch.object(server.pipeline,'transcribe_local',return_value={'provider':'owned-inert','segments':[{'start':0,'end':1,'text':'owned speech <source>'}]}),
                      patch.object(server.pipeline,'analyze_video_mp4',return_value=[{'timestamp':0,'visual_summary':'owned visual <source>','visible_text':['42'],'confidence':0.9}]),
                      patch.object(server.pipeline,'create_summary',side_effect=AssertionError('No duplicate summary provider'))):s.enter_context(p)
            return server.analyze_prepared_sync(self.job_id)

    def report_module(self):
        self.assertTrue((Path(server.__file__).parent/'analysis_report.py').is_file(), 'Required report feature source is absent, not an import setup failure')
        return importlib.import_module('analysis_report')

    def ready(self):
        result=self.analyze(); module=self.report_module()
        report=module.record(self.job,result,'explicit caller text ``` not an instruction','owned fixture; author not independently attested')
        local={'filename':'segment_000.mp4','size_bytes':8,'sha256':hashlib.sha256(b'01234567').hexdigest()}
        drive={'status':'drive_ready','job_id':self.job_id,'source_fingerprint':self.manifest['source']['fingerprint'],
            'parent_id':'parent-fixture','folder_id':'folder-fixture','local_segments':[local],
            'files':[{'file_id':'segment-fixture','file_name':local['filename'],'verified':True,'url':'https://drive.google.com/file/d/segment-fixture/view'}]}
        return result,module,report,drive

    def transport(self,report,drive, *, fault=False, changed=None):
        remotes={'folder-fixture':{'id':'folder-fixture','mimeType':server.drive_storage.FOLDER_MIME,'parents':['parent-fixture']},
            'segment-fixture':{'id':'segment-fixture','mimeType':'video/mp4','parents':['folder-fixture'],'size':'8',
                'sha256Checksum':drive['local_segments'][0]['sha256'],'webViewLink':'https://drive.google.com/file/d/segment-fixture/view'}}
        calls=[]
        def upload(token,path,name,parent,mime_type,*,file_id):
            states=list(path.parent.glob('drive-*.json')); self.assertEqual(len(states),1)
            saved=json.loads(states[0].read_bytes());self.assertEqual(saved['file_id'],file_id)
            self.assertEqual((name,parent,mime_type),('A_server_analysis.md','folder-fixture','text/markdown'))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),report['report_sha256'])
            calls.append(file_id)
            if fault:raise RuntimeError('inert interruption after durable id')
            remotes[file_id]={'id':file_id,'name':name,'mimeType':mime_type,'parents':[parent],
                'size':str(path.stat().st_size),'sha256Checksum':report['report_sha256'],
                'webViewLink':'https://drive.google.com/file/d/'+file_id+'/view'}
        stack=ExitStack()
        for p in (patch.object(server.drive_storage,'configured',return_value=True),patch.object(server.drive_storage,'_access_token',return_value='inert'),
            patch.object(server.drive_storage,'_env',return_value='parent-fixture'),patch.object(server.drive_storage,'_new_id',return_value='report-fixture'),
            patch.object(server.drive_storage,'_get_file',side_effect=lambda token,key:remotes.get(key)),patch.object(server.drive_storage,'_upload_file',side_effect=upload)):stack.enter_context(p)
        return stack,remotes,calls

    def test_actual_analyze_persists_dynamic_md_without_summary_provider(self):
        result=self.analyze();self.assertEqual(result['status'],'complete')
        # Baseline reaches real analysis+owned result.json, then genuinely lacks dynamic MD.
        paths=list((self.job/'reports').glob('*/A_server_analysis.md'))
        self.assertEqual(len(paths),1,'Actual analyzed evidence has no durable A Markdown artifact')
        text=paths[0].read_text(encoding='utf-8');self.assertIn('owned visual',text);self.assertIn('owned speech',text);self.assertIn('pending_chatgpt',text)
        self.assertIn(result['source_sha256'],text);self.assertIn('Transcript',text);self.assertIn('not proof',text)

    def test_caller_text_exact_provenance_immutable_bytes_and_stale_summary_rejected(self):
        result,module,report,_=self.ready();path=self.job/report['relative_path'];before=path.read_bytes()
        self.assertIn(b'caller_submitted',before);self.assertIn(b'not_independently_attested',before)
        again=module.record(self.job,result,'explicit caller text ``` not an instruction','owned fixture; author not independently attested')
        self.assertEqual(again['report_sha256'],report['report_sha256']);self.assertEqual(before,path.read_bytes())
        queue=JobQueue(self.root/'queue',autostart=False);self.addCleanup(queue.close)
        with patch.object(server,'get_queue',return_value=queue):
            accepted=server.finalize_analysis_report_sync(self.job_id,report['cache_signature'],report['analysis_sha256'],'explicit tool summary','unknown author; owned fixture')
            self.assertEqual(accepted['summary_status'],'caller_submitted');self.assertEqual(accepted['status'],'awaiting_segments')
            with self.assertRaises(ValueError):server.finalize_analysis_report_sync(self.job_id,'0'*64,report['analysis_sha256'],'text','unknown')
        self.assertEqual(before,path.read_bytes())

    def test_same_folder_id_reserved_before_write_duplicates_reuse_exact_file(self):
        _,_,report,drive=self.ready();stack,remote,calls=self.transport(report,drive)
        with stack:
            first=server.drive_storage.upload_analysis_report(self.job,report,drive)
            second=server.drive_storage.upload_analysis_report(self.job,report,drive)
        self.assertEqual(calls,['report-fixture']);self.assertEqual(first,second)
        self.assertTrue(first['verified']);self.assertEqual(first['folder_id'],'folder-fixture')

    def test_changed_moved_renamed_remote_report_never_overwritten(self):
        _,_,report,drive=self.ready();stack,remote,calls=self.transport(report,drive)
        with stack:
            server.drive_storage.upload_analysis_report(self.job,report,drive)
            original=dict(remote['report-fixture'])
            for field,value in [('name','changed.md'),('parents',['elsewhere']),('sha256Checksum','f'*64),('mimeType','video/mp4')]:
                with self.subTest(field=field):
                    remote['report-fixture']={**original,field:value}
                    with self.assertRaises(ValueError):server.drive_storage.upload_analysis_report(self.job,report,drive)
            self.assertEqual(calls,['report-fixture'])

    def test_interrupted_upload_keeps_id_and_only_explicit_same_id_retry(self):
        _,_,report,drive=self.ready();stack,_,calls=self.transport(report,drive,fault=True)
        with stack:
            with self.assertRaises(RuntimeError):server.drive_storage.upload_analysis_report(self.job,report,drive)
        state=json.loads(next((self.job/report['relative_path']).parent.glob('drive-*.json')).read_bytes())
        self.assertEqual(state['file_id'],'report-fixture');self.assertEqual(state['status'],'uploading');self.assertEqual(calls,['report-fixture'])
        stack,_,calls=self.transport(report,drive)
        with stack,patch.object(server.drive_storage,'_new_id',side_effect=AssertionError('ID may not be regenerated')):
            retried=server.drive_storage.upload_analysis_report(self.job,report,drive)
        self.assertEqual(retried['file_id'],'report-fixture');self.assertEqual(calls,['report-fixture'])

    def test_stale_report_or_changed_segment_has_zero_remote_write(self):
        result,module,report,drive=self.ready();path=self.job/report['relative_path'];original=path.read_bytes();path.write_bytes(b'changed')
        stack,_,calls=self.transport(report,drive)
        with stack:
            with self.assertRaises(ValueError):server.drive_storage.upload_analysis_report(self.job,report,drive)
        self.assertEqual(calls,[])
        with self.assertRaises(ValueError):module.read(self.job,result)
        path.write_bytes(original)  # Explicit fixture restoration, not product cleanup.
        for bad in ({**drive,'local_segments':{}},{**drive,'files':[{}]},
                    {**drive,'local_segments':[{**drive['local_segments'][0],'sha256':'bad'}]}):
            with self.subTest(bad=bad),patch.object(server.drive_storage,'configured',return_value=True),patch.object(server.drive_storage,'_access_token',side_effect=AssertionError('Malformed local metadata must not reach transport')):
                with self.assertRaises(ValueError):server.drive_storage.upload_analysis_report(self.job,report,bad)

    def test_readonly_report_getter_performs_no_submit_or_transport(self):
        result,_,report,_=self.ready();queue=JobQueue(self.root/'queue',autostart=False);self.addCleanup(queue.close)
        with patch.object(queue,'submit',side_effect=AssertionError('getter must not queue')),patch.object(server.drive_storage,'_get_file',side_effect=AssertionError('getter must not use network')):
            snapshot=server.report_snapshot(self.job_id,result,queue)
        self.assertEqual(snapshot['report_sha256'],report['report_sha256']);self.assertEqual(snapshot['status'],'awaiting_segments')

    def test_actual_prepared_getter_report_corruption_preserves_a_drive_and_b_without_replay(self):
        # Supply the original tool_payload's required preparation fields before
        # actual analysis; missing fixture keys must not masquerade as report RED.
        self.manifest['source'].update(url='https://example.invalid/owned-source', duration_seconds=2)
        self.manifest['segments'][0].update(index=0, preview_filename=None, mime_type='video/mp4')
        atomic_json(self.job/'manifest.json', self.manifest)
        result, _, report, drive = self.ready()
        atomic_json(self.job/'drive-manifest.json', drive)
        queue = JobQueue(self.root/'getter-queue', autostart=False)
        self.addCleanup(queue.close)
        pointer = self.job/'analysis-report.json'
        stable = {p:p.read_bytes() for p in (self.job/'manifest.json', self.job/'analysis/result.json',
                   self.job/'drive-manifest.json', self.job/report['relative_path'])}
        def prepared_binding(value):
            # Expiring media links may refresh normally; source/segment bindings must remain exact.
            return {**value, 'segments':[{k:v for k,v in row.items() if k not in ('url','preview_url')}
                                        for row in value['segments']]}
        with patch.object(server, 'get_queue', return_value=queue), \
             patch.object(server.drive_storage, 'configured', return_value=True), \
             patch.object(queue, 'submit', side_effect=AssertionError('getter must not submit a report')) as submit, \
             patch.object(queue, 'resume', side_effect=AssertionError('getter must not resume a report')) as resume, \
             patch.object(server.drive_storage, '_get_file', side_effect=AssertionError('getter must not use Drive transport')) as transport:
            valid = asyncio.run(server.get_prepared_video(self.job_id))
            self.assertEqual(valid['prepared']['status'], 'ready')
            self.assertEqual(valid['analysis']['transcript'], result['transcript'])
            self.assertEqual(valid['analysis']['timeline'], result['timeline'])
            self.assertEqual(valid['drive']['status'], 'drive_ready')
            self.assertIn('segment-fixture', valid['drive']['handoff_text'])
            self.assertEqual(valid['report']['report_sha256'], report['report_sha256'])
            bad_states = (b'{broken-private-pointer',
                          json.dumps({**report, 'operation_id':'f'*32}).encode('utf-8'))
            for bad in bad_states:
                with self.subTest(corruption='invalid-json' if bad.startswith(b'{broken') else 'unknown-operation'):
                    pointer.write_bytes(bad)  # Explicit owned corruption, never product recovery.
                    snapshot = asyncio.run(server.get_prepared_video(self.job_id))
                    self.assertEqual(prepared_binding(snapshot['prepared']), prepared_binding(valid['prepared']))
                    self.assertTrue(snapshot['prepared']['segments'][0]['url'].startswith(
                        server.MCP_MEDIA_BASE_URL+'/media/'+self.job_id+'/segments/segment_000.mp4?'))
                    self.assertEqual(snapshot['analysis'], valid['analysis'])
                    self.assertEqual(snapshot['drive'], valid['drive'])
                    self.assertEqual(snapshot['pending_operations'], [])
                    self.assertIsNone(snapshot['active_operation'])
                    self.assertEqual(snapshot['report'], {'job_id':self.job_id,
                        'cache_signature':result['cache_signature'], 'evidence_status':result['status'], 'status':'report_invalid',
                        'error_code':'REPORT_STATE_INVALID', 'error':'報告狀態無法核對；未重新提交或上傳。',
                        'summary_status':'unknown'})
                    self.assertNotIn('private', json.dumps(snapshot['report']))
                    self.assertEqual(pointer.read_bytes(), bad)
                    for path, raw in stable.items(): self.assertEqual(path.read_bytes(), raw)
            submit.assert_not_called(); resume.assert_not_called(); transport.assert_not_called()
            self.assertEqual(queue.for_job(self.job_id)['pending_operations'], [])
class RealReportQueueTests(unittest.TestCase):
    def test_actual_owned_workers_stopped_before_report_fifo_and_terminal_dedupe(self):
        for fast in ('analyze','drive'):
            with self.subTest(first_completed=fast):self.owned_queue_order(fast)

    def owned_queue_order(self, fast):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();worker=root/'inert_worker.py'
            coordination=root/'completion-order';coordination.mkdir()
            protocol={'version':1,'nonce':os.urandom(16).hex(),'job_id':'0123456789ab',
                'first_completed':fast,'directory':str(coordination),'queue_root':str(root/'q')}
            worker.write_text("""import hashlib,json,os,stat,sys,time
from pathlib import Path

w=Path(sys.argv[1]).resolve()
t=json.loads((w/'input.json').read_text())
inputs=t['inputs'];operation=t['operation']
protocol=inputs.get('completion_protocol')
deadline=time.monotonic()+4

def bounded_wait(path):
    while not path.exists():
        if time.monotonic()>=deadline:
            raise TimeoutError('Owned completion coordination did not finish within four seconds')
        time.sleep(.01)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Owned coordination record must be a regular file')
    return json.loads(path.read_bytes())

def publish(name,value):
    path=coord/name;temporary=coord/(name+'.tmp-'+w.name)
    if path.exists():
        raise ValueError('Duplicate owned coordination record')
    with temporary.open('x',encoding='utf-8') as stream:
        json.dump(value,stream)
    os.replace(temporary,path)

def output(finished):
    raw=json.dumps({'ok':True,'result':{'job_id':inputs['job_id'],
        'operation':operation,'finished':finished}}).encode('utf-8')
    (w/'output.json').write_bytes(raw)
    return raw

if protocol is None:
    # Report jobs retain the original ordinary inert-worker path.
    time.sleep(inputs.get('delay',.15))
    output(time.time())
else:
    if operation not in ('analyze','drive') or set(protocol)!={
        'version','nonce','job_id','first_completed','directory','queue_root'}:
        raise ValueError('Unexpected owned completion protocol')
    coord=Path(protocol['directory']);queue_root=Path(protocol['queue_root'])
    if (protocol['version']!=1 or protocol['job_id']!=inputs['job_id']
        or protocol['first_completed'] not in ('analyze','drive')
        or len(protocol['nonce'])!=32 or any(c not in '0123456789abcdef' for c in protocol['nonce'])
        or len(w.name)!=32 or any(c not in '0123456789abcdef' for c in w.name)
        or coord.is_symlink() or not coord.is_dir() or coord.resolve()!=coord
        or queue_root.is_symlink() or not queue_root.is_dir() or queue_root.resolve()!=queue_root
        or w.parent!=queue_root or coord.parent!=queue_root.parent
        or coord.name!='completion-order'):
        raise ValueError('Completion coordination escaped its owned task identity')
    identity={'version':1,'nonce':protocol['nonce'],'job_id':inputs['job_id'],
        'operation':operation,'operation_id':w.name}
    publish(operation+'-ready.json',identity)
    other='drive' if operation=='analyze' else 'analyze'
    peer=bounded_wait(coord/(other+'-ready.json'))
    if (set(peer)!=set(identity) or peer['version']!=1 or peer['nonce']!=identity['nonce']
        or peer['job_id']!=identity['job_id'] or peer['operation']!=other
        or not isinstance(peer['operation_id'],str) or len(peer['operation_id'])!=32
        or any(c not in '0123456789abcdef' for c in peer['operation_id'])
        or peer['operation_id']==w.name):
        raise ValueError('Readiness does not identify the other owned worker')
    # Both real workers have published readiness before either finishes.
    time.sleep(inputs.get('delay',.15))
    if operation!=protocol['first_completed']:
        completed=bounded_wait(coord/(other+'-completed.json'))
        if (set(completed)!=set(peer)|{'finished','output_sha256'}
            or any(completed[key]!=value for key,value in peer.items())):
            raise ValueError('Completion does not match the ready owned worker')
        peer_work=queue_root/peer['operation_id'];peer_output=peer_work/'output.json'
        if (peer_work.is_symlink() or not peer_work.is_dir() or peer_work.resolve()!=peer_work
            or peer_output.is_symlink() or not stat.S_ISREG(peer_output.stat().st_mode)):
            raise ValueError('Completed output escaped the other owned worker')
        raw=peer_output.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=completed['output_sha256'] or json.loads(raw)!={
            'ok':True,'result':{'job_id':inputs['job_id'],'operation':other,'finished':completed['finished']}}:
            raise ValueError('Completion was not published after the exact closed result')
    if time.monotonic()>=deadline:
        raise TimeoutError('Owned completion coordination did not finish within four seconds')
    finished=time.monotonic_ns()
    raw=output(finished)
    # write_bytes has closed output.json before this atomic completion publication.
    publish(operation+'-completed.json',{**identity,'finished':finished,
        'output_sha256':hashlib.sha256(raw).hexdigest()})
""",encoding='utf-8')
            observed=[];queue=None
            def idle(q):
                if q.report_prerequisites_stopped('0123456789ab'):
                    with q.connect() as db:done=db.execute("SELECT count(*) FROM jobs WHERE operation IN ('analyze','drive') AND status='completed'").fetchone()[0]
                    if done==2:
                        self.assertEqual(q._pair_processes,{})
                        observed.append(q.submit('report',{'job_id':'0123456789ab','report_sha256':'a'*64},'0123456789ab')['operation_id'])
            kwargs={'idle_callback':idle} if 'idle_callback' in inspect.signature(JobQueue).parameters else {}
            writer=root/'writer.lock'
            queue=JobQueue(root/'q',worker=worker,autostart=False,timeout_seconds=60,
                paired_same_job=True,writer_path=writer,writer_context=lambda:server.pipeline.file_writer_lock(writer),**kwargs)
            try:
                a=queue.submit('analyze',{'job_id':'0123456789ab','completion_protocol':protocol,'delay':.1 if fast=='analyze' else .3},'0123456789ab');b=queue.submit('drive',{'job_id':'0123456789ab','completion_protocol':protocol,'delay':.1 if fast=='drive' else .3},'0123456789ab');queue.start()
                deadline=time.time()+5
                while time.time()<deadline and (queue.get(a['operation_id'])['status']!='completed' or queue.get(b['operation_id'])['status']!='completed' or not observed):time.sleep(.02)
                self.assertEqual(queue.get(a['operation_id'])['status'],'completed');self.assertEqual(queue.get(b['operation_id'])['status'],'completed')
                results={key:queue.get(item['operation_id'])['result']['finished'] for key,item in [('analyze',a),('drive',b)]}
                self.assertLess(results[fast],results['drive' if fast=='analyze' else 'analyze'])
                self.assertTrue(observed,'Actual original workers finished, but no report FIFO callback ran')
                deadline=time.time()+5
                while time.time()<deadline and queue.get(observed[0])['status']!='completed':time.sleep(.02)
                self.assertEqual(queue.get(observed[0])['result_kind'],'report');self.assertEqual(len(set(observed)),1)
                again=queue.submit('report',{'job_id':'0123456789ab','report_sha256':'a'*64},'0123456789ab');self.assertEqual(again['operation_id'],observed[0])
            finally:queue.close()

    def test_report_restart_is_interrupted_and_requires_explicit_same_operation_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            q=JobQueue(Path(raw),autostart=False)
            try:
                item=q.submit('report',{'job_id':'0123456789ab'},'0123456789ab')
                with q.connect() as db:db.execute("UPDATE jobs SET status='running' WHERE id=?",(item['operation_id'],))
                # Start recovery without a scheduler child race: preserve real recovery SQL,
                # inert Thread.start only. No worker/native process is claimed in this case.
                with patch('threading.Thread.start'):
                    q.start()
                self.assertEqual(q.get(item['operation_id'])['status'],'interrupted')
                self.assertEqual(q.submit('report',{'job_id':'0123456789ab'},'0123456789ab')['status'],'interrupted')
                self.assertEqual(q.resume(item['operation_id'])['status'],'queued')
            finally:
                q._thread=None;q.close()

if __name__=='__main__':unittest.main()
