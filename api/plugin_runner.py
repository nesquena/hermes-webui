"""
Plugin subprocess runner — stdin/stdout JSON-line protocol.

Runs inside the plugin subprocess. Reads JSON requests from stdin,
dispatches to registered handlers via CompatHandler, writes JSON
responses to stdout.

Set by plugin_manager.py at spawn time:
  HERMES_PLUGIN_NAME=<plugin_name>
  HERMES_PLUGIN_DIR=/path/to/plugin/root

Protocol:
  Request (stdin, one JSON line):
    {"id": int, "method": "GET|POST", "path": "/sub_route",
     "query": "...", "headers": {...}, "body_b64": "..."}

  Response (stdout, one JSON line):
    {"id": int, "status": 200, "headers": {...}, "body_b64": "..."}

Lifecycle:
  - On stdin EOF: exit 0
  - On handler exception: write error response, continue
  - On JSON decode error: write error, continue
"""

import base64
import json
import os
import sys
import traceback
from types import SimpleNamespace


class CompatHandler:
    """Accumulates handler.write() calls and serializes to a JSON response."""

    def __init__(self, request_headers: dict, body_bytes: bytes):
        self._status = 200
        self._resp_headers: dict[str, str] = {}
        self._body_parts: list[bytes] = []
        self._body_bytes = body_bytes
        self._hdrs = {k.lower(): v for k, v in (request_headers or {}).items()}
        self._read_pos = 0

    @property
    def headers(self):
        return self

    def get(self, key, default=None):
        return self._hdrs.get(key.lower() if isinstance(key, str) else key, default)

    def send_response(self, status: int):
        self._status = status

    def send_header(self, key: str, value: str):
        self._resp_headers[key] = value

    def end_headers(self):
        pass

    @property
    def wfile(self):
        return self

    def write(self, data: bytes | str):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._body_parts.append(data)

    @property
    def rfile(self):
        return self

    def read(self, length: int) -> bytes:
        remaining = len(self._body_bytes) - self._read_pos
        if remaining <= 0:
            return b""
        chunk = self._body_bytes[self._read_pos:self._read_pos + length]
        self._read_pos += len(chunk)
        return chunk

    def to_response(self, request_id: int) -> dict:
        body = b"".join(self._body_parts)
        resp = {
            "id": request_id,
            "status": self._status,
            "headers": self._resp_headers,
        }
        if body:
            resp["body_b64"] = base64.b64encode(body).decode("ascii")
        return resp

    def j(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def bad(self, msg: str, status: int = 400):
        self.j({"error": msg}, status=status)


def _load_plugin(plugin_name: str, plugin_dir: str):
    import importlib.util

    init_path = os.path.join(plugin_dir, "__init__.py")
    if not os.path.isfile(init_path):
        sys.stderr.write(f"[plugin:{plugin_name}] __init__.py not found\n")
        sys.stderr.flush()
        return None

    spec = importlib.util.spec_from_file_location(plugin_name, init_path)
    if spec is None:
        sys.stderr.write(f"[plugin:{plugin_name}] failed to create module spec\n")
        sys.stderr.flush()
        return None
    mod = importlib.util.module_from_spec(spec)

    # Make adjacent modules (e.g. tools.py) importable
    parent = os.path.dirname(plugin_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    spec.loader.exec_module(mod)
    if not hasattr(mod, "register"):
        sys.stderr.write(f"[plugin:{plugin_name}] no register() found\n")
        sys.stderr.flush()
        return None
    return mod


def main():
    plugin_name = os.environ.get("HERMES_PLUGIN_NAME", "unknown")
    plugin_dir = os.environ.get("HERMES_PLUGIN_DIR", "")

    # Apply resource limits to this process (POSIX; silently skipped elsewhere).
    # NOTE: RLIMIT_AS caps *virtual address space*, not resident RAM. CPython
    # plus shared libs already map 200-400MB before any plugin code runs, and a
    # plugin importing numpy / Pillow / a cffi-backed lib can map well past that
    # at startup. A tight value (e.g. 512MB) would make the interpreter abort
    # before emitting the ready signal, so the plugin appears permanently
    # unavailable. We set a generous ceiling that only trips on runaway
    # allocation; true physical-RAM limiting belongs at the cgroup layer.
    for name, value in {
        "RLIMIT_AS": 3 * 1024 * 1024 * 1024, "RLIMIT_NPROC": 64,
        "RLIMIT_NOFILE": 256, "RLIMIT_CPU": 300,
    }.items():
        try:
            import resource
            soft, hard = resource.getrlimit(getattr(resource, name))
            resource.setrlimit(getattr(resource, name), (min(value, hard), hard))
        except (OSError, ValueError, AttributeError, ImportError):
            pass
    try:
        os.setpgrp()
    except OSError:
        pass

    out = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def send(data):
        out.write(json.dumps(data) + "\n")
        out.flush()

    def err(status, message, request_id=0):
        send({
            "id": request_id,
            "status": status,
            "headers": {"content-type": "application/json; charset=utf-8"},
            "body_b64": base64.b64encode(json.dumps({"error": message}).encode()).decode("ascii"),
        })

    if not plugin_dir:
        err(500, "HERMES_PLUGIN_DIR not set")
        return

    mod = _load_plugin(plugin_name, plugin_dir)
    if mod is None:
        sys.stderr.write(f"[plugin:{plugin_name}] failed to load\n")
        sys.stderr.flush()
        return

    routes = mod.register()
    if not isinstance(routes, dict):
        err(500, "register() did not return a route map")
        return

    send({"ready": True, "routes": list(routes.keys())})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            err(400, "invalid JSON request")
            continue

        req_id = req.get("id", 0)
        method = req.get("method", "GET")
        path = req.get("path", "/")
        query = req.get("query", "")
        req_headers = req.get("headers", {})
        body_b64 = req.get("body_b64") or ""
        body_bytes = base64.b64decode(body_b64) if body_b64 else b""

        parsed = SimpleNamespace(path=path, query=query)

        method_handlers = routes.get(path, {})
        handler_fn = method_handlers.get(method)
        if handler_fn is None:
            err(404, "not found", request_id=req_id)
            continue

        try:
            ch = CompatHandler(req_headers, body_bytes)
            handler_fn(ch, parsed)
            resp = ch.to_response(req_id)
        except Exception:
            err(500, "plugin internal error", request_id=req_id)
            sys.stderr.write(f"[plugin:{plugin_name}] handler error:\n")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            continue

        send(resp)


if __name__ == "__main__":
    main()
