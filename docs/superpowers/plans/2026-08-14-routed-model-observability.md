# Routed Model Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the actual model reported by an OpenAI-compatible streaming response, persist it on the Hermes WebUI assistant message/session, and render Requested, Routed, and Provider metadata.

**Architecture:** A WebUI-only lifecycle adapter registers an idempotent `post_api_request` observer with the active Hermes plugin manager. A `ContextVar` isolates capture state per streaming worker; the existing gateway-routing normalization, persistence, `done` event, journal, and footer paths carry the safe result without changing TokenTable or Hermes Agent.

**Tech Stack:** Python 3.11+, `contextvars`, Hermes lifecycle hooks, pytest, vanilla JavaScript, CSS, Node.js syntax/runtime lint, Playwright browser QA.

---

## Working Boundary

- Work only in `C:\Users\Joa\.hermes\worktrees\hermes-webui\routed-model-observability`.
- Branch must remain `feature/routed-model-observability` and begin implementation at design commit `e1d61572`.
- The dirty master checkout at `C:\Users\Joa\hermes-webui` remains read-only and unchanged.
- Do not modify TokenTable, Hermes Agent, provider configuration, database schemas, or environment files.
- Do not restart a user-owned WebUI or gateway. Browser QA launches and stops only its own isolated process.
- Do not push, open a PR, merge, or deploy.
- Stage only the files named by the current task.

## File Map

- Create `api/routed_model_observability.py`: safe provider-label resolution, idempotent lifecycle registration, per-context capture, snapshot, and cleanup.
- Create `tests/test_routed_model_observability.py`: lifecycle filtering, context isolation, cleanup, provider naming, and streaming integration.
- Modify `api/streaming.py`: begin/reset capture around the real agent turn, use SSE-derived routing as the fallback to existing explicit gateway metadata, and allowlist `source`.
- Modify `tests/test_732_gateway_routing_metadata.py`: source normalization, persistence contract, UI source guard, and safe DOM construction assertions.
- Modify `static/ui.js`: turn SSE-derived routing metadata into the three labelled footer values.
- Modify `static/style.css`: responsive, wrapping footer layout for desktop and narrow viewports.

## Execution Runtime Preparation

The machine currently has Python interpreters but no installed `pytest` in the probed environments. Preserve the required test-first order: add the first failing test before creating the isolated environment. Then prepare ignored local tooling:

```powershell
py -V:3.11 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt pytest pytest-timeout ruff playwright
npm ci
& '.\.venv\Scripts\python.exe' -m playwright install chromium
```

`.venv/` and `node_modules/` are already ignored. If any install needs network access, request that approval at execution time; do not alter dependency manifests merely to bootstrap local verification.

### Task 1: Add the run-scoped lifecycle capture adapter

**Files:**
- Create: `tests/test_routed_model_observability.py`
- Create: `api/routed_model_observability.py`

- [ ] **Step 1: Write the first failing adapter tests**

Create `tests/test_routed_model_observability.py` with these imports and tests before the production module exists:

