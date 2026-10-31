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


def _sync_http_send(tmp_path, monkeypatch, sid, history, prompt, answer, reseed=True):
    """One real synchronous ``POST /api/chat``; returns the cold-reloaded session.

    ``reseed=False`` keeps whatever the previous request persisted for ``sid``
    on disk and sends a follow-up into it — the cold-load shape, where the
    previous turn's persisted result is this turn's input.
    """
    import server as server_mod
    from api import models

    _state_patches(tmp_path, monkeypatch)
    monkeypatch.setitem(
        sys.modules, "run_agent", SimpleNamespace(AIAgent=_agent_class(sid, answer))
    )
    if reseed:
        _new_session(tmp_path, sid, history)
    else:
        # Force the follow-up to start from the DISK state the previous request
        # persisted, not an in-memory session object cached by this process.
        models.SESSIONS.pop(sid, None)
        assert models.Session.load(sid) is not None, (
            f"a cold follow-up needs the session {sid!r} persisted on disk"
        )

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


def _install_multimodal_current_turn_agent(monkeypatch, sid, answer="A2"):
    """Substitute the fake Agent so the CURRENT user row is multimodal.

    The image part rides only on the Agent's returned row (the HTTP fixture
    submits a text-only prompt) — exactly the shape the #8574 review probes:
    the sync image path must keep durable ownership of that row while the
    identity dedup shortens the historical prefix.
    """
    original_setitem = monkeypatch.setitem

    def setitem(mapping, key, value):
        if mapping is sys.modules and key == "run_agent":
            parent = _agent_class(sid, answer)

            class MultimodalCurrentTurnAgent(parent):
                def run_conversation(self, **kwargs):
                    result = super().run_conversation(**kwargs)
                    result["messages"][-2]["content"] = [
                        {"type": "text", "text": kwargs["persist_user_message"]},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ]
                    return result

            value = SimpleNamespace(AIAgent=MultimodalCurrentTurnAgent)
        return original_setitem(mapping, key, value)

    monkeypatch.setattr(monkeypatch, "setitem", setitem)


def _image_user_rows(rows):
    return [
        r
        for r in rows or []
        if isinstance(r, dict)
        and r.get("role") == "user"
        and isinstance(r.get("content"), list)
        and any(
            isinstance(part, dict)
            and part.get("type") in {"image", "image_url", "input_image"}
            for part in r["content"]
        )
    ]


@pytest.mark.parametrize("polluted", [False, True])
def test_current_image_row_owns_turn_after_dedup(tmp_path, monkeypatch, polluted):
    """#8574 review (nesquena-hermes 2026-10-28): a multimodal current user
    row must survive the shrinking identity dedup with durable ownership.

    The sync image path mints a turn token, but before this repair the
    retained image row carried it only after ``_settle_current_turn_boundary``
    ran. When the identity dedup shortens the historical prefix (same-id
    replay pair removed), the authority's ``current_turn_user_idx`` no longer
    addresses the current row on the reduced list; the settle then
    materializes a SECOND plain-text copy of the prompt at the front while
    the original image row stays un-stamped — the current prompt is
    duplicated, moved before old history, and its ownership differs between
    display and model context.

    Repair (mirroring the streaming path's pre/post-dedup settlement in
    ``_settle_result_messages``): stamp the token onto the retained result
    row while the checkpoint index still addresses the original Agent
    result, so the token survives the shrinking dedup and the final settle
    locates the current row by token/retained-row authority. The
    empty-result fallback and the call-scoped protected rows stay intact.
    """
    sid = f"i8574-image-{'dup' if polluted else 'clean'}"
    _install_multimodal_current_turn_agent(monkeypatch, sid)
    history = _DUPLICATE_HISTORIES["same-id"] if polluted else _CLEAN_HISTORY
    reloaded = _sync_http_send(
        tmp_path, monkeypatch, sid, copy.deepcopy(history), "Q2", "A2"
    )
    assert reloaded is not None
    context = reloaded.context_messages
    display = reloaded.messages

    # --- model context: one multimodal current row, no plain-text echo ---
    plain_q2 = [
        r
        for r in context
        if isinstance(r, dict) and r.get("role") == "user" and r.get("content") == "Q2"
    ]
    assert not plain_q2, (
        "the settle must not materialize a second plain-text copy of the "
        "current prompt when the identity dedup shortens the historical "
        f"prefix; got {_contents(context)}"
    )
    image_rows = _image_user_rows(context)
    assert len(image_rows) == 1, (
        f"exactly one retained image user row must survive the dedup; "
        f"got {_contents(context)}"
    )
    current = image_rows[0]
    assert current.get("_active_turn_token"), (
        f"the retained image row must carry the active-turn token; got {current}"
    )
    assert type(current.get("id")) is int, (
        f"the retained image row must keep a valid stable id; got {current}"
    )
    assert context[2] is current and context[0]["content"] == "Q1", (
        f"the current row must stay after the retained history, not at the "
        f"front; got {_contents(context)}"
    )
    assert context[-1]["content"] == "A2", (
        f"the assistant reply must close the context; got {_contents(context)}"
    )
    if polluted:
        assert _contents(context).count("Q1") == 1 and _contents(context).count("A1") == 1, (
            f"the replayed duplicate pair must still be collapsed; "
            f"got {_contents(context)}"
        )

    # --- display: the normal exchange, with the SAME current-row ownership ---
    assert _contents(display) == ["Q1", "A1", "Q2", "A2"], (
        f"the display must show the normal Q1/A1/Q2/A2 exchange; "
        f"got {_contents(display)}"
    )
    display_current = [
        r
        for r in display
        if isinstance(r, dict)
        and r.get("role") == "user"
        and r.get("_active_turn_token") == current["_active_turn_token"]
    ]
    assert len(display_current) == 1, (
        "the display must carry exactly one current-turn row; "
        f"got {_contents(display)}"
    )
    assert display_current[0].get("id") == current["id"], (
        "display and model context must agree on the current row's stable "
        f"id; display={display_current[0].get('id')} "
        f"context={current.get('id')}"
    )


