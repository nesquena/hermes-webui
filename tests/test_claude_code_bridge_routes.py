from __future__ import annotations

import collections
import http.cookies
import io
import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from api import auth, helpers, routes, terminal


PUBLIC_ID = "claude_code_0123456789abcdef01234567"


class _Headers(dict):
    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Handler:
    def __init__(self, path: str, *, body: dict | None = None, headers=None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.path = path
        self.command = "POST" if body is not None else "GET"
        self.headers = _Headers(headers or {})
        if body is not None:
            self.headers["Content-Length"] = str(len(raw))
        self.client_address = ("127.0.0.1", 43210)
        self.request = None
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name: str) -> str | None:
        values = [
            value
            for key, value in self.sent_headers
            if key.lower() == name.lower()
        ]
        return values[-1] if values else None

    def headers_named(self, name: str) -> list[str]:
        return [
            value
            for key, value in self.sent_headers
            if key.lower() == name.lower()
        ]

    def json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _descriptor(*, resumable: bool = True):
    profile = SimpleNamespace(
        model_id="anthropic.qwen-aeon",
        label="Claude Qwen",
    )
    return SimpleNamespace(
        public_id=PUBLIC_ID,
        store=SimpleNamespace(label="Claude Local"),
        claude_session_id=str(uuid4()),
        profile=profile if resumable else None,
        cwd=SimpleNamespace(name="QwenLocal") if resumable else None,
        workspace_label="QwenLocal" if resumable else None,
        can_remote_resume=resumable,
    )


def _post(path: str, body: dict, *, headers=None) -> _Handler:
    handler = _Handler(path, body=body, headers=headers)
    routes.handle_post(handler, urlparse(path))
    return handler


def _get(path: str, *, headers=None) -> _Handler:
    handler = _Handler(path, headers=headers)
    routes.handle_get(handler, urlparse(path))
    return handler


@pytest.fixture(autouse=True)
def _isolated_bridge(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "valid-auth")
    monkeypatch.setattr(auth, "verify_session", lambda value: value == "valid-auth")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "_ensure_claude_bridge_singleton",
        lambda: True,
        raising=False,
    )
    with terminal._LOCK:
        previous = dict(terminal._TERMINALS)
        terminal._TERMINALS.clear()
    yield
    with terminal._LOCK:
        terminal._TERMINALS.clear()
        terminal._TERMINALS.update(previous)


@pytest.fixture
def managed_terminals():
    opened: list[int] = []

    class _Proc:
        pid = 424242

        def poll(self):
            return None

    def make(public_id: str):
        read_fd, write_fd = os.pipe()
        opened.extend((read_fd, write_fd))
        handle = f"handle-{uuid4().hex}"
        generation = str(uuid4())
        term = terminal.TerminalSession(
            session_id=handle,
            workspace="/safe/workspace",
            proc=_Proc(),
            master_fd=write_fd,
            kind="claude_code",
            handle=handle,
            generation=generation,
            persistent_when_unwatched=True,
            _backlog=collections.deque(),
        )
        term.public_session_id = public_id
        with terminal._LOCK:
            terminal._TERMINALS[handle] = term
        return term

    first = make(PUBLIC_ID)
    second = make("claude_code_fedcba9876543210fedcba98")
    yield first, second
    with terminal._LOCK:
        terminal._TERMINALS.clear()
    for fd in opened:
        try:
            os.close(fd)
        except OSError:
            pass


def _cookie_header(
    term,
    operation: str,
    capability: str,
    *,
    generation: str | None = None,
) -> dict[str, str]:
    name = helpers.claude_terminal_capability_cookie_name(
        term.handle,
        generation or term.generation,
        operation,
    )
    return {"Cookie": f"{name}={capability}"}


