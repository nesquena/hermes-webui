"""Big-session save-hotpath regression tests (2026-07-13).

Covers the changes that keep a multi-MB session usable:

1. Session.save() writes compact JSON (no indent) that every existing reader —
   full json.loads and the metadata-prefix scanner — still parses.
2. The #1558 backup safeguard takes the metadata-prefix fast path on normal
   grow-the-conversation saves and still produces a .bak on shrink.
3. Byte-identical re-saves skip the disk write entirely (no-op skip).
4. Composer drafts persist to a tiny sidecar file instead of rewriting the
   whole session JSON; sidecar wins over the legacy in-file field.
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

import pytest

import api.config as _cfg
import api.models as _models
from api.models import (
    Session,
    composer_draft_sidecar_path,
    delete_composer_draft_sidecar,
    read_composer_draft_sidecar,
    resolve_composer_draft,
    write_composer_draft_sidecar,
)


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    """Isolated SESSION_DIR + index + caches, mirroring test_session_duplicate_edit."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index_file = sessions_dir / "_index.json"
    # api.models imports SESSION_DIR/SESSION_INDEX_FILE at module level, so
    # patch BOTH api.config and api.models bindings.
    monkeypatch.setattr(_cfg, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(_cfg, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_file)
    with _models.LOCK:
        _models.SESSIONS.clear()
    _models._DRAFT_SIDECAR_CACHE.clear()
    yield sessions_dir
    with _models.LOCK:
        _models.SESSIONS.clear()
    _models._DRAFT_SIDECAR_CACHE.clear()


def _msg(role, content, idx):
    return {"id": f"m{idx}", "role": role, "content": content, "timestamp": float(idx)}


def _make_session(sid, n_messages):
    s = Session(session_id=sid, title="hotpath test")
    s.messages = [_msg("user" if i % 2 == 0 else "assistant", f"msg {i}", i) for i in range(n_messages)]
    return s


class _NullingFastJson:
    """Minimal orjson-shaped codec that exposes its non-finite behaviour."""

    OPT_NON_STR_KEYS = 1

    @staticmethod
    def dumps(obj, option=0):
        def normalize(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            return value

        return json.dumps(normalize(obj), separators=(",", ":")).encode("utf-8")

    @staticmethod
    def loads(text):
        def reject_nonfinite(value):
            raise ValueError(f"non-finite literal {value}")

        return json.loads(text, parse_constant=reject_nonfinite)


def test_save_writes_compact_json_metadata_prefix_still_readable(session_store):
    s = _make_session("hotpath1", 4)
    s.save(skip_index=True)
    text = (session_store / "hotpath1.json").read_text(encoding="utf-8")
    # Compact output: no pretty-print newlines (escaped \n inside strings is fine).
    assert text.count("\n") == 0, "session JSON should be compact, not indented"
    parsed = json.loads(text)
    assert len(parsed["messages"]) == 4
    assert parsed["message_count"] == 4
    # The metadata-prefix scanner must still work on the compact layout.
    meta = Session.load_metadata_only("hotpath1")
    assert meta is not None
    assert getattr(meta, "_loaded_metadata_only", False) is True
    assert meta._metadata_message_count == 4
    assert meta.messages == []
    # And a full load round-trips.
    full = Session.load("hotpath1")
    assert len(full.messages) == 4


def test_backup_guard_grow_makes_no_bak_shrink_makes_bak(session_store):
    s = _make_session("hotpath2", 3)
    s.save(skip_index=True)
    s.messages.append(_msg("user", "grow", 3))
    s.save(skip_index=True)
    bak = session_store / "hotpath2.json.bak"
    assert not bak.exists(), "grow-the-conversation saves must not produce a backup"
    # Shrink → .bak snapshot of the pre-shrink state.
    s2 = Session.load("hotpath2")
    s2.messages = s2.messages[:1]
    s2.save(skip_index=True)
    assert bak.exists(), "a shrinking save must leave a recoverable .bak"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 4
    assert len(json.loads((session_store / "hotpath2.json").read_text(encoding="utf-8"))["messages"]) == 1


def test_backup_guard_does_not_trust_stale_message_count_on_shrink(session_store):
    """External/legacy writers may leave a derived prefix count stale."""
    s = _make_session("hotpath-stale-count", 3)
    s.save(skip_index=True)
    payload = json.loads(s.path.read_text(encoding="utf-8"))
    payload["message_count"] = 1  # stale low count, but three real messages
    s.path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    incoming = Session.load("hotpath-stale-count")
    assert incoming is not None
    incoming.messages = incoming.messages[:1]
    incoming.save(skip_index=True)

    backup = s.path.with_suffix(".json.bak")
    assert backup.exists()
    assert len(json.loads(backup.read_text(encoding="utf-8"))["messages"]) == 3
    assert len(json.loads(s.path.read_text(encoding="utf-8"))["messages"]) == 1


def test_backup_guard_refuses_empty_overwrite_with_active_stream(session_store):
    s = _make_session("hotpath3", 2)
    s.save(skip_index=True)
    stub = Session(session_id="hotpath3", title="stub")
    stub.messages = []
    stub.active_stream_id = "stream-x"
    stub.save(skip_index=True)
    on_disk = json.loads((session_store / "hotpath3.json").read_text(encoding="utf-8"))
    assert len(on_disk["messages"]) == 2, (
        "an empty active/pending snapshot must not overwrite a populated session"
    )


def test_noop_resave_skips_disk_write(session_store):
    s = _make_session("hotpath4", 2)
    s.save(skip_index=True)
    p = session_store / "hotpath4.json"
    st1 = os.stat(p)
    # Identical content, no updated_at touch → must not rewrite the file.
    s.save(touch_updated_at=False, skip_index=True)
    st2 = os.stat(p)
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size), (
        "byte-identical re-save should skip the disk write"
    )
    # Real change → must write.
    s.title = "changed"
    s.save(touch_updated_at=False, skip_index=True)
    st3 = os.stat(p)
    assert st3.st_mtime_ns != st1.st_mtime_ns
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "changed"


def test_noop_skip_defers_to_external_writer(session_store):
    s = _make_session("hotpath5", 1)
    s.save(skip_index=True)
    p = session_store / "hotpath5.json"
    # Another writer replaces the file (different Session object / process).
    other = Session.load("hotpath5")
    other.title = "external"
    other.save(skip_index=True)
    # The original object's payload is unchanged, but the file on disk moved on:
    # the skip must not fire, and this save must win with its own content.
    s.save(touch_updated_at=False, skip_index=True)
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "hotpath test"


@pytest.mark.parametrize("replacement", ["atomic", "inplace"])
def test_noop_skip_detects_same_stat_external_replacement(session_store, replacement):
    s = _make_session(f"hotpath_same_stat_{replacement}", 1)
    s.title = "owned-title"
    s.save(touch_updated_at=False, skip_index=True)
    p = s.path
    original_stat = p.stat()
    external = p.read_text(encoding="utf-8").replace("owned-title", "other-title")
    assert len(external.encode("utf-8")) == original_stat.st_size

    if replacement == "atomic":
        replacement_path = p.with_suffix(".external")
        replacement_path.write_text(external, encoding="utf-8")
        os.replace(replacement_path, p)
    else:
        with p.open("r+", encoding="utf-8") as handle:
            handle.write(external)
            handle.flush()
            os.fsync(handle.fileno())
    os.utime(p, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    same_stat = p.stat()
    assert (same_stat.st_mtime_ns, same_stat.st_size) == (
        original_stat.st_mtime_ns,
        original_stat.st_size,
    )

    s.save(touch_updated_at=False, skip_index=True)
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "owned-title"


def test_noop_skip_detects_atomic_replacement_after_digest_read(
    session_store, monkeypatch
):
    s = _make_session("digest-race-atomic", 2)
    s.title = "owned-A"
    s.save(touch_updated_at=False, skip_index=True)
    p = s.path
    owned = p.read_bytes()
    original_stat = p.stat()
    replacement = owned.replace(b"owned-A", b"owned-B")
    assert len(replacement) == len(owned)

    original_open = Path.open
    replacement_injected = False

    class ReplaceAtDigestEof:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._stream, name)

        def read(self, *args, **kwargs):
            nonlocal replacement_injected
            chunk = self._stream.read(*args, **kwargs)
            if chunk == b"" and not replacement_injected:
                replacement_injected = True
                external = p.with_suffix(".external")
                external.write_bytes(replacement)
                os.replace(external, p)
                os.utime(p, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            return chunk

    def injecting_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == p and args and args[0] == "rb" and not replacement_injected:
            return ReplaceAtDigestEof(stream)
        return stream

    monkeypatch.setattr(Path, "open", injecting_open)
    s.save(touch_updated_at=False, skip_index=True)

    assert replacement_injected, "test must replace the file after digest EOF"
    assert p.read_bytes() == owned


def test_noop_skip_detects_in_place_change_during_digest_read(
    session_store, monkeypatch
):
    s = _make_session("digest-race-in-place", 2)
    s.title = "owned-A"
    s.messages[0]["content"] = "x" * (1024 * 1024 + 128)
    s.save(touch_updated_at=False, skip_index=True)
    p = s.path
    owned = p.read_bytes()
    original_stat = p.stat()
    replacement = owned.replace(b"owned-A", b"owned-B")
    assert len(replacement) == len(owned) > 1024 * 1024

    original_open = Path.open
    replacement_injected = False

    class ReplaceAfterFirstChunk:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._stream, name)

        def read(self, *args, **kwargs):
            nonlocal replacement_injected
            chunk = self._stream.read(*args, **kwargs)
            if chunk and not replacement_injected:
                replacement_injected = True
                p.write_bytes(replacement)
                os.utime(p, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            return chunk

    def injecting_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == p and args and args[0] == "rb" and not replacement_injected:
            return ReplaceAfterFirstChunk(stream)
        return stream

    monkeypatch.setattr(Path, "open", injecting_open)
    s.save(touch_updated_at=False, skip_index=True)

    assert replacement_injected, "test must replace the file during chunked digesting"
    assert p.read_bytes() == owned


def test_noop_resave_after_reload_skips_disk_write(session_store, monkeypatch):
    _make_session("hotpath_restart", 2).save(touch_updated_at=False, skip_index=True)
    reloaded = Session.load("hotpath_restart")
    assert reloaded is not None
    assert not hasattr(reloaded, "_last_saved_digest")

    def unexpected_replace(*_args, **_kwargs):
        raise AssertionError("a true no-op after cache loss must not rewrite the session")

    monkeypatch.setattr(_models, "_safe_replace", unexpected_replace)
    reloaded.save(touch_updated_at=False, skip_index=True)


def test_same_session_saves_are_serialized(session_store, monkeypatch):
    seed = _make_session("hotpath_locked", 1)
    seed.save(touch_updated_at=False, skip_index=True)
    first = Session.load("hotpath_locked")
    second = Session.load("hotpath_locked")
    assert first is not None and second is not None
    first.title = "first-writer"
    second.title = "second-writer"

    original_replace = _models._safe_replace
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def controlled_replace(src, dst):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return original_replace(src, dst)

    monkeypatch.setattr(_models, "_safe_replace", controlled_replace)
    errors = []

    def save(session):
        try:
            session.save(touch_updated_at=False, skip_index=True)
        except Exception as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=save, args=(first,))
    second_thread = threading.Thread(target=save, args=(second,))
    first_thread.start()
    assert first_entered.wait(timeout=5)
    second_thread.start()
    assert not second_entered.wait(timeout=0.2), (
        "a second writer for the same session must wait for the first save"
    )
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not errors
    assert second_entered.is_set()
    assert json.loads(seed.path.read_text(encoding="utf-8"))["title"] == "second-writer"


@pytest.mark.parametrize("fast_codec", [None, _NullingFastJson], ids=["stdlib", "fast-codec"])
def test_nested_nonfinite_values_roundtrip_deterministically(session_store, monkeypatch, fast_codec):
    monkeypatch.setattr(_models, "_orjson", fast_codec)
    session = _make_session(f"hotpath_nonfinite_{'fast' if fast_codec else 'stdlib'}", 1)
    session.estimated_cost = float("nan")
    session.messages[0]["metrics"] = {
        "positive": float("inf"),
        "nested": [1.25, {"negative": float("-inf")}],
    }
    session.save(touch_updated_at=False, skip_index=True)
    first_bytes = session.path.read_bytes()

    reloaded = Session.load(session.session_id)
    assert reloaded is not None
    assert isinstance(reloaded.estimated_cost, float)
    assert math.isnan(reloaded.estimated_cost)
    assert reloaded.messages[0]["metrics"]["positive"] == float("inf")
    assert reloaded.messages[0]["metrics"]["nested"][0] == 1.25
    assert reloaded.messages[0]["metrics"]["nested"][1]["negative"] == float("-inf")
    reloaded.save(touch_updated_at=False, skip_index=True)
    assert reloaded.path.read_bytes() == first_bytes


@pytest.mark.parametrize("fast_codec", [None, _NullingFastJson], ids=["stdlib", "fast-codec"])
def test_malformed_session_json_fails_consistently(monkeypatch, fast_codec):
    monkeypatch.setattr(_models, "_orjson", fast_codec)
    with pytest.raises((json.JSONDecodeError, ValueError)):
        _models._json_loads_session('{"messages": [}')


def test_draft_sidecar_roundtrip_and_precedence(session_store):
    sid = "hotpath6"
    s = _make_session(sid, 1)
    s.composer_draft = {"text": "legacy draft", "files": []}
    s.save(skip_index=True)
    # No sidecar yet → legacy field resolves.
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == "legacy draft"
    # Sidecar write does NOT touch the session file.
    st1 = os.stat(session_store / f"{sid}.json")
    stored = write_composer_draft_sidecar(sid, {"text": "sidecar draft", "files": []})
    st2 = os.stat(session_store / f"{sid}.json")
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert stored["text"] == "sidecar draft"
    assert composer_draft_sidecar_path(sid).exists()
    assert read_composer_draft_sidecar(sid)["text"] == "sidecar draft"
    # Sidecar wins over legacy — including an EMPTY sidecar (cleared draft).
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == "sidecar draft"
    write_composer_draft_sidecar(sid, {"text": "", "files": []})
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == ""
    # compact() serves the sidecar value.
    assert s.compact()["composer_draft"]["text"] == ""
    # Delete → falls back to legacy again.
    delete_composer_draft_sidecar(sid)
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == "legacy draft"


def test_draft_sidecar_uses_safe_replace(session_store, monkeypatch):
    calls = []
    original = _models._safe_replace

    def tracked_replace(src, dst):
        calls.append((src, dst))
        return original(src, dst)

    monkeypatch.setattr(_models, "_safe_replace", tracked_replace)
    write_composer_draft_sidecar("hotpath-safe-replace", {"text": "draft", "files": []})
    assert calls, "draft sidecars must retain the platform-safe replacement path"


def test_draft_sidecar_rejects_unsafe_session_ids(session_store):
    assert composer_draft_sidecar_path("../evil") is None
    assert read_composer_draft_sidecar("../evil") is None
    with pytest.raises(ValueError):
        write_composer_draft_sidecar("../evil", {"text": "x"})


def test_draft_sidecar_not_picked_up_as_session(session_store):
    sid = "hotpath7"
    _make_session(sid, 1).save(skip_index=True)
    write_composer_draft_sidecar(sid, {"text": "draft"})
    top_level = {p.name for p in session_store.glob("*.json")}
    assert f"{sid}.json" in top_level
    assert not any("_drafts" in str(p) for p in session_store.glob("*.json")), (
        "draft sidecars must live outside the top-level *.json session scan"
    )