# ── #8574 gate certification RED, finding 1 ────────────────────────────
#
# Two committed turns that repeat the SAME prompt verbatim inside one
# timestamp bucket, each with its own durable id and its own active-turn
# token, and identical source/attachments (``_ts`` absent). The gate
# reproduced over real ``POST /api/chat`` + cold ``Session.load()``:
#
#   candidate cold model context starts with IDs 3, 2, 4, 5
#       — owner 1 is deleted and owner 3 moves before the first answer;
#   master (no persisted dedup) preserves 1, 2, 3, 4;
#   the display transcript retains BOTH owners on both trees.
#
# The token-bearing-user branch of ``_deduplicate_context_messages``
# replaced the earlier exact-key match BEFORE any distinct-durable-id check,
# so the second turn's row overwrote the first turn's row in place.
_DISTINCT_OWNER_HISTORY = [
    {
        "role": "user",
        "content": "higher",
        "timestamp": 100.0,
        "id": 1,
        "_source": "webui",
        "_active_turn_token": "s1:100",
    },
    {"role": "assistant", "content": "guess 50", "timestamp": 101.0, "id": 2},
    {
        "role": "user",
        "content": "higher",
        "timestamp": 100.0,
        "id": 3,
        "_source": "webui",
        "_active_turn_token": "s2:100",
    },
    {"role": "assistant", "content": "guess 75", "timestamp": 103.0, "id": 4},
]


