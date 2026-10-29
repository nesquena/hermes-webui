"""#6310 residual — the synchronous chat route must apply the identity dedup.

Maintainer residual boundary (nesquena-hermes 2026-10-27, on issue #6310):

    The synchronous route still assigns IDs before replay dedup, and its
    deduplicated context assignment is subsequently replaced by
    ``_next_context_messages``. Those are source-traced boundaries that
    need route-level acceptance tests... Verify one retained ownership set
    across model context and display, both stale-user repair paths, a
    subsequent settlement, and streaming plus synchronous chat before
    treating the whole issue as resolved.

Root cause on current master: ``api/routes.py`` computes the identity dedup
(``_deduplicate_context_messages`` — the stable-id ownership repair shipped
in #8519) at the synchronous settle, but the assignment is overwritten a
few lines later by the raw ``_next_context_messages``. The streaming settle
(``api/streaming.py``) applies the same dedup as its final persistence
step; the synchronous route never does. A previous context that carries a
duplicate pair — same-id replay pollution, or id-less legacy rows —
therefore persists the duplicate into ``context_messages``: the display
merge collapses it, so the model-facing context diverges from the visible
transcript and the model is sent the repeated rows again.

Each test drives a REAL synchronous ``POST /api/chat`` against
``server.Handler`` on an ephemeral port and, as the parity oracle, the REAL
streaming worker, followed by a cold ``models.Session.load``. Only
inference is substituted: the fake Agent echoes the sent history verbatim
(the standard replay shape) and appends the new exchange.
"""

from __future__ import annotations

import copy
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _agent_class(sid, answer):
    """Fake Agent whose only substitution is inference.

    ``run_conversation`` echoes the sent conversation history verbatim
    (the standard replay shape the installed Agent produces) and appends
    the freshly submitted prompt plus its reply on top.
    """

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
            msgs = [dict(r) for r in (conversation_history or []) if isinstance(r, dict)]
            msgs.append({"role": "user", "content": prompt})
            msgs.append({"role": "assistant", "content": answer})
            return {
                "messages": msgs,
                "final_response": answer,
                "completed": True,
                "current_turn_user_idx": len(msgs) - 2,
                "turn_id": "i6310-sync-turn",
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


def _contents(rows):
    return [r.get("content") for r in rows if isinstance(r, dict)]


def _sync_http_send(tmp_path, monkeypatch, sid, history, prompt, answer):
    """One real synchronous ``POST /api/chat``; returns the cold-reloaded session."""
    import server as server_mod
    from api import models

    _state_patches(tmp_path, monkeypatch)
    monkeypatch.setitem(
        sys.modules, "run_agent", SimpleNamespace(AIAgent=_agent_class(sid, answer))
    )
    _new_session(tmp_path, sid, history)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
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


def _worker_send(tmp_path, monkeypatch, sid, history, prompt, answer):
    """One real ``_run_agent_streaming`` send; returns the cold-reloaded session."""
    import queue
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
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _agent_class(sid, answer))

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


_CLEAN_HISTORY = [
    {"role": "user", "content": "Q1", "timestamp": 1.0, "id": 1},
    {"role": "assistant", "content": "A1", "timestamp": 2.0, "id": 2},
]

# A previous context carrying a duplicate pair. ``same-id`` models replay
# pollution that persisted an already-owned row twice under one durable id;
# ``idless`` models legacy/imported rows that never carried an id at all.
_DUPLICATE_HISTORIES = {
    "same-id": _CLEAN_HISTORY
    + [
        {"role": "user", "content": "Q1", "timestamp": 1.0, "id": 1},
        {"role": "assistant", "content": "A1", "timestamp": 2.0, "id": 2},
    ],
    "idless": _CLEAN_HISTORY
    + [
        {"role": "user", "content": "Q1", "timestamp": 1.0},
        {"role": "assistant", "content": "A1", "timestamp": 2.0},
    ],
}


@pytest.mark.parametrize("kind", ["same-id", "idless"])
def test_sync_chat_collapses_previous_context_duplicate_pair(tmp_path, monkeypatch, kind):
    """A duplicate pair in the previous context must not reach the model.

    The synchronous settle's identity dedup (``_deduplicate_context_messages``)
    is overwritten by the raw ``_next_context_messages`` on current master, so
    the pair survives into ``context_messages`` while the display merge
    collapses it — the model-facing context then diverges from the visible
    transcript and repeats rows the user already saw answered.
    """
    reloaded = _sync_http_send(
        tmp_path, monkeypatch, f"i6310-{kind}-sync", _DUPLICATE_HISTORIES[kind], "Q2", "A2"
    )
    assert reloaded is not None
    context_contents = _contents(reloaded.context_messages)
    assert context_contents.count("Q1") == 1, (
        f"the same-id/id-less duplicate Q1 must be collapsed out of the "
        f"synchronous model context; got {context_contents}"
    )
    assert context_contents.count("A1") == 1, (
        f"the same-id/id-less duplicate A1 must be collapsed out of the "
        f"synchronous model context; got {context_contents}"
    )
    # The retained row keeps its durable id.
    kept_q1 = [
        r for r in reloaded.context_messages if isinstance(r, dict) and r.get("content") == "Q1"
    ]
    assert kept_q1 and kept_q1[0].get("id") == 1, (
        f"the retained Q1 must keep its durable id; got {kept_q1}"
    )


