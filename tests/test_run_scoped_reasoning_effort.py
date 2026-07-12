import json
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 1
    cursor = brace + 1
    while depth and cursor < len(source):
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    return source[brace + 1 : cursor - 1]


def test_browser_effort_validation_is_strict_and_optional():
    from api.routes import _normalize_run_reasoning_effort

    assert _normalize_run_reasoning_effort(None) is None
    assert _normalize_run_reasoning_effort("") is None
    assert _normalize_run_reasoning_effort(" XHigh ") == "xhigh"
    assert _normalize_run_reasoning_effort("none") == "none"

    with pytest.raises(ValueError, match="Unknown reasoning effort"):
        _normalize_run_reasoning_effort("extreme")


def test_gateway_request_effort_wins_over_shared_profile_default(monkeypatch):
    import api.gateway_chat as gateway_chat

    monkeypatch.setattr(
        gateway_chat,
        "coerce_reasoning_effort_for_model",
        lambda effort, *args, **kwargs: effort,
    )
    cfg = {"agent": {"reasoning_effort": "medium"}}

    assert gateway_chat._gateway_reasoning_effort_for_request(
        cfg,
        model="test-model",
        model_provider="test-provider",
        reasoning_effort="xhigh",
    ) == "xhigh"
    assert gateway_chat._gateway_reasoning_effort_for_request(
        cfg,
        model="test-model",
        model_provider="test-provider",
    ) == "medium"


def test_concurrent_session_starts_keep_independent_efforts(tmp_path, monkeypatch):
    from api.models import Session
    import api.routes as routes

    started = []

    class CapturingThread:
        def __init__(self, *, target, args, kwargs, daemon):
            started.append({"target": target, "args": args, "kwargs": kwargs, "daemon": daemon})

        def start(self):
            return None

    monkeypatch.setattr(Session, "save", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: True)
    monkeypatch.setattr(routes.threading, "Thread", CapturingThread)

    responses = []
    try:
        for session_id, effort in (("run-effort-low", "low"), ("run-effort-xhigh", "xhigh")):
            responses.append(
                routes._start_chat_stream_for_session(
                    Session(session_id=session_id, title="Untitled"),
                    msg=f"Use {effort}",
                    attachments=[],
                    workspace=str(tmp_path),
                    model="test-model",
                    model_provider="test-provider",
                    reasoning_effort=effort,
                )
            )
    finally:
        for response in responses:
            stream_id = response.get("stream_id")
            routes.STREAMS.pop(stream_id, None)
            routes.unregister_stream_owner(stream_id)

    assert [call["kwargs"]["reasoning_effort"] for call in started] == ["low", "xhigh"]
    assert all(call["target"] is routes._run_gateway_chat_streaming for call in started)


def test_browser_snapshots_immediate_and_queued_turn_effort():
    messages = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    commands = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")

    assert "reasoning_effort:reasoningEffortForSend||undefined" in messages
    assert messages.count("reasoning_effort:reasoningEffortForSend||undefined") >= 5
    assert "send({reasoningEffort:next.reasoning_effort})" in ui
    assert "window.getComposerReasoningEffortForRun=getComposerReasoningEffortForRun" in ui
    assert "typeof getComposerReasoningEffortForRun==='function'" in commands
    assert "reasoning_effort:ownerReasoningEffort||undefined" in commands


def test_queue_helper_snapshots_active_effort_without_overwriting_explicit_value():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    helper = _function_body(ui, "_reasoningEffortForQueuedMessage")
    script = textwrap.dedent(
        f"""
        const S = {{session: {{session_id: 'active'}}}};
        const window = {{getComposerReasoningEffortForRun: () => 'low'}};
        function resolve(sid, payload) {{{helper}}}
        console.log(JSON.stringify([
          resolve('active', {{}}),
          resolve('active', {{reasoning_effort: 'xhigh'}}),
          resolve('background', {{}}) ?? null
        ]));
        """
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["low", "xhigh", None]