def test_sync_chat_preserves_distinct_durable_user_owners(tmp_path, monkeypatch):
    """#8574 gate finding 1: distinct durable owners survive the persisted dedup.

    The synchronous settle now persists
    ``_deduplicate_context_messages``' reduced list. Its token-bearing-user
    branch replaced an earlier exact-key match (identical content, timestamp,
    source and attachments) before checking distinct ids or turn tokens, so
    the second committed turn's row overwrote the first turn's row: the
    model-facing context lost owner 1 and reordered owner 3 ahead of the
    first answer, while the visible transcript still showed both.

    Master keeps the raw list (1, 2, 3, 4), so the repair must restore that
    ownership without re-introducing the duplicate-pair collapse this PR
    fixes elsewhere.
    """
    reloaded = _sync_http_send(
        tmp_path,
        monkeypatch,
        "i8574-owner-sync",
        copy.deepcopy(_DISTINCT_OWNER_HISTORY),
        "lower",
        "guess 90",
    )
    assert reloaded is not None
    context = reloaded.context_messages
    display = reloaded.messages

    context_user_ids = [
        r.get("id") for r in context if isinstance(r, dict) and r.get("role") == "user"
    ]
    assert context_user_ids == [1, 3, 5], (
        "both independently owned historical user rows must survive the "
        "synchronous identity dedup with their durable ids, followed by the "
        f"new turn; got ids={context_user_ids} contents={_contents(context)}"
    )
    assert _contents(context) == [
        "higher",
        "guess 50",
        "higher",
        "guess 75",
        "lower",
        "guess 90",
    ], (
        "the model context must keep the historical order (owner 1 before its "
        f"answer, owner 3 before its answer); got {_contents(context)}"
    )
    # The display transcript is the ownership oracle: it retains both owners.
    assert _contents(display).count("higher") == 2, (
        "the display must retain both independently owned user rows; got "
        f"{_contents(display)}"
    )
    assert _contents(context).count("higher") == 2, (
        "the model context must agree with the display on how many 'higher' "
        f"turns it carries; got {_contents(context)}"
    )
    kept_tokens = [
        r.get("_active_turn_token")
        for r in context
        if isinstance(r, dict) and r.get("role") == "user" and r.get("content") == "higher"
    ]
    assert kept_tokens == ["s1:100", "s2:100"], (
        "each retained owner must keep its own active-turn token; got "
        f"{kept_tokens}"
    )


# ── #8574 gate certification RED, finding 2 ────────────────────────────
#
# Polluted same-id history, an Agent result that rewrites the first
# historical answer and returns a multimodal current user row. The
# fail-closed projection keeps the raw previous context; the persisted
# shrinking dedup then ran BEFORE boundary settlement, so the settle placed
# the current row from a stale ``current_turn_user_idx``:
#
#   candidate cold context = [Q2, Q1, A1]  (historical first row lost);
#   master retains          [Q1, A1, Q1, A1, Q2].
_POLLUTED_SAME_ID_HISTORY = [
    {"role": "user", "content": "Q1", "timestamp": 1.0, "id": 1},
    {"role": "assistant", "content": "A1", "timestamp": 2.0, "id": 2},
    {"role": "user", "content": "Q1", "timestamp": 1.0, "id": 1},
    {"role": "assistant", "content": "A1", "timestamp": 2.0, "id": 2},
]


def _install_rewriting_multimodal_agent(monkeypatch, sid, answer="A2"):
    """Agent that rewrites the first historical answer and returns an image row.

    Rewriting the first historical answer makes the returned conversation
    diverge from the projection this process sent, so
    ``_dedupe_replayed_context_messages`` fails closed and returns the raw
    previous context — the shape the #8574 gate used for its settlement
    ordering finding. The current user row stays multimodal so the sync image
    path mints a turn token, exactly as the gate probe did.
    """
    original_setitem = monkeypatch.setitem

    def setitem(mapping, key, value):
        if mapping is sys.modules and key == "run_agent":
            parent = _agent_class(sid, answer)

            class RewritingMultimodalAgent(parent):
                def run_conversation(self, **kwargs):
                    result = super().run_conversation(**kwargs)
                    msgs = result["messages"]
                    msgs[1]["content"] = "A1-rewritten"
                    msgs[-2]["content"] = [
                        {"type": "text", "text": kwargs["persist_user_message"]},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ]
                    return result

            value = SimpleNamespace(AIAgent=RewritingMultimodalAgent)
        return original_setitem(mapping, key, value)

    monkeypatch.setattr(monkeypatch, "setitem", setitem)