def test_resume_requires_real_auth_even_when_onboarding_open(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setenv("HERMES_WEBUI_ONBOARDING_OPEN", "1")
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: pytest.fail(
            "dedicated bridge auth must precede generic session guards"
        ),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 403
    assert response.json() == {"error": "authentication_required"}


def test_resume_rejects_invalid_authenticated_state(monkeypatch):
    monkeypatch.setattr(auth, "verify_session", lambda _value: False)

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 403
    assert response.json() == {"error": "authentication_required"}


def test_client_cannot_override_launch_fields(monkeypatch):
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session",
        lambda _public_id: pytest.fail("override fields must fail before resolution"),
    )

    response = _post(
        "/api/claude-code/resume",
        {
            "session_id": PUBLIC_ID,
            "cwd": "/tmp",
            "argv": ["/bin/sh"],
            "model": "anthropic.ornith",
            "uuid": str(uuid4()),
        },
    )

    assert response.status == 400
    assert response.json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/claude-code/resume", {"session_id": {"value": PUBLIC_ID}}),
        ("/api/claude-code/resume", {"session_id": [PUBLIC_ID]}),
        ("/api/claude-code/terminal-token", {"handle": 7, "generation": "g", "operation": "input"}),
        ("/api/claude-code/terminal/input", {"handle": "h", "generation": True, "data": "x"}),
        ("/api/claude-code/stop", {"handle": 1.5, "generation": "g"}),
    ],
)
def test_bridge_rejects_non_string_identifiers(path, body):
    response = _post(path, body)

    assert response.status == 400
    assert response.json() == {"error": "invalid_request"}


def test_status_projection_omits_private_fields(monkeypatch):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state="inactive"),
    )

    response = _get(f"/api/claude-code/status?session_id={PUBLIC_ID}")

    assert response.status == 200
    forbidden = {
        "uuid",
        "cwd",
        "pid",
        "pgid",
        "argv",
        "config_dir",
        "wrapper",
        "lock_path",
        "claude_session_id",
        "transcript_path",
    }
    assert forbidden.isdisjoint(response.json())
    assert response.json() == {
        "kind": "claude_code",
        "profile": "qwen",
        "label": "Claude Qwen",
        "can_remote_resume": True,
        "coarse_status": "inactive",
        "workspace_label": "QwenLocal",
    }
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Referrer-Policy") == "no-referrer"


def test_status_reresolves_on_every_request(monkeypatch):
    calls = []
    descriptors = [_descriptor(), None]

    def resolve(public_id):
        calls.append(public_id)
        return descriptors.pop(0)

    monkeypatch.setattr("api.claude_code_bridge.resolve_session", resolve)
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state="inactive"),
    )

    first = _get(f"/api/claude-code/status?session_id={PUBLIC_ID}")
    second = _get(f"/api/claude-code/status?session_id={PUBLIC_ID}")

    assert first.status == 200
    assert second.status == 404
    assert second.json() == {"error": "not_found"}
    assert calls == [PUBLIC_ID, PUBLIC_ID]


def test_resume_reuses_existing_hermes_terminal_without_probe_or_spawn(
    monkeypatch, managed_terminals
):
    existing, _other = managed_terminals
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: _descriptor()
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda *_args, **_kwargs: pytest.fail("existing terminal must not be probed"),
    )
    monkeypatch.setattr(
        terminal,
        "start_managed_terminal",
        lambda *_args, **_kwargs: pytest.fail("existing terminal must not respawn"),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 200
    assert response.json() == {
        "ok": True,
        "attached": True,
        "handle": existing.handle,
        "generation": existing.generation,
    }
    stream_cookie_name = helpers.claude_terminal_capability_cookie_name(
        existing.handle,
        existing.generation,
        "stream",
    )
    assert any(
        header.startswith(f"{stream_cookie_name}=")
        for header in response.headers_named("Set-Cookie")
    )


def test_resume_attach_race_returns_indistinguishable_not_found(
    monkeypatch, managed_terminals
):
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: _descriptor()
    )
    monkeypatch.setattr(
        terminal,
        "issue_terminal_capability",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError("retired")),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 404
    assert response.json() == {"error": "not_found"}


