"""Full-router regressions for the shared shell/worker freshness boundary.

These tests require the complete repository and its test runner. A token-helper
replay is not a substitute for executing these route and exact-byte assertions.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
from queue import Queue
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import api.asset_identity_cache as identity_cache
import api.config as api_config
import api.routes as routes


class Handler:
    def __init__(self, headers=None):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.headers = headers or {}
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)


class StaticIdentityPublicRoutesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.index = self.root / "index.html"
        self.index.write_text('<html>old <script src="static/ui.js?v=__WEBUI_VERSION__"></script></html>', encoding="utf-8")
        (self.root / "sw.js").write_text("const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';", encoding="utf-8")
        self.bundle = self.root / "ui.js"
        self.bundle.write_bytes(b"alpha")
        self.now = 100.0
        self.cache = identity_cache.AssetIdentityCache(clock=lambda: self.now, wait_seconds=5)
        for target, name, value in (
            (identity_cache, "ASSET_IDENTITY_CACHE", self.cache),
            (api_config, "get_static_root", lambda: self.root),
            (api_config, "get_index_html_path", lambda: self.index),
            (routes, "_INDEX_SHELL_CACHE", {}),
            (routes, "_STATIC_CACHE", {}),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def request(self, path, headers=None):
        handler = Handler(headers)
        # server.py treats only an exact False as 404; the response writers
        # return None, so truthiness is not the handled contract here.
        self.assertIsNot(routes.handle_get(handler, urlparse("http://test" + path)), False)
        return handler

    def shell_token(self):
        handler = self.request("/")
        self.assertEqual(handler.status, 200)
        match = re.search(r'static/ui.js\?v=([^"\s]+)', bytes(handler.body).decode())
        self.assertIsNotNone(match)
        return match.group(1)

    def test_shell_worker_and_query_variants_reuse_one_scan(self):
        with patch.object(routes, "_static_content_identity", wraps=routes._static_content_identity) as scan:
            token = self.shell_token()
            for path in ("/", "/index.html?v=arbitrary", "/sw.js?v=old", "/sw.js?v=other"):
                handler = self.request(path)
                self.assertEqual(handler.status, 200)
                self.assertIn(token.encode(), bytes(handler.body))
            self.assertEqual(scan.call_count, 1)

    def test_shell_and_worker_turn_over_together_after_expiry(self):
        first = self.shell_token()
        stamp = self.bundle.stat()
        self.bundle.write_bytes(b"bravo")
        os.utime(self.bundle, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.bundle.stat().st_size, stamp.st_size)
        self.assertEqual(self.bundle.stat().st_mtime_ns, stamp.st_mtime_ns)
        self.now += 1
        second = self.shell_token()
        self.assertNotEqual(second, first)
        worker = self.request("/sw.js")
        self.assertEqual(worker.status, 200)
        self.assertIn(second.encode(), bytes(worker.body))
        self.assertIn(("Cache-Control", "no-store"), worker.sent_headers)

    def test_negative_reuse_never_authorizes_shell_cache_or_worker(self):
        with patch.object(routes, "_static_content_identity", side_effect=PermissionError("unreadable")) as scan:
            first = self.request("/")
            self.assertIn(b"old ", bytes(first.body))
            self.assertNotIn("base", routes._INDEX_SHELL_CACHE)
            stamp = self.index.stat()
            self.index.write_text(self.index.read_text().replace("old ", "new "), encoding="utf-8")
            os.utime(self.index, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(self.index.stat().st_size, stamp.st_size)
            self.assertEqual(self.index.stat().st_mtime_ns, stamp.st_mtime_ns)
            second = self.request("/")
            self.assertIn(b"new ", bytes(second.body))
            self.assertNotIn("base", routes._INDEX_SHELL_CACHE)
            self.assertIn(routes._ASSET_IDENTITY_UNAVAILABLE_TOKEN.encode(), bytes(second.body))
            worker = self.request("/sw.js")
            self.assertEqual(worker.status, 503)
            self.assertIn(("Cache-Control", "no-store"), worker.sent_headers)
            self.assertEqual(scan.call_count, 1)

    def test_mixed_public_request_burst_uses_one_flight(self):
        arrivals = Queue()
        class ObservedCondition(threading.Condition):
            def wait(self, timeout=None):
                arrivals.put(threading.get_ident())
                return super().wait(timeout)
        self.cache._condition = ObservedCondition(threading.Lock())
        release = threading.Event()
        original = routes._static_content_identity
        calls = []
        def scan(root):
            calls.append(root)
            arrivals.put(threading.get_ident())
            if not release.wait(5):
                raise AssertionError("scan release timed out")
            return original(root)
        with patch.object(routes, "_static_content_identity", side_effect=scan):
            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(self.request, "/" if i % 2 else "/sw.js") for i in range(16)]
                try:
                    seen = set()
                    while len(seen) < 16:
                        seen.add(arrivals.get(timeout=5))
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                handlers = [future.result(5) for future in futures]
        token = routes._assets_cache_bust_token(self.root).encode()
        self.assertTrue(all(handler.status == 200 and token in bytes(handler.body) for handler in handlers))
        self.assertEqual(len(calls), 1)

    def test_timed_out_worker_fails_closed_without_replacement_scan(self):
        cache = identity_cache.AssetIdentityCache(wait_seconds=0.02)
        started, release = threading.Event(), threading.Event()
        original = routes._static_content_identity
        calls = []
        def scan(root):
            calls.append(root)
            started.set()
            if not release.wait(5):
                raise AssertionError("scan release timed out")
            return original(root)
        with patch.object(identity_cache, "ASSET_IDENTITY_CACHE", cache), \
             patch.object(routes, "_static_content_identity", side_effect=scan):
            with ThreadPoolExecutor(max_workers=1) as pool:
                leader = pool.submit(self.request, "/")
                try:
                    self.assertTrue(started.wait(5))
                    worker = self.request("/sw.js")
                    self.assertEqual(worker.status, 503)
                    self.assertIn(("Cache-Control", "no-store"), worker.sent_headers)
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                self.assertEqual(leader.result(5).status, 200)

    def test_freshness_never_weakens_exact_byte_etags_or_cache_headers(self):
        token = self.shell_token()
        first = self.request("/static/ui.js?v=" + token)
        self.assertEqual(bytes(first.body), b"alpha")
        old_etag = dict(first.sent_headers)["ETag"]
        stamp = self.bundle.stat()
        self.bundle.write_bytes(b"bravo")
        os.utime(self.bundle, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.bundle.stat().st_size, stamp.st_size)
        self.assertEqual(self.bundle.stat().st_mtime_ns, stamp.st_mtime_ns)
        # Deliberately stay INSIDE the identity freshness window. Even an old
        # URL must return current bytes and may never claim immutable caching.
        for version in (token, "arbitrary"):
            response = self.request("/static/ui.js?v=" + version, {"If-None-Match": old_etag})
            self.assertEqual(response.status, 200)
            self.assertEqual(bytes(response.body), b"bravo")
            headers = dict(response.sent_headers)
            self.assertEqual(headers["ETag"], 'W/"' + hashlib.sha256(b"bravo").hexdigest() + '"')
            self.assertEqual(headers["Cache-Control"], "public, max-age=0, must-revalidate")


if __name__ == "__main__":
    unittest.main()
