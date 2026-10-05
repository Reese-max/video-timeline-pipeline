"""Offline transport, filesystem and SQLite discovery regressions."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import pipeline
TOKEN = "offline-private-token"

class Response:

    def __init__(self, value):
        self.stream = io.BytesIO(json.dumps(value).encode())
        self.headers = {}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def read(self, size=-1):
        return self.stream.read(size)

class Patches:
    def __init__(self, stack): self.stack = stack
    def setenv(self, key, value): self.stack.enter_context(patch.dict(os.environ, {key: value}))
    def setattr(self, target, key, value): self.stack.enter_context(patch.object(target, key, value))

class InstagramBudgetRecoveryTests(unittest.TestCase):
    @contextlib.contextmanager
    def offline_case(self):
        import cost_tracker
        with contextlib.ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": TOKEN, "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60"}, clear=True))
            stack.enter_context(cost_tracker.cost_root_scope(None))
            self.patcher = Patches(stack)
            self.patcher.setattr(pipeline.time, "sleep", lambda seconds: None)
            calls = []
            def ready(request, **kwargs):
                calls.append(request)
                if request.get_method() == "POST": return Response({"snapshot_id": "sd_offline"})
                if "/progress/" in request.full_url: return Response({"status": "ready"})
                return Response([])
            self.patcher.setattr(pipeline.urllib.request, "urlopen", ready)
            yield root, calls

    def test_invalid_cap_stops_before_transport(self):
        for cap in [None, 0, -1, True, 1.5, '3', float('nan'), float('inf')]:
            with self.subTest(cap=cap):
                with self.offline_case() as (root, calls):
                    with self.assertRaises(ValueError):
                        pipeline.fetch_instagram_author('testauthor', max_items=cap, allow_paid=True, root=root)
                    self.assertTrue(calls == [])
                    self.assertTrue(not list(root.rglob('.submitted')))

    def test_invalid_timeout_stops_before_transport(self):
        for timeout in ['0', '-1', 'invalid']:
            with self.subTest(timeout=timeout):
                with self.offline_case() as (root, calls):
                    self.patcher.setenv('BRIGHTDATA_RUN_TIMEOUT_SECONDS', timeout)
                    with self.assertRaises(ValueError):
                        pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
                    self.assertTrue(calls == [])

    def test_invalid_env_cap_stops_before_transport(self):
        for budget in ['', '0', '-2', '1.5', 'false', 'nan']:
            with self.subTest(budget=budget):
                with self.offline_case() as (root, calls):
                    self.patcher.setenv('BRIGHTDATA_INSTAGRAM_MAX_ITEMS', budget)
                    with self.assertRaises(ValueError):
                        pipeline.fetch_instagram_author('testauthor', allow_paid=True, root=root)
                    self.assertTrue(calls == [])

    def test_effective_bound_printed_on_stderr_before_actual_post(self):
        with self.offline_case() as (root, calls):
            stdout, stderr = (io.StringIO(), io.StringIO())
            real_transport = pipeline.urllib.request.urlopen

            def check(request, **kwargs):
                if request.get_method() == 'POST':
                    self.assertTrue('3' in stderr.getvalue())
                    self.assertTrue(TOKEN not in stdout.getvalue() + stderr.getvalue())
                    self.assertTrue(json.loads(request.data)['input'][0]['num_of_posts'] == 3)
                return real_transport(request, **kwargs)
            self.patcher.setattr(pipeline.urllib.request, 'urlopen', check)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            self.assertTrue(result['instagram']['max_items'] == 3)
            self.assertTrue(len(calls) == 3)
            self.assertTrue(TOKEN not in next(root.rglob('submission.json')).read_text())

    def test_cli_reads_budget_from_documented_env_before_validation(self):
        for environment in ['project', 'root']:
            with self.subTest(environment=environment):
                with self.offline_case() as (root, calls):
                    project, root = (root / 'project', root / 'data')
                    project.mkdir()
                    root.mkdir()
                    ((project if environment == 'project' else root) / '.env').write_text('BRIGHTDATA_INSTAGRAM_MAX_ITEMS=3\nBRIGHTDATA_API_TOKEN=' + TOKEN + '\n')
                    self.patcher.setattr(pipeline, '__file__', str(project / 'pipeline.py'))
                    self.patcher.setattr(pipeline.sys, 'argv', ['pipeline.py', '--root', str(root), '--instagram-author', 'testauthor', '--instagram-discover-only', '--allow-batch-cloud'])
                    self.assertTrue(pipeline.run_rss_command(pipeline.parse_args()) == 0)
                    posts = [r for r in calls if r.get_method() == 'POST']
                    self.assertTrue(len(posts) == 1)
                    self.assertTrue(json.loads(posts[0].data)['input'][0]['num_of_posts'] == 3)
                    self.assertTrue(list(root.rglob('submission.json')) and list(root.rglob('instagram-*-latest.json')))

    def test_unknown_submission_retains_receipt_and_never_posts_twice(self):
        for response in [{}, {'snapshot_id': 'future_bad'}, {'snapshot_id': 3}, None, [], 'unknown']:
            with self.subTest(response=response):
                with self.offline_case() as (root, calls):
                    calls = []

                    def unknown(request, **kwargs):
                        calls.append(request)
                        return Response({**response, 'diagnostic': TOKEN} if isinstance(response, dict) else response)
                    self.patcher.setattr(pipeline.urllib.request, 'urlopen', unknown)
                    for _ in range(2):
                        with self.assertRaisesRegex(RuntimeError, '禁止重送'):
                            pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
                    self.assertTrue(len(calls) == 1)
                    receipt = next(root.rglob('submission.json')).read_text()
                    self.assertTrue(TOKEN not in receipt and '"response"' in receipt)
                    self.assertTrue(next(root.rglob('.submitted')).is_file())

    def test_real_sqlite_ledger_tracks_submission_and_uncertainty(self):
        for response in [{'snapshot_id': 'sd_ledger'}, {}, None, []]:
            with self.subTest(response=response):
                with self.offline_case() as (root, calls):
                    import cost_tracker
                    self.patcher.setenv('BUDGET_HARD_STOP', 'false')
                    self.patcher.setenv('DAILY_BUDGET_USD', '100')
                    real_transport = pipeline.urllib.request.urlopen

                    def transport(request, **kwargs):
                        if request.get_method() == 'POST':
                            return Response(response)
                        return real_transport(request, **kwargs)
                    self.patcher.setattr(pipeline.urllib.request, 'urlopen', transport)
                    ready = isinstance(response, dict) and response.get('snapshot_id') == 'sd_ledger'
                    with cost_tracker.cost_root_scope(root):
                        if ready:
                            pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
                        else:
                            with self.assertRaisesRegex(RuntimeError, '禁止重送'):
                                pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
                    with sqlite3.connect(root / 'operations.db') as db:
                        status, cost_status, operation = db.execute('SELECT status,cost_status,operation FROM usage_costs').fetchone()
                        reservation = db.execute('SELECT status FROM budget_reservations').fetchone()[0]
                    self.assertTrue(operation == 'datasets.trigger' and cost_status == 'estimated')
                    self.assertTrue(status == ('succeeded' if ready else 'unknown'))
                    self.assertTrue(reservation == ('settled' if ready else 'reserved'))
                    self.assertTrue('"response"' in next(root.rglob('submission.json')).read_text())

    def test_transport_timeout_blocks_second_submission_across_restart(self):
        with self.offline_case() as (root, calls):
            calls = []

            def timeout(request, **kwargs):
                calls.append(request)
                raise TimeoutError(TOKEN + ' submission timeout')
            self.patcher.setattr(pipeline.urllib.request, 'urlopen', timeout)
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, '禁止重送'):
                    pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            self.assertTrue(len(calls) == 1)
            receipt = json.loads(next(root.rglob('submission.json')).read_text())
            self.assertTrue('error' in receipt and TOKEN not in str(receipt))

    def test_poll_transport_failure_recovers_same_snapshot_without_post(self):
        with self.offline_case() as (root, calls):
            calls = []
            recovering = False

            def transport(request, **kwargs):
                calls.append(request)
                if request.get_method() == 'POST':
                    return Response({'snapshot_id': 'sd_resume'})
                if '/progress/' in request.full_url:
                    if not recovering:
                        raise urllib.error.URLError('synthetic poll failure')
                    return Response({'status': 'ready'})
                return Response([])
            self.patcher.setattr(pipeline.urllib.request, 'urlopen', transport)
            with self.assertRaisesRegex(RuntimeError, 'synthetic poll failure'):
                pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            recovering = True
            result = pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            self.assertTrue(result['instagram']['snapshot_id'] == 'sd_resume')
            self.assertTrue(sum((r.get_method() == 'POST' for r in calls)) == 1)
            self.assertTrue(all(('sd_resume' in r.full_url for r in calls if r.get_method() == 'GET')))

    def test_receipt_write_failure_leaves_marker_and_never_resubmits(self):
        with self.offline_case() as (root, calls):
            real_write = pipeline.write_json

            def write(path, value):
                if path.name == 'submission.json':
                    raise OSError('synthetic disk full')
                return real_write(path, value)
            self.patcher.setattr(pipeline, 'write_json', write)
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            self.patcher.setattr(pipeline, 'write_json', real_write)
            with self.assertRaisesRegex(RuntimeError, '禁止再次計費'):
                pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            self.assertTrue(len(calls) == 1)
            self.assertTrue(next(root.rglob('.submitted')).is_file())

    def test_record_cap_does_not_authorize_strict_dollar_guard(self):
        with self.offline_case() as (root, calls):
            import cost_tracker
            self.patcher.setenv('BUDGET_HARD_STOP', 'true')
            self.patcher.setenv('DAILY_BUDGET_USD', '100')
            with cost_tracker.cost_root_scope(root), self.assertRaisesRegex(RuntimeError, '供應商端可執行'):
                pipeline.fetch_instagram_author('testauthor', max_items=3, allow_paid=True, root=root)
            self.assertTrue(calls == [])
            self.assertTrue(not list(root.rglob('.submitted')))
            self.assertTrue(not list(root.rglob('submission.json')))

if __name__ == "__main__":
    unittest.main()
