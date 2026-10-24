import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
import mcp_server as server


class ProtocolSmokeTests(unittest.TestCase):
    def test_checkpoint_and_polling_fixes_use_fresh_widget_cache_key(self):
        released_uri = server.ui_uri(17)
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
                    self.assertEqual(set(tools), {'prepare_video_for_chatgpt', 'analyze_prepared_video', 'upload_prepared_video_to_drive', 'get_prepared_video', 'get_video_job', 'cancel_video_job', 'resume_video_job', 'collect_social_post', 'ingest_video_segments', 'finalize_analysis_report'})
                    self.assertEqual(tools['get_video_job']['inputSchema']['required'], ['operation_id'])
                    self.assertIn('background', tools['prepare_video_for_chatgpt']['inputSchema']['properties'])
                    resources = call('resources/list', {}, 3)['resources']
                    resource = resources[0]
                    contents = call('resources/read', {'uri': resource['uri']}, 4)['contents']
                    self.assertIn('<html', contents[0]['text'])
                    # Hosts that still hold the previous tool definitions read the old URIs (10-20 deploy).
                    self.assertEqual([item['uri'] for item in resources], [server.UI_URI, *server.LEGACY_UI_URIS])
                    for number, legacy in enumerate(server.LEGACY_UI_URIS, 5):
                        self.assertEqual(call('resources/read', {'uri': legacy}, number)['contents'][0]['text'], contents[0]['text'])
                    self.assertEqual([uri.split('/')[-1].split('-')[0] for uri in server.LEGACY_UI_URIS], ['v16', 'v17'])
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
                    self.assertEqual(health['version'], 18)
                    self.assertIn('/v18-', str(resource['uri']))
                    self.assertIn('version: "18"', contents[0]['text'])
                finally:
                    if server._QUEUE is not None:
                        server._QUEUE.close()


if __name__ == '__main__':
    unittest.main()
