"""Tests for the TLS-aware /health probe shared by all launchers.

Covers the shared shell helper (``scripts/lib/health_probe.sh``), the Python
probe in ``bootstrap.py`` (``wait_for_health``), and two regressions raised in
review of the original TLS-probe PR:

1. ``ctl.sh status`` must not crash when ``.env`` contains shell-readonly
   variable assignments such as ``UID=1000`` (it sources ``.env`` to learn the
   scheme).
2. Probes must fall back to plain HTTP when TLS cert/key are configured but the
   server fell back to serving HTTP (server.py's tested contract), instead of
   polling HTTPS forever.
"""
from __future__ import annotations

import http.server
import importlib.util
import os
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HEALTH_PROBE = REPO_ROOT / "scripts" / "lib" / "health_probe.sh"
CTL = REPO_ROOT / "ctl.sh"
BOOTSTRAP = REPO_ROOT / "bootstrap.py"


# ── helpers ─────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args):  # suppress server log noise during tests
        pass


class _Server:
    """Minimal /health server, optionally TLS-wrapped, on a background thread."""

    def __init__(self, cert: str | None = None, key: str | None = None):
        self.port = _free_port()
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), _HealthHandler)
        if cert and key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    cert = str(tmp_path / "cert.pem")
    key = str(tmp_path / "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
         "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return cert, key


def _run_helper(host: str, port: int, env: dict[str, str]) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        ["bash", str(HEALTH_PROBE), host, str(port), "/health", "3"],
        capture_output=True, text=True, env=merged, timeout=30,
    )


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("hermes_bootstrap_probe", BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── shared shell helper ─────────────────────────────────────────────────────

def test_helper_scheme_http_without_tls():
    out = subprocess.run(
        ["bash", "-c", f". {HEALTH_PROBE}; hermes_webui_probe_scheme"],
        capture_output=True, text=True, env={**os.environ,
                                             "HERMES_WEBUI_TLS_CERT": "",
                                             "HERMES_WEBUI_TLS_KEY": ""},
    )
    assert out.stdout == "http"


def test_helper_scheme_https_with_tls():
    out = subprocess.run(
        ["bash", "-c", f". {HEALTH_PROBE}; hermes_webui_probe_scheme"],
        capture_output=True, text=True,
        env={**os.environ, "HERMES_WEBUI_TLS_CERT": "/c", "HERMES_WEBUI_TLS_KEY": "/k"},
    )
    assert out.stdout == "https"


def test_helper_plain_http_probe():
    with _Server() as srv:
        res = _run_helper("127.0.0.1", srv.port,
                          {"HERMES_WEBUI_TLS_CERT": "", "HERMES_WEBUI_TLS_KEY": ""})
    assert res.returncode == 0
    assert '"status": "ok"' in res.stdout


def test_helper_self_signed_warns_and_succeeds(self_signed_cert):
    cert, key = self_signed_cert
    with _Server(cert, key) as srv:
        res = _run_helper("127.0.0.1", srv.port,
                          {"HERMES_WEBUI_TLS_CERT": cert, "HERMES_WEBUI_TLS_KEY": key})
    assert res.returncode == 0, res.stderr
    assert '"status": "ok"' in res.stdout
    assert "self-signed" in res.stderr.lower()


def test_helper_insecure_optin_is_silent(self_signed_cert):
    cert, key = self_signed_cert
    with _Server(cert, key) as srv:
        res = _run_helper("127.0.0.1", srv.port, {
            "HERMES_WEBUI_TLS_CERT": cert,
            "HERMES_WEBUI_TLS_KEY": key,
            "HERMES_WEBUI_TLS_INSECURE_PROBE": "1",
        })
    assert res.returncode == 0, res.stderr
    assert '"status": "ok"' in res.stdout
    # Explicit opt-in is silent by contract.
    assert res.stderr.strip() == ""


def test_helper_https_falls_back_to_http_when_server_is_plain_http(self_signed_cert):
    """TLS configured in env, but the server serves plain HTTP (server.py's
    fallback-on-bad-cert contract). The probe must still succeed via HTTP."""
    cert, key = self_signed_cert
    with _Server() as srv:  # plain HTTP server
        res = _run_helper("127.0.0.1", srv.port,
                          {"HERMES_WEBUI_TLS_CERT": cert, "HERMES_WEBUI_TLS_KEY": key})
    assert res.returncode == 0, res.stderr
    assert '"status": "ok"' in res.stdout


def test_helper_insecure_optin_falls_back_to_http(self_signed_cert):
    """Shell helper: insecure opt-in + server serving plain HTTP must still
    succeed via the HTTP fallback (and stay silent per the opt-in contract)."""
    cert, key = self_signed_cert
    with _Server() as srv:  # plain HTTP
        res = _run_helper("127.0.0.1", srv.port, {
            "HERMES_WEBUI_TLS_CERT": cert,
            "HERMES_WEBUI_TLS_KEY": key,
            "HERMES_WEBUI_TLS_INSECURE_PROBE": "1",
        })
    assert res.returncode == 0, res.stderr
    assert '"status": "ok"' in res.stdout
    assert res.stderr.strip() == ""


# ── bootstrap.py wait_for_health ────────────────────────────────────────────

def test_bootstrap_probe_self_signed(monkeypatch, self_signed_cert):
    cert, key = self_signed_cert
    monkeypatch.setenv("HERMES_WEBUI_TLS_CERT", cert)
    monkeypatch.setenv("HERMES_WEBUI_TLS_KEY", key)
    monkeypatch.delenv("HERMES_WEBUI_TLS_INSECURE_PROBE", raising=False)
    bs = _load_bootstrap()
    with _Server(cert, key) as srv:
        assert bs.wait_for_health(f"https://127.0.0.1:{srv.port}/health", timeout=8)


def test_bootstrap_probe_http_fallback(monkeypatch, self_signed_cert):
    """TLS env set but server serves HTTP -> must succeed via HTTP fallback,
    not poll HTTPS forever (regression for server.py's fallback contract)."""
    cert, key = self_signed_cert
    monkeypatch.setenv("HERMES_WEBUI_TLS_CERT", cert)
    monkeypatch.setenv("HERMES_WEBUI_TLS_KEY", key)
    bs = _load_bootstrap()
    with _Server() as srv:  # plain HTTP
        # The returned scheme must reflect the ACTUAL successful scheme (http),
        # not the configured https — so the ready URL / browser-open uses the
        # scheme the server is reachable on, not a dead https:// (Codex gate).
        assert bs.wait_for_health(f"https://127.0.0.1:{srv.port}/health", timeout=8) == "http"


def test_bootstrap_probe_returns_scheme_actually_used(monkeypatch, self_signed_cert):
    """wait_for_health returns the scheme that answered, truthy on success."""
    cert, key = self_signed_cert
    # HTTPS server with self-signed cert -> answered over https.
    monkeypatch.setenv("HERMES_WEBUI_TLS_CERT", cert)
    monkeypatch.setenv("HERMES_WEBUI_TLS_KEY", key)
    monkeypatch.delenv("HERMES_WEBUI_TLS_INSECURE_PROBE", raising=False)
    bs = _load_bootstrap()
    with _Server(cert, key) as srv:
        assert bs.wait_for_health(f"https://127.0.0.1:{srv.port}/health", timeout=8) == "https"
    # Plain-HTTP, no TLS env -> answered over http.
    monkeypatch.delenv("HERMES_WEBUI_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TLS_KEY", raising=False)
    bs = _load_bootstrap()
    with _Server() as srv:
        assert bs.wait_for_health(f"http://127.0.0.1:{srv.port}/health", timeout=8) == "http"
    # Timeout -> empty string (falsy, so `if not wait_for_health(...)` still works).
    assert bs.wait_for_health("http://127.0.0.1:1/health", timeout=0.5) == ""


def test_bootstrap_probe_insecure_optin_http_fallback(monkeypatch, self_signed_cert):
    """Insecure opt-in is set AND the server fell back to plain HTTP.

    The probe must still succeed via the HTTP fallback rather than spinning on
    unverified HTTPS until timeout. Locks the reachability of the HTTP fallback
    from inside the insecure-opt-in branch (guards against a future refactor
    that moves the fallback under the non-opt-in ``else``).
    """
    cert, key = self_signed_cert
    monkeypatch.setenv("HERMES_WEBUI_TLS_CERT", cert)
    monkeypatch.setenv("HERMES_WEBUI_TLS_KEY", key)
    monkeypatch.setenv("HERMES_WEBUI_TLS_INSECURE_PROBE", "1")
    bs = _load_bootstrap()
    with _Server() as srv:  # plain HTTP server, no TLS
        assert bs.wait_for_health(f"https://127.0.0.1:{srv.port}/health", timeout=8)


def test_bootstrap_probe_plain_http(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TLS_KEY", raising=False)
    bs = _load_bootstrap()
    with _Server() as srv:
        assert bs.wait_for_health(f"http://127.0.0.1:{srv.port}/health", timeout=8)


def test_bootstrap_probe_rejects_bad_scheme(monkeypatch):
    bs = _load_bootstrap()
    with pytest.raises(ValueError):
        bs.wait_for_health("file:///etc/passwd", timeout=1)


# ── ctl.sh status regression: readonly .env vars ────────────────────────────

def test_ctl_status_survives_readonly_env_vars(tmp_path: Path):
    """`.env` with UID=/GID= must not crash `ctl.sh status` (it sources .env)."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "ctl.sh").write_bytes(CTL.read_bytes())
    (repo / "scripts" / "lib" / "health_probe.sh").write_bytes(HEALTH_PROBE.read_bytes())
    (repo / ".env").write_text("UID=1000\nGID=1000\nHERMES_WEBUI_PORT=8787\n",
                               encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    res = subprocess.run(
        ["bash", str(repo / "ctl.sh"), "status"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "hermes-webui" in res.stdout
    assert "readonly variable" not in res.stderr
