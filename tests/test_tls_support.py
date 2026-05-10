"""
Tests for optional TLS/HTTPS support (HERMES_WEBUI_TLS_CERT / TLS_KEY).

Tests use a self-signed certificate generated at test time via openssl.
"""

import http.client
import json
import os
import ssl
import subprocess
import textwrap
import time
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _gen_test_cert(tmpdir: Path) -> tuple[str, str]:
    """Generate a self-signed cert and key pair for testing."""
    cert = str(tmpdir / "test_cert.pem")
    key = str(tmpdir / "test_key.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            key,
            "-out",
            cert,
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(
    host: str, port: int, use_ssl: bool = False, timeout: float = 8.0
) -> bool:
    """Poll until the server accepts a connection or times out."""
    ctx = None
    if use_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + timeout
    while time.time() < deadline:
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
            time.sleep(0.5)
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
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


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
        r = subprocess.run(
            [os.sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(ROOT),
        )
        self.assertEqual(r.stdout.strip(), "True")

    def test_tls_enabled_false_when_env_absent(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("HERMES_WEBUI_TLS_CERT", "HERMES_WEBUI_TLS_KEY")
        }
        code = textwrap.dedent("""\
            import os
            os.environ.pop('HERMES_WEBUI_TLS_CERT', None)
            os.environ.pop('HERMES_WEBUI_TLS_KEY', None)
            from api.config import TLS_ENABLED
            print(TLS_ENABLED)
        """)
        r = subprocess.run(
            [os.sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(ROOT),
            env=env,
        )
        self.assertEqual(r.stdout.strip(), "False")

    def test_tls_enabled_false_when_only_cert_set(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("HERMES_WEBUI_TLS_CERT", "HERMES_WEBUI_TLS_KEY")
        }
        env["HERMES_WEBUI_TLS_CERT"] = "/tmp/cert.pem"
        code = textwrap.dedent("""\
            from api.config import TLS_ENABLED
            print(TLS_ENABLED)
        """)
        r = subprocess.run(
            [os.sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(ROOT),
            env=env,
        )
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
        port = _find_free_port()
        self._proc = _start_server(port, cert=self._cert, key=self._key)
        self.assertTrue(
            _wait_for_server("127.0.0.1", port, use_ssl=True),
            "TLS server did not start in time",
        )
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

    def test_http_without_tls_still_works(self):
        port = _find_free_port()
        self._proc = _start_server(port)
        self.assertTrue(
            _wait_for_server("127.0.0.1", port, use_ssl=False),
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data.get("status"), "ok")
        conn.close()

    def test_tls_startup_failure_fallback_to_http(self):
        """Bad cert paths should print a warning and start HTTP anyway."""
        port = _find_free_port()
        self._proc = _start_server(
            port,
            cert="/nonexistent/cert.pem",
            key="/nonexistent/key.pem",
        )
        # Server should be reachable over plain HTTP even though TLS setup failed
        self.assertTrue(
            _wait_for_server("127.0.0.1", port, use_ssl=False),
            "HTTP fallback server did not start after TLS failure",
        )
        # Confirm TLS warning was printed
        os.set_blocking(self._proc.stdout.fileno(), False)
        output = ""
        try:
            output = self._proc.stdout.read(2000) or ""
        except BlockingIOError:
            output = ""
        self.assertIn("TLS setup failed", output)


if __name__ == "__main__":
    unittest.main()