def test_sync_chat_settles_boundary_before_shrinking_dedup(tmp_path, monkeypatch):
    """#8574 gate finding 2: settle before AND after the shrinking dedup.

    The streaming settle (``_settle_result_messages``) runs
    ``_settle_current_turn_boundary`` on the replayed context BEFORE the
    identity dedup shrinks it and again AFTER. The synchronous route settled
    only once, after the shrink, so the authority's
    ``current_turn_user_idx`` (an index into the pre-shrink list) addressed
    the wrong slot and the current turn was materialized at the FRONT of the
    reduced history: the model context began with the new prompt and the
    historical first row was pushed behind it.

    Master (no persisted dedup) keeps [Q1, A1, Q1, A1, Q2]; the repaired
    candidate must keep the historical first row at the front too.
    """
    _install_rewriting_multimodal_agent(monkeypatch, "i8574-settle-sync")
    reloaded = _sync_http_send(
        tmp_path,
        monkeypatch,
        "i8574-settle-sync",
        copy.deepcopy(_POLLUTED_SAME_ID_HISTORY),
        "Q2",
        "A2",
    )
    assert reloaded is not None
    context = reloaded.context_messages
    contents = _contents(context)

    first = context[0]
    assert isinstance(first, dict) and first.get("role") == "user", (
        f"the first model-context row must be a historical user row; got {contents}"
    )
    assert first.get("content") == "Q1", (
        "the historical first row must survive at the front of the model "
        f"context after a fail-closed projection; got {contents}"
    )
    assert contents.count("Q2") == 1, (
        "the current prompt must be retained exactly once; got "
        f"{contents}"
    )
    assert context[-1] is not None and context[-1].get("content") == "Q2", (
        "the current turn must be settled at the tail of the retained "
        f"history, not spliced in front of it; got {contents}"
    )
    current = context[-1]
    assert current.get("_active_turn_token"), (
        "the settled current row must keep the active-turn token minted for "
        f"the image row; got {current}"
    )
    assert contents.count("Q1") == 1, (
        "the replayed same-id duplicate pair must still be collapsed out of "
        f"the model context; got {contents}"
    )


def test_streaming_worker_preserves_distinct_durable_user_owners(tmp_path, monkeypatch):
    """The shared helper fix must hold on the streaming settle too.

    ``_deduplicate_context_messages`` is one helper used by both paths; the
    gate noted the weakness already existed in streaming. Drive the REAL
    streaming worker with the same distinct-owner history and cold-reload.
    """
    stream_reloaded = _worker_send(
        tmp_path,
        monkeypatch,
        "i8574-owner-stream",
        copy.deepcopy(_DISTINCT_OWNER_HISTORY),
        "lower",
        "guess 90",
    )
    assert stream_reloaded is not None
    stream_user_ids = [
        r.get("id")
        for r in stream_reloaded.context_messages
        if isinstance(r, dict) and r.get("role") == "user"
    ]
    assert stream_user_ids == [1, 3, 5], (
        "the streaming settle must retain both independently owned user rows "
        f"with their durable ids; got ids={stream_user_ids} "
        f"contents={_contents(stream_reloaded.context_messages)}"
    )


# ── unit pins for the token-branch replacement semantics ───────────────
#
# The repair must not over-correct: a token-bearing row that is a mere
# replay of an already-retained row (same durable id, or a prior row with no
# durable id at all) must STILL supersede it. These shapes are what the
# token branch existed for.


def test_dedup_token_branch_keeps_distinct_durable_owners():
    """Unit pin for gate finding 1 at the helper boundary."""
    from api.streaming import _deduplicate_context_messages

    out = _deduplicate_context_messages(copy.deepcopy(_DISTINCT_OWNER_HISTORY))
    assert _contents(out) == ["higher", "guess 50", "higher", "guess 75"], (
        "two token-bearing user rows with distinct durable ids are "
        f"independently owned turns and must both survive; got {_contents(out)}"
    )
    assert [r.get("id") for r in out if r.get("role") == "user"] == [1, 3], (
        "each independently owned user row must keep its own durable id; got "
        f"{[r.get('id') for r in out if r.get('role') == 'user']}"
    )


def test_dedup_token_branch_still_replaces_a_matching_id_replay():
    """The token holder still supersedes a replay that shares its durable id."""
    from api.streaming import _deduplicate_context_messages

    rows = [
        {
            "role": "user",
            "content": "same prompt",
            "timestamp": 5.0,
            "id": 7,
            "_source": "webui",
            "_active_turn_token": "s1:5",
        },
        {"role": "assistant", "content": "first answer", "timestamp": 6.0, "id": 8},
        {
            "role": "user",
            "content": "same prompt",
            "timestamp": 5.0,
            "id": 7,
            "_source": "webui",
            "_active_turn_token": "s2:5",
        },
    ]
    out = _deduplicate_context_messages(rows)
    assert _contents(out) == ["same prompt", "first answer"], (
        "a token-bearing replay of the SAME durable id must still replace the "
        f"earlier row rather than duplicate it; got {_contents(out)}"
    )
    assert out[0].get("_active_turn_token") == "s2:5", (
        "the replacement row must be the token holder; got "
        f"{out[0].get('_active_turn_token')}"
    )