@pytest.mark.parametrize(
    "descriptor,state,expected_status,expected_error",
    [
        (None, None, 404, "not_found"),
        (_descriptor(resumable=False), None, 422, "unsafe_session"),
        (_descriptor(), "active_elsewhere", 409, "active_elsewhere"),
        (_descriptor(), "ownership_unknown", 503, "ownership_unknown"),
    ],
)
def test_resume_maps_safe_outcomes(
    monkeypatch, descriptor, state, expected_status, expected_error
):
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state=state),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == expected_status
    assert response.json() == {"error": expected_error}


def test_resume_requires_process_singleton(monkeypatch):
    monkeypatch.setattr(routes, "_ensure_claude_bridge_singleton", lambda: False)
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session",
        lambda _public_id: pytest.fail("singleton gate must precede resolution"),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 503
    assert response.json() == {"error": "single_process_required"}


def test_resume_maps_terminal_limit_without_raw_exception(monkeypatch):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state="inactive"),
    )
    monkeypatch.setattr(
        terminal,
        "start_managed_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            terminal.ManagedTerminalLimitError("private path /tmp/secret")
        ),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 429
    assert response.json() == {"error": "terminal_limit"}
    assert "/tmp/secret" not in response.wfile.getvalue().decode("utf-8")


@pytest.mark.parametrize(
    "runner_state,expected_status,expected_error",
    [
        ("invalid_session", 404, "not_found"),
        ("ownership_conflict", 409, "active_elsewhere"),
        ("active_elsewhere", 409, "active_elsewhere"),
        ("ownership_unknown", 503, "ownership_unknown"),
    ],
)
def test_resume_maps_runner_readiness_without_raw_details(
    monkeypatch, runner_state, expected_status, expected_error
):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state="inactive"),
    )
    monkeypatch.setattr(
        terminal,
        "start_managed_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            terminal.ManagedTerminalStartError(runner_state)
        ),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == expected_status
    assert response.json() == {"error": expected_error}


def test_resume_maps_probe_exception_to_ownership_unknown(monkeypatch):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private path /tmp/secret")
        ),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 503
    assert response.json() == {"error": "ownership_unknown"}
    assert "/tmp/secret" not in response.wfile.getvalue().decode("utf-8")


def test_resume_maps_spawn_os_error_to_ownership_unknown(monkeypatch):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state="inactive"),
    )
    monkeypatch.setattr(
        terminal,
        "start_managed_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("private path /tmp/secret")
        ),
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 503
    assert response.json() == {"error": "ownership_unknown"}


def test_status_maps_probe_exception_to_ownership_unknown(monkeypatch):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private path /tmp/secret")
        ),
    )

    response = _get(f"/api/claude-code/status?session_id={PUBLIC_ID}")

    assert response.status == 503
    assert response.json() == {"error": "ownership_unknown"}


def test_status_maps_unknown_probe_state_to_ownership_unknown(monkeypatch):
    descriptor = _descriptor()
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda *_args, **_kwargs: SimpleNamespace(state="ownership_unknown"),
    )

    response = _get(f"/api/claude-code/status?session_id={PUBLIC_ID}")

    assert response.status == 503
    assert response.json() == {"error": "ownership_unknown"}


def test_resume_starts_only_authoritative_descriptor_workspace(monkeypatch):
    descriptor = _descriptor()
    started = []
    created = SimpleNamespace(handle="new-handle", generation=str(uuid4()))
    monkeypatch.setattr(
        "api.claude_code_bridge.resolve_session", lambda _public_id: descriptor
    )
    monkeypatch.setattr(
        "api.claude_code_bridge.probe_runtime_status",
        lambda _descriptor, fresh=False: SimpleNamespace(state="inactive"),
    )
    monkeypatch.setattr(
        terminal,
        "start_managed_terminal",
        lambda public_id, workspace: started.append((public_id, workspace)) or created,
    )
    monkeypatch.setattr(
        terminal,
        "issue_terminal_capability",
        lambda _handle, _generation, _operation: "stream-capability",
    )

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 200
    assert response.json()["attached"] is False
    assert started == [(PUBLIC_ID, descriptor.cwd)]


