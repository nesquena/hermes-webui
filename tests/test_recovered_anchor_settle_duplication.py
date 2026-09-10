"""Geometric duplication of recovered reasoning-only rows at turn settle.

Refs #7388 / #7416 (linear journal replay). This is the INDEPENDENT geometric
leg: each settled turn re-appended every empty recovered anchor once more.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib

import pytest

from api.models import Session
from api.streaming import (
    _assign_stable_message_ids,
    _merge_display_messages_after_agent_result,
    _restore_display_reasoning_metadata,
    _settle_result_messages,
)
from api import streaming as _streaming

STREAM_ID = "34d53038e2d3485cb67cd90ef8408faf"


def _recovered_anchor(reasoning: str, ts: int = 1788356394) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "timestamp": ts,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": STREAM_ID,
        "reasoning": reasoning,
    }


def _is_clone(message) -> bool:
    return bool(
        isinstance(message, dict)
        and message.get("_recovered_from_run_journal")
        and message.get("role") == "assistant"
        and not (message.get("content") or "")
    )


def _clone_blocks(messages) -> list[int]:
    blocks, run = [], 0
    for message in messages:
        if _is_clone(message):
            run += 1
        elif run:
            blocks.append(run)
            run = 0
    if run:
        blocks.append(run)
    return blocks


def _compacted_history() -> tuple[list[dict], list[dict]]:
    """Display transcript with a recovered anchor + a COMPACTED model context.

    The anchor's turn was compressed out of the context, so its API-safe
    position lies beyond the end of ``result_messages`` (production shape).
    """
    display = [
        {"role": "user", "content": "reload the webui", "timestamp": 1788356170},
        {"role": "assistant", "content": "Restarting the WebUI unit.", "timestamp": 1788356390},
        _recovered_anchor("The command was killed with SIGTERM"),
        {"role": "user", "content": "did it come back?", "timestamp": 1788357000},
        {"role": "assistant", "content": "Yes, it is up.", "timestamp": 1788357010},
    ]
    context = [
        {"role": "user", "content": "did it come back?", "timestamp": 1788357000},
        {"role": "assistant", "content": "Yes, it is up.", "timestamp": 1788357010},
    ]
    return display, context


def _settle_turn(display, context, turn: int):
    msg_text = f"follow-up {turn}"
    ts = 1788440000 + turn * 10
    result = copy.deepcopy(context) + [
        {"role": "user", "content": msg_text, "timestamp": ts},
        {"role": "assistant", "content": f"reply {turn}", "timestamp": ts + 5},
    ]
    restored = _restore_display_reasoning_metadata(copy.deepcopy(display), copy.deepcopy(result))
    merged = _merge_display_messages_after_agent_result(
        copy.deepcopy(display), copy.deepcopy(context), restored, msg_text, source="webui",
    )
    return merged, result


def test_restore_display_reasoning_does_not_append_unaligned_rows_past_result_end():
    display, context = _compacted_history()
    result = copy.deepcopy(context) + [
        {"role": "user", "content": "follow-up 1"},
        {"role": "assistant", "content": "reply 1"},
    ]

    restored = _restore_display_reasoning_metadata(display, copy.deepcopy(result))

    # The anchor's API-safe slot (2) is beyond the compacted result's own
    # rows, so nothing may be appended after the new reply.
    assert [m.get("content") for m in restored] == [m.get("content") for m in result]
    assert not any(_is_clone(m) for m in restored)


def test_settled_turns_do_not_double_recovered_anchors():
    display, context = _compacted_history()
    assert sum(_is_clone(m) for m in display) == 1
    for turn in range(1, 5):
        display, context = _settle_turn(display, context, turn)
        assert sum(_is_clone(m) for m in display) == 1, (
            f"turn {turn}: clone blocks {_clone_blocks(display)}"
        )
    # Original anchor stays where it was, right after its own reply.
    assert _is_clone(display[2])
    assert display[-1]["content"] == "reply 4"


def test_reasoning_only_row_still_restored_when_its_successor_is_present():
    previous = [
        {"role": "user", "content": "old turn", "timestamp": 1},
        {"role": "assistant", "content": "", "timestamp": 2, "reasoning": "visible thinking card"},
        {"role": "assistant", "content": "old answer", "timestamp": 3},
    ]
    updated = [
        {"role": "user", "content": "old turn"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new turn"},
        {"role": "assistant", "content": "new answer"},
    ]

    restored = _restore_display_reasoning_metadata(previous, updated)

    assert [m.get("content") for m in restored] == [
        "old turn", "", "old answer", "new turn", "new answer",
    ]
    assert restored[1]["reasoning"] == "visible thinking card"


def _repeated_prompt_history(n_anchors: int) -> tuple[list[dict], list[dict]]:
    display = [
        {"role": "user", "content": "older turn", "timestamp": 1788356100, "id": 1},
        {"role": "assistant", "content": "older answer", "timestamp": 1788356200, "id": 2},
        *[_recovered_anchor(f"recovered thinking {i}", 1788356394 + i) for i in range(n_anchors)],
        {"role": "user", "content": "continue", "timestamp": 1788357000, "id": 3},
        {"role": "assistant", "content": "first answer", "timestamp": 1788357010, "id": 4},
    ]
    context = [copy.deepcopy(display[-2]), copy.deepcopy(display[-1])]
    return display, context


def _settle_session_turn(session, turn: int, prompt: str, answer: str):
    """Production order: ids minted on result rows, then restore -> merge."""
    previous = list(session.messages)
    previous_context = list(session.context_messages)
    ts = 1788440000 + turn * 10
    result = copy.deepcopy(previous_context) + [
        {"role": "user", "content": prompt, "timestamp": ts},
        {"role": "assistant", "content": answer, "timestamp": ts + 5},
    ]
    _settle_result_messages(session, previous, previous_context, result, prompt, "webui", None)


def test_repeated_prompt_with_distinct_stable_id_does_not_realign_anchors(monkeypatch):
    monkeypatch.setattr(_streaming, "_annotate_media_snapshots_for_settled_messages", lambda m: None)
    display, context = _repeated_prompt_history(64)
    session = Session(session_id="a" * 12, title="t", messages=copy.deepcopy(display))
    session.context_messages = copy.deepcopy(context)
    assert sum(_is_clone(m) for m in session.messages) == 64

    # Guard the fixture: the new "continue" row is a DIFFERENT turn (id 5) that
    # is content-identical to the historical successor (id 3).
    probe = copy.deepcopy(context) + [
        {"role": "user", "content": "continue", "timestamp": 1788440000},
        {"role": "assistant", "content": "a different new answer", "timestamp": 1788440005},
    ]
    _assign_stable_message_ids(probe, display, context)
    assert (display[-2]["id"], probe[2]["id"]) == (3, 5)
    assert _streaming._message_identity(display[-2]) == _streaming._message_identity(probe[2])

    for turn in range(1, 4):
        _settle_session_turn(session, turn, "continue", f"a different new answer {turn}")
        assert sum(_is_clone(m) for m in session.messages) == 64, (
            f"turn {turn}: clone blocks {_clone_blocks(session.messages)}"
        )
        assert session.messages[-1]["content"] == f"a different new answer {turn}"
    # Anchors stay in their original block, right after "older answer".
    assert _clone_blocks(session.messages) == [64]
    assert session.messages[1]["content"] == "older answer" and _is_clone(session.messages[2])


def test_successor_alignment_prefers_stable_ids_over_content():
    previous = [
        {"role": "user", "content": "older turn", "timestamp": 1, "id": 1},
        {"role": "assistant", "content": "older answer", "timestamp": 2, "id": 2},
        {"role": "assistant", "content": "", "timestamp": 3, "reasoning": "card"},
        {"role": "user", "content": "continue", "timestamp": 4, "id": 3},
        {"role": "assistant", "content": "first answer", "timestamp": 5, "id": 4},
    ]

    def restore(prev, successor_id, prompt="continue"):
        row = {"role": "user", "content": prompt}
        if successor_id is not None:
            row["id"] = successor_id
        updated = [
            {"role": "user", "content": "older turn", "id": 1},
            {"role": "assistant", "content": "older answer", "id": 2},
            row,
            {"role": "assistant", "content": "new answer"},
        ]
        restored = _restore_display_reasoning_metadata(copy.deepcopy(prev), updated)
        return [m.get("content") for m in restored]

    restored_shape = ["older turn", "older answer", "", "continue", "new answer"]
    skipped_shape = ["older turn", "older answer", "continue", "new answer"]
    assert restore(previous, 3) == restored_shape
    assert restore(previous, 5) == skipped_shape
    # Exactly one side carries an id -> rejected, never a content fallback.
    # (A workspace-prefixed prompt is identity-equal but escapes id carry-forward.)
    prefixed = "[Workspace::v1: /tmp/ws] continue"
    assert _streaming._message_identity({"role": "user", "content": prefixed}) == (
        _streaming._message_identity(previous[3])
    )
    assert restore(previous, None, prefixed) == [
        "older turn", "older answer", prefixed, "new answer",
    ]
    idless = copy.deepcopy(previous)
    del idless[3]["id"]
    assert restore(idless, 3) == skipped_shape
    # Neither side carries an id -> legacy content identity still restores.
    assert restore(idless, None) == restored_shape


_REAL_SIDECAR = os.environ.get("HERMES_WEBUI_REAL_CLONE_SIDECAR", "")


def _assistant_successor_history(n_anchors: int) -> tuple[list[dict], list[dict]]:
    """64 anchors before a historical assistant ``"Done."`` (id 2); compacted context."""
    display = [
        {"role": "user", "content": "do it", "timestamp": 1788356100, "id": 1},
        *[_recovered_anchor(f"recovered thinking {i}", 1788356394 + i) for i in range(n_anchors)],
        {"role": "assistant", "content": "Done.", "timestamp": 1788357010, "id": 2},
    ]
    context = [copy.deepcopy(display[0]), copy.deepcopy(display[-1])]
    return display, context


def _settle_replacing_result(monkeypatch, display, context, result, prompt):
    """Result that REPLACES the compacted context (does not extend it)."""
    monkeypatch.setattr(_streaming, "_annotate_media_snapshots_for_settled_messages", lambda m: None)
    session = Session(session_id="d" * 12, title="t", messages=copy.deepcopy(display))
    session.context_messages = copy.deepcopy(context)
    _settle_result_messages(
        session, list(session.messages), list(session.context_messages), result, prompt, "webui", None,
    )
    return session


def test_historical_assistant_successor_with_same_content_does_not_forge_current_id(monkeypatch):
    display, context = _assistant_successor_history(64)
    assert sum(_is_clone(m) for m in display) == 64
    result = [
        {"role": "user", "content": "do it again", "timestamp": 1788440000},
        {"role": "assistant", "content": "Done.", "timestamp": 1788440005},
    ]
    session = _settle_replacing_result(monkeypatch, display, context, result, "do it again")
    assert sum(_is_clone(m) for m in session.messages) == 64, _clone_blocks(session.messages)
    # The distinct current "Done." must NOT inherit the historical successor's id 2.
    assert result[-1]["id"] != 2 and _streaming._is_stable_message_id(result[-1]["id"])
    assert _clone_blocks(session.messages) == [64]
    assert session.messages[-1]["content"] == "Done." and session.messages[-2]["content"] == "do it again"


def test_whole_repeated_user_assistant_pair_does_not_forge_or_double(monkeypatch):
    display, context = _assistant_successor_history(64)
    result = [
        {"role": "user", "content": "do it", "timestamp": 1788440000},
        {"role": "assistant", "content": "Done.", "timestamp": 1788440005},
    ]
    session = _settle_replacing_result(monkeypatch, display, context, result, "do it")
    assert sum(_is_clone(m) for m in session.messages) == 64, _clone_blocks(session.messages)
    assert [m["id"] for m in result] != [1, 2]
    assert result[-1]["id"] != 2
    assert _clone_blocks(session.messages) == [64]


def test_assistant_only_result_has_boundary_zero_and_inherits_nothing(monkeypatch):
    display, context = _assistant_successor_history(4)
    result = [{"role": "assistant", "content": "Done.", "timestamp": 1788440005}]
    session = _settle_replacing_result(monkeypatch, display, [context[-1]], result, "do it again")
    assert result[0]["id"] != 2
    assert sum(_is_clone(m) for m in session.messages) == 4


def _current_only_collision_result() -> list[dict]:
    """Compacted CURRENT-only result whose text echoes the old context."""
    return [
        {"role": "user", "content": "do it", "timestamp": 1788440000},
        {"role": "assistant", "content": "Done.", "timestamp": 1788440005},
        {"role": "assistant", "content": "Verified.", "timestamp": 1788440010},
    ]


def test_current_only_collision_without_authority_fails_closed_to_boundary_zero(monkeypatch):
    display, context = _assistant_successor_history(64)
    result = _current_only_collision_result()
    assert _streaming._messages_have_prefix(result, context)  # identity ignores ids/ts
    assert _streaming._active_turn_boundary(copy.deepcopy(result), context, None, "do it") == 0
    session = _settle_replacing_result(monkeypatch, display, context, result, "do it")
    assert sum(_is_clone(m) for m in session.messages) == 64, _clone_blocks(session.messages)
    assert _clone_blocks(session.messages) == [64]
    assert session.messages[-1]["content"] == "Verified."


def test_current_only_collision_with_exact_authority_keeps_one_block(monkeypatch):
    display, context = _assistant_successor_history(64)
    result = _current_only_collision_result()
    identity = _streaming._resolve_active_turn_authority(
        {"token": None, "text": "do it", "current_turn_user_idx": None, "turn_id": ""},
        result={"messages": result, "current_turn_user_idx": 0, "turn_id": "turn-1"},
    )
    assert _streaming._active_turn_boundary_is_valid(identity)
    assert _streaming._active_turn_boundary(copy.deepcopy(result), context, identity, "do it") == 0
    monkeypatch.setattr(_streaming, "_annotate_media_snapshots_for_settled_messages", lambda m: None)
    session = Session(session_id="f" * 12, title="t", messages=copy.deepcopy(display))
    session.context_messages = copy.deepcopy(context)
    _settle_result_messages(
        session, list(session.messages), list(session.context_messages), result, "do it", "webui", identity,
    )
    assert _clone_blocks(session.messages) == [64]


def test_append_only_result_control_keeps_boundary_after_prefix(monkeypatch):
    display, context = _assistant_successor_history(64)
    result = copy.deepcopy(context) + [
        {"role": "user", "content": "do it", "timestamp": 1788440000},
        {"role": "assistant", "content": "Verified.", "timestamp": 1788440010},
    ]
    assert _streaming._active_turn_boundary(copy.deepcopy(result), context, None, "do it") == 2
    session = _settle_replacing_result(monkeypatch, display, context, result, "do it")
    assert _clone_blocks(session.messages) == [64]
    assert session.messages[-1]["content"] == "Verified."


def test_prefix_without_current_user_at_or_after_it_fails_closed():
    boundary = _streaming._active_turn_boundary
    prev = [{"role": "user", "content": "do it"}, {"role": "assistant", "content": "Done."}]
    # Historical user matches the prompt, but no current user row follows the prefix.
    assert boundary(prev + [{"role": "assistant", "content": "Verified."}], prev, None, "do it") == 0
    # A prompt-matching user AFTER the prefix is the current turn.
    assert boundary(prev + [{"role": "user", "content": "do it"}], prev, None, "do it") == 2


def test_active_turn_boundary_contract():
    boundary = _streaming._active_turn_boundary
    prev = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert boundary([], prev, None, "x") == 0
    assert boundary([{"role": "assistant", "content": "Done."}], prev, None, "x") == 0
    extended = prev + [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    assert boundary(extended, prev, None, "x") == 2
    replaced = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    assert boundary(replaced, prev, None, "x") == 0
    tokened = [{"role": "user", "content": "x", "_active_turn_token": "tok"}, {"role": "assistant", "content": "y"}]
    assert boundary(prev + tokened, prev, {"token": "tok"}, "x") == 2


def _identity_hole_history(successor_id) -> tuple[list[dict], list[dict]]:
    """Historical successor carries ``successor_id``; anchor sits right before it."""
    display = [
        {"role": "user", "content": "older turn", "timestamp": 1},
        {"role": "assistant", "content": "older answer", "timestamp": 2},
        {"role": "assistant", "content": "", "timestamp": 3, "reasoning": "historical card"},
        {"role": "user", "content": "continue", "timestamp": 4, "id": successor_id},
        {"role": "assistant", "content": "first answer", "timestamp": 5},
    ]
    context = [copy.deepcopy(display[-2]), copy.deepcopy(display[-1])]
    return display, context


def _reasoning_cards(messages) -> list:
    return [m.get("reasoning") for m in messages]


def _settled_session(monkeypatch, display, context, result_ids=()):
    monkeypatch.setattr(_streaming, "_annotate_media_snapshots_for_settled_messages", lambda m: None)
    session = Session(session_id="b" * 12, title="t", messages=copy.deepcopy(display))
    session.context_messages = copy.deepcopy(context)
    result = copy.deepcopy(context) + [
        {"role": "user", "content": "continue", "timestamp": 10},
        {"role": "assistant", "content": "new answer", "timestamp": 15},
    ]
    for row, rid in zip(result, result_ids, strict=False):
        if rid is not None:
            row["id"] = rid
    _settle_result_messages(
        session, list(session.messages), list(session.context_messages), result, "continue", "webui", None,
    )
    return session, result


# The new "continue" is a distinct turn: exactly one historical card may survive.
_ONE_CARD = [None, None, "historical card", None, None, None, None]


def test_bool_successor_id_never_matches_integer_one(monkeypatch):
    display, context = _identity_hole_history(True)
    session, result = _settled_session(monkeypatch, display, context, result_ids=(None, None, 1, 2))
    assert True == 1 and result[2]["id"] == 1  # noqa: E712 - the hole being closed
    assert _reasoning_cards(session.messages) == _ONE_CARD
    assert all(type(m["id"]) is int for m in result)  # replayed True row re-minted


def test_bool_successor_id_never_matches_minted_integer_one(monkeypatch):
    display, context = _identity_hole_history(True)
    session, result = _settled_session(monkeypatch, display, context)
    assert _reasoning_cards(session.messages) == _ONE_CARD
    assert all(type(m["id"]) is int for m in result)


def test_float_successor_id_never_matches_integer_one(monkeypatch):
    display, context = _identity_hole_history(1.0)
    session, result = _settled_session(monkeypatch, display, context, result_ids=(None, None, 1, 2))
    assert 1.0 == 1 and result[2]["id"] == 1
    assert _reasoning_cards(session.messages) == _ONE_CARD
    assert all(type(m["id"]) is int for m in result)


def test_reused_positive_id_cannot_establish_successor_ownership(monkeypatch):
    display, context = _identity_hole_history(7)
    display[0]["id"] = 7  # the same id is owned by two API-safe historical rows
    session, result = _settled_session(monkeypatch, display, context, result_ids=(7, 8, 7, 9))
    assert [m["id"] for m in result] == [7, 8, 7, 9]
    assert _reasoning_cards(session.messages) == _ONE_CARD


def test_unique_stable_id_control_still_restores_before_its_own_successor(monkeypatch):
    display, context = _identity_hole_history(3)
    display[0]["id"], display[1]["id"], display[4]["id"] = 1, 2, 4
    context[1]["id"] = 4
    session, result = _settled_session(monkeypatch, display, context)
    assert [m["id"] for m in result] == [3, 4, 5, 6]
    assert _reasoning_cards(session.messages) == _ONE_CARD
    assert session.messages[2]["reasoning"] == "historical card"


def test_bool_ids_do_not_survive_save_load_into_minted_collision(tmp_path, monkeypatch):
    from api import models, run_journal

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: sessions)
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())

    display, context = _identity_hole_history(True)
    persisted = Session(session_id="c" * 12, title="t", messages=copy.deepcopy(display))
    persisted.context_messages = copy.deepcopy(context)
    persisted.save()
    loaded = Session.load("c" * 12)
    assert loaded is not None and loaded.messages[3]["id"] is True  # survives save/load

    session, result = _settled_session(
        monkeypatch, loaded.messages, loaded.context_messages, result_ids=(None, None, 1, 2),
    )
    assert result[2]["id"] == 1 and _reasoning_cards(session.messages) == _ONE_CARD
    session.save()
    reloaded = Session.load("b" * 12)
    assert reloaded is not None
    assert _reasoning_cards(reloaded.messages) == _ONE_CARD
    assert [m["id"] for m in reloaded.messages[-2:]] == [1, 2]
    assert all(type(m["id"]) is int for m in result)  # replayed True row re-minted


def _isolated_session_store(tmp_path, monkeypatch):
    from api import models, run_journal

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: sessions)
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    return sessions


def test_idless_successor_card_survives_settle_save_load(tmp_path, monkeypatch):
    """Persisted-display pin: the helper-level id skip never drops the card."""
    _isolated_session_store(tmp_path, monkeypatch)
    display, context = _identity_hole_history(None)
    del display[3]["id"]  # legacy transcript: successor predates stable ids
    context = [copy.deepcopy(display[-2]), copy.deepcopy(display[-1])]
    persisted = Session(session_id="e" * 12, title="t", messages=copy.deepcopy(display))
    persisted.context_messages = copy.deepcopy(context)
    persisted.save()
    loaded = Session.load("e" * 12)
    assert loaded is not None and "id" not in loaded.messages[3]

    session, result = _settled_session(monkeypatch, loaded.messages, loaded.context_messages)
    assert result[0]["id"] is not None  # minted before restore: helper-level skip applies
    session.save()
    reloaded = Session.load("b" * 12)
    assert reloaded is not None
    assert _reasoning_cards(reloaded.messages) == _ONE_CARD
    assert reloaded.messages[2]["reasoning"] == "historical card"
    assert [m["content"] for m in reloaded.messages[-2:]] == ["continue", "new answer"]


class _FakePostHandler:
    def __init__(self):
        self.status, self.headers, self.body, self.wfile = None, {}, bytearray(), self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)


def test_handle_chat_sync_passes_result_turn_authority_to_settlement(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from api import models, routes

    _isolated_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", models.SESSION_INDEX_FILE)
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "m", "provider": "p"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(_streaming, "_annotate_media_snapshots_for_settled_messages", lambda m: None)

    display, context = _assistant_successor_history(4)
    session = Session(
        session_id="g" * 12, workspace=str(tmp_path), messages=copy.deepcopy(display),
        context_messages=copy.deepcopy(context), model="m", model_provider="p",
    )
    session.save(touch_updated_at=False)
    # Whole history re-sent as CURRENT rows; the Agent says the turn starts at 0.
    result_rows = copy.deepcopy(context) + [
        {"role": "user", "content": "do it"}, {"role": "assistant", "content": "Verified."},
    ]
    for row in result_rows:
        row.pop("id", None)
        row.pop("timestamp", None)

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            return {
                "messages": result_rows, "final_response": "Verified.", "completed": True,
                "current_turn_user_idx": 0, "turn_id": "turn-sync-1",
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    handler = _FakePostHandler()
    routes._handle_chat_sync(
        handler, {"session_id": "g" * 12, "message": "do it", "workspace": str(tmp_path)},
    )
    assert handler.status == 200
    # Boundary 0 from exact authority: no historical id (1/2) forged onto current rows.
    assert result_rows[0]["id"] != 1 and result_rows[1]["id"] != 2
    reloaded = Session.load("g" * 12)
    assert reloaded is not None
    assert _clone_blocks(reloaded.messages) == [4]
    assert reloaded.messages[-1]["content"] == "Verified."


@pytest.mark.skipif(
    not _REAL_SIDECAR or not pathlib.Path(_REAL_SIDECAR).is_file(),
    reason="set HERMES_WEBUI_REAL_CLONE_SIDECAR to a captured sidecar to run",
)
def test_real_captured_sidecar_does_not_double_per_settled_turn():
    data = json.loads(pathlib.Path(_REAL_SIDECAR).read_text())
    display = copy.deepcopy(data["messages"])
    context = copy.deepcopy(data.get("context_messages") or [])
    before = sum(_is_clone(m) for m in display)
    assert before > 0
    for turn in range(1, 3):
        display, context = _settle_turn(display, context, turn)
        assert sum(_is_clone(m) for m in display) == before, _clone_blocks(display)