def test_dedup_token_branch_still_replaces_an_id_less_prior_row():
    """An id-less prior row keeps the legacy collapse (no ownership proof)."""
    from api.streaming import _deduplicate_context_messages

    rows = [
        {
            "role": "user",
            "content": "same prompt",
            "timestamp": 5.0,
            "_source": "webui",
            "_active_turn_token": "s1:5",
        },
        {
            "role": "user",
            "content": "same prompt",
            "timestamp": 5.0,
            "id": 9,
            "_source": "webui",
            "_active_turn_token": "s2:5",
        },
    ]
    out = _deduplicate_context_messages(rows)
    assert _contents(out) == ["same prompt"], (
        "a prior row without a durable id grants no independent ownership, so "
        f"the token holder must still replace it; got {_contents(out)}"
    )
    assert out[0].get("id") == 9 and out[0].get("_active_turn_token") == "s2:5", (
        f"the retained row must be the token holder; got {out[0]}"
    )


# ── #8574 gate certification RED, finding 2 ────────────────────────────
#
# ``user_exact_index`` names only the MOST RECENT same-exact-key owner. With
# two independently owned turns that repeat a prompt verbatim inside one
# timestamp bucket — ``_DISTINCT_OWNER_HISTORY`` (durable ids 1 and 3) — a
# durable REPLAY of the first owner (same id, same timestamp, same
# active-turn token: the shape a recovery/import path leaves behind after the
# second committed turn) was compared against owner 3, judged independently
# owned, and APPENDED. The same durable owner then reached the persisted model
# context twice while the display transcript kept one. A token-bearing
# collision must be resolved against EVERY retained same-key durable owner, so
# this replay collapses into owner 1's own slot.


def _user_ids(rows):
    return [
        r.get("id") for r in rows if isinstance(r, dict) and r.get("role") == "user"
    ]


def _replayed_first_owner_history():
    """``_DISTINCT_OWNER_HISTORY`` plus a durable replay of its first owner."""
    return copy.deepcopy(_DISTINCT_OWNER_HISTORY) + [
        copy.deepcopy(_DISTINCT_OWNER_HISTORY[0])
    ]


def test_dedup_token_branch_collapses_replay_of_the_earlier_owner():
    """Three-occurrence helper case: the replay updates its OWN slot.

    The replayed row is byte-identical to owner 1, so the exact index (which
    owner 3 republished) points at owner 3's slot; the independently-owned
    judgement then lets the replay fall through to the append. It must instead
    find owner 1 among ALL retained same-key owners and update that slot.
    """
    from api.streaming import _deduplicate_context_messages

    out = _deduplicate_context_messages(_replayed_first_owner_history())
    assert _contents(out) == ["higher", "guess 50", "higher", "guess 75"], (
        "a durable replay of an already-retained owner must collapse into that "
        f"owner's own slot, not append a third copy; got {_contents(out)}"
    )
    assert _user_ids(out) == [1, 3], (
        "owner 1 must appear exactly once, in its original slot, with owner 3 "
        f"untouched behind it; got ids={_user_ids(out)}"
    )
    assert out[0].get("_active_turn_token") == "s1:100" and out[0].get("id") == 1, (
        "the collapsed slot must keep its own durable id and active-turn token; "
        f"got {out[0]}"
    )
    assert out[2].get("id") == 3 and out[2].get("_active_turn_token") == "s2:100", (
        "the genuinely distinct owner must keep its own id, token and slot; "
        f"got {out[2]}"
    )