def test_sync_chat_context_agrees_with_display_on_same_id_pair(tmp_path, monkeypatch):
    """One retained ownership set for the same-id pair.

    The display merge collapses a same-id pair by durable-id matching, so the
    model-facing context must report exactly the same occurrence set as the
    visible transcript after the settle (maintainer residual 2026-10-27:
    'Verify one retained ownership set across model context and display').
    The id-less pair is deliberately excluded here: the display merge's
    matching contract keeps rows without a durable id, which is a separate
    surface from the model-context residual this repair addresses.
    """
    reloaded = _sync_http_send(
        tmp_path, monkeypatch, "i6310-own-sync", _DUPLICATE_HISTORIES["same-id"], "Q2", "A2"
    )
    assert reloaded is not None
    context_contents = _contents(reloaded.context_messages)
    display_contents = _contents(reloaded.messages)
    assert context_contents == display_contents, (
        "context and display must report the same ownership set after the "
        f"synchronous settle; context={context_contents} display={display_contents}"
    )


def test_sync_chat_context_matches_streaming_settle(tmp_path, monkeypatch):
    """Parity oracle: the synchronous route must land the same retained set
    as the streaming settle for the same input.

    The streaming worker applies the identity dedup as its final persistence
    step; the synchronous route must not diverge from it (maintainer residual
    2026-10-27: 'streaming plus synchronous chat').
    """
    history = _DUPLICATE_HISTORIES["same-id"]
    sync_reloaded = _sync_http_send(
        tmp_path, monkeypatch, "i6310-parity-sync", history, "Q2", "A2"
    )
    stream_reloaded = _worker_send(
        tmp_path, monkeypatch, "i6310-parity-stream", history, "Q2", "A2"
    )
    assert sync_reloaded is not None and stream_reloaded is not None
    sync_contents = _contents(sync_reloaded.context_messages)
    stream_contents = _contents(stream_reloaded.context_messages)
    assert sync_contents == stream_contents, (
        "the synchronous route and the streaming settle must retain the same "
        f"ownership set; sync={sync_contents} stream={stream_contents}"
    )


def test_clean_history_still_settles_unchanged(tmp_path, monkeypatch):
    """Positive control: a duplicate-free previous context is untouched.

    The identity dedup must not collapse the legitimate current exchange or
    reorder anything on a clean history — proving the new assertions
    discriminate the duplicate case instead of over-collapsing.
    """
    reloaded = _sync_http_send(
        tmp_path, monkeypatch, "i6310-clean-sync", _CLEAN_HISTORY, "Q2", "A2"
    )
    assert reloaded is not None
    assert _contents(reloaded.context_messages) == ["Q1", "A1", "Q2", "A2"], (
        f"a clean history must settle unchanged; got {_contents(reloaded.context_messages)}"
    )


@pytest.mark.parametrize("polluted", [False, True])
def test_current_image_row_owns_turn_after_dedup(tmp_path, monkeypatch, polluted):
    """#8574 review (nesquena-hermes 2026-10-28): a multimodal current user
    row must survive the shrinking identity dedup with durable ownership.

    The sync image path mints a turn token but the retained image row carries
    it only after ``_settle_current_turn_boundary`` runs. When the identity
    dedup shortens the historical prefix (same-id replay pair removed), the
    authority's ``current_turn_user_idx`` no longer addresses the current row
    on the reduced list; the settle then materializes a SECOND plain-text copy
    of the prompt at the front while the original image row stays un-stamped.
    """
    sid = "image-boundary-" + str(polluted)
    original_setitem = monkeypatch.setitem

    def setitem(mapping, key, value):
        if mapping is sys.modules and key == "run_agent":
            parent = _agent_class(sid, "A2")

            class ImageAgent(parent):
                def run_conversation(self, **kwargs):
                    result = super().run_conversation(**kwargs)
                    result["messages"][-2]["content"] = [
                        {"type": "text", "text": kwargs["persist_user_message"]},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ]
                    return result

            value = SimpleNamespace(AIAgent=ImageAgent)
        return original_setitem(mapping, key, value)

    monkeypatch.setattr(monkeypatch, "setitem", setitem)
    history = _DUPLICATE_HISTORIES["same-id"] if polluted else _CLEAN_HISTORY
    session = _sync_http_send(tmp_path, monkeypatch, sid, copy.deepcopy(history), "Q2", "A2")
    rows = session.context_messages
    image_rows = [r for r in rows if r.get("role") == "user" and isinstance(r.get("content"), list)]
    assert len(image_rows) == 1
    assert not [r for r in rows if r.get("role") == "user" and r.get("content") == "Q2"]
    assert image_rows[0].get("_active_turn_token")
    assert type(image_rows[0].get("id")) is int
    assert rows[0]["content"] == "Q1"
    assert rows[-1]["content"] == "A2"
