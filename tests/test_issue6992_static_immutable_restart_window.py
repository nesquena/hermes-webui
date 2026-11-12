"""Regression tests for #6992 — an in-place update must not serve
mixed-revision static assets under one immutable ``?v=`` URL.

Mechanism (verified on master 71ae507d3):

* ``_apply_update_inner`` swaps the checkout (fetch / stash / pull / pop) and
  then calls ``_schedule_restart()``, which sleeps ``delay`` seconds and
  ``os.execv()``s into the new image.
* For that whole window the OLD process keeps answering ``/static/*`` from a
  working tree that is already the next revision, while the page template and
  ``static/sw.js`` still stamp ``?v=`` with the OLD process's startup
  ``WEBUI_VERSION`` — so the URL does not change but the bytes do.
* ``_serve_static`` stamps every non-empty ``?v=`` as
  ``public, max-age=31536000, immutable``, so a browser can durably pin
  revision-B bytes under the revision-A URL: a mixed front-end bundle.

This is not what #5428 covers: that skew check compares the bundle version
stamped into the running page against the server-reported version, and here
every request in the window carries the same (old-process) token, so the two
sides always agree and the checker passes.

Fix under test: while this process's checkout may be changing under it — the
``update_hold`` phase of an apply, or the ``restart_pending`` phase of the
scheduled re-exec — a fingerprinted URL is NOT advertised as immutable and
degrades to the short-cache policy, so a possibly-mixed response can never be
pinned for a year.

The normal path is pinned too: with admission open (the steady state) a
fingerprinted URL keeps the exact same ``immutable`` header as before.
"""

import os
from urllib.parse import urlparse

import pytest

import api.config as api_config


class _FakeHandler:
    """Minimal request handler stand-in (same shape as the other static tests)."""

    def __init__(self, request_headers=None):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = dict(request_headers or {})

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


def _make_static_file(static_root, name, content):
    path = static_root / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _serve(routes, path, query="", request_headers=None):
    """Invoke _serve_static via the real urllib parse path."""
    parsed = urlparse(f"http://x{path}{('?' + query) if query else ''}")
    h = _FakeHandler(request_headers)
    routes._serve_static(h, parsed)
    return h


def _set_mtime_ns(path, mtime_ns):
    os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.fixture
def isolated_static(tmp_path, monkeypatch):
    """Stand up an isolated static/ tree and rebind _serve_static to use it."""
    from api import routes

    static_root = tmp_path / "static"
    static_root.mkdir()

    monkeypatch.setattr(routes, "_STATIC_CACHE", {}, raising=True)
    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    yield static_root


@pytest.fixture
def admission_open_afterwards():
    """Guarantee the run_drain fence state my tests touch is put back."""
    from api import run_drain

    yield run_drain
    assert run_drain.admission_status()["open"], (
        "test left the run-admission fence closed; later tests would see a "
        "permanently degraded cache policy"
    )


# ── Normal path must not degrade ────────────────────────────────────────────


def test_fingerprinted_url_stays_immutable_when_tree_is_stable(isolated_static):
    """Steady state: a non-empty ?v= is still an immutable fingerprint."""
    from api import routes

    _make_static_file(isolated_static, "ui.js", b"x" * 2000)

    h = _serve(routes, "/static/ui.js", query="v=abc1234")
    assert h.status == 200
    assert h.header("Cache-Control") == "public, max-age=31536000, immutable"
    assert bytes(h.body) == b"x" * 2000


def test_repeat_fingerprinted_request_stays_immutable(isolated_static):
    """The hot path must not degrade across repeated identical requests."""
    from api import routes

    _make_static_file(isolated_static, "ui.js", b"y" * 4096)

    for _ in range(3):
        h = _serve(routes, "/static/ui.js", query="v=stable-token")
        assert h.header("Cache-Control") == "public, max-age=31536000, immutable"


# ── Restart-pending window (the #6992 symptom) ──────────────────────────────


