"""Regression coverage for mobile reload recovery after compression session rotation."""

import collections
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"


def _function_block(source: str, marker: str) -> str:
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 1
    i = brace + 1
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start:i]


def test_load_session_follows_backend_continuation_hint():
    """Reloading a stale pre-compression URL should follow the backend continuation hint.

    The continuation is followed by re-entering loadSession() with the hinted id;
    URL/localStorage are updated by that successful inner load, NOT written
    speculatively up-front (so a rejected/cross-profile continuation can't poison
    restore state with an unusable id — #2980 hardening).
    """
    src = SESSIONS_JS.read_text(encoding="utf-8")
    load_session = _function_block(src, "async function loadSession")

    assert "continuation_session_id" in load_session
    assert "loadSession(continuationSid" in load_session
    assert "skipContinuationResolve" in load_session
    # The re-entrant follow must pass skipContinuationResolve:true to prevent recursion.
    assert "skipContinuationResolve:true" in load_session
    # Restore-state safety: the continuation id must NOT be written to localStorage/URL
    # before the inner load proves it is loadable.
    assert "localStorage.setItem('hermes-webui-session',continuationSid)" not in load_session
    assert "_setActiveSessionUrl(continuationSid)" not in load_session


def test_continuation_lookup_is_profile_scoped(tmp_path, monkeypatch):
    """#2980 hardening: a continuation in a DIFFERENT profile must NOT be resolved.

    The snapshot is profile 'work'; a same-parent child in 'personal' must be
    filtered out, while the same-profile child resolves. Guards against a
    crafted/colliding foreign-profile sidecar leaking cross-profile.
    """
    from api import routes, config

    class _S:
        def __init__(self, sid, profile, parent=None, snap=False, updated=0.0):
            self.session_id = sid
            self.profile = profile
            self.parent_session_id = parent
            self.pre_compression_snapshot = snap
            self.updated_at = updated
            self.created_at = updated

    snapshot = _S("snap00000001", "work", snap=True)
    same_profile_child = _S("cont00000001", "work", parent="snap00000001", updated=200.0)
    foreign_child = _S("frgn00000001", "personal", parent="snap00000001", updated=300.0)

    # Empty session dir so only in-memory SESSIONS are considered.
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path, raising=False)
    fake = collections.OrderedDict()
    for s in (same_profile_child, foreign_child):
        fake[s.session_id] = s
    monkeypatch.setattr(routes, "SESSIONS", fake, raising=False)

    result = routes._pre_compression_continuation_session_id(snapshot)
    assert result == "cont00000001", f"expected same-profile continuation, got {result!r}"

    # Sanity: if the ONLY child is foreign-profile, no continuation is returned.
    fake2 = collections.OrderedDict()
    fake2[foreign_child.session_id] = foreign_child
    monkeypatch.setattr(routes, "SESSIONS", fake2, raising=False)
    assert routes._pre_compression_continuation_session_id(snapshot) is None


def test_continuation_lookup_uses_index_without_scanning_sidecars(tmp_path, monkeypatch):
    """Indexed continuation metadata should avoid an O(all sidecars) recovery scan."""
    from api import routes, config

    class _S:
        def __init__(self, sid, profile, snap=False):
            self.session_id = sid
            self.profile = profile
            self.parent_session_id = None
            self.pre_compression_snapshot = snap
            self.updated_at = 100.0
            self.created_at = 100.0

    snapshot = _S("snapindex001", "work", snap=True)
    index_file = tmp_path / "_index.json"
    (tmp_path / "childindex01.json").write_text("{}", encoding="utf-8")
    for idx in range(50):
        (tmp_path / f"noise{idx:08d}.json").write_text("{}", encoding="utf-8")
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": "childindex01",
                    "profile": "work",
                    "parent_session_id": "snapindex001",
                    "pre_compression_snapshot": False,
                    "updated_at": 300.0,
                    "created_at": 200.0,
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(routes, "SESSIONS", collections.OrderedDict(), raising=False)
    monkeypatch.setattr(
        routes.Session,
        "load_metadata_only",
        staticmethod(lambda _sid: (_ for _ in ()).throw(AssertionError("must not scan sidecars"))),
    )

    assert routes._pre_compression_continuation_session_id(snapshot) == "childindex01"


def test_indexed_continuation_lookup_keeps_profile_scope(tmp_path, monkeypatch):
    """The index fast path must preserve the cross-profile continuation guard."""
    from api import routes, config

    class _S:
        def __init__(self, sid, profile, snap=False):
            self.session_id = sid
            self.profile = profile
            self.parent_session_id = None
            self.pre_compression_snapshot = snap
            self.updated_at = 100.0
            self.created_at = 100.0

    snapshot = _S("snapindex002", "work", snap=True)
    index_file = tmp_path / "_index.json"
    (tmp_path / "foreignidx01.json").write_text("{}", encoding="utf-8")
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": "foreignidx01",
                    "profile": "personal",
                    "parent_session_id": "snapindex002",
                    "updated_at": 300.0,
                    "created_at": 200.0,
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(routes, "SESSIONS", collections.OrderedDict(), raising=False)
    monkeypatch.setattr(
        routes.Session,
        "load_metadata_only",
        staticmethod(lambda _sid: (_ for _ in ()).throw(AssertionError("must not scan sidecars"))),
    )

    assert routes._pre_compression_continuation_session_id(snapshot) is None
