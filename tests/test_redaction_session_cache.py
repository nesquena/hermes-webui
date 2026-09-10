"""Regression: `redact_session_lists_cached` — the persisted per-message
redaction cache for large-session conversation switches.

Locks:
  * first call computes + persists a cache file; secrets are redacted,
  * repeat call serves from cache (zero `_redact_messages` work),
  * appended messages recompute individually (append-mostly splice),
  * corrupt/foreign cache files fall back gracefully (never fail a response),
  * `api_redact_enabled` participates in validation (toggle recomputes),
  * the per-request `_active_turn_user` decoration is applied after retrieval
    and is NEVER persisted to the cache file.
  * `delete_redaction_session_cache` removes the file on session deletion
    (deleted conversations must not linger in `redaction_cache/`) and is a
    safe no-op for missing/unsafe ids.
"""
import json

import pytest

from api import helpers as H
from api.helpers import delete_redaction_session_cache, redact_session_lists_cached


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    import api.config
    monkeypatch.setattr(api.config, "STATE_DIR", tmp_path)
    return tmp_path


def _msgs(secret="sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
    return [
        {"role": "user", "content": f"hello one {secret}"},
        {"role": "assistant", "content": "plain reply"},
    ]


def _spy(monkeypatch):
    calls = {"n": 0}
    real = H._redact_messages

    def counting(messages, **kwargs):
        calls["n"] += len(messages) if isinstance(messages, list) else 1
        return real(messages, **kwargs)

    monkeypatch.setattr(H, "_redact_messages", counting)
    return calls


def test_cold_computes_persists_and_redacts(state_dir):
    msgs = _msgs()
    out = redact_session_lists_cached("sessA", {"messages": msgs})
    assert _SECRET_STATE_OK(out)
    cache_file = state_dir / "redaction_cache" / "sessA.json"
    assert cache_file.exists()
    stored = json.loads(cache_file.read_text())
    assert stored["enabled"] is True
    # persisted projections are decoration-free and redacted
    assert all("_active_turn_user" not in m for m in stored["lists"]["messages"])


def _SECRET_STATE_OK(payload):
    text = json.dumps(payload)
    return "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in text


def test_repeat_serves_from_cache_zero_redaction_work(state_dir, monkeypatch):
    calls = _spy(monkeypatch)
    msgs = _msgs()
    redact_session_lists_cached("sessB", {"messages": msgs})
    first = calls["n"]
    assert first == len(msgs)
    out2 = redact_session_lists_cached("sessB", {"messages": msgs})
    assert calls["n"] == first  # zero new redaction work — spliced from cache
    assert out2["messages"][0]["content"].startswith("hello one")


def test_append_recomputes_only_new_message(state_dir, monkeypatch):
    calls = _spy(monkeypatch)
    msgs = _msgs()
    redact_session_lists_cached("sessC", {"messages": msgs})
    assert calls["n"] == len(msgs)
    first_count = calls["n"]
    msgs.append({"role": "user", "content": "a fresh follow-up message"})
    out = redact_session_lists_cached("sessC", {"messages": msgs})
    assert calls["n"] == first_count + 1  # +1: only the appended item recomputed
    assert out["messages"][-1]["content"] == "a fresh follow-up message"


def test_change_to_existing_message_recomputes_it(state_dir, monkeypatch):
    calls = _spy(monkeypatch)
    msgs = _msgs()
    redact_session_lists_cached("sessD", {"messages": msgs})
    assert calls["n"] == 2
    msgs[1]["content"] = "assistant reply was edited"
    out = redact_session_lists_cached("sessD", {"messages": msgs})
    assert calls["n"] == 3  # the edited message was recomputed
    assert out["messages"][1]["content"] == "assistant reply was edited"


def test_corrupt_cache_falls_back_gracefully(state_dir):
    redact_session_lists_cached("sessE", {"messages": _msgs()})
    cache_file = state_dir / "redaction_cache" / "sessE.json"
    cache_file.write_text("{not json at all")
    out = redact_session_lists_cached("sessE", {"messages": _msgs()})
    assert _SECRET_STATE_OK(out)


def test_enabled_toggle_recomputes(state_dir, monkeypatch):
    import api.config
    msgs = _msgs()
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    redact_session_lists_cached("sessF", {"messages": msgs})
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    out = redact_session_lists_cached("sessF", {"messages": msgs})
    # disabled = verbatim passthrough; the enabled=True cache must not serve
    assert out["messages"][0]["content"] == msgs[0]["content"]
    assert _SECRET_STATE_OK({"x": "cleared"})  # sanity: helper intact


def test_turn_decoration_applied_but_never_persisted(state_dir):
    msgs = [
        {"role": "user", "content": "hi", "_active_turn_token": "tok-1"},
        {"role": "assistant", "content": "ho"},
    ]
    out = redact_session_lists_cached(
        "sessG", {"messages": msgs}, _active_turn_token="tok-1")
    assert out["messages"][0].get("_active_turn_user") is True
    stored = json.loads(
        (state_dir / "redaction_cache" / "sessG.json").read_text())
    assert all("_active_turn_user" not in m for m in stored["lists"]["messages"])
    # a later request without the token must not see a stale flag
    out2 = redact_session_lists_cached("sessG", {"messages": msgs})
    assert all("_active_turn_user" not in m for m in out2["messages"])


def test_midlist_insertion_never_serves_stale_projection(state_dir):
    # Insert at index 0 shifts every digest: the splice must not pair old
    # projections with the wrong messages.
    msgs = [
        {"role": "user", "content": "alpha message"},
        {"role": "assistant", "content": "beta reply"},
    ]
    redact_session_lists_cached("sessIns", {"messages": msgs})
    msgs.insert(0, {"role": "user", "content": "zeroth inserted message"})
    out = redact_session_lists_cached("sessIns", {"messages": msgs})
    assert [m["content"] for m in out["messages"]] == [
        "zeroth inserted message", "alpha message", "beta reply",
    ]


def test_truncation_serves_matching_prefix(state_dir, monkeypatch):
    calls = _spy(monkeypatch)
    msgs = [
        {"role": "user", "content": "keep one"},
        {"role": "assistant", "content": "keep two"},
        {"role": "user", "content": "drop three"},
    ]
    redact_session_lists_cached("sessTrunc", {"messages": msgs})
    assert calls["n"] == 3
    cache_file = state_dir / "redaction_cache" / "sessTrunc.json"
    stored_before = json.loads(cache_file.read_text(encoding="utf-8"))
    assert len(stored_before["lists"]["messages"]) == 3

    out = redact_session_lists_cached("sessTrunc", {"messages": msgs[:2]})
    assert calls["n"] == 3  # prefix spliced, nothing recomputed
    assert [m["content"] for m in out["messages"]] == ["keep one", "keep two"]

    # Regression (#7452 review): truncation to a full prefix must overwrite on-disk projection
    stored_after = json.loads(cache_file.read_text(encoding="utf-8"))
    assert len(stored_after["lists"]["messages"]) == 2
    assert len(stored_after["digests"]["messages"]) == 2
    assert [m["content"] for m in stored_after["lists"]["messages"]] == ["keep one", "keep two"]


def test_get_vs_delete_race_never_republishes_deleted_session_projection(state_dir, monkeypatch):
    import os
    from api.helpers import _redact_session_cache_path

    # Initial session and populated cache
    msgs = _msgs()
    redact_session_lists_cached("sessRace", {"messages": msgs})
    path = _redact_session_cache_path("sessRace")
    assert path.exists()

    # Interleave a delete immediately before os.replace in redact_session_lists_cached
    real_replace = os.replace

    def racing_replace(src, dst):
        if "sessRace" in str(dst):
            # Concurrent delete fires before the prepared tmp is published
            delete_redaction_session_cache("sessRace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", racing_replace)

    # Trigger write path with modified messages
    msgs_new = _msgs() + [{"role": "user", "content": "racing message"}]
    redact_session_lists_cached("sessRace", {"messages": msgs_new})

    # The projection must NOT be republished for the deleted session
    assert not path.exists()


def test_recreated_session_can_populate_cache_after_delete(state_dir):
    from api.helpers import _redact_session_cache_path

    path = _redact_session_cache_path("sessRecreate")
    redact_session_lists_cached("sessRecreate", {"messages": _msgs()})
    assert path.exists()

    assert delete_redaction_session_cache("sessRecreate") is True
    assert not path.exists()

    # When the session is recreated, caching should resume normally
    new_msgs = [{"role": "user", "content": "hello newly created session"}]
    redact_session_lists_cached("sessRecreate", {"messages": new_msgs})
    assert path.exists()
    cached = json.loads(path.read_text(encoding="utf-8"))
    assert [m["content"] for m in cached["lists"]["messages"]] == ["hello newly created session"]


def test_real_thread_deletion_during_prepublication_redaction_never_republishes(state_dir, monkeypatch):
    import threading
    from api.helpers import _redact_session_cache_path, _redact_messages

    sid = "sessThreadPrepub"
    path = _redact_session_cache_path(sid)

    # Populate initial cache
    redact_session_lists_cached(sid, {"messages": _msgs()})
    assert path.exists()

    # Intercept pre-publication redaction work to trigger a concurrent real-thread delete
    entered_redaction = threading.Event()
    delete_done = threading.Event()
    real_redact_messages = _redact_messages

    def slow_redact_messages(*args, **kwargs):
        entered_redaction.set()
        delete_done.wait(timeout=5.0)
        return real_redact_messages(*args, **kwargs)

    monkeypatch.setattr("api.helpers._redact_messages", slow_redact_messages)

    # Start background writer with modified messages that require cache write
    new_msgs = _msgs() + [{"role": "user", "content": "in-flight addition"}]
    writer_thread = threading.Thread(
        target=redact_session_lists_cached,
        args=(sid, {"messages": new_msgs}),
    )
    writer_thread.start()

    # Wait until background writer enters redaction before publication
    assert entered_redaction.wait(timeout=5.0)

    # Main thread deletes the session cache while background writer is working
    assert delete_redaction_session_cache(sid) is True
    assert not path.exists()

    # Allow background writer to proceed to publication attempt
    delete_done.set()
    writer_thread.join(timeout=5.0)
    assert not writer_thread.is_alive()

    # The stale background writer MUST NOT have republished the deleted session cache
    assert not path.exists()

    # Positive control: subsequent session recreation publishes properly
    monkeypatch.setattr("api.helpers._redact_messages", real_redact_messages)
    out_recreated = redact_session_lists_cached(sid, {"messages": new_msgs})
    assert path.exists()
    cached = json.loads(path.read_text(encoding="utf-8"))
    assert [m["content"] for m in cached["lists"]["messages"]] == [m["content"] for m in out_recreated["messages"]]


def test_monotonic_generation_prevents_aba_republication(state_dir):
    from api.helpers import _REDACTION_SESSION_GEN, _redact_session_cache_path

    sid = "sessMonotonicABA"
    path = _redact_session_cache_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    # Initial token is default 0
    token_0 = _REDACTION_SESSION_GEN.get(sid, 0)
    assert token_0 == 0

    # Delete session increments generation
    assert delete_redaction_session_cache(sid) is True
    token_1 = _REDACTION_SESSION_GEN.get(sid, 0)
    assert token_1 == 1

    # Simulate repeated deletions/churn; generation must be strictly monotonic
    for i in range(2, 20):
        path.write_text("{}", encoding="utf-8")
        assert delete_redaction_session_cache(sid) is True
        assert _REDACTION_SESSION_GEN.get(sid, 0) == i

    # Old tokens (like token_0) can never match live generation
    assert _REDACTION_SESSION_GEN.get(sid, 0) > token_0
    assert not path.exists()


def test_distinct_session_deletes_leave_generation_bookkeeping_bounded(state_dir, monkeypatch):
    from api.helpers import _REDACTION_SESSION_GEN, _MAX_REDACTION_GEN_CAP, delete_redaction_session_cache, _redact_session_cache_path

    # 1. Many deletes of missing valid IDs must not grow bookkeeping at all (0 entries admitted)
    initial_len = len(_REDACTION_SESSION_GEN)
    for i in range(5000):
        assert delete_redaction_session_cache(f"missing_{i}") is False
    assert len(_REDACTION_SESSION_GEN) == initial_len
    assert not any(k.startswith("missing_") for k in _REDACTION_SESSION_GEN)

    # 2. Heavy churn of existing sessions must stay strictly bounded by _MAX_REDACTION_GEN_CAP
    cap = 25
    monkeypatch.setattr("api.helpers._MAX_REDACTION_GEN_CAP", cap)

    for i in range(100):
        sid = f"churn_{i}"
        p = _redact_session_cache_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        assert delete_redaction_session_cache(sid) is True
    assert len(_REDACTION_SESSION_GEN) <= cap


def test_paused_predelete_writer_cannot_publish_across_reclamation_bound(state_dir, monkeypatch):
    import threading
    from api.helpers import (
        _redact_session_cache_path,
        _redact_messages,
        _REDACTION_SESSION_GEN,
        _REDACTION_IN_FLIGHT,
        delete_redaction_session_cache,
    )

    cap = 10
    monkeypatch.setattr("api.helpers._MAX_REDACTION_GEN_CAP", cap)

    sid = "sessPausedWriterBound"
    path = _redact_session_cache_path(sid)

    # Populate initial cache
    redact_session_lists_cached(sid, {"messages": _msgs()})
    assert path.exists()

    # Intercept pre-publication redaction to simulate a paused in-flight writer
    entered_redaction = threading.Event()
    churn_done = threading.Event()
    real_redact_messages = _redact_messages

    def slow_redact_messages(*args, **kwargs):
        entered_redaction.set()
        churn_done.wait(timeout=5.0)
        return real_redact_messages(*args, **kwargs)

    monkeypatch.setattr("api.helpers._redact_messages", slow_redact_messages)

    new_msgs = _msgs() + [{"role": "user", "content": "racing message while churn crosses bound"}]
    writer_thread = threading.Thread(
        target=redact_session_lists_cached,
        args=(sid, {"messages": new_msgs}),
    )
    writer_thread.start()

    assert entered_redaction.wait(timeout=5.0)
    assert _REDACTION_IN_FLIGHT.get(sid, 0) == 1

    # Delete the target session while writer is paused
    assert delete_redaction_session_cache(sid) is True
    assert not path.exists()
    assert sid in _REDACTION_SESSION_GEN

    # Heavily cross the reclamation cap with other deleted sessions
    for i in range(50):
        other_sid = f"other_{i}"
        p = _redact_session_cache_path(other_sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        delete_redaction_session_cache(other_sid)

    # Target session must NOT have been evicted because its writer is still in-flight
    assert sid in _REDACTION_SESSION_GEN
    assert len(_REDACTION_SESSION_GEN) <= cap + 1

    # Resume the paused writer
    churn_done.set()
    writer_thread.join(timeout=5.0)
    assert not writer_thread.is_alive()

    # Stale writer must NOT have published the cache file
    assert not path.exists()

    # Positive control: subsequent recreation works
    monkeypatch.setattr("api.helpers._redact_messages", real_redact_messages)
    out_recreated = redact_session_lists_cached(sid, {"messages": new_msgs})
    assert path.exists()
    cached = json.loads(path.read_text(encoding="utf-8"))
    assert [m["content"] for m in cached["lists"]["messages"]] == [m["content"] for m in out_recreated["messages"]]





def test_delete_removes_cache_file_leaves_sibling_intact(state_dir):
    # Deleting a session must remove its redaction-cache file (a deleted
    # conversation is not recoverable from redaction_cache/), while an
    # unrelated session's cache is untouched.
    redact_session_lists_cached("sessDel", {"messages": _msgs()})
    redact_session_lists_cached("sessKeep", {"messages": _msgs()})
    doomed = state_dir / "redaction_cache" / "sessDel.json"
    sibling = state_dir / "redaction_cache" / "sessKeep.json"
    assert doomed.exists() and sibling.exists()
    assert delete_redaction_session_cache("sessDel") is True
    assert not doomed.exists()
    assert sibling.exists()
    # second delete is a no-op
    assert delete_redaction_session_cache("sessDel") is False


def test_delete_noop_on_missing_or_invalid(state_dir):
    assert delete_redaction_session_cache("nope") is False
    assert delete_redaction_session_cache("") is False
    assert delete_redaction_session_cache("../escape") is False
    assert delete_redaction_session_cache("..\\escape") is False
    assert delete_redaction_session_cache(".") is False
    # nothing escaped the cache dir
    assert list((state_dir / "redaction_cache").glob("*.json")) == []
    assert (state_dir / "escape.json").exists() is False


def _utime(path, mtime_ns):
    import os
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_rules_content_digest_not_pathname_metadata(tmp_path):
    # Regression (#7414 review): a rules identity must be CONTENT, not pathname
    # metadata. Two different policy byte-for-byte files that share size AND
    # mtime_ns must produce DIFFERENT content digests — otherwise a stale
    # projection survives a redaction-policy change that happens to retain the
    # same file size/mtime. We test the content-identity primitive directly:
    # the rules key captures these digests once at import, so a mid-process
    # file swap must NOT change the key (only a restart with new bytes does).
    import os
    body_a = "VALUE = 1\n"
    body_b = "VALUE = 2\n"
    assert len(body_a) == len(body_b)  # same size by construction
    fixed_mtime = 1234567890123456789
    path = tmp_path / "policy.py"
    path.write_bytes(body_a.encode("utf-8"))
    _utime(path, fixed_mtime)
    digest_a = H._content_digest(path)
    assert digest_a is not None
    # Rewrite with different bytes, same size, same mtime_ns.
    path.write_bytes(body_b.encode("utf-8"))
    _utime(path, fixed_mtime)
    st_b = os.stat(path)
    digest_b = H._content_digest(path)
    assert digest_b is not None
    assert H._content_digest(path) == digest_b  # deterministic
    assert st_b.st_size == len(body_a)  # identical size
    assert digest_a != digest_b, "content digest must change when policy bytes change"


def test_rules_key_captured_at_import_matches_module_digest():
    # The rules key's content digests are captured ONCE at import, so they match
    # this module's actual source bytes (the policy that was loaded, not the
    # on-disk bytes at call time — the TOCTOU fix).
    assert H._REDACT_RULES_HELPERS_DIGEST == H._content_digest(H.__file__)
    assert H._REDACT_RULES_HELPERS_DIGEST is not None
    assert H._redact_session_cache_rules_key() is not None


def test_rules_key_is_stable_per_process(monkeypatch):
    # The rules key is frozen at import (matching the loaded policy), so it must
    # be stable across calls and independent of the WebUI version stamp (which
    # was removed from the identity — a constant 'unknown'/None can't collapse
    # the key). This is the "unchanged rule identity -> zero-work" control.
    k1 = H._redact_session_cache_rules_key()
    assert k1 is not None
    assert H._redact_session_cache_rules_key() == k1
    import api.config as C
    monkeypatch.setattr(C, "_current_webui_version", lambda: None)
    assert H._redact_session_cache_rules_key() == k1
    monkeypatch.setattr(C, "_current_webui_version", lambda: "unknown")
    assert H._redact_session_cache_rules_key() == k1
    monkeypatch.setattr(C, "_current_webui_version", lambda: "v0.52.264")
    assert H._redact_session_cache_rules_key() == k1


def test_rules_key_none_fail_closed_recomputes(state_dir, monkeypatch):
    # Regression (#7414 review): if no trustworthy content identity can be
    # computed (rules_key is None), the persistent cache must be SKIPPED — not
    # read-and-reused, and not freshly re-validated against an authorizable key.
    msgs = _msgs()
    redact_session_lists_cached("sessNone", {"messages": msgs})  # seed a valid cache
    calls = _spy(monkeypatch)
    monkeypatch.setattr(H, "_redact_session_cache_rules_key", lambda: None)

    out = redact_session_lists_cached("sessNone", {"messages": msgs})

    # Fail-closed: full recompute, nothing spliced from the seeded cache.
    assert calls["n"] == len(msgs)
    assert _SECRET_STATE_OK(out)
    # A None identity must NOT be persisted as an authorizable cache key.
    stored = json.loads((state_dir / "redaction_cache" / "sessNone.json").read_text())
    assert stored["rules_key"] is not None


def test_delete_clears_in_memory_redaction_memos(state_dir):
    # The in-memory decision/redactor LRUs key on ORIGINAL strings (including
    # plaintext secrets) and are retained process-wide; deleting a session must
    # drop them from RAM, not just the on-disk projection (#7414 review follow-up
    # on the adversarial review's secret-retention finding).
    secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    H._redact_text_lru.cache_clear()
    H._redact_fn_lru.cache_clear()
    H._redact_text_big_lru.cache_clear()
    sample = f"delete me and my secret {secret} must not linger"
    out = H._redact_text(sample, _enabled=True)
    assert secret not in out
    assert H._redact_text_lru.cache_info().currsize >= 1
    assert H._redact_fn_lru.cache_info().currsize >= 1
    # Deleting a valid session (even with no projection file) clears the memos.
    assert delete_redaction_session_cache("sessRam") is False  # no on-disk file
    assert H._redact_text_lru.cache_info().currsize == 0
    assert H._redact_fn_lru.cache_info().currsize == 0
    assert H._redact_text_big_lru.cache_info().currsize == 0
