"""
Tests for optional TLS/HTTPS support (HERMES_WEBUI_TLS_CERT / TLS_KEY).

Tests use a self-signed certificate generated at test time via openssl.
"""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import requires_fcntl

ROOT = Path(__file__).parent.parent


def _run_config_probe(code: str, env=None, *, attempts: int = 3, timeout: int = 60):
    """Run a short `python -c` probe that imports api.config, with retries.

    Importing api.config in a fresh subprocess is heavyweight, and under a fully
    parallel test suite the box can be saturated enough that a 10s timeout trips
    and the runner SIGKILLs the child (returncode -9) — a pure resource-contention
    flake, not a logic failure. Use a generous timeout and retry the spawn on
    TimeoutExpired / SIGKILL so the full-suite run is deterministic (#4740 sweep:
    zero tolerance for load-dependent flakes). A genuine non-zero exit with output
    is returned immediately (no retry) so real failures still surface fast.
    """
    last_exc = None
    for attempt in range(attempts):
        try:
            r = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(ROOT), env=env,
            )
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            continue  # contention — respawn
        # returncode -9 == SIGKILL (OOM/oversubscription under load): retry.
        if r.returncode == -9 and attempt < attempts - 1:
            continue
        return r
    raise AssertionError(
        f"config probe subprocess did not complete after {attempts} attempts "
        f"(timeout={timeout}s each); last error: {last_exc!r}"
    )


