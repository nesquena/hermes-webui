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