def test_dedup_token_branch_collapses_a_timestamp_shifted_owner_replay():
    """Same collapse when the replay's timestamp moved off the exact index."""
    from api.streaming import _deduplicate_context_messages

    rows = _replayed_first_owner_history()
    rows[-1]["timestamp"] = 250.0
    out = _deduplicate_context_messages(rows)
    assert _user_ids(out) == [1, 3], (
        "a replayed durable owner whose timestamp drifted must still collapse "
        f"into its own slot; got ids={_user_ids(out)} contents={_contents(out)}"
    )
    assert out[0].get("timestamp") == 250.0, (
        "the surviving row must be the newest (token-preferred) replay; got "
        f"{out[0].get('timestamp')}"
    )
    # Control: the same replay WITHOUT a token keeps collapsing on the
    # long-standing stable-id path.
    tokenless = _replayed_first_owner_history()
    tokenless[-1]["timestamp"] = 250.0
    tokenless[-1].pop("_active_turn_token")
    tokenless_out = _deduplicate_context_messages(tokenless)
    assert _user_ids(tokenless_out) == [1, 3], (
        "the tokenless control must keep the legacy collapse; got "
        f"ids={_user_ids(tokenless_out)}"
    )
    assert tokenless_out[0].get("timestamp") == 100.0, (
        "the tokenless replay must be dropped rather than replace its owner; "
        f"got {tokenless_out[0].get('timestamp')}"
    )


def test_dedup_token_branch_still_appends_a_genuinely_new_owner():
    """A token-bearing row matching no retained owner still appends."""
    from api.streaming import _deduplicate_context_messages

    rows = copy.deepcopy(_DISTINCT_OWNER_HISTORY) + [
        {
            "role": "user",
            "content": "higher",
            "timestamp": 100.0,
            "id": 9,
            "_source": "webui",
            "_active_turn_token": "s3:100",
        },
        {"role": "assistant", "content": "guess 110", "timestamp": 104.0, "id": 10},
    ]
    out = _deduplicate_context_messages(rows)
    assert _user_ids(out) == [1, 3, 9], (
        "a third independently owned turn with a distinct durable id must still "
        f"be appended; got ids={_user_ids(out)} contents={_contents(out)}"
    )
    assert _contents(out) == [
        "higher",
        "guess 50",
        "higher",
        "guess 75",
        "higher",
        "guess 110",
    ], f"the appended owner must keep the historical order; got {_contents(out)}"


def test_sync_chat_cold_follow_up_collapses_replayed_owner(tmp_path, monkeypatch):
    """#8574 finding 2 over the real route: cold follow-up, no re-seed.

    Request 1 seeds two independently owned turns plus a durable replay of the
    first owner. Its persisted result then becomes the INPUT of request 2 — a
    real follow-up ``POST /api/chat`` on the same session, cold-loaded from
    disk with no re-seeding. The replay must collapse into owner 1's own slot,
    so the model context and the display transcript each carry exactly one row
    per owner, in the same order.
    """
    sid = "i8574-replay-followup"
    first = _sync_http_send(
        tmp_path,
        monkeypatch,
        sid,
        _replayed_first_owner_history(),
        "lower",
        "guess 90",
    )
    assert first is not None
    first_ids = _user_ids(first.context_messages)
    assert first_ids == [1, 3, 5], (
        "request 1 must persist one row per owner plus the new turn; got "
        f"ids={first_ids} contents={_contents(first.context_messages)}"
    )
    assert _contents(first.context_messages).count("higher") == 2, (
        "the replayed owner must be collapsed out of the persisted context; got "
        f"{_contents(first.context_messages)}"
    )

    second = _sync_http_send(
        tmp_path,
        monkeypatch,
        sid,
        first.context_messages,
        "even lower",
        "guess 95",
        reseed=False,
    )
    assert second is not None
    context = second.context_messages
    display = second.messages

    assert _user_ids(context) == [1, 3, 5, 7], (
        "the cold follow-up must keep exactly one row per owner, in order, "
        f"followed by the new turn; got ids={_user_ids(context)} "
        f"contents={_contents(context)}"
    )
    assert _contents(context) == [
        "higher",
        "guess 50",
        "higher",
        "guess 75",
        "lower",
        "guess 90",
        "even lower",
        "guess 95",
    ], f"the cold follow-up context order drifted; got {_contents(context)}"
    assert _contents(context).count("higher") == 2, (
        "the model context must not carry the replayed owner twice; got "
        f"{_contents(context)}"
    )
    assert _user_ids(display) == [1, 3, 5, 7], (
        "the display transcript must agree on owner count and order; got "
        f"ids={_user_ids(display)} contents={_contents(display)}"
    )
    assert _contents(display).count("higher") == 2, (
        "the display must retain exactly the two independently owned turns; got "
        f"{_contents(display)}"
    )
