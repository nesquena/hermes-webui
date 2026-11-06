"""
Tests for #2223: compression session rotation must not destroy session history.

The previous implementation renamed old_sid.json → new_sid.json during context
compression, destroying the only persistent copy of the uncompressed history
before the new session had been saved.  If the summariser also failed, the user
was left with zero recoverable messages.

The fix preserves old_sid.json and creates new_sid.json as a fresh file, setting
parent_session_id to link the lineage.
"""
import json
import pathlib
import textwrap
import threading

import pytest

STREAMING = pathlib.Path(__file__).resolve().parents[1] / "api" / "streaming.py"
streaming_src = STREAMING.read_text(encoding="utf-8")


# ── Structural checks ────────────────────────────────────────────────────────


class TestNoRenameDuringCompression:
    """The destructive old_path.rename(new_path) call must be removed."""

    def test_rename_call_removed(self):
        """old_path.rename(new_path) must not appear in the compression rotation block."""
        # The old code had: if old_path.exists() and not new_path.exists(): old_path.rename(new_path)
        # That line must be gone.
        assert "old_path.rename(new_path)" not in streaming_src, (
            "old_path.rename(new_path) still present — compression rotation "
            "still destroys session history (#2223)"
        )

    def test_parent_session_id_stamped_on_continuation(self):
        """The continuation session must carry parent_session_id linking to old_sid."""
        assert "s.parent_session_id = old_sid" in streaming_src, (
            "parent_session_id not stamped on continuation session (#2223)"
        )

    def test_old_session_preservation_logic_exists(self):
        """There must be logic to preserve the pre-compression session file."""
        assert "Preserved pre-compression session" in streaming_src, (
            "Pre-compression session preservation logging not found (#2223)"
        )


    def test_parent_session_id_stamped_unconditionally(self):
        """Stage-353 Opus SHOULD-FIX: the continuation session's parent_session_id
        must be stamped UNCONDITIONALLY to old_sid (the immediate predecessor).

        The previous `if not s.parent_session_id` guard skipped the stamp on
        fork-of-fork compressions (i.e. when the session already had a
        parent_session_id from a prior /branch operation), so the continuation
        would jump back to the original fork instead of the just-preserved
        snapshot, losing access to the recoverable history in old_sid.json.

        The fix removes the guard: continuation ALWAYS points to the preserved
        snapshot. Traversal then walks new → old → old.parent → ... root.
        """
        # The guarded form is the bug; the unconditional form is the fix.
        assert "if not s.parent_session_id:\n                        s.parent_session_id = old_sid" not in streaming_src, (
            "Guarded parent_session_id stamping resurfaced — breaks fork-of-fork "
            "lineage traversal after compression"
        )

    def test_old_session_parent_preserved_during_archive_save(self):
        """Stage-353 Opus SHOULD-FIX: when preserving old_sid.json to disk, the
        OLD session's parent_session_id must NOT be cleared.

        Previous bug: code did `s.parent_session_id = None; s.save(); s.parent_session_id = _old_parent`.
        The save persisted parent=None to disk; in-memory restoration didn't help.
        Result: fork lineage badge ("Forked from X") disappeared on the old snapshot.
        """
        # The clearing pattern must be gone.
        assert "s.parent_session_id = None" not in streaming_src, (
            "Clearing parent_session_id before preservation save resurfaced — "
            "breaks fork lineage on the old snapshot"
        )

    def test_preservation_helper_marks_snapshot_without_marking_continuation(self, tmp_path, monkeypatch):
        """The rotation preservation path marks only old_sid as a sidebar-hidden snapshot."""
        import api.models as models
        import api.streaming as streaming
        from api.models import Session

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
        models.SESSIONS.clear()

        old = Session(
            session_id="old_sid",
            title="Forked Long Chat",
            parent_session_id="fork_parent",
            messages=[{"role": "user", "content": "before"}],
        )
        old.save()
        continuation = Session(
            session_id="new_sid",
            title="Forked Long Chat",
            parent_session_id="fork_parent",
            messages=[
                {"role": "user", "content": "before"},
                {"role": "assistant", "content": "after"},
            ],
        )

        streaming._preserve_pre_compression_snapshot(continuation, "old_sid")

        old_payload = json.loads((session_dir / "old_sid.json").read_text(encoding="utf-8"))
        assert old_payload["pre_compression_snapshot"] is True
        assert old_payload["parent_session_id"] == "fork_parent"
        assert len(old_payload["messages"]) == 2
        index = json.loads((session_dir / "_index.json").read_text(encoding="utf-8"))
        index_by_id = {entry["session_id"]: entry for entry in index}
        assert index_by_id["old_sid"]["pre_compression_snapshot"] is True
        assert "new_sid" not in index_by_id
        assert continuation.session_id == "new_sid"
        assert continuation.parent_session_id == "fork_parent"
        assert not continuation.pre_compression_snapshot

    def test_preservation_helper_marks_only_disjoint_continuations(self, tmp_path, monkeypatch):
        """Persist a parent offset only when the full merge adds every child row."""
        import api.models as models
        import api.streaming as streaming
        from api.models import Session

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
        models.SESSIONS.clear()

        parent_messages = [
            {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
            for idx in range(1000)
        ]
        parent = Session(session_id="disjoint_parent", messages=parent_messages)
        parent.save(touch_updated_at=False)
        disjoint_child = Session(
            session_id="disjoint_child",
            messages=[
                {"role": "assistant", "content": f"child {idx}", "timestamp": 1000.0 + idx}
                for idx in range(500)
            ],
        )

        assert streaming._preserve_pre_compression_snapshot(
            disjoint_child,
            "disjoint_parent",
        ) is True
        boundary = streaming._compression_disjoint_parent_boundary(
            disjoint_child,
            "disjoint_parent",
        )
        assert boundary == {
            "parent_session_id": "disjoint_parent",
            "message_count": len(parent_messages),
            "visible_message_count": len(parent_messages),
            "last_message_at": 999.0,
            "parent_updated_at": parent.updated_at,
            "child_message_count": len(disjoint_child.messages),
            "child_messages_digest": models._compression_child_messages_digest(
                disjoint_child.messages
            ),
            "canonical_parent_proof": True,
        }

        replay_parent = Session(session_id="replay_parent", messages=parent_messages)
        replay_parent.save(touch_updated_at=False)
        suffix_replay_child = Session(
            session_id="suffix_replay_child",
            messages=parent_messages[-100:]
            + [
                {"role": "assistant", "content": f"child {idx}", "timestamp": 1000.0 + idx}
                for idx in range(400)
            ],
        )

        assert streaming._preserve_pre_compression_snapshot(
            suffix_replay_child,
            "replay_parent",
        ) is True
        assert streaming._compression_disjoint_parent_boundary(
            suffix_replay_child,
            "replay_parent",
        ) is None

        noncanonical_messages = list(parent_messages[:-1])
        noncanonical_messages.insert(470, dict(noncanonical_messages[470]))
        noncanonical_parent = Session(
            session_id="noncanonical_parent",
            messages=noncanonical_messages,
        )
        noncanonical_parent.save(touch_updated_at=False)
        canonical_child = Session(
            session_id="canonical_child",
            messages=[
                {"role": "assistant", "content": f"child {idx}", "timestamp": 1000.0 + idx}
                for idx in range(500)
            ],
        )

        assert streaming._disjoint_compression_parent_message_count(
            noncanonical_messages,
            canonical_child.messages,
        ) is None


class TestMergePreservesHistory:
    """_merge_display_messages_after_agent_result must preserve all previous
    display messages when compression returns only a marker."""

    @pytest.fixture
    def merge(self):
        from api.streaming import _merge_display_messages_after_agent_result
        return _merge_display_messages_after_agent_result

    def test_marker_only_preserves_all_previous(self, merge):
        """When result is just a compression-failure marker, previous display survives."""
        previous_display = [
            {"role": "user", "content": f"msg{i}"} for i in range(100)
        ] + [
            {"role": "assistant", "content": f"reply{i}"} for i in range(100)
        ]
        previous_context = list(previous_display)
        marker = {
            "role": "user",
            "content": (
                "Summary generation was unavailable. 200 message(s) were removed "
                "to free context space but could not be summarized."
            ),
        }
        result = [marker, {"role": "user", "content": "continue"}, {"role": "assistant", "content": "ok"}]

        merged = merge(previous_display, previous_context, result, "continue")

        # All 200 original messages must survive.
        assert len(merged) >= 200
        for i in range(100):
            assert merged[i]["content"] == f"msg{i}"

    def test_empty_result_preserves_all_previous(self, merge):
        """If result_messages is empty, previous display is returned unchanged."""
        previous_display = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        previous_context = list(previous_display)

        merged = merge(previous_display, previous_context, [], "test")

        assert merged == previous_display

    def test_none_result_preserves_all_previous(self, merge):
        """If result_messages is None, previous display is returned unchanged."""
        previous_display = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        previous_context = list(previous_display)

        merged = merge(previous_display, previous_context, None, "test")

        assert merged == previous_display


def test_cumulative_rotation_skips_disjoint_boundary_full_history_work(monkeypatch, tmp_path):
    import api.models as models
    import api.streaming as streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)

    old_sid = "cumulative_rotation_parent"
    parent_messages = [
        {"role": "assistant", "content": f"parent {index}", "timestamp": float(index)}
        for index in range(1000)
    ]
    models.Session(session_id=old_sid, messages=parent_messages).save()
    rotating = models.Session(
        session_id=old_sid,
        messages=[
            *[dict(message) for message in parent_messages],
            {"role": "user", "content": "next", "timestamp": 1000.0},
            {"role": "assistant", "content": "answer", "timestamp": 1001.0},
        ],
    )
    new_sid = "cumulative_rotation_child"
    agent_lock = streaming._get_session_agent_lock(old_sid)
    with streaming.LOCK:
        models.SESSIONS.clear()
        models.SESSIONS[old_sid] = rotating

    full_parent_loads = 0
    merge_calls = 0
    real_load = models.Session.load.__func__
    real_merge = streaming.merge_session_messages_append_only

    def counted_load(cls, session_id):
        nonlocal full_parent_loads
        if session_id == old_sid:
            full_parent_loads += 1
        return real_load(cls, session_id)

    def counted_merge(*args, **kwargs):
        nonlocal merge_calls
        merge_calls += 1
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(models.Session, "load", classmethod(counted_load))
    monkeypatch.setattr(streaming, "merge_session_messages_append_only", counted_merge)

    assert streaming._migrate_compression_session_ownership(
        rotating,
        old_sid,
        new_sid,
        agent_lock,
    ) is True

    assert full_parent_loads == 0
    assert merge_calls == 0
    assert rotating.parent_session_id == old_sid
    assert rotating.compression_disjoint_parent_boundary is None
    assert models.SESSIONS[new_sid] is rotating


