"""#7237 round 8 — release-gate review findings (nesquena-hermes 2026-10-15).

Finding 1 (CORE): ``_agent_replay_settle_projection`` models only the
installed Agent's pass-0 assistant merge and pass-3 user merge. Native pass 1
(stray / duplicate tool-result removal) and pass 2 (unanswered tool-call
pruning plus payload-empty drop) are not modeled, so the strict-prefix proof
refuses the genuine suffix and the settle fails closed: the current exchange
survives in display but is dropped from the saved model context. Repro shapes:
``duplicate-tool`` (two tool results for one call) and ``codex-interim`` (a
Codex interim assistant row between the call row and its result).

Finding 2: ``_replay_user_row_is_plain_mergeable`` recognizes only the webui
marker prefixes. The installed ``_merge_consecutive_users`` refuses to fold
whenever ``split_user_originated_turn(prev)[0] is not None``, which also
covers the legacy ``[CONTEXT SUMMARY]:`` handoff and a merged carrier
containing ``[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]``. The user
fold then welds the carrier into the next user row, the strict-prefix proof
refuses the changed prefix, and the current exchange is lost. Repro shapes:
``legacy-carrier`` and ``merged-carrier``.

Each end-to-end test drives the REAL streaming worker
(``_run_agent_streaming``) or a REAL synchronous ``POST /api/chat`` against
``server.Handler`` on an ephemeral port, followed by a cold
``models.Session.load``. Only inference is substituted: the fake Agent's
``run_conversation`` runs the INSTALLED
``agent.agent_runtime_helpers.repair_message_sequence`` as its pre-call role
repair, exactly like the production Agent.
"""

from __future__ import annotations

import copy
import json
import http.client
import queue
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CALL_X = {
    "id": "call-x",
    "type": "function",
    "function": {"name": "tool_f", "arguments": "{}"},
}

# (prior history rows, prompt, answer) per review shape.
def _tool_history(*tool_rows):
    return [
        {"role": "user", "content": "OLD_Q", "timestamp": 1.0},
        {"role": "assistant", "content": "", "tool_calls": [copy.deepcopy(CALL_X)], "timestamp": 2.0},
        *tool_rows,
        {"role": "assistant", "content": "OLD_ANSWER", "timestamp": 5.0},
    ]


SHAPES = {
    # Finding 1 — tool shapes (prompt CURRENT / reply CURRENT_ANSWER).
    "duplicate-tool": (
        _tool_history(
            {"role": "tool", "content": "RESULT_ONE", "tool_call_id": "call-x", "timestamp": 3.0},
            {"role": "tool", "content": "RESULT_TWO", "tool_call_id": "call-x", "timestamp": 4.0},
        ),
        "CURRENT",
        "CURRENT_ANSWER",
    ),
    "codex-interim": (
        _tool_history(
            {"role": "assistant", "content": "interim", "finish_reason": "incomplete", "timestamp": 3.0},
            {"role": "tool", "content": "RESULT_ONE", "tool_call_id": "call-x", "timestamp": 4.0},
        ),
        "CURRENT",
        "CURRENT_ANSWER",
    ),
    # Finding 2 — carrier shapes (prompt U3 / reply ANSWER3).
    "legacy-carrier": (
        [
            {"role": "user", "content": "[CONTEXT SUMMARY]: prior compacted handoff", "timestamp": 1.0},
            {"role": "assistant", "content": "", "_partial": True, "timestamp": 2.0},
            {"role": "user", "content": "U2", "timestamp": 3.0},
            {"role": "assistant", "content": "A2", "timestamp": 4.0},
        ],
        "U3",
        "ANSWER3",
    ),
    "merged-carrier": (
        [
            {
                "role": "user",
                "content": (
                    "PRIOR STUFF\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
                    "[CONTEXT SUMMARY]: prior compacted handoff"
                ),
                "timestamp": 1.0,
            },
            {"role": "assistant", "content": "", "_partial": True, "timestamp": 2.0},
            {"role": "user", "content": "U2", "timestamp": 3.0},
            {"role": "assistant", "content": "A2", "timestamp": 4.0},
        ],
        "U3",
        "ANSWER3",
    ),
    # Positive controls — must stay green on both paths.
    "single-result-tool": (
        _tool_history(
            {"role": "tool", "content": "RESULT_ONE", "tool_call_id": "call-x", "timestamp": 3.0},
        ),
        "CURRENT",
        "CURRENT_ANSWER",
    ),
    "plain-user-round7": (
        [
            {"role": "user", "content": "U1", "timestamp": 1.0},
            {"role": "assistant", "content": "", "_partial": True, "timestamp": 2.0},
            {"role": "user", "content": "U2", "timestamp": 3.0},
            {"role": "assistant", "content": "A2", "timestamp": 4.0},
        ],
        "U3",
        "ANSWER3",
    ),
}

