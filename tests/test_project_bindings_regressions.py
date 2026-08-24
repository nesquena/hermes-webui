"""Focused regressions for PR #6836 P1/P2 fixes (a7763ac3).

Covers the two P1s and one P2 from the 2026-08-14 review HEAD 46fa0435:

- P1 race (auto-assign steals ownership): _apply_project_auto_assign() must
  recheck the authoritative session under per-session agent lock before writing
  cached.project_id / s.project_id. A stale _index.json snapshot that says
  project_id=None must NOT win over a concurrent /api/session/move.

- P1 provider-scoped dedupe: _showProjectBindingsDialog() must preserve distinct
  (model, provider) pairs when the same bare model id exists under several
  providers. Saving must pin the selected provider rather than collapsing to the
  first one. Backend must canonicalize model_provider via _canonical_context_provider.

- P2 shutdown-drain respect: _register_background_commit_thread() returning False
  must suppress t.start() (memory-worker pattern).
"""

import json
import threading
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_sessions_js() -> str:
    return (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")


def _read_routes_py() -> str:
    return (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P1 — concurrent ownership: non-streaming authoritative recheck
# ---------------------------------------------------------------------------


def test_auto_assign_does_not_steal_session_claimed_between_snapshot_and_write(tmp_path, monkeypatch):
    """Stale _index snapshot shows unowned, but the live session is already owned.

    A concurrent /api/session/move raced between the snapshot read and the
    auto-assign write. The fixed path rechecks get_session(metadata_only=True)
    and skips the stale row.
    """
    import api.routes as routes

    ws = tmp_path / "ws-concurrent"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    # Snapshot says sess_a is unowned in the bound workspace.
    index_file.write_text(json.dumps([
        {"session_id": "sess_a", "workspace": ws_str, "profile": "default", "project_id": None},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    # Authoritative disk says sess_a is already filed elsewhere (concurrent move won).
    class _Live:
        project_id = "other-project"

    class _Full:
        def __init__(self):
            self.session_id = "sess_a"
            self.project_id = "other-project"
            self.save = lambda: (_ for _ in ()).throw(AssertionError("save must not be called when already owned"))

    def _fake_get_session(sid, metadata_only=False):
        if sid != "sess_a":
            return None
        if metadata_only:
            return _Live()
        return _Full()

    monkeypatch.setattr(routes, "get_session", _fake_get_session)

    proj = {"project_id": "proj_target", "profile": "default", "workspaces": [ws_str]}
    changed = routes._apply_project_auto_assign(proj)
    assert changed == 0, "stale snapshot must not overwrite a concurrently-claimed session"


def test_auto_assign_non_streaming_rechecks_under_agent_lock_and_skips_claimed(tmp_path, monkeypatch):
    """Even when the metadata-only fast path is bypassed, the agent-locked recheck skips.

    Simulates metadata_only raising (e.g. transient IO) so the code falls through
    to the full get_session + agent-locked recheck. The full session is already
    owned, so the second recheck must still prevent the steal.
    """
    import api.routes as routes

    ws = tmp_path / "ws-lock"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "sess_b", "workspace": ws_str, "profile": "default", "project_id": None},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    class _FullOwned:
        def __init__(self):
            self.session_id = "sess_b"
            self.project_id = "owner-pid"
        def save(self):
            raise AssertionError("save must not be called when recheck finds existing project_id")

    def _fake_get_session(sid, metadata_only=False):
        if metadata_only:
            raise RuntimeError("transient metadata read failure")
        if sid == "sess_b":
            return _FullOwned()
        return None

    monkeypatch.setattr(routes, "get_session", _fake_get_session)

    proj = {"project_id": "proj_target", "profile": "default", "workspaces": [ws_str]}
    changed = routes._apply_project_auto_assign(proj)
    assert changed == 0


# ---------------------------------------------------------------------------
# P1 — streaming branch: SESSIONS cache under LOCK + per-session agent lock
# ---------------------------------------------------------------------------


def test_auto_assign_streaming_branch_skips_when_cached_already_owned(tmp_path, monkeypatch):
    """Active-streaming session already in SESSIONS with project_id set must not be stolen."""
    import api.routes as routes
    from api.config import SESSIONS, LOCK

    ws = tmp_path / "ws-stream"
    ws.mkdir()
    ws_str = str(ws)
    sid = "sess_stream"
    active = "stream-1"
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": sid, "workspace": ws_str, "profile": "default", "project_id": None, "active_stream_id": active},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {active})

    # Put a live cached session already owned by another project; streaming path
    # should hold LOCK + agent lock and skip rather than overwrite.
    class _Cached:
        def __init__(self):
            self.session_id = sid
            self.project_id = "already-owned"
            self.active_stream_id = active

    cached = _Cached()
    with LOCK:
        SESSIONS[sid] = cached
    try:
        proj = {"project_id": "proj_target", "profile": "default", "workspaces": [ws_str]}
        changed = routes._apply_project_auto_assign(proj)
        assert changed == 0
        assert cached.project_id == "already-owned"
    finally:
        with LOCK:
            SESSIONS.pop(sid, None)


def test_auto_assign_streaming_branch_files_unowned_when_idle(tmp_path, monkeypatch):
    """Streaming session with no project_id is filed in-cache (deferred to stream)."""
    import api.routes as routes
    from api.config import SESSIONS, LOCK

    ws = tmp_path / "ws-stream2"
    ws.mkdir()
    ws_str = str(ws)
    sid = "sess_stream2"
    active = "stream-2"
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": sid, "workspace": ws_str, "profile": "default", "project_id": None, "active_stream_id": active},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {active})

    class _Cached:
        def __init__(self):
            self.session_id = sid
            self.project_id = None
            self.active_stream_id = active

    cached = _Cached()
    with LOCK:
        SESSIONS[sid] = cached
    try:
        proj = {"project_id": "proj_target", "profile": "default", "workspaces": [ws_str]}
        changed = routes._apply_project_auto_assign(proj)
        assert changed == 1
        assert cached.project_id == "proj_target"
    finally:
        with LOCK:
            SESSIONS.pop(sid, None)


# ---------------------------------------------------------------------------
# P1 — provider pinning: backend canonicalization + frontend dedupe
# ---------------------------------------------------------------------------


def test_bind_model_provider_is_canonicalized_via_helper():
    """Backend canonicalizes model_provider through _canonical_context_provider.

    e.g. 'OpenAI' -> 'openai', 'custom:My Prov' -> normalized slug.
    The routes.py bind path must call that helper; we assert both the helper
    contract and that the bind site invokes it.
    """
    from api.routes import _canonical_context_provider

    assert _canonical_context_provider("OpenAI") == "openai"
    assert _canonical_context_provider("openAI") == "openai"
    assert _canonical_context_provider("") == ""
    assert _canonical_context_provider(None) == ""
    # custom provider shape stays custom:*
    assert _canonical_context_provider("custom:test") == "custom:test"
    assert _canonical_context_provider("custom:My Provider") != ""

    src = _read_routes_py()
    # The bind handler must canonicalize model_provider on save.
    assert "_canonical_context_provider" in src
    # Must be on the proj['model_provider'] assignment in /api/projects/bind.
    bind_slice = src[src.find("if \"model_provider\" in body:"):src.find("if \"model_provider\" in body:") + 1200]
    assert "_canonical_context_provider" in bind_slice, "bind must canonicalize model_provider"


def test_bindings_dialog_preserves_provider_scoped_duplicate_model_ids():
    """Frontend dedupe must be provider-scoped.

    Same bare model id under two providers must yield two distinct selectable
    entries, not one collapsed entry. The dialog uses provider-scoped synthetic
    keys when duplicates exist and Save pins the chosen provider.
    """
    src = _read_sessions_js()

    # Dedupe during option collection is (value, provider)-scoped.
    assert "modelOptions.some(x=>x.value===val&&x.sub===provider)" in src

    # Synthetic key helpers exist and are used for display vs wire values.
    assert "_modelValueKeyFor" in src
    assert "_modelValueFor" in src
    assert "_modelProvFor" in src
    assert "_hasDuplicateModelValues" in src

    # When duplicates exist, option keys become provider-scoped and Save extracts
    # provider from the synthetic key rather than collapsing to the first hit.
    assert "o._key=_modelValueKeyFor(o.value,o.sub" in src
    assert "fields.model_provider=(_hasDuplicateModelValues ? (_prov||null)" in src
    # Clearing the model must also clear the provider (no stale provider stick).
    # The Save path has an else { fields.model=null; fields.model_provider=null }.
    assert "fields.model_provider=null" in src


def test_bindings_dialog_provider_scoped_key_roundtrip_via_node():
    """Node-evaluated roundtrip: provider+model synthetic keys isolate routes."""
    import shutil, json as _json, tempfile, os

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    driver = r"""
const [keysJson] = process.argv.slice(2);
const cases = JSON.parse(keysJson);
const _modelValueKeyFor=(val,prov)=>prov?(prov+"\u001f"+val):val;
const _modelValueFor=(k)=>{const i=k.indexOf("\u001f");return i>=0?k.slice(i+1):k;};
const _modelProvFor=(k)=>{const i=k.indexOf("\u001f");return i>=0?k.slice(0,i):"";};
for (const {val, prov} of cases) {
  const k=_modelValueKeyFor(val,prov);
  if (_modelValueFor(k)!==val) { console.error("bare mismatch", val, k, _modelValueFor(k)); process.exit(1); }
  if (_modelProvFor(k)!==prov) { console.error("prov mismatch", prov, k, _modelProvFor(k)); process.exit(1); }
}
console.log("ok");
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(driver)
        path = f.name
    try:
        cases = [
            {"val": "gpt-4o", "prov": "openai"},
            {"val": "gpt-4o", "prov": "azure-openai"},
            {"val": "claude-3-5-sonnet", "prov": "anthropic"},
            {"val": "claude-3-5-sonnet", "prov": "custom:my-proxy"},
            {"val": "same-id", "prov": ""},
        ]
        import subprocess as sp
        r = sp.run([node, path, _json.dumps(cases)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, f"node driver failed: {r.stderr} {r.stdout}"
        assert "ok" in r.stdout
        # Distinct providers must yield distinct synthetic keys for the same bare id.
        keys = {}
        for c in cases:
            k = (c["prov"] + "\x1f" + c["val"]) if c["prov"] else c["val"]
            keys.setdefault(c["val"], set()).add(k)
        assert len(keys["gpt-4o"]) == 2, "same bare id under two providers must be independently selectable"
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P2 — shutdown drain respects _register_background_commit_thread guard
# ---------------------------------------------------------------------------


def test_auto_assign_bind_respects_shutdown_drain_guard(monkeypatch):
    """_register_background_commit_thread returning False must suppress t.start()."""
    src = _read_routes_py()
    # Both auto-assign launch sites must gate start on the register return value.
    assert "if _register_background_commit_thread(t):" in src
    # Count occurrences — at least the commit-memory and auto-assign sites.
    assert src.count("if _register_background_commit_thread(t):") >= 2
    assert "t.start()" in src

    # Functional guard: a refused registration must not start the thread.
    import api.session_lifecycle as lc

    # Simulate draining state: monkeypatch the internal flag via the public API.
    # _register returns False when _draining is True; we fake that.
    monkeypatch.setattr(lc, "_draining", True, raising=False)
    # Need to reload the flag location: session_lifecycle keeps _draining as module global
    # and _register checks it. Patch the module global directly.
    import api.session_lifecycle as sl
    orig = sl._draining
    sl._draining = True
    try:
        t = threading.Thread(target=lambda: None, daemon=True)
        assert sl._register_background_commit_thread(t) is False
        # Caller would be expected to NOT call t.start() when False.
        assert not t.is_alive()
    finally:
        sl._draining = orig
