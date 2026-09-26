"""#6885 slice 2a: pending goal continuations survive a WebUI process restart.

The in-memory ``PENDING_GOAL_CONTINUATION`` marker set is lost on restart: a
goal turn interrupted by a server restart (or closed tab) has no durable
continuation owner, so the frontend's next turn is treated as an ordinary
turn and the standing goal never resumes (#6888's "durable intent" step,
kept intentionally bounded: no DB schema change, atomic file registry only).
"""

from pathlib import Path

import pytest


@pytest.fixture
def state_dir():
    """The registry lives under conftest's isolated WEBUI_STATE_DIR; keep the
    file clean between cases (same shard == same file)."""
    from api import goal_continuation_store as store
    store._PENDING_GOAL_FILE.unlink(missing_ok=True) if store._PENDING_GOAL_FILE else None
    from api.config import STATE_DIR
    yield STATE_DIR
    store._PENDING_GOAL_FILE.unlink(missing_ok=True) if store._PENDING_GOAL_FILE else None


class TestStoreRoundtrip:
    def test_save_then_load_restores_set(self, state_dir):
        from api.goal_continuation_store import load_pending_goal_continuations, save_pending_goal_continuations
        save_pending_goal_continuations({"sess-a", "sess-b"})
        assert load_pending_goal_continuations() == {"sess-a", "sess-b"}

    def test_load_missing_file_returns_empty(self, state_dir):
        from api.goal_continuation_store import load_pending_goal_continuations
        assert load_pending_goal_continuations() == set()

    def test_load_corrupt_file_returns_empty_and_does_not_raise(self, state_dir):
        from api.goal_continuation_store import load_pending_goal_continuations
        (state_dir / "pending_goal_continuations.json").write_text("{not-json[[[", encoding="utf-8")
        assert load_pending_goal_continuations() == set()

    def test_save_is_atomic_no_tmp_leftover(self, state_dir):
        from api.goal_continuation_store import load_pending_goal_continuations, save_pending_goal_continuations
        save_pending_goal_continuations({"sess-a"})
        leftovers = [p.name for p in state_dir.glob("*.tmp")] + [p.name for p in state_dir.glob("*.tmp.*")]
        assert leftovers == []
        assert load_pending_goal_continuations() == {"sess-a"}

    def test_file_location_under_state_dir(self, state_dir):
        from api.goal_continuation_store import _PENDING_GOAL_FILE
        assert _PENDING_GOAL_FILE == state_dir / "pending_goal_continuations.json"


class TestRecoveryWiring:
    def test_recovery_updates_routes_marker_set(self, state_dir):
        """Startup recovery must merge disk state into the running marker set."""
        from api.goal_continuation_store import save_pending_goal_continuations
        save_pending_goal_continuations({"sess-recovered"})
        from api.config import PENDING_GOAL_CONTINUATION
        before = set(PENDING_GOAL_CONTINUATION)
        from api import goal_continuation_store as store
        store.recover_pending_goal_continuations()
        assert "sess-recovered" in PENDING_GOAL_CONTINUATION
        # Never remove live in-memory markers on recovery.
        assert before <= set(PENDING_GOAL_CONTINUATION)

    def test_streaming_add_persists_after_marker_add(self):
        src = Path("api/streaming.py").read_text(encoding="utf-8")
        m = __import__("re").search(r"PENDING_GOAL_CONTINUATION\.add\(session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 400]
        assert "snapshot_pending_goal_continuations" in tail, (
            "durable slice: the streaming add point must snapshot the pending set"
        )

    def test_gateway_add_persists_after_marker_add(self):
        src = Path("api/gateway_chat.py").read_text(encoding="utf-8")
        m = __import__("re").search(r"PENDING_GOAL_CONTINUATION\.add\(session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 400]
        assert "snapshot_pending_goal_continuations" in tail

    def test_routes_consume_persists_after_discard(self):
        src = Path("api/routes.py").read_text(encoding="utf-8")
        m = __import__("re").search(r"PENDING_GOAL_CONTINUATION\.discard\(s\.session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 400]
        assert "snapshot_pending_goal_continuations" in tail

    def test_startup_import_wires_recovery(self):
        """The webui startup path must call recovery (guarded, never blocking)."""
        from api import goal_continuation_store as store
        assert hasattr(store, "recover_pending_goal_continuations")