```python
from concurrent.futures import ThreadPoolExecutor
import sys
import types

import pytest

from api.routed_model_observability import (
    _install_post_api_request_observer,
    _observe_post_api_request,
    begin_routed_model_capture,
    provider_display_name,
    reset_routed_model_capture,
    snapshot_routed_model_capture,
)


@pytest.fixture
def capture_without_real_plugins(monkeypatch):
    monkeypatch.setattr(
        "api.routed_model_observability._install_post_api_request_observer",
        lambda: None,
    )


def test_matching_webui_post_api_request_captures_last_response_model(
    capture_without_real_plugins,
):
    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    try:
        _observe_post_api_request(
            platform="webui",
            session_id="session-a",
            task_id="session-a",
            response_model="gpt-5.6",
            provider="custom",
        )
        _observe_post_api_request(
            platform="webui",
            session_id="session-a",
            task_id="session-a",
            response_model="gpt-5.6-sol",
            provider="custom",
        )
        assert snapshot_routed_model_capture() == {
            "requested_model": "auto",
            "requested_provider": "TokenTable",
            "used_model": "gpt-5.6-sol",
            "used_provider": "TokenTable",
            "source": "openai-compatible-sse",
        }
    finally:
        reset_routed_model_capture(token)

    assert snapshot_routed_model_capture() is None


@pytest.mark.parametrize(
    "event",
    [
        {"platform": "telegram", "session_id": "session-a", "task_id": "session-a"},
        {"platform": "webui", "session_id": "session-b", "task_id": "session-a"},
        {"platform": "webui", "session_id": "session-a", "task_id": "task-b"},
    ],
)
def test_unrelated_lifecycle_events_are_ignored(capture_without_real_plugins, event):
    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    try:
        _observe_post_api_request(
            response_model="wrong-model",
            provider="wrong-provider",
            **event,
        )
        assert snapshot_routed_model_capture() is None
    finally:
        reset_routed_model_capture(token)


@pytest.mark.parametrize("response_model", [None, "", "   ", {"model": "bad"}, "x" * 241])
def test_missing_or_unsafe_response_model_is_not_claimed(
    capture_without_real_plugins,
    response_model,
):
    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    try:
        _observe_post_api_request(
            platform="webui",
            session_id="session-a",
            task_id="session-a",
            response_model=response_model,
            provider="custom",
        )
        assert snapshot_routed_model_capture() is None
    finally:
        reset_routed_model_capture(token)


def test_contextvars_isolate_simultaneous_webui_turns(capture_without_real_plugins):
    def run(session_id, stream_id, routed_model):
        token = begin_routed_model_capture(
            session_id=session_id,
            stream_id=stream_id,
            task_id=session_id,
            requested_model="auto",
            requested_provider="TokenTable",
        )
        try:
            _observe_post_api_request(
                platform="webui",
                session_id=session_id,
                task_id=session_id,
                response_model=routed_model,
                provider="custom",
            )
            return snapshot_routed_model_capture()
        finally:
            reset_routed_model_capture(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "session-a", "stream-a", "model-a")
        second = pool.submit(run, "session-b", "stream-b", "model-b")

    assert first.result()["used_model"] == "model-a"
    assert second.result()["used_model"] == "model-b"


def test_named_custom_provider_preserves_display_name():
    config = {
        "custom_providers": [
            {"name": "TokenTable", "base_url": "https://example.invalid/v1"}
        ]
    }
    assert provider_display_name("custom:tokentable", "custom", config) == "TokenTable"
    assert provider_display_name(None, "openai-codex", config) == "openai-codex"


def test_observer_registration_is_idempotent(monkeypatch):
    manager = types.SimpleNamespace(_hooks={})
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.discover_plugins = lambda: None
    plugins.get_plugin_manager = lambda: manager
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    _install_post_api_request_observer()
    _install_post_api_request_observer()

    assert manager._hooks["post_api_request"] == [_observe_post_api_request]


def test_unavailable_plugin_manager_fails_open(monkeypatch):
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.discover_plugins = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    reset_routed_model_capture(token)
```

- [ ] **Step 2: Prepare the ignored local test environment**

Run the commands in “Execution Runtime Preparation”. Confirm:

```text
.venv\Scripts\python.exe -m pytest --version
npm --version
node --version
```

