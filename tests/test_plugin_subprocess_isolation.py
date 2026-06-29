"""RFC #3383: Failure isolation tests for plugin subprocess bridge."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _make_plugin(name: str, code: str) -> Path:
    td = tempfile.mkdtemp(prefix="tpl_")
    root = Path(td) / name
    d = root / "dashboard"; d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"name": name, "tab": {"path": f"/{name}"}, "label": name}))
    (root / "__init__.py").write_text(code)
    return root


def _proc(plugin_dir: Path):
    runner = Path(__file__).parent.parent / "api" / "plugin_runner.py"
    return subprocess.Popen(
        [sys.executable, str(runner)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"HOME": os.environ.get("HOME", "/"), "PATH": os.environ.get("PATH", ""),
             "HERMES_PLUGIN_NAME": plugin_dir.name, "HERMES_PLUGIN_DIR": str(plugin_dir),
             "PYTHONUNBUFFERED": "1"},
        text=True,
    )


def _send(proc, req: dict, timeout=5):
    import select
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush()
    assert select.select([proc.stdout], [], [], timeout)[0]
    return json.loads(proc.stdout.readline().strip())


_HELLO = 'def register():\n def h(h,p):\n  h.send_response(200);h.send_header("content-type","application/json");h.end_headers();h.wfile.write(b\'{"ok":1}\')\n return {"/":{"GET":h}}'
_CRASH = 'def register():\n def c(h,p):raise RuntimeError("crash")\n return {"/c":{"GET":c}}'
_HEADER = 'def register():\n def h(h,p):\n  h.send_response(200);h.send_header("set-cookie","s=h");h.send_header("content-type","text/plain");h.end_headers();h.wfile.write(b"ok")\n return {"/h":{"GET":h}}'


class _H:
    """Minimal handler for testing _dispatch_plugin_subprocess."""
    def __init__(self, headers=None, body=b""):
        self._h = headers or {}; self._b = body; self._r = 0
    @property
    def headers(self): return self._h
    @property
    def rfile(self): return self
    def read(self, n):
        c = self._b[self._r:self._r + n]; self._r += len(c); return c
    def send_response(self, s): self._s = s
    def send_header(self, k, v):
        if not hasattr(self, "_rh"): self._rh = {}
        self._rh[k] = v
    def end_headers(self): pass
    @property
    def wfile(self): return self
    def write(self, d):
        if not hasattr(self, "_rb"): self._rb = b""
        self._rb += d


# ── Protocol: happy path, invalid JSON, handler crash ─────────────────

def test_protocol():
    hdir = _make_plugin("hp", _HELLO)
    cdir = _make_plugin("cp", _CRASH)
    try:
        ph = _proc(hdir); pc = _proc(cdir)
        assert json.loads(ph.stdout.readline().strip()).get("ready")
        assert json.loads(pc.stdout.readline().strip()).get("ready")
        try:
            # happy path
            assert _send(ph, {"id": 1, "method": "GET", "path": "/", "query": "", "headers": {}})["status"] == 200
            # invalid JSON → 400, recover
            ph.stdin.write("bad\n"); ph.stdin.flush()
            import select; assert select.select([ph.stdout], [], [], 5)[0]
            assert json.loads(ph.stdout.readline().strip())["status"] == 400
            assert _send(ph, {"id": 2, "method": "GET", "path": "/", "query": "", "headers": {}})["status"] == 200
            # crash → 500, still alive
            assert _send(pc, {"id": 1, "method": "GET", "path": "/c", "query": "", "headers": {}})["status"] == 500
            assert pc.poll() is None
        finally:
            ph.terminate(); ph.wait(); pc.terminate(); pc.wait()
    finally:
        import shutil; shutil.rmtree(hdir.parent, ignore_errors=True)
        shutil.rmtree(cdir.parent, ignore_errors=True)


# ── Env filtering (RFC: no secrets to subprocess) ─────────────────────

def test_env():
    from api.plugin_manager import _build_plugin_env
    with patch.dict(os.environ, {"HOME": "/u", "PATH": "/b", "OPENAI_API_KEY": "sk-x"}, clear=True):
        e = _build_plugin_env("t", Path("/t"))
        assert e["HOME"] == "/u" and "OPENAI_API_KEY" not in e


# ── Lifecycle + dispatch: spawn/kill, oversized, unavailable, headers ─

def test_dispatch():
    from api.plugin_manager import spawn_plugin, get_plugin_process, kill_plugin, PLUGIN_PROCESSES
    from api.routes import _dispatch_plugin_subprocess
    import api.plugins as m

    hdir = _make_plugin("dd", _HELLO)
    hadir = _make_plugin("hd", _HEADER)
    PLUGIN_PROCESSES.clear()
    try:
        # lifecycle
        p = spawn_plugin("dd", hdir); assert p and p.poll() is None
        assert get_plugin_process("dd") is p
        kill_plugin("dd"); assert get_plugin_process("dd") is None

        # sized body → 413
        p2 = spawn_plugin("dd", hdir); assert p2
        old = dict(m._PLUGIN_STATIC_ROOTS); m._PLUGIN_STATIC_ROOTS["dd"] = hdir / "dashboard"
        try:
            h = _H(headers={"Content-Length": "2000000"})
            _dispatch_plugin_subprocess(h, "dd", "/x", "POST", SimpleNamespace(path="", query=""))
            assert getattr(h, "_s", None) is not None
        finally:
            kill_plugin("dd")

        # unavailable → 503
        with patch("api.plugins.get_plugin_root", return_value=None):
            h2 = _H()
            _dispatch_plugin_subprocess(h2, "nope", "/r", "GET", SimpleNamespace(path="", query=""))
            assert getattr(h2, "_s", None) is not None

        # header filtering
        p3 = spawn_plugin("hd", hadir); assert p3
        m._PLUGIN_STATIC_ROOTS["hd"] = hadir / "dashboard"
        try:
            h3 = _H()
            assert _dispatch_plugin_subprocess(h3, "hd", "/h", "GET", SimpleNamespace(path="", query="")) is True
            rh = {k.lower() for k in getattr(h3, "_rh", {})}
            assert "set-cookie" not in rh and "content-type" in rh
        finally:
            kill_plugin("hd")
        m._PLUGIN_STATIC_ROOTS = old
    finally:
        import shutil
        shutil.rmtree(hdir.parent, ignore_errors=True)
        shutil.rmtree(hadir.parent, ignore_errors=True)


# ── FIX #2: response-id correlation ───────────────────────────────────
#
# _dispatch_plugin_subprocess writes a request carrying a numeric id and must
# read frames until it finds the one whose id matches — skipping any stale
# orphan frame a previous (slow/timed-out) handler left in the pipe. Without
# the correlation loop the dispatcher would return the FIRST line it reads,
# which could be another request's response body.

def test_dispatch_skips_stale_frame_and_correlates_response_id():
    """A stale frame (wrong id) sitting ahead of our real response must be
    skipped; the dispatcher must return the body of the frame whose id matches
    the request it sent. Non-vacuous: if the id-correlation loop is removed and
    the dispatcher returns the first line read, it gets the STALE body and the
    final assertion (b'{"ok":1}') fails."""
    import os
    import threading
    import json as _json
    import base64
    import api.plugin_manager as pm
    from api.routes import _dispatch_plugin_subprocess

    stdin_r, stdin_w = os.pipe()    # dispatcher -> fake plugin
    stdout_r, stdout_w = os.pipe()  # fake plugin -> dispatcher

    class FakeProc:
        def __init__(self):
            self.stdin = os.fdopen(stdin_w, "wb", buffering=0)
            self.stdout = os.fdopen(stdout_r, "rb", buffering=0)

        def poll(self):
            return None  # always "alive"

    proc = FakeProc()
    seen = {}

    def fake_plugin():
        rf = os.fdopen(stdin_r, "rb")
        wf = os.fdopen(stdout_w, "wb", buffering=0)
        line = rf.readline()
        req = _json.loads(line.decode("utf-8"))
        rid = req["id"]
        seen["req_id"] = rid
        # 1) a STALE orphan frame with a DIFFERENT id and a tell-tale body
        stale = _json.dumps({
            "id": (rid + 1) % 1000000, "status": 200, "headers": {},
            "body_b64": base64.b64encode(b'{"stale":1}').decode("ascii"),
        }) + "\n"
        # 2) the CORRECT frame echoing our id
        good = _json.dumps({
            "id": rid, "status": 200,
            "headers": {"content-type": "application/json"},
            "body_b64": base64.b64encode(b'{"ok":1}').decode("ascii"),
        }) + "\n"
        wf.write(stale.encode("utf-8"))
        wf.write(good.encode("utf-8"))
        wf.flush()

    t = threading.Thread(target=fake_plugin, daemon=True)
    t.start()
    try:
        h = _H()
        with patch.object(pm, "get_plugin_process", return_value=proc):
            result = _dispatch_plugin_subprocess(
                h, "corr", "/r", "GET", SimpleNamespace(path="", query=""),
            )
        t.join(timeout=5)
        assert result is True
        assert getattr(h, "_s", None) == 200
        # The decisive check: we got the body of the id-matched frame, NOT the
        # stale frame that arrived first.
        assert getattr(h, "_rb", b"") == b'{"ok":1}'
        assert seen.get("req_id") is not None
    finally:
        for fd in (stdin_r, stdin_w, stdout_r, stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass


def test_dispatch_502_when_no_frame_matches_request_id():
    """If the plugin only ever emits frames with the wrong id, the dispatcher
    must give up with a 502 (correlation failed) rather than hang or return a
    mismatched body. Non-vacuous: a dispatcher that returned the first frame
    unconditionally would yield status 200 here, failing the 502 assertion."""
    import os
    import threading
    import json as _json
    import base64
    import api.plugin_manager as pm
    from api.routes import _dispatch_plugin_subprocess

    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()

    class FakeProc:
        def __init__(self):
            self.stdin = os.fdopen(stdin_w, "wb", buffering=0)
            self.stdout = os.fdopen(stdout_r, "rb", buffering=0)

        def poll(self):
            return None

    proc = FakeProc()

    def fake_plugin():
        rf = os.fdopen(stdin_r, "rb")
        wf = os.fdopen(stdout_w, "wb", buffering=0)
        line = rf.readline()
        req = _json.loads(line.decode("utf-8"))
        rid = req["id"]
        # Emit a flood of WRONG-id frames (more than MAX_FRAME_SKIPS+1).
        for k in range(1, 20):
            frame = _json.dumps({
                "id": (rid + k) % 1000000, "status": 200, "headers": {},
                "body_b64": base64.b64encode(b'{"wrong":1}').decode("ascii"),
            }) + "\n"
            try:
                wf.write(frame.encode("utf-8"))
                wf.flush()
            except (BrokenPipeError, OSError):
                break

    t = threading.Thread(target=fake_plugin, daemon=True)
    t.start()
    try:
        h = _H()
        with patch.object(pm, "get_plugin_process", return_value=proc):
            _dispatch_plugin_subprocess(
                h, "nomatch", "/r", "GET", SimpleNamespace(path="", query=""),
            )
        t.join(timeout=5)
        # j(handler, ..., status=502) returns True; the recorded status is 502.
        assert getattr(h, "_s", None) == 502
    finally:
        for fd in (stdin_r, stdin_w, stdout_r, stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass


# ── FIX #3: CSRF narrowed to Origin: null (security) ──────────────────
#
# The plugin CSRF exemption must accept ONLY the sandboxed-iframe case
# (Origin: null + X-Plugin-Request marker), NOT a blanket /api/plugins/
# prefix exemption. A cross-origin attacker page carries a concrete Origin
# and must still be rejected by _check_csrf.

def test_is_plugin_request_only_exempts_null_origin():
    """Unit: _is_plugin_request is True ONLY for Origin: null + marker on a
    plugin path. A concrete cross-origin Origin must NOT be exempted."""
    from api.routes import _is_plugin_request

    class _Hdr(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    class _Req:
        def __init__(self, headers):
            self.headers = _Hdr(headers)

    path = "/api/plugins/foo/bar"
    # Sandboxed iframe: Origin: null + marker → exempt
    assert _is_plugin_request(_Req({"Origin": "null", "X-Plugin-Request": "1"}), path) is True
    # Cross-origin attacker page: concrete Origin → NOT exempt
    assert _is_plugin_request(_Req({"Origin": "https://evil.example", "X-Plugin-Request": "1"}), path) is False
    # Marker without null Origin → NOT exempt
    assert _is_plugin_request(_Req({"X-Plugin-Request": "1"}), path) is False
    # null Origin but no marker → NOT exempt
    assert _is_plugin_request(_Req({"Origin": "null"}), path) is False
    # Right headers but non-plugin path → NOT exempt
    assert _is_plugin_request(_Req({"Origin": "null", "X-Plugin-Request": "1"}), "/api/settings") is False


def test_csrf_gate_rejects_cross_origin_plugin_post_allows_null_origin():
    """Integration through handle_post: a plugin POST carrying a REAL
    cross-origin Origin is rejected (403) by the CSRF gate, while an
    Origin: null plugin POST is allowed through to dispatch.

    Non-vacuous: if the exemption regressed to a blanket /api/plugins/ prefix,
    _is_plugin_request would return True for the cross-origin request too, the
    CSRF gate would be skipped, dispatch would be reached, and the status-403
    assertion would fail."""
    import api.routes as routes

    class _Hdr(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    class _Handler:
        def __init__(self, headers):
            self.headers = _Hdr(headers)
            self.command = "POST"

    parsed = SimpleNamespace(path="/api/plugins/foo/bar", query="")

    # ── Cross-origin plugin POST → must be REJECTED (403) ──
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        return True

    h_cross = _Handler({
        "X-Plugin-Request": "1", "Origin": "https://evil.example",
        "Host": "localhost:8000",
    })
    with patch("api.routes._check_csrf", return_value=False), \
         patch("api.routes.j", side_effect=fake_j), \
         patch("api.routes._dispatch_plugin_subprocess", return_value="DISPATCHED") as disp_cross:
        routes.handle_post(h_cross, parsed)
    assert captured.get("status") == 403, "cross-origin plugin POST must be CSRF-rejected"
    disp_cross.assert_not_called()

    # ── Origin: null plugin POST → allowed past CSRF, reaches dispatch ──
    h_null = _Handler({
        "X-Plugin-Request": "1", "Origin": "null", "Host": "localhost:8000",
    })
    with patch("api.routes._check_csrf", return_value=False), \
         patch("api.routes._dashboard_plugin_enabled", return_value=True), \
         patch("api.routes._dispatch_plugin_subprocess", return_value="DISPATCHED") as disp_null:
        result = routes.handle_post(h_null, parsed)
    disp_null.assert_called_once()
    assert result == "DISPATCHED"