def _gen_test_cert(tmpdir: Path) -> tuple[str, str]:
    """Generate a self-signed cert and key pair for testing."""
    cert = str(tmpdir / "test_cert.pem")
    key = str(tmpdir / "test_key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", key, "-out", cert, "-days", "1", "-nodes",
         "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return cert, key


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, use_ssl: bool = False,
                     timeout: float = 30.0, proc: "subprocess.Popen | None" = None) -> bool:
    """Poll until the server accepts a connection or times out.

    Bumped to 30s because the server subprocess imports the whole app, which can
    exceed a tight budget under the parallel (9-shard) suite + concurrent agents.
    If ``proc`` is supplied, bail out early the moment the subprocess has exited
    (e.g. failed to bind the port) instead of polling a dead process to the
    deadline — that turns a slow flake into a fast, deterministic retry signal.
    """
    ctx = None
    if use_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # subprocess died (e.g. port already taken) — don't wait it out
        try:
            if use_ssl:
                c = http.client.HTTPSConnection(host, port, timeout=2, context=ctx)
            else:
                c = http.client.HTTPConnection(host, port, timeout=2)
            c.request("GET", "/health")
            resp = c.getresponse()
            resp.read()
            c.close()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _start_server(port: int, cert: str = None, key: str = None) -> subprocess.Popen:
    """Start server.py as a subprocess with the given TLS env vars."""
    env = {k: v for k, v in os.environ.items()}
    env["HERMES_WEBUI_HOST"] = "127.0.0.1"
    env["HERMES_WEBUI_PORT"] = str(port)
    env.pop("HERMES_WEBUI_TLS_CERT", None)
    env.pop("HERMES_WEBUI_TLS_KEY", None)
    if cert:
        env["HERMES_WEBUI_TLS_CERT"] = cert
    if key:
        env["HERMES_WEBUI_TLS_KEY"] = key
    env["HERMES_WEBUI_STATE_DIR"] = str(Path(tempfile.mkdtemp()))
    proc = subprocess.Popen(
        [os.sys.executable, str(ROOT / "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    return proc


def _terminate(proc: "subprocess.Popen | None") -> None:
    if proc is None or proc.poll() is not None:
        return
    with suppress(Exception):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _start_and_wait(use_ssl: bool, cert: str = None, key: str = None,
                    attempts: int = 4) -> subprocess.Popen:
    """Start the server and wait until it is reachable, retrying on a fresh port.

    Defeats the _find_free_port() TOCTOU race (the OS can hand the just-released
    port to another process before server.py binds it, especially under the
    parallel suite). On a failed bring-up we tear the subprocess down and retry
    with a NEWLY chosen port. Returns the live Popen, or raises AssertionError
    with the captured server output after exhausting attempts.
    """
    last_output = ""
    for _ in range(attempts):
        port = _find_free_port()
        proc = _start_server(port, cert=cert, key=key)
        if _wait_for_server("127.0.0.1", port, use_ssl=use_ssl, proc=proc):
            proc._test_port = port  # type: ignore[attr-defined]
            return proc
        # Capture diagnostics before retrying with a fresh port.
        with suppress(Exception):
            os.set_blocking(proc.stdout.fileno(), False)
            last_output = (proc.stdout.read(4000) or "")[:4000]
        _terminate(proc)
    raise AssertionError(
        f"server did not become reachable after {attempts} attempts "
        f"(use_ssl={use_ssl}); last server output:\n{last_output}"
    )


# ── Test class ──────────────────────────────────────────────────────────────

class TestTLSConfigFlag(unittest.TestCase):

    def test_tls_enabled_true_when_both_env_set(self):
        code = textwrap.dedent("""\
            import os
            os.environ['HERMES_WEBUI_TLS_CERT'] = '/tmp/cert.pem'
            os.environ['HERMES_WEBUI_TLS_KEY'] = '/tmp/key.pem'
            from api.config import TLS_ENABLED
            print(TLS_ENABLED)
        """)
        r = _run_config_probe(code)
        self.assertEqual(r.stdout.strip(), "True")

    def test_tls_enabled_false_when_env_absent(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERMES_WEBUI_TLS_CERT", "HERMES_WEBUI_TLS_KEY")}
        code = textwrap.dedent("""\
            import os
            os.environ.pop('HERMES_WEBUI_TLS_CERT', None)
            os.environ.pop('HERMES_WEBUI_TLS_KEY', None)
            from api.config import TLS_ENABLED
            print(TLS_ENABLED)
        """)
        r = _run_config_probe(code, env=env)
        self.assertEqual(r.stdout.strip(), "False")

    def test_tls_enabled_false_when_only_cert_set(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERMES_WEBUI_TLS_CERT", "HERMES_WEBUI_TLS_KEY")}
        env["HERMES_WEBUI_TLS_CERT"] = "/tmp/cert.pem"
        code = textwrap.dedent("""\
            from api.config import TLS_ENABLED
            print(TLS_ENABLED)
        """)
        r = _run_config_probe(code, env=env)
        self.assertEqual(r.stdout.strip(), "False")


class TestTLSEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = Path(tempfile.mkdtemp())
        cls._cert, cls._key = _gen_test_cert(cls._tmpdir)

    @classmethod
    def tearDownClass(cls):
        with suppress(Exception):
            import shutil
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def tearDown(self):
        if hasattr(self, "_proc") and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def test_https_server_responds_to_health(self):
        self._proc = _start_and_wait(use_ssl=True, cert=self._cert, key=self._key)
        port = self._proc._test_port
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=ctx)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data.get("status"), "ok")
        conn.close()

    def test_stalled_tls_handshake_does_not_block_other_clients(self):
        """A raw TCP client that never speaks TLS must not wedge HTTPS accept()."""
        port = _find_free_port()
        self._proc = _start_server(port, cert=self._cert, key=self._key)
        self.assertTrue(
            _wait_for_server("127.0.0.1", port, use_ssl=True),
            "TLS server did not start in time",
        )

        raw = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            # Give the kernel a moment to deliver the TCP connection so the
            # server's accept loop dequeues the raw socket before the HTTPS
            # request arrives — this guarantees the test exercises the fix.
            time.sleep(0.05)
            # Do not send a TLS ClientHello. Before the fix, the listening
            # SSLSocket performed the handshake in the single accept loop, so
            # this one idle client blocked every later browser/API request.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, timeout=2, context=ctx,
            )
            conn.request("GET", "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
            self.assertEqual(data.get("status"), "ok")
            conn.close()
        finally:
            raw.close()

    def test_http_without_tls_still_works(self):
        self._proc = _start_and_wait(use_ssl=False)
        port = self._proc._test_port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data.get("status"), "ok")
        conn.close()

    @requires_fcntl
    def test_tls_startup_failure_fallback_to_http(self):
        """Bad cert paths should print a warning and start HTTP anyway."""
        # Server should be reachable over plain HTTP even though TLS setup failed.
        self._proc = _start_and_wait(
            use_ssl=False, cert="/nonexistent/cert.pem", key="/nonexistent/key.pem",
        )
        # Confirm the TLS warning was printed. Drain ALL currently-available
        # stdout, not just the first N bytes: startup can emit an unbounded
        # amount of unrelated preamble first (optional-dep plugin/tool import
        # warnings when the test venv lacks `requests`/`websockets`, the startup
        # config banner, the state-dir hint, etc.), which can push the
        # "TLS setup failed" line well past any fixed-size single read and make
        # this test flaky depending on how noisy the environment is.
        os.set_blocking(self._proc.stdout.fileno(), False)
        output = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                chunk = self._proc.stdout.read(65536)
            except BlockingIOError:
                chunk = None
            if chunk:
                output += chunk
                if "TLS setup failed" in output:
                    break
            else:
                if "TLS setup failed" in output:
                    break
                time.sleep(0.05)
        self.assertIn("TLS setup failed", output)


if __name__ == "__main__":
    unittest.main()
