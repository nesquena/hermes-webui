"""Round-4 review blockers: sync settle proof immutability and user normalization."""

import copy
import sys
from types import SimpleNamespace

import pytest

from api import config, models, routes, streaming


def _sync_handler():
    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    return Handler()


def _sync_session(tmp_path, monkeypatch, sid, context, message="follow-up"):
    """Run the real sync route with a mutating agent; return the reloaded session."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(
        config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"}
    )
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=list(context),
        context_messages=list(context),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)

    captured = {}

    class MutatingAgent:
        """Simulate the installed Agent's shallow-copy + tail repair.

        The real Agent receives the conversation history, shallow-copies the
        LIST, and performs its consecutive-user repair on the tail dict IN
        PLACE (folding an unanswered user tail together with the freshly
        submitted prompt). It then returns the repaired conversation with no
        separate current-user row — the prompt lives folded inside the merged
        boundary row.
        """

        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            merged = None
            if history and history[-1].get("role") == "user":
                merged = history[-1]
                merged["content"] = (
                    str(merged.get("content") or "")
                    + "\n\n"
                    + str(kwargs.get("persist_user_message") or "")
                )
                merged["_active_turn_token"] = "sync-repair-token"
            captured["history"] = list(history)
            return {
                "messages": history
                + [{"role": "assistant", "content": "follow-up answer"}],
                "final_response": "follow-up answer",
                "completed": True,
                "current_turn_user_idx": len(history) - 1 if merged is not None else len(history),
                "turn_id": "sync-turn",
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=MutatingAgent))
    handler = _sync_handler()
    routes._handle_chat_sync(
        handler,
        {
            "session_id": sid,
            "message": message,
            "workspace": str(tmp_path),
        },
    )
    assert handler.status == 200
    return models.Session.load(sid), captured


def test_sync_route_proof_snapshot_survives_agent_tail_repair(tmp_path, monkeypatch):
    """bl1: the settle proof is a second deep copy, not the mutated history.

    The projection handed to ``run_conversation`` must be a separate object
    from the snapshot the settle compares against. The installed Agent
    shallow-copies the list and repairs the tail dict in place, so sharing one
    object let the repair move the proof: the mutated projection then matched
    the returned list exactly, the settle treated the folded current user row
    as history, and the follow-up lost its prompt (the answer attached to the
    raw tail).
    """
    context = [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        {"role": "assistant", "content": "first answer", "timestamp": 2.0},
        {"role": "user", "content": "an earlier unanswered tail", "timestamp": 3.0},
    ]
    session, captured = _sync_session(tmp_path, monkeypatch, "sync-proof-isolated", context)

    text = [row.get("content") for row in session.context_messages]
    assert text == [
        "first question",
        "first answer",
        "an earlier unanswered tail",
        "follow-up",
        "follow-up answer",
    ], (text, captured)
    assert any(
        isinstance(row, dict) and row.get("content") == "follow-up" and row.get("_active_turn_token")
        for row in session.context_messages
    ), "the restored current user row must carry its active-turn ownership"


def test_sync_route_agent_mutation_never_reaches_the_sent_projection(tmp_path, monkeypatch):
    """bl1 companion: the object the settle proves against stays pristine."""
    context = [
        {"role": "user", "content": "plain question", "timestamp": 1.0},
        {"role": "assistant", "content": "plain answer", "timestamp": 2.0},
        {"role": "user", "content": "tail question", "timestamp": 3.0},
    ]
    session, captured = _sync_session(tmp_path, monkeypatch, "sync-proof-pristine", context)
    # The route's projection was copied again before the agent ran; the repair
    # mutated only the agent's copy, so the persisted tail keeps its own text.
    assert [row.get("content") for row in session.context_messages][:3] == [
        "plain question",
        "plain answer",
        "tail question",
    ]


def _composed_session(tmp_path, monkeypatch, sid, context, messages):
    """Run two successive real-worker sends with an installed-repair stub."""
    import queue

    import api.config as config

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *a, **k: [])
    from api import profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _: None)
    monkeypatch.setattr(
        streaming, "resolve_model_provider", lambda *a, **k: ("test-model", None, None)
    )

    histories = []

    class RepairAgent:
        """Model the installed Agent's pass-3 consecutive-user repair.

        The Agent receives the projection, appends the freshly submitted
        prompt, and folds consecutive plain-text user rows together with a
        literal ``\\n\\n`` boundary. The returned list therefore no longer
        starts with the raw projection whenever an unanswered user tail
        precedes the prompt — exactly the shape that breaks an un-normalized
        settle proof.
        """

        def __init__(self, **kw):
            self.session_id = sid
            self.platform = "webui"
            self.model = "test-model"
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, user_message, conversation_history=None,
                             persist_user_message=None, **kw):
            history = copy.deepcopy(list(conversation_history or []))
            merged = list(history)
            merged.append({"role": "user", "content": persist_user_message})
            repaired = []
            for msg in merged:
                if (
                    repaired
                    and repaired[-1].get("role") == "user"
                    and msg.get("role") == "user"
                ):
                    repaired[-1]["content"] = (
                        str(repaired[-1].get("content") or "")
                        + "\n\n"
                        + str(msg.get("content") or "")
                    )
                else:
                    repaired.append(dict(msg))
            repaired.append({"role": "assistant", "content": "ANSWER " + str(persist_user_message)})
            histories.append(copy.deepcopy(conversation_history))
            return {
                "messages": repaired,
                "final_response": "ANSWER " + str(persist_user_message),
                "completed": True,
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: RepairAgent)
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=list(context),
        context_messages=list(context),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)
    for i, msg in enumerate(messages):
        config.SESSION_AGENT_CACHE.clear()
        models.SESSIONS[sid] = session
        stream = sid + "-send" + str(i)
        routes._prepare_chat_start_session_for_stream(
            session,
            msg=msg,
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
            stream_id=stream,
            started_at=10.0 + i,
        )
        config.STREAMS[stream] = queue.Queue()
        streaming._run_agent_streaming(sid, msg, "test-model", str(tmp_path), stream, [])
        session = models.Session.load(sid)
    return session, histories


@pytest.mark.parametrize("second", ["second follow-up", "same tail"])
def test_two_successive_sends_keep_prompt_and_reply(tmp_path, monkeypatch, second):
    """bl2: historical plain-user replay is normalized before the snapshot.

    The installed Agent merges consecutive plain-text user rows, so a follow-up
    after an unanswered user tail returns a folded boundary row. The replay
    projection must mirror that merge BEFORE the settle snapshot is taken, or
    the projection/snapshot pair drifts, the prefix proof breaks, and the
    second successive send loses its prompt and reply.
    """
    context = [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        {"role": "assistant", "content": "first answer", "timestamp": 2.0},
        {"role": "user", "content": "unanswered tail", "timestamp": 3.0},
    ]
    session, histories = _composed_session(
        tmp_path, monkeypatch, "sync-compose-" + second.replace(" ", "-"), context,
        ["follow-up one", second],
    )
    text = [row.get("content") for row in session.context_messages]
    # Both rounds' prompts AND replies survived.
    assert "follow-up one" in text, (text, histories)
    assert "ANSWER follow-up one" in text, (text, histories)
    assert second in text, (text, histories)
    assert "ANSWER " + second in text, (text, histories)
    # The raw historical tail stays individually addressable in the durable
    # context (only the projection folds it).
    assert "unanswered tail" in text, (text, histories)
