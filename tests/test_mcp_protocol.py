import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
import mcp_server as server


class ProtocolSmokeTests(unittest.TestCase):
    def test_report_reconciliation_uses_fresh_widget_cache_key(self):
        released_uri = server.ui_uri(19)
        self.assertNotEqual(server.UI_URI, released_uri)
        self.assertIn(released_uri, server.LEGACY_UI_URIS)

    def test_initialize_tools_resource_and_runtime_health_without_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(server, 'MCP_ROOT', root), patch.object(server, 'MCP_JOBS_ROOT', root / 'mcp_jobs'), \
                    patch.object(server, '_QUEUE', None), TestClient(server.mcp.streamable_http_app(), base_url='http://127.0.0.1:8787') as client:
                headers = {'Accept': 'application/json, text/event-stream'}
                def call(method, params, number):
                    response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
                    self.assertEqual(response.status_code, 200)
                    if response.headers.get('mcp-session-id'):
                        headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
                    raw = response.text
                    if response.headers.get('content-type', '').startswith('text/event-stream'):
                        raw = '\n'.join(line[5:].strip() for line in raw.splitlines() if line.startswith('data:'))
                    value = json.loads(raw)
                    self.assertNotIn('error', value)
                    return value['result']
                try:
                    result = call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'offline-fixture', 'version': '1'}}, 1)
                    self.assertEqual(result['serverInfo']['name'], 'video-timeline-pipeline')
                    headers['MCP-Protocol-Version'] = result['protocolVersion']
                    self.assertEqual(client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code, 202)
                    tools = {tool['name']: tool for tool in call('tools/list', {}, 2)['tools']}
                    self.assertEqual(set(tools), {'probe_video_source', 'prepare_video_for_chatgpt', 'analyze_prepared_video', 'upload_prepared_video_to_drive', 'get_prepared_video', 'get_video_job', 'cancel_video_job', 'resume_video_job', 'collect_social_post', 'ingest_video_segments', 'finalize_analysis_report'})
                    self.assertEqual(tools['get_video_job']['inputSchema']['required'], ['operation_id'])
                    self.assertIn('background', tools['prepare_video_for_chatgpt']['inputSchema']['properties'])
                    resources = call('resources/list', {}, 3)['resources']
                    files = [{'file_id': 'file-preview', 'download_url': 'https://files.example/preview.jpg',
                              'mime_type': 'image/jpeg', 'file_name': 'preview_000.jpg'}]
                    ranges = [{'start_seconds': 0.125, 'end_seconds': 119.875}]
                    with patch.object(server, 'normalize_file_references', wraps=server.normalize_file_references) as receive:
                        handoff = call('tools/call', {'name': 'ingest_video_segments',
                                       'arguments': {'files': files, 'segment_ranges': ranges}}, 5)
                    self.assertFalse(handoff.get('isError', False))
                    receive.assert_called_once_with(files)
                    self.assertEqual(handoff['structuredContent']['files_received'], 1)
                    self.assertEqual(handoff['structuredContent']['files'][0]['start_seconds'], 0.125)
                    for number, bad_files in enumerate(([], ['https://files.example/preview.jpg'],
                            [{'download_url': 'https://files.example/preview.jpg'}],
                            [{'file_id': 'file-preview'}],
                            [{'file_id': ' ', 'download_url': 'https://files.example/preview.jpg'}],
                            [{'file_id': 'file-preview', 'download_url': 'http://files.example/preview.jpg'}],
                            [files[0], files[0]]), 6):
                        with self.subTest(files=bad_files):
                            denied = call('tools/call', {'name': 'ingest_video_segments',
                                          'arguments': {'files': bad_files}}, number)
                            self.assertTrue(denied['isError'])
                            self.assertNotIn('structuredContent', denied)
                    with patch.object(server, 'prepare_video_locked', side_effect=server.JobBusyError('busy')):
                        busy = call('tools/call', {'name': 'prepare_video_for_chatgpt',
                                    'arguments': {'url': 'https://video.example/test.mp4'}}, 13)
                    self.assertFalse(busy.get('isError', False))
                    self.assertEqual(busy['structuredContent']['status'], 'busy')
                    self.assertEqual(busy['structuredContent']['error_code'], 'MCP_BUSY')
                    resource = resources[0]
                    contents = call('resources/read', {'uri': resource['uri']}, 4)['contents']
                    self.assertIn('<html', contents[0]['text'])
                    # Hosts that still hold the previous tool definitions read the old URIs (10-20 deploy).
                    self.assertEqual([item['uri'] for item in resources], [server.UI_URI, *server.LEGACY_UI_URIS])
                    for number, legacy in enumerate(server.LEGACY_UI_URIS, 5):
                        self.assertEqual(call('resources/read', {'uri': legacy}, number)['contents'][0]['text'], contents[0]['text'])
                    self.assertEqual([uri.split('/')[-1].split('-')[0] for uri in server.LEGACY_UI_URIS], ['v18', 'v19'])
                    self.assertEqual(tools['prepare_video_for_chatgpt']['_meta']['openai/outputTemplate'], server.UI_URI)
                    health = client.get('/health').json()
                    deadline = time.monotonic() + 3
                    while health['writer_busy'] and time.monotonic() < deadline:
                        time.sleep(0.02)  # The empty scheduler briefly holds its ownership lock while polling.
                        health = client.get('/health').json()
                    self.assertTrue(health['jobs']['scheduler_alive'])
                    self.assertFalse(health['writer_busy'])
                    self.assertFalse(health['deployment_draining'])
                    self.assertEqual(health['runtime_sha256'], server.RUNTIME_SHA256)
                    self.assertEqual(health['version'], 20)
                    self.assertIn('/v20-', str(resource['uri']))
                    self.assertIn('version: "20"', contents[0]['text'])
                finally:
                    if server._QUEUE is not None:
                        server._QUEUE.close()


if __name__ == '__main__':
    unittest.main()
