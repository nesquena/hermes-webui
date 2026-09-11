"""Authority-scope contract for browser-persistent immutable video bytes."""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from types import SimpleNamespace


class Handler:
    def __init__(self, cookie=""):
        self.headers = {"Cookie": cookie} if cookie else {}
        self.request = SimpleNamespace()
        self.status = None
        self.body = bytearray()
        self.sent_headers = []
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_scope_is_opaque_stable_and_bound_to_authority_and_profile(monkeypatch):
    from api import auth

    monkeypatch.setattr(auth, "_signing_key", lambda: b"test-signing-key")
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: handler.cookie)
    monkeypatch.setattr(auth, "verify_session", lambda cookie: cookie in {"session-a.sig", "session-b.sig"})

    a = SimpleNamespace(cookie="session-a.sig")
    b = SimpleNamespace(cookie="session-b.sig")
    a_default = auth.build_media_cache_scope(a, profile_name="default", workspace_path="/workspace/a")
    a_default_again = auth.build_media_cache_scope(a, profile_name="default", workspace_path="/workspace/a")
    a_named = auth.build_media_cache_scope(a, profile_name="named", workspace_path="/workspace/a")
    b_default = auth.build_media_cache_scope(b, profile_name="default", workspace_path="/workspace/a")

    assert a_default == a_default_again
    assert len(a_default) == 64
    assert a_default != a_named
    assert a_default != b_default
    assert a_default != auth.build_media_cache_scope(a, profile_name="default", workspace_path="/workspace/b")
    # Conversation session IDs authorize individual paths per read; they are
    # not a persistent identity boundary inside one auth/profile/workspace.
    assert a_default == auth.build_media_cache_scope(
        a,
        profile_name="default",
        workspace_path="/workspace/a",
    )
    assert "session-a" not in a_default
    assert "default" not in a_default


def test_scope_rotates_on_webui_update(monkeypatch):
    from api import auth, updates

    monkeypatch.setattr(auth, "_signing_key", lambda: b"test-signing-key")
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    handler = SimpleNamespace(headers={})
    monkeypatch.setattr(updates, "WEBUI_VERSION", "v1")
    before = auth.build_media_cache_scope(handler, profile_name="default", workspace_path="/workspace/a")
    monkeypatch.setattr(updates, "WEBUI_VERSION", "v2")
    after = auth.build_media_cache_scope(handler, profile_name="default", workspace_path="/workspace/a")
    assert before != after


def test_scope_uses_session_minted_by_trusted_header_gate(monkeypatch):
    from api import auth, routes

    monkeypatch.setattr(auth, "_signing_key", lambda: b"test-signing-key")
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_trusted_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_trusted_auth_username", lambda _handler: "alice")
    monkeypatch.setattr(auth, "_trusted_auth_bound_profile", lambda _handler: None)
    monkeypatch.setattr(auth, "_save_sessions", lambda _sessions: None)
    monkeypatch.setattr(routes, "_raw_peer_is_trusted_proxy", lambda _handler: True)
    auth._sessions.clear()
    handler = Handler()
    assert auth.check_auth(handler, SimpleNamespace(path="/api/media-cache/scope", query="")) is True
    assert getattr(handler, "_trusted_auth_session_cookie_value", None)
    assert len(auth.build_media_cache_scope(handler, profile_name="default", workspace_path="/workspace/a")) == 64
    auth._sessions.clear()


def test_scope_without_auth_is_stable_per_profile(monkeypatch):
    from api import auth

    monkeypatch.setattr(auth, "_signing_key", lambda: b"test-signing-key")
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    handler = SimpleNamespace(headers={})

    default = auth.build_media_cache_scope(handler, profile_name="default", workspace_path="/workspace/a")
    named = auth.build_media_cache_scope(handler, profile_name="named", workspace_path="/workspace/a")
    assert len(default) == 64
    assert default != named
    assert default == auth.build_media_cache_scope(handler, profile_name="default", workspace_path="/workspace/a")


def test_scope_fails_closed_for_missing_or_invalid_authenticated_session(monkeypatch):
    from api import auth

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    assert auth.build_media_cache_scope(SimpleNamespace(headers={}), profile_name="default") is None

    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "bad.sig")
    monkeypatch.setattr(auth, "verify_session", lambda _cookie: False)
    assert auth.build_media_cache_scope(SimpleNamespace(headers={}), profile_name="default") is None