TOOL_SHAPES = ("duplicate-tool", "codex-interim")
CARRIER_SHAPES = ("legacy-carrier", "merged-carrier")
CONTROLS = ("single-result-tool", "plain-user-round7")
ALL_SHAPES = TOOL_SHAPES + CARRIER_SHAPES + CONTROLS


def _installed_repair():
    """The installed Agent's pre-call role repair (test skips without it)."""
    return pytest.importorskip(
        "agent.agent_runtime_helpers",
        reason="hermes-agent modules not importable",
    ).repair_message_sequence


@pytest.fixture(autouse=True)
def _agent_checkout_importable(monkeypatch):
    """Keep the Agent checkout importable for the whole test body.

    ``repair_message_sequence``'s pass-3 lazily imports sibling Agent modules
    (``agent.conversation_loop`` → ``hermes_logging``), and the installed
    ``is_compaction_summary_message`` runs from the same checkout. The suite's
    sys.path isolation guard restores an earlier snapshot mid-run (shard-order
    dependent — the same hazard ``test_gateway_sync`` documents), so prepend
    the checkout BEFORE importing anything from it.
    """
    from api import config as config_mod

    agent_dir = getattr(config_mod, "_AGENT_DIR", None)
    if agent_dir is not None:
        monkeypatch.syspath_prepend(str(agent_dir))
    pytest.importorskip(
        "agent.agent_runtime_helpers",
        reason="hermes-agent modules not importable",
    )


def _sanitize(messages, **kwargs):
    from api.streaming import _sanitize_messages_for_agent

    return _sanitize_messages_for_agent(copy.deepcopy(messages), **kwargs)


def _row_texts(rows):
    return [row.get("content") for row in rows if isinstance(row, dict)]


def _fake_agent_class(sid, answer):
    """A fake Agent whose only substitution is inference.

    ``run_conversation`` mirrors the installed Agent: it shallow-copies the
    conversation list, appends the freshly submitted prompt as a user row, runs
    the INSTALLED ``repair_message_sequence`` as its pre-call role repair (the
    production pre-call belt), and appends the completed reply on top.
    """

    repair = _installed_repair()

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.session_id = sid
            self.platform = "webui"
            self.model = "test-model"
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(
            self,
            user_message=None,
            conversation_history=None,
            persist_user_message=None,
            **_kwargs,
        ):
            prompt = persist_user_message if persist_user_message is not None else user_message
            msgs = list(conversation_history or [])
            msgs.append({"role": "user", "content": prompt})
            repair(None, msgs)
            msgs.append({"role": "assistant", "content": answer})
            current_user_idx = max(
                (idx for idx, row in enumerate(msgs) if isinstance(row, dict) and row.get("role") == "user"),
                default=len(msgs),
            )
            return {
                "messages": msgs,
                "final_response": answer,
                "completed": True,
                "current_turn_user_idx": current_user_idx,
                "turn_id": "round8-turn",
            }

    return FakeAgent


def _state_patches(tmp_path, monkeypatch):
    from api import config as config_mod
    from api import models, routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(
        config_mod, "get_config", lambda: {"model": "test-model", "provider": "test-provider"}
    )
    monkeypatch.setattr(
        routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"}
    )
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    monkeypatch.setattr(config_mod, "_resolve_cli_toolsets", lambda *a, **k: [])
    return state_dir


def _new_session(tmp_path, sid, history):
    from api import models

    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=list(history),
        context_messages=list(history),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)
    return session


def _worker_send(tmp_path, monkeypatch, sid, history, prompt, answer):
    """One real ``_run_agent_streaming`` send; returns the cold-reloaded session.

    Mirrors the proven round-4 composed-session harness patch set exactly (the
    worker path needs no routes-level patches).
    """
    from api import config as config_mod
    from api import models, profiles, routes, streaming

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(config_mod, "get_config", lambda: {})
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config_mod, "_resolve_cli_toolsets", lambda *a, **k: [])
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _: None)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *a, **k: ("test-model", None, None))
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _fake_agent_class(sid, answer))

    session = _new_session(tmp_path, sid, history)
    config_mod.SESSION_AGENT_CACHE.clear()
    models.SESSIONS[sid] = session
    stream = sid + "-send"
    routes._prepare_chat_start_session_for_stream(
        session,
        msg=prompt,
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id=stream,
        started_at=10.0,
    )
    config_mod.STREAMS[stream] = queue.Queue()
    streaming._run_agent_streaming(sid, prompt, "test-model", str(tmp_path), stream, [])
    return models.Session.load(sid)