@pytest.mark.parametrize("persisted_count", [True, False, 1.5, "1"])
def test_marked_prefix_count_rejects_non_exact_integers(tmp_path, persisted_count):
    import api.models as models

    path = tmp_path / "malformed-count.json"
    path.write_text(
        json.dumps(
            {
                "session_id": "malformed-count",
                "title": "Malformed count",
                "created_at": 1.0,
                "updated_at": 2.0,
                "_mc_v": models._MESSAGE_COUNT_MARKER,
                "message_count": persisted_count,
                "messages": [{"role": "user", "content": "one"}],
            }
        ),
        encoding="utf-8",
    )

    assert models._prefix_message_count(path) is None


@pytest.mark.parametrize("persisted_count", [True, False, 1.5, -1])
def test_invalid_persisted_count_cannot_skip_compression_boundary(
    persisted_count,
):
    import api.streaming as streaming

    session = type(
        "SessionStub",
        (),
        {
            "messages": [
                {"role": "assistant", "content": "parent", "timestamp": 1.0},
                {"role": "user", "content": "next", "timestamp": 2.0},
            ]
        },
    )()

    assert streaming._compression_boundary_may_add_parent_rows(
        session,
        persisted_count,
    ) is True


@pytest.mark.parametrize(
    ("persisted_count", "expected"),
    [(0, False), (1, False), (2, True), (3, True)],
)
def test_exact_persisted_count_only_skips_for_a_strictly_longer_sequence(
    persisted_count,
    expected,
):
    import api.streaming as streaming

    session = type(
        "SessionStub",
        (),
        {
            "messages": [
                {"role": "assistant", "content": "parent", "timestamp": 1.0},
                {"role": "user", "content": "next", "timestamp": 2.0},
            ]
        },
    )()

    assert streaming._compression_boundary_may_add_parent_rows(
        session,
        persisted_count,
    ) is expected