def test_media_cache_scope_route_returns_no_store_opaque_payload(monkeypatch, tmp_path):
    from api import auth, routes
    from api.media_snapshots import capture_snapshot

    seen = {}

    def build(_handler, **kwargs):
        seen.update(kwargs)
        return "f" * 64

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"snapshot-video")
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    digest = capture_snapshot(clip)
    assert digest
    session = SimpleNamespace(profile="named", workspace=str(tmp_path))
    monkeypatch.setattr(auth, "build_media_cache_scope", build)
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda profile, handler=None: profile == "named")
    monkeypatch.setattr(routes, "_session_media_token_allows_path", lambda sid, path, types: sid == "session-a" and path.name == "clip.mp4")
    handler = Handler()
    routes.handle_get(
        handler,
        SimpleNamespace(
            path="/api/media-cache/scope",
            query=urllib.parse.urlencode(
                {"session_id": "session-a", "path": str(clip), "snap": digest}
            ),
        ),
    )

    assert handler.status == 200
    payload = json.loads(bytes(handler.body))
    assert payload["scope"] == "f" * 64
    assert payload["schema"] == 2
    assert len(payload["resource"]) == 64
    assert str(clip) not in payload["resource"]
    assert ("Cache-Control", "no-store") in handler.sent_headers
    assert seen["profile_name"] == "named"
    assert "cache_context" not in seen
    assert seen["workspace_path"].replace("\\", "/").endswith(tmp_path.name)


def test_media_cache_resource_is_opaque_and_binds_canonical_target(monkeypatch, tmp_path):
    from api import auth

    monkeypatch.setattr(auth, "_signing_key", lambda: b"resource-test-key")
    digest = "a" * 64
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"

    first_resource = auth.build_media_cache_resource(first, digest)
    second_resource = auth.build_media_cache_resource(second, digest)

    assert len(first_resource) == 64
    assert first_resource != second_resource
    assert str(first) not in first_resource


def test_media_cache_scope_rejects_server_denied_snapshot(monkeypatch, tmp_path):
    from api import auth, routes
    from api.media_snapshots import capture_snapshot

    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"snapshot-video")
    digest = capture_snapshot(target)
    assert digest
    session = SimpleNamespace(profile="named", workspace=str(tmp_path))
    monkeypatch.setattr(auth, "build_media_cache_scope", lambda *_a, **_k: "f" * 64)
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_session_media_token_allows_path", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_media_deny_reason", lambda _target: "denied by server")

    denied = Handler()
    routes.handle_get(
        denied,
        SimpleNamespace(
            path="/api/media-cache/scope",
            query=urllib.parse.urlencode(
                {"session_id": "session-a", "path": str(target), "snap": digest}
            ),
        ),
    )

    assert denied.status == 404
    assert b"resource" not in bytes(denied.body)


def test_media_cache_scope_rejects_retargeted_path_without_digest_binding(
    monkeypatch, tmp_path
):
    from api import auth, routes
    from api.media_snapshots import capture_snapshot

    store = tmp_path / "snapshots"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    alias = tmp_path / "alias.mp4"
    first.write_bytes(b"first-snapshot")
    second.write_bytes(b"second-live")
    try:
        alias.symlink_to(first)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink unavailable: {exc}")
    digest = capture_snapshot(first)
    assert digest

    session = SimpleNamespace(profile="named", workspace=str(tmp_path))
    monkeypatch.setattr(auth, "build_media_cache_scope", lambda *_a, **_k: "f" * 64)
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_session_media_token_allows_path", lambda *_a, **_k: True)

    def request():
        handler = Handler()
        routes.handle_get(
            handler,
            SimpleNamespace(
                path="/api/media-cache/scope",
                query=urllib.parse.urlencode(
                    {"session_id": "session-a", "path": str(alias), "snap": digest}
                ),
            ),
        )
        return handler

    initial = request()
    assert initial.status == 200
    initial_resource = json.loads(bytes(initial.body))["resource"]

    alias.unlink()
    alias.symlink_to(second)
    retargeted = request()

    assert retargeted.status == 404
    assert initial_resource not in bytes(retargeted.body).decode("utf-8")


def test_media_cache_scope_route_rejects_missing_or_foreign_session(monkeypatch):
    from api import routes

    missing = Handler()
    routes.handle_get(missing, SimpleNamespace(path="/api/media-cache/scope", query=""))
    assert missing.status == 400

    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: SimpleNamespace(profile="other", workspace="/workspace/other"))
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda _profile, handler=None: False)
    foreign = Handler()
    routes.handle_get(
        foreign,
        SimpleNamespace(
            path="/api/media-cache/scope",
            query=(
                "session_id=foreign&path=/workspace/other/clip.mp4&snap="
                + "0" * 64
            ),
        ),
    )
    assert foreign.status == 404
    assert ("Cache-Control", "no-store") in foreign.sent_headers

    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda _profile, handler=None: True)
    monkeypatch.setattr(routes, "_session_media_token_allows_path", lambda _sid, _path, _types: False)
    revoked = Handler()
    routes.handle_get(
        revoked,
        SimpleNamespace(
            path="/api/media-cache/scope",
            query=(
                "session_id=session-a&path=/workspace/named/clip.mp4&snap="
                + "0" * 64
            ),
        ),
    )
    assert revoked.status == 404