def _sync_http_send(tmp_path, monkeypatch, sid, history, prompt, answer):
    """One real synchronous ``POST /api/chat`` against ``server.Handler`` on an
    ephemeral port; returns the cold-reloaded session."""
    import server as server_mod
    from api import models

    _state_patches(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=_fake_agent_class(sid, answer)))
    _new_session(tmp_path, sid, history)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=60)
        body = json.dumps(
            {"session_id": sid, "message": prompt, "workspace": str(tmp_path)}
        ).encode()
        conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = response.read()
        status = response.status
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200, payload
    return models.Session.load(sid)


def _assert_current_turn_survived(reloaded, prompt, answer, label):
    display = _row_texts(reloaded.messages)
    context = _row_texts(reloaded.context_messages)
    assert prompt in display, f"{label}: prompt lost from display {display}"
    assert answer in display, f"{label}: answer lost from display {display}"
    assert prompt in context, (
        f"{label}: prompt lost from cold context_messages {context}"
    )
    assert answer in context, (
        f"{label}: the current answer lost from cold context_messages {context}"
    )


# ── Component: the settle projection must BE the installed repair ────────────


@pytest.mark.parametrize("shape", ALL_SHAPES)
def test_settle_projection_equals_the_installed_repair(shape):
    """Finding 1 + 2 at component level: the settle snapshot is normalized the
    way the installed Agent's WHOLE repair sequence normalizes the sent rows
    (pass 0, stray/duplicate tool-result removal, unanswered-call pruning,
    user merge) — on a separate deep copy."""
    from api.streaming import _agent_replay_settle_projection

    repair = _installed_repair()
    history, _prompt, _answer = SHAPES[shape]
    sent = _sanitize(history, normalize_bare_assistant_adjacency=True)
    expected = copy.deepcopy(sent)
    repair(None, expected)
    projected = _agent_replay_settle_projection(copy.deepcopy(sent))
    assert _row_texts(projected) == _row_texts(expected), (
        f"{shape}: settle projection must equal the installed repair of the "
        f"sent rows; projected={_row_texts(projected)} "
        f"expected={_row_texts(expected)}"
    )


@pytest.mark.parametrize("shape", ALL_SHAPES)
def test_settle_projection_proves_the_current_turn_suffix(shape):
    """The reviewer's component repro: sanitize → settle projection →
    ``_proven_current_turn_suffix`` against the installed repair's output."""
    from api.streaming import _agent_replay_settle_projection, _proven_current_turn_suffix

    repair = _installed_repair()
    history, prompt, answer = SHAPES[shape]
    sent = _sanitize(history, normalize_bare_assistant_adjacency=True)
    returned = copy.deepcopy(sent)
    returned.append({"role": "user", "content": prompt})
    repair(None, returned)
    returned.append({"role": "assistant", "content": answer})

    projected = _agent_replay_settle_projection(copy.deepcopy(sent))
    proven = _proven_current_turn_suffix(projected, returned)
    assert proven is not None, (
        f"{shape}: the strict-prefix proof must pass against the installed "
        f"repair's output; projection={_row_texts(projected)} "
        f"returned={_row_texts(returned)}"
    )
    proven_texts = _row_texts(proven)
    assert prompt in proven_texts, f"{shape}: prompt not in proven suffix {proven_texts}"
    assert answer in proven_texts, f"{shape}: answer not in proven suffix {proven_texts}"


# ── Regression: folding survives an unimportable recognizer ──────────────────


def test_user_fold_survives_an_unimportable_recognizer():
    """CI-shard regression (round-8 re-gate): the suite's sys.path isolation
    guard can make the agent checkout unimportable mid-shard. The settle
    projection must then still fold adjacent plain user rows and still refuse
    the literal carrier shapes — the recognizer's fallback is a content
    mirror, never a blanket refusal to fold (which regressed every user-fold
    consumer in shard order: round-4's two-successive-sends and round-7's
    fold guard went red on CI while passing in isolation)."""
    import sys

    from api import config as config_mod
    from api.streaming import _agent_replay_settle_projection

    agent_dir = str(getattr(config_mod, "_AGENT_DIR", "") or "")
    saved_modules = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "agent"
        or name.startswith("agent.")
        or name in ("hermes_state", "hermes_state_local")
    }
    saved_path = list(sys.path)
    try:
        for name in saved_modules:
            sys.modules.pop(name, None)
        if agent_dir:
            sys.path[:] = [entry for entry in sys.path if entry != agent_dir]
        folded = _agent_replay_settle_projection(
            [
                {"role": "user", "content": "U1"},
                {"role": "user", "content": "U2"},
                {"role": "assistant", "content": "A2"},
            ]
        )
        assert [r.get("content") for r in folded] == ["U1\n\nU2", "A2"], (
            f"plain user rows must still fold without the recognizer; got {folded}"
        )
        carrier = _agent_replay_settle_projection(
            [
                {"role": "user", "content": "[CONTEXT SUMMARY]: prior compacted handoff"},
                {"role": "user", "content": "U2"},
                {"role": "assistant", "content": "A2"},
            ]
        )
        assert [r.get("content") for r in carrier] == [
            "[CONTEXT SUMMARY]: prior compacted handoff",
            "U2",
            "A2",
        ], f"literal carriers must still refuse the fold without the recognizer; got {carrier}"
    finally:
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


