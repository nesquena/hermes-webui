"""Opt-in native WebUI/Agent fixture; only model inputs are synthetic.

All homes, configs, sentinels and credentials are disposable. The subprocess
receives an allowlisted environment, not the caller's provider credentials.
Docker operations use a pre-existing image and exact profile/session labels.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import yaml

PROFILES = ("native-alpha", "native-beta")


class _FixtureServer(ThreadingHTTPServer):
    fixture: ModelFixture


class _ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._send(
            {
                "object": "list",
                "data": [{"id": "native-workspace-fixture", "object": "model"}],
            }
        )

    def do_POST(self) -> None:
        request = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        )
        results = [m for m in request.get("messages", []) if m.get("role") == "tool"]
        tools = {t.get("function", {}).get("name") for t in request.get("tools", [])}
        message: dict[str, Any]
        if {"terminal", "read_file"} <= tools and not results:
            calls = [
                {
                    "id": "native_pwd",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": "pwd", "timeout": 20}),
                    },
                },
                {
                    "id": "native_read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "sentinel.txt"}),
                    },
                },
            ]
            message = {"role": "assistant", "content": None, "tool_calls": calls}
            finish = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": "Synthetic fixture finished; real tool responses: "
                + json.dumps(results),
            }
            finish = "stop"
        cast(_FixtureServer, self.server).fixture.record(request, results)
        ident = "fixture-" + secrets.token_hex(8)
        common = {
            "id": ident,
            "created": int(time.time()),
            "model": "native-workspace-fixture",
        }
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    dict(call, index=i) for i, call in enumerate(delta["tool_calls"])
                ]
            for fragment, reason in ((delta, None), ({}, finish)):
                payload = dict(
                    common,
                    object="chat.completion.chunk",
                    choices=[{"index": 0, "delta": fragment, "finish_reason": reason}],
                )
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._send(
                dict(
                    common,
                    object="chat.completion",
                    choices=[{"index": 0, "message": message, "finish_reason": finish}],
                )
            )

    def _send(self, value: dict[str, Any]) -> None:
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ModelFixture:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = _FixtureServer(("127.0.0.1", 0), _ModelHandler)
        self.server.fixture = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def record(self, request: dict[str, Any], results: list[dict[str, Any]]) -> None:
        with self.lock:
            self.records.append({"stream": request.get("stream"), "results": results})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "fixture model did not stop"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class NativeWorkspaceCase:
    def __init__(self, repo: Path, backend: str) -> None:
        self.repo, self.backend = repo, backend
        self.agent_dir = (
            Path(os.environ["HERMES_WEBUI_AGENT_DIR"]).expanduser().resolve()
        )
        configured = os.environ.get("HERMES_WEBUI_PYTHON", "").strip()
        self.python = (
            Path(configured).expanduser()
            if configured
            else self.agent_dir / "venv/bin/python"
        )
        if not self.python.is_file() or not (self.agent_dir / "run_agent.py").is_file():
            raise RuntimeError(
                "set HERMES_WEBUI_AGENT_DIR and HERMES_WEBUI_PYTHON to an installed native Agent"
            )
        self.image = os.environ.get(
            "HERMES_WEBUI_TEST_DOCKER_IMAGE", "python:3.11-slim"
        )
        if backend == "docker":
            subprocess.run(
                ["docker", "image", "inspect", self.image],
                capture_output=True,
                check=True,
                timeout=15,
            )
        self.root = Path(tempfile.mkdtemp(prefix="hermes-native-workspace-"))
        self.home = self.root / "home"
        self.hermes = self.home / ".hermes"
        self.state = self.root / "webui-state"
        self.control = self.root / "control-workspace"
        self.workspaces = {name: self.root / f"{name}-selected" for name in PROFILES}
        self.defaults = {name: self.root / f"{name}-default" for name in PROFILES}
        self.process = None
        self.server_log = None
        self.model = None
        self.sessions: dict[str, str] = {}
        self.password = secrets.token_urlsafe(24)
        self.url = ""

    def __enter__(self) -> NativeWorkspaceCase:
        try:
            self._start()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _start(self) -> None:
        for path in (
            self.hermes,
            self.state,
            self.control,
            *self.workspaces.values(),
            *self.defaults.values(),
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.control / "sentinel.txt").write_text("CONTROL-MUST-NOT-BE-USED\n")
        self.model = ModelFixture()
        for name, workspace in self.workspaces.items():
            (workspace / "sentinel.txt").write_text(f"SELECTED-{name}\n")
            (self.defaults[name] / "sentinel.txt").write_text(
                f"DEFAULT-{name}-MUST-NOT-BE-USED\n"
            )
            profile = self.hermes / "profiles" / name
            profile.mkdir(parents=True)
            self._write_config(profile / "config.yaml", name)
        self._write_config(self.hermes / "config.yaml", "control")
        (self.state / "settings.json").write_text(
            json.dumps(
                {
                    "default_workspace": str(self.control),
                    "check_for_updates": False,
                    "auto_generate_title": False,
                }
            )
        )
        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.server_log = (self.root / "server.log").open("w")
        self.process = subprocess.Popen(
            [str(self.python), str(self.repo / "server.py")],
            cwd=self.repo,
            env=self._environment(port),
            stdout=self.server_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "WebUI exited: "
                        + (self.root / "server.log").read_text()[-2000:]
                    )
                try:
                    if client.get(self.url + "/health", timeout=1).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise TimeoutError("WebUI readiness deadline exceeded")

    def _write_config(self, path: Path, profile: str) -> None:
        assert self.model is not None
        terminal = {
            "backend": "local" if profile == "control" else self.backend,
            "cwd": str(
                self.control if profile == "control" else self.defaults[profile]
            ),
            "docker_image": self.image,
            "container_persistent": False,
            "docker_mount_cwd_to_workspace": True,
            "docker_run_as_host_user": False,
            "docker_persist_across_processes": False,
            "docker_network": False,
            "docker_volumes": [],
            "docker_extra_args": [],
            "docker_env": {},
            "docker_forward_env": [],
            "env_passthrough": [],
        }
        config = {
            "model": {
                "provider": "custom",
                "default": "native-workspace-fixture",
                "base_url": f"http://127.0.0.1:{self.model.port}/v1",
            },
            "terminal": terminal,
            "agent": {"max_turns": 4},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "plugins": {},
            "mcp_servers": {},
            "custom_providers": [],
            "checkpoints": {"enabled": False},
        }
        path.write_text(yaml.safe_dump(config))
        path.chmod(0o600)

    def _environment(self, port: int) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "HERMES_HOME": str(self.hermes),
            "HERMES_BASE_HOME": str(self.hermes),
            "HERMES_WEBUI_STATE_DIR": str(self.state),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_AGENT_DIR": str(self.agent_dir),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(self.control),
            "HERMES_WEBUI_PASSWORD": self.password,
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_COOKIE_NAME": "native_workspace_auth",
            "HERMES_WEBUI_PROFILE_COOKIE_NAME": "native_workspace_profile",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
        }

    def call(
        self, client: httpx.Client, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        response = client.request(method, self.url + path, timeout=15, **kwargs)
        response.raise_for_status()
        return response.json()

    def run_profile(self, name: str) -> dict[str, Any]:
        assert self.model is not None
        with httpx.Client(trust_env=False) as client:
            self.call(
                client, "POST", "/api/auth/login", json={"password": self.password}
            )
            html = client.get(self.url + "/", timeout=15).text
            match = re.search(r'csrfToken\s*:\s*("[^"]*")', html)
            assert match, "CSRF boot value missing after real login"
            client.headers.update(
                {"X-Hermes-CSRF-Token": json.loads(match.group(1)), "Origin": self.url}
            )
            self.call(client, "POST", "/api/profile/switch", json={"name": name})
            selected = str(self.workspaces[name])
            self.call(
                client,
                "POST",
                "/api/workspaces/add",
                json={"path": selected, "name": name, "create": False},
            )
            created = self.call(
                client,
                "POST",
                "/api/session/new",
                json={
                    "profile": name,
                    "workspace": selected,
                    "worktree": False,
                    "enabled_toolsets": ["terminal", "file"],
                },
            )["session"]
            sid = created["session_id"]
            self.sessions[name] = sid
            first_record = len(self.model.records)
            started = self.call(
                client,
                "POST",
                "/api/chat/start",
                json={
                    "session_id": sid,
                    "profile": name,
                    "workspace": selected,
                    "message": "Synthetic read-only workspace routing test.",
                    "model": "native-workspace-fixture",
                    "model_provider": "custom",
                },
            )
            events = self._stream(client, sid, started["stream_id"])
            persisted = self.call(
                client, "GET", "/api/session", params={"session_id": sid}
            )["session"]
        records = self.model.records[first_record:]
        # Read real tool-role responses, never assistant prose or workspace tags.
        results = {
            m["tool_call_id"]: m["content"] for r in records for m in r["results"]
        }
        pwd = json.loads(results["native_pwd"]) if "native_pwd" in results else {}
        read = json.loads(results["native_read"]) if "native_read" in results else {}
        containers = self._owned_containers(name, sid)
        checks = {
            "completed": any(e["event"] == "done" for e in events)
            and not any(e["event"] in {"error", "apperror"} for e in events),
            "session_workspace": persisted.get("workspace") == selected,
            "terminal_pwd": pwd.get("output", "").strip()
            == ("/workspace" if self.backend == "docker" else selected)
            and pwd.get("exit_code") == 0,
            "selected_sentinel": f"SELECTED-{name}" in read.get("content", "")
            and "error" not in read,
        }
        if self.backend == "docker":
            checks["selected_mount"] = bool(containers) and all(
                any(
                    m.get("Source") == selected and m.get("Destination") == "/workspace"
                    for m in c.get("Mounts", [])
                )
                for c in containers
            )
            checks["network_none"] = bool(containers) and all(
                c["HostConfig"]["NetworkMode"] == "none" for c in containers
            )
        report = {
            "profile": name,
            "session_id": sid,
            "workspace": selected,
            "checks": checks,
            "tool_results": results,
            "stream_errors": [
                e["data"] for e in events if e["event"] in {"error", "apperror"}
            ],
        }
        print("NATIVE_WORKSPACE_RESULT " + json.dumps(report), flush=True)
        return report

    def _stream(
        self, client: httpx.Client, sid: str, stream_id: str
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        done = threading.Event()
        errors: list[str] = []

        def read() -> None:
            try:
                with client.stream(
                    "GET",
                    self.url + "/api/chat/stream",
                    params={"stream_id": stream_id},
                    timeout=65,
                ) as response:
                    response.raise_for_status()
                    event = None
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            value = json.loads(line[5:].strip())
                            events.append({"event": event, "data": value})
                            if event in {"done", "error", "apperror"}:
                                return
            except Exception as exc:
                errors.append(str(exc))
            finally:
                done.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + 65
        while not done.wait(0.2) and time.monotonic() < deadline:
            pending = self.call(
                client, "GET", "/api/approval/pending", params={"session_id": sid}
            ).get("pending")
            if pending:
                assert pending.get("command") == "pwd", (
                    "refusing unexpected fixture command"
                )
                self.call(
                    client,
                    "POST",
                    "/api/approval/respond",
                    json={
                        "session_id": sid,
                        "approval_id": pending["approval_id"],
                        "mirror_token": pending.get("_gateway_mirror_token", ""),
                        "choice": "once",
                        "yolo": False,
                    },
                )
        assert done.is_set(), "SSE completion deadline exceeded"
        reader.join(timeout=2)
        assert not errors, errors
        return events

    def _owned_containers(self, profile: str, sid: str) -> list[dict[str, Any]]:
        if self.backend != "docker":
            return []
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=hermes-profile={profile}",
                "--filter",
                f"label=hermes-task-id={sid}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        containers = []
        for cid in listed.stdout.split():
            data = json.loads(
                subprocess.run(
                    ["docker", "inspect", cid],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                ).stdout
            )[0]
            labels = data["Config"].get("Labels", {})
            assert (
                labels.get("hermes-profile") == profile
                and labels.get("hermes-task-id") == sid
            )
            containers.append(data)
        return containers

    def __exit__(self, *_exc: Any) -> None:
        try:
            if self.process is not None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.server_log is not None:
                self.server_log.close()
            if self.model is not None:
                self.model.close()
            for profile, sid in self.sessions.items():
                for container in self._owned_containers(profile, sid):
                    subprocess.run(
                        ["docker", "rm", "-f", container["Id"]],
                        capture_output=True,
                        check=True,
                        timeout=15,
                    )
                assert not self._owned_containers(profile, sid), (
                    "fixture container cleanup failed"
                )
        finally:
            shutil.rmtree(self.root)