def test_terminal_token_is_cookie_only_and_operation_scoped(managed_terminals):
    term, _other = managed_terminals

    response = _post(
        "/api/claude-code/terminal-token",
        {
            "handle": term.handle,
            "generation": term.generation,
            "operation": "input",
        },
    )

    assert response.status == 200
    cookie_header = response.header("Set-Cookie")
    assert cookie_header is not None
    expected_name = helpers.claude_terminal_capability_cookie_name(
        term.handle,
        term.generation,
        "input",
    )
    assert cookie_header.startswith(f"{expected_name}=")
    capability = cookie_header.split("=", 1)[1].split(";", 1)[0]
    assert capability
    assert capability not in response.wfile.getvalue().decode("utf-8")
    assert capability not in response.path
    assert "HttpOnly" in cookie_header
    assert "SameSite=Strict" in cookie_header
    assert "Path=/api/claude-code" in cookie_header


def test_terminal_capability_cookie_is_secure_over_https(
    monkeypatch, managed_terminals
):
    term, _other = managed_terminals
    monkeypatch.setenv("HERMES_WEBUI_SECURE", "1")

    response = _post(
        "/api/claude-code/terminal-token",
        {
            "handle": term.handle,
            "generation": term.generation,
            "operation": "stream",
        },
    )

    assert "; Secure" in response.header("Set-Cookie")


@pytest.mark.parametrize("operation", ["stream", "input", "resize", "stop"])
def test_terminal_token_accepts_only_documented_operations(
    managed_terminals, operation
):
    term, _other = managed_terminals

    response = _post(
        "/api/claude-code/terminal-token",
        {
            "handle": term.handle,
            "generation": term.generation,
            "operation": operation,
        },
    )

    assert response.status == 200


def test_two_terminal_cookie_jar_retains_every_operation_capability(
    managed_terminals,
):
    first, second = managed_terminals
    jar = http.cookies.SimpleCookie()

    for term in (first, second):
        for operation in ("stream", "input", "resize", "stop"):
            response = _post(
                "/api/claude-code/terminal-token",
                {
                    "handle": term.handle,
                    "generation": term.generation,
                    "operation": operation,
                },
            )
            jar.load(response.header("Set-Cookie"))

    assert len(jar) == 8
    browser_cookie = "; ".join(
        f"{name}={morsel.value}" for name, morsel in jar.items()
    )
    handler = _Handler(
        "/api/claude-code/terminal/output",
        headers={"Cookie": browser_cookie},
    )
    for operation in ("stream", "input", "resize", "stop"):
        first_capability = routes._claude_terminal_authority(
            handler,
            first.handle,
            first.generation,
            operation,
        )
        second_capability = routes._claude_terminal_authority(
            handler,
            second.handle,
            second.generation,
            operation,
        )
        assert first_capability != second_capability
        assert terminal._authorised_managed_terminal(
            handle=first.handle,
            generation=first.generation,
            capability=first_capability,
            operation=operation,
        ) is first
        assert terminal._authorised_managed_terminal(
            handle=second.handle,
            generation=second.generation,
            capability=second_capability,
            operation=operation,
        ) is second
        with pytest.raises(KeyError):
            terminal._authorised_managed_terminal(
                handle=second.handle,
                generation=second.generation,
                capability=first_capability,
                operation=operation,
            )