# ── End-to-end: real streaming worker + cold reload ──────────────────────────

@pytest.mark.parametrize("shape", ALL_SHAPES)
def test_streaming_worker_keeps_prompt_and_answer(shape, tmp_path, monkeypatch):
    history, prompt, answer = SHAPES[shape]
    reloaded = _worker_send(
        tmp_path, monkeypatch, f"round8-worker-{shape}", history, prompt, answer
    )
    assert reloaded is not None, "the cold reload must find the persisted session"
    _assert_current_turn_survived(reloaded, prompt, answer, f"worker/{shape}")


# ── End-to-end: real synchronous POST /api/chat + cold reload ────────────────


@pytest.mark.parametrize("shape", ALL_SHAPES)
def test_sync_post_api_chat_keeps_prompt_and_answer(shape, tmp_path, monkeypatch):
    history, prompt, answer = SHAPES[shape]
    reloaded = _sync_http_send(
        tmp_path, monkeypatch, f"round8-sync-{shape}", history, prompt, answer
    )
    assert reloaded is not None, "the cold reload must find the persisted session"
    _assert_current_turn_survived(reloaded, prompt, answer, f"sync/{shape}")


# -- Maintainer fold: the installed Agent's argument sanitizer runs before its role repair ---------------------------
# The real pre-call pipeline is sanitize_tool_call_arguments (blank/None arguments -> "{}") then
# repair_message_sequence. The settle projection modeled only the repair, so a history holding an empty-arguments call
# projected a prefix the Agent never returns and the current exchange was dropped from the saved model context
# (worker and synchronous HTTP, both arguments "" and None). Codex re-gate reproduction on 2b70b08fb716.


@pytest.fixture
def _agent_precall_pipeline(monkeypatch):
    native = pytest.importorskip("agent.agent_runtime_helpers", reason="hermes-agent modules not importable")
    if not callable(getattr(native, "sanitize_tool_call_arguments", None)):
        pytest.skip("installed Agent has no sanitize_tool_call_arguments")
    # The sanitizer reads two AIAgent members from run_agent; stand them in instead of importing the whole Agent.
    monkeypatch.setattr(native, "_ra", lambda: SimpleNamespace(AIAgent=SimpleNamespace(
        _TOOL_CALL_ARGUMENTS_CORRUPTION_MARKER="[corrupted arguments]",
        _get_tool_call_id_static=lambda tc: (tc.get("call_id") or tc.get("id")) if isinstance(tc, dict) else None)))
    repair = native.repair_message_sequence

    def precall(agent, messages):
        native.sanitize_tool_call_arguments(messages)
        return repair(agent, messages)

    monkeypatch.setattr(sys.modules[__name__], "_installed_repair", lambda: precall)


@pytest.mark.parametrize("path", ["worker", "sync"])
@pytest.mark.parametrize("arguments", ["", None, '{"path": "a.txt", "content": "trunc', "{}"])
def test_blank_tool_call_arguments_keep_the_current_exchange(path, arguments, _agent_precall_pipeline, tmp_path,
                                                             monkeypatch):
    call = copy.deepcopy(CALL_X)
    call["function"]["arguments"] = arguments
    history = [
        {"role": "user", "content": "OLD_Q", "timestamp": 1.0},
        {"role": "assistant", "content": "", "tool_calls": [call], "timestamp": 2.0},
        {"role": "tool", "content": "RESULT_ONE", "tool_call_id": "call-x", "timestamp": 3.0},
        {"role": "assistant", "content": "OLD_ANSWER", "timestamp": 4.0},
    ]
    send = _worker_send if path == "worker" else _sync_http_send
    label = {"": "blank", None: "none", "{}": "control"}.get(arguments, "corrupt")
    reloaded = send(tmp_path, monkeypatch, f"blank-args-{path}-{label}", history, "CURRENT", "CURRENT_ANSWER")
    _assert_current_turn_survived(reloaded, "CURRENT", "CURRENT_ANSWER", f"{path}/{label}")