def test_restart_pending_window_refuses_immutable_for_changed_bytes(
    isolated_static, admission_open_afterwards
):
    """The same ?v= URL must not be pinned immutable once its bytes moved.

    Simulates the window between ``_schedule_restart()``'s ``time.sleep(delay)``
    and its ``os.execv()``: the checkout has already been swapped, the process
    has marked the restart pending, and it is still the one answering
    ``/static/*``.
    """
    from api import routes

    run_drain = admission_open_afterwards
    target = _make_static_file(isolated_static, "ui.js", b"revision-A" * 300)

    before = _serve(routes, "/static/ui.js", query="v=abc1234")
    assert before.header("Cache-Control") == "public, max-age=31536000, immutable"
    etag_before = before.header("ETag")

    # The in-place update rewrites the working tree under the running process.
    target.write_bytes(b"revision-B-longer" * 300)
    _set_mtime_ns(target, 1_900_000_123_000_000_000)

    epoch = run_drain.mark_restart_pending()
    try:
        during = _serve(routes, "/static/ui.js", query="v=abc1234")
    finally:
        run_drain.abandon_restart_pending(epoch)

    # Same URL, genuinely different revision ...
    assert during.header("ETag") != etag_before, (
        "fixture did not actually change the file revision"
    )
    assert bytes(during.body) != bytes(before.body)
    # ... so it must not be advertised as immutable.
    assert during.header("Cache-Control") != "public, max-age=31536000, immutable", (
        "restart-pending window served immutable for a ?v= URL whose bytes "
        "came from a different revision (#6992)"
    )


def test_update_hold_window_refuses_immutable_for_changed_bytes(
    isolated_static, admission_open_afterwards
):
    """The apply itself (fetch/stash/pull) is also a tree-changing window."""
    from api import routes

    run_drain = admission_open_afterwards
    target = _make_static_file(isolated_static, "ui.js", b"revision-A" * 300)

    before = _serve(routes, "/static/ui.js", query="v=abc1234")
    assert before.header("Cache-Control") == "public, max-age=31536000, immutable"

    # A git pull rewrites files in place while the hold is open.
    target.write_bytes(b"revision-B" * 500)
    _set_mtime_ns(target, 1_900_000_456_000_000_000)

    token = run_drain.hold_run_admission("update:webui")
    try:
        during = _serve(routes, "/static/ui.js", query="v=abc1234")
    finally:
        run_drain.release_run_admission_hold(token)

    assert during.header("Cache-Control") != "public, max-age=31536000, immutable", (
        "update-hold window served immutable for a ?v= URL whose bytes came "
        "from a different revision (#6992)"
    )


def test_restart_pending_window_does_not_pin_a_stale_304(
    isolated_static, admission_open_afterwards
):
    """A conditional GET during the window must not re-pin the mixed bytes.

    The 304 branch reuses the same Cache-Control value, so an immutable 304
    would refresh a year-long pin on a response whose bytes just moved.
    """
    from api import routes

    run_drain = admission_open_afterwards
    target = _make_static_file(isolated_static, "ui.js", b"revision-A" * 300)

    first = _serve(routes, "/static/ui.js", query="v=abc1234")
    etag = first.header("ETag")

    target.write_bytes(b"revision-B" * 300)
    _set_mtime_ns(target, 1_900_000_789_000_000_000)

    epoch = run_drain.mark_restart_pending()
    try:
        # The client still holds revision A and revalidates it mid-window.
        revalidated = _serve(
            routes,
            "/static/ui.js",
            query="v=abc1234",
            request_headers={"If-None-Match": etag},
        )
    finally:
        run_drain.abandon_restart_pending(epoch)

    assert revalidated.status == 200  # bytes moved, so no 304
    assert revalidated.header("Cache-Control") != "public, max-age=31536000, immutable", (
        "restart-pending window pinned a conditional GET as immutable (#6992)"
    )


def test_window_degradation_is_lifted_once_admission_reopens(
    isolated_static, admission_open_afterwards
):
    """The degradation is a window, not a new steady state."""
    from api import routes

    run_drain = admission_open_afterwards
    _make_static_file(isolated_static, "ui.js", b"z" * 2048)

    epoch = run_drain.mark_restart_pending()
    run_drain.abandon_restart_pending(epoch)

    after = _serve(routes, "/static/ui.js", query="v=abc1234")
    assert after.header("Cache-Control") == "public, max-age=31536000, immutable"
