import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlsplit, parse_qs, urlencode
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
import mcp_server as server
from job_queue import JobQueue


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'mcp_jobs'
        self.job_id = '0123456789ab'
        self.job = self.jobs / self.job_id
        (self.job / 'segments').mkdir(parents=True)
        (self.job / 'source.mp4').write_bytes(b'original source')
        (self.job / 'segments' / 'segment_000.mp4').write_bytes(b'0123456789')
        self.manifest = {'version':2,'status':'ready','job_id':self.job_id,
                         'source':{'title':'fixture','url':'https://example.test/video','duration_seconds':10},
                         'segment_seconds':15,'max_segments':None,'segments':[
                             {'index':0,'filename':'segment_000.mp4','start_seconds':0,'end_seconds':10,
                              'size_bytes':10,'mime_type':'video/mp4'}]}
        (self.job / 'manifest.json').write_text(json.dumps(self.manifest),encoding='utf-8')
        self.patches = [patch.object(server,'MCP_ROOT',self.root),patch.object(server,'MCP_JOBS_ROOT',self.jobs),
                        patch.object(server,'MCP_MIN_FREE_BYTES',0),
                        patch.dict(os.environ,{'MINIMAX_MODEL':'fixture-model','MINIMAX_BASE_URL':'https://api.minimax.io/v1'})]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def test_cache_invalidates_changed_bytes_even_with_identical_size_and_mtime(self):
        source = self.job / 'source.mp4'
        first = server.analysis_cache_signature(source,self.manifest)
        stamp = source.stat()
        source.write_bytes(b'different video')
        os.utime(source,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual(source.stat().st_size,stamp.st_size)
        self.assertNotEqual(first,server.analysis_cache_signature(source,self.manifest))

    def test_cache_tracks_models_language_backend_and_chunking_without_credentials(self):
        source = self.job / 'source.mp4'
        first = server.analysis_cache_signature(source,self.manifest)
        for field,value in [('GROQ_LANGUAGE','ja'),('GROQ_ASR_MODEL','different'),('TRANSCRIBE_BACKEND','local'),
                            ('WHISPER_MODEL','small')]:
            with self.subTest(field=field), patch.dict(os.environ,{field:value}):
                self.assertNotEqual(first,server.analysis_cache_signature(source,self.manifest))
        with patch.dict(os.environ,{'GROQ_API_KEY':'fixture-only-not-a-real-key'}):
            self.assertEqual(first,server.analysis_cache_signature(source,self.manifest))
        self.assertNotEqual(first,server.analysis_cache_signature(source,{**self.manifest,'segment_seconds':60}))
        with patch.object(server.pipeline,'minimax_model',return_value='changed-model'):
            self.assertNotEqual(first,server.analysis_cache_signature(source,self.manifest))

    def test_partial_evidence_restores_but_stale_signature_is_not_returned(self):
        result_path = self.job / 'analysis' / 'result.json'
        result_path.parent.mkdir()
        result = {'status':'partial','cache_signature':server.analysis_cache_signature(self.job/'source.mp4',self.manifest),
                  'audio_status':'error','visual_status':'complete','timeline':[]}
        result_path.write_text(json.dumps(result),encoding='utf-8')
        self.assertEqual(server.cached_analysis(self.job_id,self.manifest)['status'],'partial')
        with patch.dict(os.environ,{'GROQ_LANGUAGE':'ko'}):
            self.assertIsNone(server.cached_analysis(self.job_id,self.manifest))

    def test_signed_media_get_head_range_expiry_tamper_and_refresh(self):
        server.storage_policy().mark_managed(self.job_id)
        app = Starlette(routes=[Route('/media/{job_id}/{filename:path}',server.media,methods=['GET','HEAD'])])
        with TestClient(app) as client:
            url = urlsplit(server.media_url(self.job_id,'segments/segment_000.mp4'))
            target = url.path + '?' + url.query
            self.assertEqual(client.get(target).content,b'0123456789')
            self.assertEqual(client.head(target).headers['content-length'],'10')
            ranged = client.get(target,headers={'Range':'bytes=2-4'})
            self.assertEqual((ranged.status_code,ranged.content),(206,b'234'))
            self.assertEqual(client.get(target+'&expires=1').status_code,403)
            self.assertEqual(client.get(target.replace('segment_000','segment_001')).status_code,403)
            self.assertEqual(client.get(url.path).status_code,403)
            params = parse_qs(url.query)
            with patch('media_lifecycle.time.time',return_value=int(params['expires'][0])+1):
                self.assertEqual(client.get(target).status_code,403)
            with patch.object(server,'MCP_LEGACY_MEDIA_UNTIL',int(time.time())+60):
                self.assertEqual(client.get(url.path).status_code,200)
                self.assertEqual(client.get(url.path+'?sig=wrong').status_code,403)
            fresh = server.tool_payload(self.manifest)['segments'][0]['url']
            self.assertIn('expires=',fresh)

    def test_background_tools_queue_once_and_restore_active_operation(self):
        queue = JobQueue(self.root / 'queue',autostart=False)
        with patch.object(server,'get_queue',return_value=queue):
            one = asyncio.run(server.analyze_prepared_video(self.job_id,background=True))
            two = asyncio.run(server.analyze_prepared_video(self.job_id,background=True))
            self.assertEqual(one.structuredContent['operation_id'],two.structuredContent['operation_id'])
            server.PreparedVideoAnalysisResult.model_validate(one.structuredContent)
            restored = asyncio.run(server.get_prepared_video(self.job_id))
            self.assertEqual(restored['active_operation']['operation_id'],one.structuredContent['operation_id'])
            self.assertEqual(asyncio.run(server.cancel_video_job(one.structuredContent['operation_id']))['status'],'cancelled')
            resumed = asyncio.run(server.resume_video_job(one.structuredContent['operation_id']))
            self.assertEqual(resumed['status'],'queued')
        queue.close()


if __name__ == '__main__': unittest.main()