def test_terminal_capability_remint_revokes_prior_token_and_stays_bounded(
    managed_terminals,
):
    term, _other = managed_terminals
    latest = {}

    for operation in ("stream", "input", "resize", "stop"):
        old = terminal.issue_terminal_capability(
            term.handle, term.generation, operation
        )
        latest[operation] = terminal.issue_terminal_capability(
            term.handle, term.generation, operation
        )
        with pytest.raises(KeyError):
            terminal._authorised_managed_terminal(
                handle=term.handle,
                generation=term.generation,
                capability=old,
                operation=operation,
            )

    assert len(term._capabilities) == 4
    for operation, capability in latest.items():
        assert terminal._authorised_managed_terminal(
            handle=term.handle,
            generation=term.generation,
            capability=capability,
            operation=operation,
        ) is term


def test_capability_for_one_terminal_cannot_type_or_stop_another(
    managed_terminals,
):
    first, second = managed_terminals
    input_capability = terminal.issue_terminal_capability(
        first.handle, first.generation, "input"
    )
    stop_capability = terminal.issue_terminal_capability(
        first.handle, first.generation, "stop"
    )

    typed = _post(
        "/api/claude-code/terminal/input",
        {
            "handle": second.handle,
            "generation": second.generation,
            "data": "x",
        },
        headers=_cookie_header(first, "input", input_capability),
    )
    stopped = _post(
        "/api/claude-code/stop",
        {"handle": second.handle, "generation": second.generation},
        headers=_cookie_header(first, "stop", stop_capability),
    )

    assert typed.status == 404
    assert typed.json() == {"error": "not_found"}
    assert stopped.status == 404
    assert stopped.json() == {"error": "not_found"}
    assert second.handle in terminal._TERMINALS


def test_resize_requires_resize_capability(managed_terminals):
    term, _other = managed_terminals
    input_capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "input"
    )

    response = _post(
        "/api/claude-code/terminal/resize",
        {
            "handle": term.handle,
            "generation": term.generation,
            "rows": 30,
            "cols": 100,
        },
        headers=_cookie_header(term, "resize", input_capability),
    )

    assert response.status == 404
    assert response.json() == {"error": "not_found"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", True),
        ("rows", 24.0),
        ("rows", "24"),
        ("rows", 7),
        ("rows", 81),
        ("cols", False),
        ("cols", 80.0),
        ("cols", "80"),
        ("cols", 19),
        ("cols", 241),
    ],
)
def test_resize_rejects_non_integer_and_out_of_range_dimensions(
    managed_terminals,
    field,
    value,
):
    term, _other = managed_terminals
    body = {
        "handle": term.handle,
        "generation": term.generation,
        "rows": 24,
        "cols": 80,
    }
    body[field] = value

    response = _post("/api/claude-code/terminal/resize", body)

    assert response.status == 400
    assert response.json() == {"error": "invalid_request"}


def test_old_generation_and_capability_cannot_reconnect(managed_terminals):
    term, _other = managed_terminals
    old_capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stream"
    )
    old_generation = term.generation
    term.generation = str(uuid4())

    response = _get(
        "/api/claude-code/terminal/output"
        f"?handle={term.handle}&generation={old_generation}",
        headers=_cookie_header(
            term,
            "stream",
            old_capability,
            generation=old_generation,
        ),
    )

    assert response.status == 404
    assert response.json() == {"error": "not_found"}


def test_stream_redacts_query_and_translates_terminal_reset(
    monkeypatch, managed_terminals
):
    term, _other = managed_terminals
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stream"
    )
    term.put_output("terminal_reset", {"generation": term.generation})
    term.put_output("terminal_closed", {"exit_code": 0})
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    path = (
        "/api/claude-code/terminal/output"
        f"?handle={term.handle}&generation={term.generation}"
    )

    response = _get(path, headers=_cookie_header(term, "stream", capability))

    assert response.status == 200
    assert response.path == "/api/claude-code/terminal/output"
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Referrer-Policy") == "no-referrer"
    stream = response.wfile.getvalue().decode("utf-8")
    assert "event: terminal_reset" in stream
    assert term.generation in stream
    assert capability not in stream


