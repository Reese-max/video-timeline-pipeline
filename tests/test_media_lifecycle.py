import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from media_lifecycle import (
    MARKER_NAME, MediaSigner, StorageCapacityError, StoragePolicy, StoragePolicyError,
)


JOB = "0123456789ab"
OTHER = "abcdef012345"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.policy = StoragePolicy(self.base / "jobs", 1_000_000, 500_000, 100, 0)

    def job(self, job_id=JOB, size=20):
        path = self.policy.root / job_id
        path.mkdir()
        (path / "segments").mkdir()
        (path / "segments" / "segment_000.mp4").write_bytes(b"x" * size)
        return path

    def symlink(self, target, path, is_dir=False):
        try:
            path.symlink_to(target, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlinks unavailable: {type(error).__name__}")

    def test_unmanaged_jobs_are_never_deployment_cleanup_candidates(self):
        path = self.job()
        (path / "source.mp4").write_bytes(b"original")
        report = self.policy.cleanup(now=10**9)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["skipped"][0]["reason"], "unmanaged")
        self.assertEqual((path / "source.mp4").read_bytes(), b"original")

    def test_explicit_adoption_starts_grace_and_reuse_refreshes_retention(self):
        self.job()
        self.policy.mark_managed(JOB, now=1000)
        self.assertEqual(self.policy.cleanup(now=1099)["candidates"], [])
        self.assertEqual(len(self.policy.cleanup(now=1100)["candidates"]), 1)
        marker = self.policy.mark_managed(JOB, now=1050)
        self.assertEqual(marker["managed_at"], 1000)
        self.assertEqual(self.policy.cleanup(now=1100)["candidates"], [])
        self.assertEqual(len(self.policy.cleanup(now=1150)["candidates"]), 1)

    def test_dry_run_does_not_touch_expired_managed_data(self):
        path = self.job()
        self.policy.mark_managed(JOB, now=1000)
        before = (path / MARKER_NAME).read_bytes()
        result = self.policy.cleanup(now=1100)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["deleted_job_ids"], [])
        self.assertEqual(result["reclaimed_bytes"], 0)
        self.assertEqual(result["candidates"][0]["job_id"], JOB)
        self.assertEqual((path / MARKER_NAME).read_bytes(), before)

    def test_active_jobs_and_valid_shares_override_retention(self):
        self.job()
        self.policy.mark_managed(JOB, now=1000)
        active = self.policy.cleanup(protected_job_ids=[JOB], now=1500)
        self.assertEqual(active["skipped"][0]["reason"], "active")
        self.policy.protect_until(JOB, 2000)
        self.policy.protect_until(JOB, 1600)
        live = self.policy.cleanup(now=1999)
        self.assertEqual(live["skipped"][0]["reason"], "live_share")
        self.assertEqual(live["skipped"][0]["protected_until"], 2000)
        self.assertEqual(len(self.policy.cleanup(now=2000)["candidates"]), 1)

    def test_signing_legacy_job_does_not_silently_adopt_it(self):
        path = self.job()
        self.assertFalse(self.policy.protect_until(JOB, 2000)["managed"])
        self.assertFalse((path / MARKER_NAME).exists())
        self.assertEqual(self.policy.cleanup(now=3000)["candidates"], [])

    def test_marker_updates_serialize_across_policy_instances(self):
        self.job()
        self.policy.mark_managed(JOB, now=1000)
        other = StoragePolicy(self.policy.root, 1_000_000, 500_000, 100, 0)
        holding, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        first_write, second_write = self.policy._write_marker, other._write_marker
        def slow_write(path, marker):
            holding.set()
            if not release.wait(10):
                raise AssertionError("Lock fixture timed out")
            first_write(path, marker)
        def observed_write(path, marker):
            second_entered.set()
            second_write(path, marker)
        with (patch.object(self.policy, "_write_marker", side_effect=slow_write),
              patch.object(other, "_write_marker", side_effect=observed_write),
              concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool):
            first = pool.submit(self.policy.protect_until, JOB, 3000)
            try:
                self.assertTrue(holding.wait(5))
                second = pool.submit(other.protect_until, JOB, 2000)
                self.assertFalse(second_entered.wait(0.2), "Separate instances must share the filesystem lock")
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(self.policy.usage(now=1000)["jobs"][0]["protected_until"], 3000)

    def test_marker_updates_serialize_across_processes(self):
        self.job()
        self.policy.mark_managed(JOB, now=1000)
        script = '''import sys,time
from pathlib import Path
from media_lifecycle import StoragePolicy
root, phase, signals = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
policy = StoragePolicy(root, 1000000, 500000, 100, 0)
original = policy._write_marker
def observed(path, value):
    (signals / (phase + "-entered")).write_text("1")
    if phase == "first":
        deadline = time.monotonic() + 45
        while not (signals / "release").exists():
            if time.monotonic() > deadline: raise RuntimeError("fixture timeout")
            time.sleep(.01)
    return original(path, value)
policy._write_marker = observed
(signals / (phase + "-started")).write_text("1")
policy.protect_until("0123456789ab", 3000 if phase == "first" else 2000)
'''
        processes = []
        def start(phase):
            child = subprocess.Popen([sys.executable, "-c", script, str(self.policy.root), phase, str(self.base)],
                                     cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            processes.append(child)
            return child
        def await_file(name):
            deadline = time.monotonic() + 25
            while not (self.base / name).exists():
                for child in processes:
                    if child.poll() is not None and child.returncode:
                        _, error = child.communicate(timeout=2)
                        self.fail(f"Process fixture exited before {name}: {error}")
                if time.monotonic() > deadline:
                    self.fail(f"Process fixture did not reach {name}")
                time.sleep(.01)
        try:
            start("first")
            await_file("first-entered")
            start("second")
            await_file("second-started")
            time.sleep(.2)
            self.assertFalse((self.base / "second-entered").exists(), "Worker process bypassed lifecycle lock")
            (self.base / "release").write_text("1")
            for child in processes:
                _, error = child.communicate(timeout=7)
                self.assertEqual(child.returncode, 0, error)
        finally:
            (self.base / "release").write_text("1")
            for child in processes:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        self.assertEqual(self.policy.usage(now=1000)["jobs"][0]["protected_until"], 3000)

    def test_usage_includes_source_and_nested_derived_files(self):
        path = self.job(size=20)
        (path / "source.mp4").write_bytes(b"x" * 40)
        (path / "work").mkdir()
        (path / "work" / "audio.wav").write_bytes(b"x" * 30)
        report = self.policy.usage()
        self.assertEqual(report["total_bytes"], 90)
        self.assertEqual(report["job_count"], 1)
        self.assertEqual(report["managed_job_count"], 0)

    def test_capacity_checks_job_total_and_actual_free_space_without_deleting(self):
        path = self.job(size=20)
        self.job(OTHER, size=30)
        self.policy.max_job_bytes = 25
        with self.assertRaises(StorageCapacityError) as caught:
            self.policy.ensure_capacity(JOB, 6)
        self.assertEqual(caught.exception.code, "job_quota")
        self.policy.max_job_bytes = 100
        self.policy.max_total_bytes = 55
        with self.assertRaises(StorageCapacityError) as caught:
            self.policy.ensure_capacity(JOB, 6)
        self.assertEqual(caught.exception.code, "total_quota")
        self.policy.min_free_bytes = 10
        fake_usage = shutil._ntuple_diskusage(100, 88, 12)
        with patch("media_lifecycle.shutil.disk_usage", return_value=fake_usage):
            with self.assertRaises(StorageCapacityError) as caught:
                self.policy.ensure_capacity(JOB, 3)
            self.assertEqual(caught.exception.code, "free_space")
        self.assertTrue(path.exists())

    def test_capacity_preflight_allows_missing_safe_job_but_does_not_create_it(self):
        report = self.policy.ensure_capacity(JOB, 50)
        self.assertEqual(report["requested_bytes"], 50)
        self.assertFalse((self.policy.root / JOB).exists())
        for invalid in ("../0123456789ab", "ABCDEFGHIJKL", "0123456789ab/x", True):
            with self.subTest(invalid=invalid), self.assertRaises(StoragePolicyError):
                self.policy.ensure_capacity(invalid, 0)
        for size in (-1, True, 2.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.policy.ensure_capacity(JOB, size)

    def test_invalid_markers_are_not_cleanup_authority(self):
        path = self.job()
        for value in (b"{bad", b"{}", json.dumps({"version": 1, "job_id": OTHER}).encode()):
            with self.subTest(marker=value):
                (path / MARKER_NAME).write_bytes(value)
                report = self.policy.cleanup(now=10**9)
                self.assertEqual(report["candidates"], [])
                self.assertEqual(report["skipped"][0]["reason"], "unsafe_or_unreadable")

    def test_unrecognized_directory_names_are_never_managed_or_deleted(self):
        (self.policy.root / "customer-originals").mkdir()
        (self.policy.root / "customer-originals" / "keep.mp4").write_bytes(b"keep")
        report = self.policy.usage()
        self.assertIn("customer-originals", report["ignored_entries"])
        self.policy.cleanup(dry_run=False, now=10**9)
        self.assertTrue((self.policy.root / "customer-originals" / "keep.mp4").exists())

    def test_symlink_jobs_and_nested_symlinks_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        target = outside / "keep.mp4"
        target.write_bytes(b"keep")
        self.symlink(outside, self.policy.root / JOB, is_dir=True)
        with self.assertRaises(StoragePolicyError):
            self.policy.mark_managed(JOB, now=0)
        self.assertEqual(self.policy.cleanup(dry_run=False, now=10**9)["candidates"], [])
        (self.policy.root / JOB).unlink()
        path = self.job()
        self.policy.mark_managed(JOB, now=0)
        self.symlink(target, path / "segments" / "external.mp4")
        with self.assertRaises(StorageCapacityError) as caught:
            self.policy.ensure_capacity(OTHER, 0)
        self.assertEqual(caught.exception.code, "unsafe_storage")
        self.assertEqual(self.policy.cleanup(dry_run=False, now=10**9)["candidates"], [])
        self.assertEqual(target.read_bytes(), b"keep")

    def test_symlink_root_is_rejected(self):
        link = self.base / "linked"
        self.symlink(self.policy.root, link, is_dir=True)
        with self.assertRaises(StoragePolicyError):
            StoragePolicy(link, 100, 100, 100, 0)

    def test_replaced_root_is_rejected(self):
        renamed = self.base / "renamed"
        self.policy.root.rename(renamed)
        self.policy.root.mkdir()
        with self.assertRaises(StoragePolicyError):
            self.policy.usage()

    @unittest.skipUnless(os.name == "posix" and getattr(shutil.rmtree, "avoids_symlink_attacks", False),
                         "Destructive cleanup requires fd-based POSIX rmtree")
    def test_explicit_cleanup_deletes_only_expired_unprotected_managed_job(self):
        self.job()
        other = self.job(OTHER)
        self.policy.mark_managed(JOB, now=0)
        before = self.policy.usage()["total_bytes"]
        result = self.policy.cleanup(dry_run=False, now=100)
        self.assertEqual(result["deleted_job_ids"], [JOB])
        self.assertFalse((self.policy.root / JOB).exists())
        self.assertTrue(other.exists())
        self.assertEqual(result["reclaimed_bytes"], before - self.policy.usage()["total_bytes"])

    @unittest.skipUnless(os.name == "posix" and getattr(shutil.rmtree, "avoids_symlink_attacks", False),
                         "Destructive cleanup requires fd-based POSIX rmtree")
    def test_cleanup_rechecks_link_protection_after_planning(self):
        path = self.job()
        self.policy.mark_managed(JOB, now=0)
        original_usage = self.policy.usage
        calls = 0
        def concurrent_share(*args, **kwargs):
            nonlocal calls
            report = original_usage(*args, **kwargs)
            calls += 1
            if calls == 1:
                self.policy.protect_until(JOB, 1000)
            return report
        with patch.object(self.policy, "usage", side_effect=concurrent_share):
            result = self.policy.cleanup(dry_run=False, now=100)
        self.assertEqual(result["deleted_job_ids"], [])
        self.assertEqual(result["skipped"][0]["reason"], "changed_since_plan")
        self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "posix" and getattr(shutil.rmtree, "avoids_symlink_attacks", False),
                         "Destructive cleanup requires fd-based POSIX rmtree")
    def test_cleanup_rmtree_refuses_a_late_symlink_swap(self):
        path = self.job()
        self.policy.mark_managed(JOB, now=0)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.mp4").write_bytes(b"original")
        original_remove = shutil.rmtree
        def swap_then_remove(name, **kwargs):
            path.rename(self.policy.root / "rescued")
            path.symlink_to(outside, target_is_directory=True)
            return original_remove(name, **kwargs)
        swap_then_remove.avoids_symlink_attacks = True
        with patch("media_lifecycle.shutil.rmtree", swap_then_remove):
            result = self.policy.cleanup(dry_run=False, now=100)
        self.assertEqual(result["deleted_job_ids"], [])
        self.assertEqual(result["skipped"][0]["reason"], "delete_failed")
        self.assertEqual((outside / "keep.mp4").read_bytes(), b"original")
        path.unlink()

    def test_platform_without_symlink_safe_delete_fails_closed(self):
        path = self.job()
        self.policy.mark_managed(JOB, now=0)
        with patch("media_lifecycle.shutil.rmtree.avoids_symlink_attacks", False, create=True):
            result = self.policy.cleanup(dry_run=False, now=100)
        self.assertEqual(result["deleted_job_ids"], [])
        self.assertEqual(result["skipped"][0]["reason"], "unsafe_platform")
        self.assertTrue(path.exists())


class MediaSignerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.keyfile = self.root / "private" / "media.key"
        self.signer = MediaSigner(self.keyfile, max_ttl_seconds=100)

    def test_key_persists_without_logging_or_replacing_it(self):
        signature = self.signer.sign(JOB, "segments/segment_000.mp4", 1050, now=1000)
        reloaded = MediaSigner(self.keyfile, max_ttl_seconds=100)
        self.assertEqual(signature, reloaded.sign(JOB, "segments/segment_000.mp4", 1050, now=1000))
        self.assertEqual(self.keyfile.stat().st_size, 32)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.keyfile.stat().st_mode), 0o600)

    def test_concurrent_creation_uses_one_complete_persisted_key(self):
        new_key = self.root / "concurrent.key"
        def create(_):
            return MediaSigner(new_key).sign(JOB, "previews/preview_000.jpg", 1050, now=1000)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(create, range(12)))
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(list(self.root.glob("concurrent.key.*.tmp")), [])

    def test_binds_job_path_and_expiry_and_accepts_canonical_query_expiry(self):
        path = "segments/segment_000.mp4"
        signature = self.signer.sign(JOB, path, 1050, now=1000)
        self.assertTrue(self.signer.verify(JOB, path, "1050", signature, now=1001))
        for job_id, name, expires in ((OTHER, path, 1050), (JOB, "previews/preview_000.jpg", 1050),
                                     (JOB, path, 1051)):
            self.assertFalse(self.signer.verify(job_id, name, expires, signature, now=1001))
        self.assertFalse(self.signer.verify(JOB, path, 1050, "0" * 64, now=1001))
        self.assertFalse(self.signer.verify(JOB, path, 1050, signature, now=1050))

    def test_same_signature_is_method_and_range_independent(self):
        # Authorization covers the resource. HTTP GET/HEAD/Range are handler concerns.
        signature = self.signer.sign(JOB, "previews/preview_000.jpg", 1050, now=1000)
        for _method in ("GET", "HEAD", "GET bytes=0-99"):
            self.assertTrue(self.signer.verify(JOB, "previews/preview_000.jpg", 1050, signature, now=1001))

    def test_rejects_traversal_other_files_and_ambiguous_encodings(self):
        for name in ("source.mp4", "segments/../source.mp4", "/segments/x.mp4", "segments\\x.mp4",
                     "segments/%2e%2e/x.mp4", "segments/x.mp4?x=1", "segments/x.mp4#fragment",
                     "segments/.hidden.mp4", "previews/x.png", "segments/sub/x.mp4", "segments/x.mp4\n"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.signer.sign(JOB, name, 1050, now=1000)
        for expiry in (True, 1050.0, "01050", "1e3", "1050&x=1", -1, 1000, 1101):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                self.signer.sign(JOB, "segments/x.mp4", expiry, now=1000)
        self.assertFalse(self.signer.verify(JOB, "segments/x.mp4", "1050", None, now=1000))
        self.assertFalse(self.signer.verify(JOB, "segments/x.mp4", "1050", "é" * 64, now=1000))

    def test_invalid_or_public_key_files_are_not_silently_repaired(self):
        short = self.root / "short.key"
        short.write_bytes(b"bad")
        short.chmod(0o600)
        with self.assertRaises(StoragePolicyError):
            MediaSigner(short)
        if os.name == "posix":
            self.keyfile.chmod(0o644)
            with self.assertRaises(StoragePolicyError):
                MediaSigner(self.keyfile)

    def test_key_symlink_is_not_followed(self):
        link = self.root / "linked.key"
        try:
            link.symlink_to(self.keyfile)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlinks unavailable: {type(error).__name__}")
        with self.assertRaises(StoragePolicyError):
            MediaSigner(link)


if __name__ == "__main__":
    unittest.main()