def test_snapshot_response_attests_exact_digest_but_live_fallback_does_not(tmp_path, monkeypatch):
    from api import routes
    from api.media_snapshots import capture_snapshot

    store = tmp_path / "snapshots"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.setattr("api.workspace.get_last_workspace", lambda: str(tmp_path))
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"snapshot-bytes")
    digest = capture_snapshot(target)
    assert digest
    target.write_bytes(b"live-bytes")

    pinned = Handler()
    routes._handle_media(pinned, SimpleNamespace(
        path="/api/media",
        query=f"path={urllib.parse.quote(str(target))}&inline=1&snap={digest}",
    ))
    assert pinned.status == 200
    assert bytes(pinned.body) == b"snapshot-bytes"
    assert ("X-Hermes-Media-Snapshot", digest) in pinned.sent_headers

    missing = Handler()
    routes._handle_media(missing, SimpleNamespace(
        path="/api/media",
        query=f"path={urllib.parse.quote(str(target))}&inline=1&snap={'0' * 64}",
    ))
    assert missing.status == 200
    assert bytes(missing.body) == b"live-bytes"
    assert not any(name == "X-Hermes-Media-Snapshot" for name, _value in missing.sent_headers)


def test_persistent_video_scope_rejects_oversize_before_hashing(monkeypatch, tmp_path):
    """An over-cap snapshot must take the native path without a whole-file hash."""
    import json

    from api import media_snapshots, routes

    store = tmp_path / "snapshots"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    store.mkdir()
    target = tmp_path / "large.mp4"
    target.write_bytes(b"live")
    digest = "a" * 64
    snapshot = store / f"{digest}.snap"
    with snapshot.open("wb") as handle:
        handle.seek(16 * 1024 * 1024)
        handle.write(b"x")
    (store / f"{digest}.src.json").write_text(
        json.dumps({"digest": digest, "sources": [str(target.resolve())]}),
        encoding="utf-8",
    )

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("oversized snapshot body was hashed")

    monkeypatch.setattr(media_snapshots, "_snapshot_bytes_match_digest", forbidden_hash)
    assert routes._immutable_video_snapshot_resource(
        target, digest, path_authorized=True
    ) is None


def test_oversize_video_snapshot_range_streams_without_hashing(monkeypatch, tmp_path):
    """Native Range playback stays bounded when a snapshot exceeds the cache cap."""
    from api import media_snapshots, routes

    store = tmp_path / "snapshots"
    store.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "large.mp4"
    target.write_bytes(b"live")
    digest = "b" * 64
    snapshot = store / f"{digest}.snap"
    with snapshot.open("wb") as handle:
        handle.write(b"S")
        handle.seek(routes._PERSISTENT_VIDEO_CACHE_MAX_BYTES + 1)
        handle.write(b"x")
    (store / f"{digest}.src.json").write_text(
        json.dumps({"digest": digest, "sources": [str(target.resolve())]}),
        encoding="utf-8",
    )

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("native Range path hashed the oversized snapshot")

    monkeypatch.setattr(media_snapshots, "_snapshot_bytes_match_digest", forbidden_hash)
    handler = Handler()
    handler.headers["Range"] = "bytes=0-0"
    routes._handle_media(
        handler,
        SimpleNamespace(
            path="/api/media",
            query=(
                f"path={urllib.parse.quote(str(target))}&inline=1&snap={digest}"
            ),
        ),
    )

    assert handler.status == 206
    assert bytes(handler.body) == b"S"
    assert ("Content-Length", "1") in handler.sent_headers
    assert not any(name == "X-Hermes-Media-Snapshot" for name, _ in handler.sent_headers)


def test_persistent_video_scope_does_not_rehash_eligible_body(monkeypatch, tmp_path):
    """Scope authorization is metadata-only; opened bytes are attested by /api/media."""
    from api import media_snapshots, routes

    store = tmp_path / "snapshots"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"eligible-snapshot")
    digest = media_snapshots.capture_snapshot(target)
    assert digest

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("scope authorization rehashed snapshot bytes")

    monkeypatch.setattr(media_snapshots, "_snapshot_bytes_match_digest", forbidden_hash)
    eligibility = routes._immutable_video_snapshot_resource(
        target, digest, path_authorized=True
    )
    assert eligibility is not None
    assert eligibility[0] == target.resolve()


def test_signout_clears_before_logout_and_cleanup_failure_never_blocks_logout():
    if not NODE:
        return
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    start = panels.index("async function signOut()")
    end = panels.index("\n\nasync function goPasswordless", start)
    function_source = panels[start:end]
    script = f"""
const signOut=({function_source});
async function run(shouldThrow){{
  const events=[];
  global.window={{HermesPersistentVideoCache:{{clearAll:async()=>{{events.push('clear');if(shouldThrow)throw new Error('quota');}}}},location:{{href:''}}}};
  global.api=async()=>{{events.push('logout');return {{trusted_logout_url:'login'}};}};
  global.showToast=()=>events.push('toast');
  global.t=()=>'';
  await signOut();
  events.push('navigate:'+window.location.href);
  return events;
}}
(async()=>{{const rows=[await run(false),await run(true)];process.stdout.write(JSON.stringify(rows));}})();
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    success, cleanup_error = json.loads(result.stdout)
    assert success == ["clear", "logout", "clear", "navigate:login"]
    assert cleanup_error == ["clear", "logout", "clear", "navigate:login"]
