"""#7506 release-stage follow-up: a full-cap dead-endpoint timeout stays a failure at a larger rebuild budget.

With HERMES_WEBUI_MODELS_REBUILD_BUDGET at ~5.5 s or more, the first pass gives a dead endpoint the whole 5 s cap. The
lend-back pass used to consume that timeout under a smaller fair-share slice and classify it as "truncated", so the
first load lost the provider's error group and the second load blocked another 5 s (master: one hit, error group on
the first load, instant second load). Found by the senior review on the #7506 r3 gate.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import pytest


@pytest.mark.parametrize("budget", ["8", "5.6"])
def test_full_cap_dead_timeout_is_not_reclassified_as_truncated(tmp_path, budget):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.split("/")[1]
            requests.append(name)
            if name == "dead":
                time.sleep(6)
            payload = {"data": 1} if name == "lm" else {"data": [{"id": name + "-model"}]}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=endpoint.serve_forever)
    thread.start()
    proc = None
    root = Path(__file__).resolve().parents[1]
    env = {key: os.environ[key] for key in ("HERMES_WEBUI_AGENT_DIR",) if key in os.environ}
    env["PATH"] = "/usr/bin:/bin"
    for key in ("HOME", "HERMES_HOME", "HERMES_BASE_HOME", "HERMES_WEBUI_STATE_DIR", "CODEX_HOME", "TMPDIR"):
        directory = tmp_path / key.lower()
        directory.mkdir()
        env[key] = str(directory)
    env["HERMES_BASE_HOME"] = env["HERMES_HOME"]
    base = f"http://127.0.0.1:{endpoint.server_port}"
    config = {
        "model": {"provider": "lmstudio", "default": "local-model", "base_url": base + "/lm/v1"},
        "custom_providers": [{"name": name + "-gw", "base_url": base + f"/{name}/v1"} for name in ("dead", "fast")],
    }
    config_path = Path(env["HERMES_HOME"]) / "config.yaml"
    config_path.write_text(json.dumps(config))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(HERMES_CONFIG_PATH=str(config_path), HERMES_WEBUI_HOST="127.0.0.1",
               HERMES_WEBUI_PORT=str(port), HERMES_WEBUI_MODELS_REBUILD_BUDGET=budget,
               HERMES_WEBUI_TEST_NETWORK_BLOCK="1", HERMES_WEBUI_PASSWORD="",
               HERMES_WEBUI_SKIP_ONBOARDING="1")
    log_path = tmp_path / "server.log"
    try:
        with log_path.open("w") as log:
            boot = ("import sys, types, runpy; "
                    "sys.modules['hermes_cli.models'] = types.SimpleNamespace("
                    "list_available_providers=lambda: [], provider_model_ids=lambda p: []); "
                    "sys.path.insert(0, sys.argv[1]); "
                    "runpy.run_path(sys.argv[1] + '/server.py', run_name='__main__')")
            proc = subprocess.Popen([sys.executable, "-c", boot, str(root)], cwd=tmp_path,
                                    env=env, stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, log_path.read_text()
            try:
                with urlopen(url + "/health", timeout=0.5) as response:
                    assert response.status == 200
                break
            except OSError:
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.05)
        observations = []
        for _ in range(2):
            start = time.monotonic()
            with urlopen(url + "/api/models", timeout=15) as response:
                catalog = json.load(response)
            observations.append({"elapsed": time.monotonic() - start,
                                 "groups": [g["provider_id"] for g in catalog["groups"]],
                                 "requests": list(requests)})
        (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))
        assert "custom:dead-gw" in observations[0]["groups"], observations   # the error group survives
        assert "custom:fast-gw" in observations[0]["groups"], observations
        assert observations[1]["elapsed"] < 1, observations                 # no second full-cap retry
        assert requests.count("dead") == 1, requests
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        endpoint.shutdown()
        endpoint.server_close()
        thread.join(5)
