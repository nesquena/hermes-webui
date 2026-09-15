"""Byte-integrity and scan-amplification regressions at the real token helper.

Use ./scripts/test.sh in a complete checkout. Fixtures advance monotonic
freshness explicitly; they never disable the production cache or weaken hashes.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import api.asset_identity_cache as identity_cache
import api.routes as routes
import api.updates as updates


class StaticIdentityFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 100.0
        self.cache = identity_cache.AssetIdentityCache(clock=lambda: self.now, wait_seconds=5)
        self.cache_patch = patch.object(identity_cache, "ASSET_IDENTITY_CACHE", self.cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.version_patch = patch.object(updates, "WEBUI_VERSION", "v0.52.294")
        self.version_patch.start()
        self.addCleanup(self.version_patch.stop)
        (self.root / "ui.js").write_bytes(b"export const value = 'alpha';\n")
        (self.root / "sw.js").write_bytes(b"worker __WEBUI_VERSION__")

    def token(self):
        return routes._assets_cache_bust_token(self.root)

    def test_repeated_calls_share_one_real_recursive_hash(self):
        with patch.object(routes, "_static_content_identity", wraps=routes._static_content_identity) as scan:
            first = self.token()
            for _ in range(40):
                self.assertEqual(self.token(), first)
            self.assertEqual(scan.call_count, 1)

    def _concurrent_scan(self, *, failure=False, expired=False):
        if expired:
            self.token()
            self.now += 1
        arrivals = Queue()
        class ObservedCondition(threading.Condition):
            def wait(self, timeout=None):
                arrivals.put(threading.get_ident())
                return super().wait(timeout)
        self.cache._condition = ObservedCondition(threading.Lock())
        release = threading.Event()
        calls = []
        count_lock = threading.Lock()
        real_scan = routes._static_content_identity
        def scan(root):
            with count_lock:
                calls.append(root)
            arrivals.put(threading.get_ident())
            if not release.wait(5):
                raise AssertionError("scan release timed out")
            if failure:
                raise PermissionError("controlled strict-inventory failure")
            return real_scan(root)
        with patch.object(routes, "_static_content_identity", side_effect=scan):
            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(self.token) for _ in range(16)]
                try:
                    seen = set()
                    while len(seen) < 16:
                        seen.add(arrivals.get(timeout=5))
                    self.assertEqual(len(calls), 1, "public callers amplified the recursive scan")
                finally:
                    release.set()
                tokens = [future.result(5) for future in futures]
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(routes._asset_identity_is_available(tokens[0]), not failure)

    def test_cold_public_token_burst_has_one_scan(self):
        self._concurrent_scan()

    def test_expired_public_token_burst_has_one_scan(self):
        self._concurrent_scan(expired=True)

    def test_failed_public_token_burst_has_one_scan(self):
        self._concurrent_scan(failure=True)

    def test_missing_root_is_negatively_cached_then_recovers(self):
        self.root = self.root / "not-created-yet"
        with patch.object(routes, "_static_content_identity", wraps=routes._static_content_identity) as scan:
            tokens = [self.token() for _ in range(30)]
            self.assertTrue(all(not routes._asset_identity_is_available(token) for token in tokens))
            self.assertEqual(scan.call_count, 1)
            self.root.mkdir()
            (self.root / "ui.js").write_bytes(b"repaired")
            self.now += 0.25
            self.assertTrue(routes._asset_identity_is_available(self.token()))
            self.assertEqual(scan.call_count, 2)

    def test_same_size_same_nanosecond_mtime_edits_change_identity_after_expiry(self):
        resources = {
            "ui.js": (b"alpha", b"bravo"),
            "vendor/nested/code.js": (b"alpha", b"bravo"),
            "vendor/nested/style.css": (b".aa{}", b".bb{}"),
            "manifest.json": (b'{"n":"a"}', b'{"n":"b"}'),
            "favicon.svg": (b"<svg>a</svg>", b"<svg>b</svg>"),
            "favicon-32.png": (b"\x89PNG\r\n\x1a\na", b"\x89PNG\r\n\x1a\nb"),
            "sw.js": (b"worker-a", b"worker-b"),
            "index.html": (b"<p>a</p>", b"<p>b</p>"),
        }
        # Binary bytes matter to the hasher regardless of MIME validity. The
        # neighboring existing regression retains its valid one-pixel PNG fixture.
        for relative, (before, after) in resources.items():
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before)
                self.now += 1
                first = self.token()
                stamp = path.stat()
                path.write_bytes(after)
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(path.stat().st_size, stamp.st_size)
                self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
                self.now += 1
                self.assertNotEqual(self.token(), first)

    def test_metadata_only_touch_is_stable_after_real_refresh(self):
        first = self.token()
        (self.root / "ui.js").touch()
        self.now += 1
        with patch.object(routes, "_static_content_identity", wraps=routes._static_content_identity) as scan:
            self.assertEqual(self.token(), first)
            self.assertEqual(scan.call_count, 1)

    def test_path_rename_is_part_of_byte_identity(self):
        first = self.token()
        (self.root / "ui.js").rename(self.root / "renamed.js")
        self.now += 1
        self.assertNotEqual(self.token(), first)

    def test_add_and_remove_are_part_of_inventory(self):
        first = self.token()
        added = self.root / "extra"
        added.write_bytes(b"additional resource")
        self.now += 1
        second = self.token()
        self.assertNotEqual(second, first)
        added.unlink()
        self.now += 1
        self.assertEqual(self.token(), first)

    def test_permission_error_never_reuses_stale_success(self):
        first = self.token()
        self.assertTrue(routes._asset_identity_is_available(first))
        self.now += 1
        with patch("os.scandir", side_effect=PermissionError("unlistable")):
            second = self.token()
            self.assertFalse(routes._asset_identity_is_available(second))
            self.assertTrue(second.endswith(routes._ASSET_IDENTITY_UNAVAILABLE_SUFFIX))
        self.now += 0.25
        self.assertEqual(self.token(), first)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires non-root POSIX")
    def test_search_only_directory_remains_fail_closed(self):
        vendor = self.root / "vendor"
        vendor.mkdir()
        leaf = vendor / "known.js"
        leaf.write_bytes(b"alpha")
        first = self.token()
        self.assertTrue(routes._asset_identity_is_available(first))
        vendor.chmod(0o111)
        try:
            with self.assertRaises(PermissionError):
                os.listdir(vendor)
            stamp = leaf.stat()
            leaf.write_bytes(b"bravo")
            os.utime(leaf, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(leaf.read_bytes(), b"bravo")
            self.assertEqual(leaf.stat().st_size, stamp.st_size)
            self.assertEqual(leaf.stat().st_mtime_ns, stamp.st_mtime_ns)
            self.now += 1
            self.assertFalse(routes._asset_identity_is_available(self.token()))
        finally:
            vendor.chmod(0o755)

    @unittest.skipUnless(os.name == "posix", "requires symlink support")
    def test_broken_symlink_makes_inventory_unavailable(self):
        self.token()
        (self.root / "broken.js").symlink_to(self.root / "missing.js")
        self.now += 1
        self.assertFalse(routes._asset_identity_is_available(self.token()))

    def test_semantic_version_changes_do_not_force_a_new_tree_scan(self):
        with patch.object(routes, "_static_content_identity", wraps=routes._static_content_identity) as scan:
            first = self.token()
            with patch.object(updates, "WEBUI_VERSION", "v0.52.295+build.meta"):
                second = self.token()
            self.assertNotEqual(first, second)
            self.assertTrue(unquote(second).startswith("v0.52.295+build.meta+a"))
            self.assertEqual(scan.call_count, 1)

    def test_empty_tree_is_a_complete_stable_inventory(self):
        empty = self.root / "empty"
        empty.mkdir()
        first = routes._assets_cache_bust_token(empty)
        self.assertTrue(routes._asset_identity_is_available(first))
        self.now += 1
        self.assertEqual(routes._assets_cache_bust_token(empty), first)


if __name__ == "__main__":
    unittest.main()