Expected: all commands exit `0`; `git status --short` still lists only the new tracked test file plus the already committed docs.

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_routed_model_observability.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'api.routed_model_observability'`. Save this exact RED command, exit code, and failure in the final evidence.

- [ ] **Step 4: Implement the minimal capture module**

Create `api/routed_model_observability.py`:

```python
"""WebUI-only capture of routed models reported by Hermes API responses."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging
import threading
from typing import Any

from api.config import _custom_provider_slug_from_name


_MAX_SCALAR_CHARS = 240
logger = logging.getLogger(__name__)
_CAPTURE: ContextVar["RoutedModelCapture | None"] = ContextVar(
    "webui_routed_model_capture",
    default=None,
)
_INSTALL_LOCK = threading.RLock()


def _safe_scalar(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_SCALAR_CHARS:
        return None
    return text


def provider_display_name(
    provider_context: str | None,
    resolved_provider: str | None,
    config: dict | None,
) -> str:
    context = _safe_scalar(provider_context)
    resolved = _safe_scalar(resolved_provider)
    if context and context.lower().startswith("custom:"):
        target = context.lower()
        custom_providers = (config or {}).get("custom_providers", [])
        if isinstance(custom_providers, list):
            for entry in custom_providers:
                if not isinstance(entry, dict):
                    continue
                name = _safe_scalar(entry.get("name"))
                if name and _custom_provider_slug_from_name(name) == target:
                    return name
    return context or resolved or ""


@dataclass
class RoutedModelCapture:
    session_id: str
    stream_id: str
    task_id: str
    requested_model: str
    requested_provider: str
    response_model: str | None = None
    response_provider: str | None = None

    def snapshot(self) -> dict | None:
        if not self.response_model:
            return None
        used_provider = self.requested_provider or self.response_provider or ""
        payload = {
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "used_model": self.response_model,
            "used_provider": used_provider,
            "source": "openai-compatible-sse",
        }
        return {key: value for key, value in payload.items() if value}


def _observe_post_api_request(
    *,
    platform=None,
    session_id=None,
    task_id=None,
    response_model=None,
    provider=None,
    **_kwargs,
) -> None:
    capture = _CAPTURE.get()
    if capture is None:
        return
    if str(platform or "").strip().lower() != "webui":
        return
    if str(session_id or "") != capture.session_id:
        return
    if str(task_id or "") != capture.task_id:
        return
    model = _safe_scalar(response_model)
    if model is None:
        return
    capture.response_model = model
    safe_provider = _safe_scalar(provider)
    if safe_provider:
        capture.response_provider = safe_provider


def _install_post_api_request_observer() -> None:
    try:
        from hermes_cli import plugins

        plugins.discover_plugins()
        manager = plugins.get_plugin_manager()
        with _INSTALL_LOCK:
            hooks = getattr(manager, "_hooks", None)
            if not isinstance(hooks, dict):
                return
            callbacks = hooks.setdefault("post_api_request", [])
            if _observe_post_api_request not in callbacks:
                callbacks.append(_observe_post_api_request)
    except Exception:
        logger.debug("Routed-model lifecycle observer unavailable", exc_info=True)


def begin_routed_model_capture(
    *,
    session_id: str,
    stream_id: str,
    task_id: str,
    requested_model: str | None,
    requested_provider: str | None,
) -> Token:
    _install_post_api_request_observer()
    capture = RoutedModelCapture(
        session_id=str(session_id),
        stream_id=str(stream_id),
        task_id=str(task_id),
        requested_model=_safe_scalar(requested_model) or "",
        requested_provider=_safe_scalar(requested_provider) or "",
    )
    return _CAPTURE.set(capture)


def snapshot_routed_model_capture() -> dict | None:
    capture = _CAPTURE.get()
    return capture.snapshot() if capture is not None else None


def reset_routed_model_capture(token: Token) -> None:
    _CAPTURE.reset(token)
```

- [ ] **Step 5: Run adapter tests and verify GREEN**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_routed_model_observability.py -q
```

Expected: all adapter tests pass.

- [ ] **Step 6: Commit the adapter atomically**

```powershell
git add -- api/routed_model_observability.py tests/test_routed_model_observability.py
git diff --cached --check
git commit -m "feat: capture routed model lifecycle metadata"
```

Expected: the commit contains exactly the two files listed above.

### Task 2: Integrate capture with streaming persistence and replay

**Files:**
- Modify: `api/streaming.py:49-53,1097-1102,4490-4510,5378-5410,5935-5950,6211-6640,7380-7410`
- Modify: `tests/test_routed_model_observability.py`
- Modify: `tests/test_732_gateway_routing_metadata.py`

- [ ] **Step 1: Add failing normalization and streaming integration tests**

Append to `tests/test_732_gateway_routing_metadata.py`:

```python
def test_sse_routing_source_is_safely_normalized_and_persisted():
    routing = _normalize_gateway_routing_metadata(
        {
            "requested_model": "auto",
            "requested_provider": "TokenTable",
            "used_model": "gpt-5.6-sol",
            "used_provider": "TokenTable",
            "source": "openai-compatible-sse",
            "api_key": "must-not-survive",
        }
    )

    assert routing == {
        "requested_model": "auto",
        "requested_provider": "TokenTable",
        "used_model": "gpt-5.6-sol",
        "used_provider": "TokenTable",
        "source": "openai-compatible-sse",
        "provider_changed": False,
        "model_changed": True,
        "has_failover": False,
    }
    assert "must-not-survive" not in repr(routing)


def test_routed_model_capture_cleanup_is_in_streaming_outer_finally():
    run_source = STREAMING_PY[STREAMING_PY.index("def _run_agent_streaming("):]
    reset_pos = run_source.rfind("reset_routed_model_capture(")
    clear_env_pos = run_source.rfind("_clear_thread_env()")
    assert reset_pos > 0
    assert clear_env_pos > reset_pos
```

Also update `test_session_persists_latest_gateway_routing_and_history_across_reload`
so its input routing object includes
`"source": "openai-compatible-sse"`, and assert that exact source remains in
`reloaded.gateway_routing`, `reloaded.gateway_routing_history[0]`, and
`reloaded.messages[-1]["_gatewayRouting"]`.

Append a full fake-agent integration test to `tests/test_routed_model_observability.py`. Use a real `Session`, patch only its disk save, and have the fake AIAgent fire the same lifecycle callback as Hermes Agent:

```python
def test_streaming_persists_sse_routed_model_in_message_session_and_done(
    monkeypatch,
    tmp_path,
):
    import queue

    import api.routed_model_observability as observation
    import api.streaming as streaming
    from api.models import Session

    monkeypatch.setattr(observation, "_install_post_api_request_observer", lambda: None)

    stream_id = "stream-routed-model"
    session = Session(session_id="session-routed-model", title="Routing")
    session.model = "auto"
    session.model_provider = "custom:tokentable"
    session.messages = []
    session.context_messages = []
    session.active_stream_id = stream_id
    monkeypatch.setattr(session, "save", lambda: None)

    class FakeAgent:
        def __init__(self, model=None, provider=None, base_url=None, session_id=None, **_kwargs):
            self.model = model
            self.provider = provider
            self.base_url = base_url
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 11
            self.session_completion_tokens = 7
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            observation._observe_post_api_request(
                platform="webui",
                session_id=self.session_id,
                task_id=kwargs["task_id"],
                response_model="gpt-5.6-sol",
                provider="custom",
            )
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "routed answer"},
                ]
            }

        def interrupt(self, _message):
            return None

    runtime = types.ModuleType("hermes_cli.runtime_provider")
    runtime.resolve_runtime_provider = lambda requested=None: {
        "provider": "custom",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-only",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.runtime_provider = runtime
    hermes_state = types.ModuleType("hermes_state")
    hermes_state.SessionDB = lambda *_args, **_kwargs: None
    injected = {
        "hermes_cli": hermes_cli,
        "hermes_cli.runtime_provider": runtime,
        "hermes_state": hermes_state,
    }
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in injected}
    sys.modules.update(injected)
    events = queue.Queue()
    try:
        monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
        monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        monkeypatch.setattr(
            streaming,
            "resolve_model_provider",
            lambda _model: ("auto", "custom:tokentable", "https://example.invalid/v1"),
        )
        monkeypatch.setattr(
            streaming,
            "resolve_custom_provider_connection",
            lambda _provider: ("test-only", "https://example.invalid/v1"),
        )
        monkeypatch.setattr(
            streaming,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "TokenTable", "base_url": "https://example.invalid/v1"}
                ]
            },
        )
        monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        streaming.STREAMS[stream_id] = events

        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text="route this",
            model="auto",
            workspace=str(tmp_path),
            stream_id=stream_id,
            model_provider="custom:tokentable",
        )
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    expected = {
        "requested_model": "auto",
        "requested_provider": "TokenTable",
        "used_model": "gpt-5.6-sol",
        "used_provider": "TokenTable",
        "source": "openai-compatible-sse",
        "provider_changed": False,
        "model_changed": True,
        "has_failover": False,
    }
    done = next(payload for event, payload in list(events.queue) if event == "done")
    assert session.messages[-1]["_gatewayRouting"] == expected
    assert session.gateway_routing == expected
    assert session.gateway_routing_history == [expected]
    assert done["usage"]["gateway_routing"] == expected
    assert done["session"]["messages"][-1]["_gatewayRouting"] == expected
    assert all(
        "_gatewayRouting" not in message
        for message in streaming._sanitize_messages_for_api(session.messages)
    )
    assert observation.snapshot_routed_model_capture() is None
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_routed_model_observability.py tests/test_732_gateway_routing_metadata.py -q
```

Expected failures:

- normalized routing omits `source`;
- streaming `done` payload lacks `gpt-5.6-sol` routing metadata.

- [ ] **Step 3: Wire capture into the streaming worker**

In `api/streaming.py`, import:

```python
from api.routed_model_observability import (
    begin_routed_model_capture,
    provider_display_name,
    reset_routed_model_capture,
    snapshot_routed_model_capture,
)
```

Add `'source'` to `_GATEWAY_ROUTING_TOP_LEVEL_KEYS`.

Near the existing `agent = None` initialization, add:

```python
    _routed_model_capture_token = None
```

Immediately before the first non-ephemeral `agent.run_conversation(...)`, start the capture once:

```python
            if not ephemeral and _routed_model_capture_token is None:
                _routed_model_capture_token = begin_routed_model_capture(
                    session_id=session_id,
                    stream_id=stream_id,
                    task_id=session_id,
                    requested_model=resolved_model or model,
                    requested_provider=provider_display_name(
                        provider_context,
                        resolved_provider,
                        _cfg,
                    ),
                )
```

At the existing `_gateway_routing = _extract_gateway_routing_metadata(...)` block, retain explicit gateway metadata as the first choice and use the SSE observation only when it is absent:

```python
                _gateway_routing = _extract_gateway_routing_metadata(
                    agent,
                    result,
                    requested_model=resolved_model or model,
                    requested_provider=provider_display_name(
                        provider_context,
                        resolved_provider,
                        _cfg,
                    ),
                )
                if not _gateway_routing:
                    _observed_routing = snapshot_routed_model_capture()
                    if _observed_routing:
                        _gateway_routing = _normalize_gateway_routing_metadata(
                            _observed_routing,
                            requested_model=resolved_model or model,
                            requested_provider=provider_display_name(
                                provider_context,
                                resolved_provider,
                                _cfg,
                            ),
                        )
```

In `_run_agent_streaming`'s existing outer `finally`, before clearing thread-local environment state, reset only the token created by this worker:

```python
        if _routed_model_capture_token is not None:
            try:
                reset_routed_model_capture(_routed_model_capture_token)
            except Exception:
                logger.debug(
                    "Failed to reset routed-model capture for stream %s",
                    stream_id,
                    exc_info=True,
                )
```

Do not add a separate SSE event. The existing saved message/session and terminal `done` event remain the single durable path, and `put(...)` continues journaling that terminal payload.

- [ ] **Step 4: Run focused persistence tests and verify GREEN**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_routed_model_observability.py tests/test_732_gateway_routing_metadata.py -q
```

Expected: all tests pass, including the exact message/session/`done` assertions.

- [ ] **Step 5: Run related streaming, stale-owner, and replay suites**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_run_journal.py tests/test_run_journal_streaming_static.py tests/test_issue765_streaming_persistence.py tests/test_stale_stream_writeback.py tests/test_cancel_stream_owner_guard.py -q
```

Expected: all selected suites pass. A failure must be diagnosed before proceeding; do not weaken stale-writeback or journal assertions.

- [ ] **Step 6: Commit streaming integration atomically**

```powershell
git add -- api/streaming.py tests/test_routed_model_observability.py tests/test_732_gateway_routing_metadata.py
git diff --cached --check
git commit -m "feat: persist SSE routed model metadata"
```

Expected: no TokenTable, Hermes Agent, config, environment, or schema file is staged.

### Task 3: Render Requested, Routed, and Provider in the assistant footer

**Files:**
- Modify: `static/ui.js:3098-3140,8552-8620`
- Modify: `static/style.css:4407-4430`
- Modify: `tests/test_732_gateway_routing_metadata.py`

- [ ] **Step 1: Add failing frontend contract tests**

Append to `tests/test_732_gateway_routing_metadata.py`:

```python
def test_sse_routed_model_footer_uses_three_safe_labelled_fields():
    assert "function _routedModelObservationFields" in UI_JS
    assert "Requested:" in UI_JS
    assert "Routed:" in UI_JS
    assert "Provider:" in UI_JS
    assert "msg-routed-model-inline" in UI_JS
    assert "msg-routed-model-field" in UI_JS
    assert ".msg-routed-model-inline" in STYLE_CSS

    start = UI_JS.index("function _appendRoutedModelObservation")
    end = UI_JS.index("function ", start + len("function "))
    body = UI_JS[start:end]
    assert ".textContent=" in body.replace(" ", "")
    assert ".innerHTML" not in body


def test_non_sse_gateway_metadata_keeps_existing_footer_path():
    assert "routing.source==='openai-compatible-sse'" in UI_JS.replace(" ", "")
    assert "_formatGatewayModelLabel" in UI_JS
    assert "_gatewayRoutingFailoverText" in UI_JS
    assert "_gatewayModelWarningText" in UI_JS
```

- [ ] **Step 2: Run frontend contract tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_732_gateway_routing_metadata.py -q
```

Expected: failures report missing `_routedModelObservationFields`, `_appendRoutedModelObservation`, and CSS selectors.

- [ ] **Step 3: Add the pure field formatter and safe DOM renderer**

In `static/ui.js`, after `_gatewayProviderName`, add:

```javascript
function _routedModelObservationFields(routing){
  if(!routing||routing.source!=='openai-compatible-sse')return[];
  const requested=String(routing.requested_model||'').trim();
  const routed=String(routing.used_model||'').trim();
  const provider=_gatewayProviderName(routing.used_provider||routing.requested_provider);
  if(!routed)return[];
  return [
    {label:'Requested',value:requested},
    {label:'Routed',value:routed},
    {label:'Provider',value:provider},
  ].filter(field=>field.value);
}
function _appendRoutedModelObservation(target,routing){
  const fields=_routedModelObservationFields(routing);
  if(!target||!fields.length)return false;
  const group=document.createElement('span');
  group.className='msg-routed-model-inline';
  for(const field of fields){
    const item=document.createElement('span');
    item.className='msg-routed-model-field';
    item.textContent=`${field.label}: ${field.value}`;
    group.appendChild(item);
  }
  target.appendChild(group);
  return true;
}
```

In the existing assistant-footer loop, identify SSE observations once and suppress the older duplicate one-line model/switch labels for that source:

```javascript
      const routing=msg._gatewayRouting||null;
      const isSseObservation=!!(routing&&routing.source==='openai-compatible-sse');
      const gatewayText=isSseObservation?'':_formatGatewayModelLabel(S.session&&S.session.model||'', '', routing);
      const failoverText=_gatewayRoutingFailoverText(routing);
      const modelWarningText=isSseObservation?'':_gatewayModelWarningText(routing);
      const routedModelFields=_routedModelObservationFields(routing);
```

Include `routedModelFields.length` in the footer presence guard. Before the existing gateway/duration fragments, create a fragment container and call the safe renderer:

```javascript
      if(routedModelFields.length){
        const routed=document.createElement('span');
        if(_appendRoutedModelObservation(routed,routing))fragments.push(routed.firstChild);
      }
```

Extend the existing `targetFoot.querySelector(...)` duplicate guard with
`.msg-routed-model-inline` so repeated `renderMessages()` calls never append a
second routed-model group to the same assistant footer.

Keep the existing generic gateway, failover, duration, usage, and warning code unchanged for non-SSE metadata.

- [ ] **Step 4: Add responsive footer CSS**

Extend the existing footer metadata selector in `static/style.css` and add:

```css
.msg-foot-with-usage {
  flex-wrap: wrap;
  min-width: 0;
}
.msg-routed-model-inline {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  min-width: 0;
  max-width: 100%;
}
.msg-routed-model-field {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  opacity: .78;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .msg-routed-model-inline {
    flex: 1 1 100%;
    width: 100%;
  }
  .msg-routed-model-field {
    flex: 1 1 100%;
  }
}
```

- [ ] **Step 5: Run frontend tests, syntax check, and runtime lint**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_732_gateway_routing_metadata.py tests/test_mobile_layout.py -q
node --check static/ui.js
npm run lint:runtime
```

Expected: all commands exit `0`; ESLint reports no runtime-guard errors.

- [ ] **Step 6: Commit UI changes atomically**

```powershell
git add -- static/ui.js static/style.css tests/test_732_gateway_routing_metadata.py
git diff --cached --check
git commit -m "feat: show routed model in assistant footer"
```

Expected: the commit contains exactly the three listed files.

### Task 4: Run the complete scoped verification gate

**Files:**
- No code changes expected.

- [ ] **Step 1: Re-run the RED-to-GREEN feature suites from a clean process**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_routed_model_observability.py tests/test_732_gateway_routing_metadata.py -q
```

Expected: all feature tests pass.

- [ ] **Step 2: Run the consolidated related regression suite**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_run_journal.py tests/test_run_journal_streaming_static.py tests/test_issue765_streaming_persistence.py tests/test_stale_stream_writeback.py tests/test_cancel_stream_owner_guard.py tests/test_mobile_layout.py tests/test_static_js_runtime_lint.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run changed-line Python lint and JavaScript validation**

```powershell
& '.\.venv\Scripts\python.exe' scripts/ruff_lint.py --diff db2583cb
node --check static/ui.js
npm run lint:runtime
git diff --check db2583cb...HEAD
```

Expected: every command exits `0`. Do not run unsafe auto-fix or broad formatting.

- [ ] **Step 4: Audit the exact diff boundary**

```powershell
git status --short --branch
git diff --stat db2583cb...HEAD
git diff --name-only db2583cb...HEAD
git log --oneline db2583cb..HEAD
```

Expected tracked implementation scope:

```text
api/routed_model_observability.py
api/streaming.py
docs/superpowers/plans/2026-08-14-routed-model-observability.md
docs/superpowers/specs/2026-08-14-routed-model-observability-design.md
static/style.css
static/ui.js
tests/test_732_gateway_routing_metadata.py
tests/test_routed_model_observability.py
```

The worktree must be clean after commits. The dirty master checkout must still show only the five pre-existing user changes at `db2583cb`.

### Task 5: Perform isolated desktop and narrow browser QA

**Files:**
- Evidence only under `C:\Users\Joa\.hermes\workspace\tokentable_ops\evidence`.
- No tracked code changes expected.

- [ ] **Step 1: Run the standard agent-free browser smoke test**

```powershell
& '.\.venv\Scripts\python.exe' tests/browser_smoke.py
```

Expected: exit `0` and `BROWSER SMOKE PASSED`; the script uses its own temporary `HERMES_HOME` and terminates only the server it created.

- [ ] **Step 2: Launch a separate isolated QA server**

First confirm port `18797` is unused. Create a task-specific temporary state directory and strip provider keys from the child environment. Start only this worktree's server with `Start-Process -WindowStyle Hidden`, recording its PID and logs in the temporary directory:

```powershell
$qaState = Join-Path $env:TEMP 'hermes-webui-routed-model-qa'
New-Item -ItemType Directory -Path $qaState -Force | Out-Null
$env:HERMES_WEBUI_PORT = '18797'
$env:HERMES_WEBUI_HOST = '127.0.0.1'
$env:HERMES_WEBUI_STATE_DIR = $qaState
$env:HERMES_HOME = $qaState
$env:HERMES_BASE_HOME = $qaState
$env:HERMES_WEBUI_SKIP_ONBOARDING = '1'
$env:HERMES_WEBUI_AGENT_DIR = Join-Path $qaState 'no-agent'
Get-ChildItem Env: | Where-Object Name -Like '*_API_KEY' | ForEach-Object { Remove-Item "Env:$($_.Name)" }
$qaProc = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList 'server.py' -WorkingDirectory (Get-Location) -RedirectStandardOutput (Join-Path $qaState 'stdout.log') -RedirectStandardError (Join-Path $qaState 'stderr.log') -PassThru -WindowStyle Hidden
```

Expected: `http://127.0.0.1:18797/health` returns `200`. This process is not the user's active WebUI.

- [ ] **Step 3: Verify desktop presentation with synthetic safe state**

Using Playwright/browser automation, open `http://127.0.0.1:18797/` at `1440x900`. In the page context set only synthetic UI state:

```javascript
S.session={model:'auto',gateway_routing:null,gateway_routing_history:[]};
S.messages=[{
  role:'assistant',
  content:'Synthetic routed-model QA response.',
  _gatewayRouting:{
    requested_model:'auto',
    requested_provider:'TokenTable',
    used_model:'gpt-5.6-sol',
    used_provider:'TokenTable',
    source:'openai-compatible-sse',
    provider_changed:false,
    model_changed:true,
    has_failover:false
  }
}];
renderMessages();
```

Assert `.msg-routed-model-inline` has text containing all three values and that no duplicate `Model switched` or generic `via TokenTable` fragment appears. Save:

```text
C:\Users\Joa\.hermes\workspace\tokentable_ops\evidence\2026-08-14-hermes-webui-routed-model-desktop.png
```

- [ ] **Step 4: Verify narrow presentation and overflow**

Resize to `390x844`, re-render the same synthetic state, and assert:

```javascript
document.documentElement.scrollWidth <= document.documentElement.clientWidth
```

Confirm the three fields wrap without clipping. Save:

```text
C:\Users\Joa\.hermes\workspace\tokentable_ops\evidence\2026-08-14-hermes-webui-routed-model-narrow.png
```

- [ ] **Step 5: Stop only the isolated QA process**

```powershell
Stop-Process -Id $qaProc.Id
Wait-Process -Id $qaProc.Id -ErrorAction SilentlyContinue
```

Expected: only the PID created in Step 2 is stopped. Do not touch any other WebUI, gateway, PM2, or TokenTable process.

### Task 6: Produce the pre-push completion report

**Files:**
- No code changes expected.

- [ ] **Step 1: Capture final branch evidence**

```powershell
git status --short --branch
git rev-parse HEAD
git log --oneline db2583cb..HEAD
git diff --stat db2583cb...HEAD
```

- [ ] **Step 2: Reconfirm dirty master was not touched**

```powershell
git -C 'C:\Users\Joa\hermes-webui' -c safe.directory=C:/Users/Joa/hermes-webui status --short --branch
git -C 'C:\Users\Joa\hermes-webui' -c safe.directory=C:/Users/Joa/hermes-webui rev-parse HEAD
```

Expected master evidence:

```text
## master...origin/master [ahead 1]
 M api/models.py
 M api/routes.py
 M api/streaming.py
 M api/updates.py
?? api/active_model_state.py
db2583cb7e864037c3b7613f37f4df108d0d7225
```

- [ ] **Step 3: Report without publishing**

Report in Taiwan Traditional Chinese:

- branch, final HEAD, and worktree path;
- exact diff file list and commit list;
- recorded RED command/failure and GREEN command/result;
- related pytest, Ruff, Node, ESLint, browser smoke, desktop, and narrow results;
- evidence image paths;
- confirmation that TokenTable/Hermes Agent were unchanged and no live process was restarted;
- explicit statement that push, PR, merge, and deploy remain `NOT RUN`.

Stop at that checkpoint and wait for separate literal authorization before any publishing or deployment action.