def test_stream_cursor_query_replays_only_unseen_output(
    monkeypatch, managed_terminals
):
    term, _other = managed_terminals
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stream"
    )
    term.put_output("output", {"text": "already-rendered"})
    term.put_output("output", {"text": "new-output"})
    term.put_output("terminal_closed", {"exit_code": 0})
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    path = (
        "/api/claude-code/terminal/output"
        f"?handle={term.handle}&generation={term.generation}&cursor=1"
    )

    response = _get(path, headers=_cookie_header(term, "stream", capability))

    assert response.status == 200
    assert response.path == "/api/claude-code/terminal/output"
    stream = response.wfile.getvalue().decode("utf-8")
    assert "already-rendered" not in stream
    assert "new-output" in stream
    assert "id: 2" in stream
    assert capability not in stream


def test_stream_cursor_behind_backlog_floor_emits_terminal_reset(
    monkeypatch, managed_terminals
):
    term, _other = managed_terminals
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stream"
    )
    term.put_output("output", {"text": "stale-output"})
    signal_calls = []
    original_attach = terminal.attach_managed_terminal

    def attach_then_close(**kwargs):
        attached, output = original_attach(**kwargs)
        output.put((2, "terminal_closed", {}))
        return attached, output

    monkeypatch.setattr(terminal, "attach_managed_terminal", attach_then_close)
    monkeypatch.setattr(
        terminal,
        "_signal_owned_group",
        lambda attached, signum: signal_calls.append((attached, signum)),
    )
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    path = (
        "/api/claude-code/terminal/output"
        f"?handle={term.handle}&generation={term.generation}&cursor=0"
    )

    response = _get(path, headers=_cookie_header(term, "stream", capability))

    assert response.status == 200
    stream = response.wfile.getvalue().decode("utf-8")
    assert "event: terminal_reset" in stream
    assert "stale-output" not in stream
    assert signal_calls


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "-1",
        "1.5",
        "nope",
        "00",
        "0001",
        "1&cursor=2",
        "9223372036854775808",
        "9" * 20,
        "9" * 100_000,
    ],
    ids=(
        "empty",
        "negative",
        "fractional",
        "non_decimal",
        "zero_leading_zero",
        "leading_zero",
        "duplicate",
        "int64_overflow",
        "overlength",
        "huge",
    ),
)
def test_stream_cursor_query_fails_closed(cursor, managed_terminals):
    term, _other = managed_terminals
    path = (
        "/api/claude-code/terminal/output"
        f"?handle={term.handle}&generation={term.generation}&cursor={cursor}"
    )

    response = _get(path)

    assert response.status == 400
    assert response.json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    ("cursor", "expected"),
    [("0", 0), ("9223372036854775807", 2**63 - 1)],
)
def test_stream_cursor_query_accepts_int64_boundaries(cursor, expected):
    parsed = urlparse(
        "/api/claude-code/terminal/output"
        f"?handle=safe-handle&generation=safe-generation&cursor={cursor}"
    )

    assert routes._claude_terminal_stream_query(parsed) == (
        "safe-handle",
        "safe-generation",
        expected,
    )


@pytest.mark.parametrize(
    "cursor",
    ["00", "0001", "9" * 100_000, "9223372036854775808"],
    ids=("zero_leading_zero", "leading_zero", "huge", "int64_overflow"),
)
def test_stream_last_event_id_fails_closed_before_attach(cursor, managed_terminals):
    term, _other = managed_terminals
    response = _get(
        "/api/claude-code/terminal/output"
        f"?handle={term.handle}&generation={term.generation}",
        headers={"Last-Event-ID": cursor},
    )

    assert response.status == 400
    assert response.json() == {"error": "invalid_request"}


def test_bridge_mutation_cross_origin_is_rejected_before_body_dispatch(monkeypatch):
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: False)

    response = _post("/api/claude-code/resume", {"session_id": PUBLIC_ID})

    assert response.status == 403
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Referrer-Policy") == "no-referrer"
