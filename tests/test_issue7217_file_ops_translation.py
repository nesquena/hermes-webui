"""#7217 P1-a: 文件操作消费边界 local_workspace 翻译接入测试。

get_session_for_file_ops 为每个本地文件操作 handler 的 session 视图附加
``local_workspace``（profile 作用域的 agent→WebUI 翻译根），handler 经
``_file_ops_ws_root`` 取根。本套件验证：

  1. 正例：映射 profile（alice：agent /workspace → webui 本地根）会话
     → local_workspace 等于 WebUI 本地根，原 workspace 字段一字不改；
  2. 反例：映射 profile 直接点名兄弟 profile 的 WebUI 根
     → resolver 拒绝 → SessionWorkspaceUnavailable（KeyError 子类，handler 404，fail-closed）；
  3. 无映射 profile（单容器）：翻译 tier 完全惰性 → local_workspace 为
     resolve 后的原路径（真实 Path），行为逐字节保持；
  4. state.db 外部会话视图也挂载 local_workspace（无 profile → None）；
  5. R1 守卫：local_workspace 以 `_` 前缀属性兜底存储，绝不被
     Session.save() 的公开属性序列化带进 sidecar；
  6. 真实 handler 端到端：create/save/delete/read/file_path 均落在
     WebUI 侧根上，并有一条 models→routes 全链路测试。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

workspace = pytest.importorskip("api.workspace")
profiles = pytest.importorskip("api.profiles")


def _write_profile(home: Path, config_text: str | None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if config_text is not None:
        (home / "config.yaml").write_text(config_text, encoding="utf-8")


@pytest.fixture
def mapped_world(tmp_path, monkeypatch):
    """Profile alice maps agent /workspace -> tmp webui_ws; bravo has no map."""
    profiles_root = tmp_path / "profiles"
    fake_home = tmp_path / "home"
    webui_ws = fake_home / "webui-ws"
    webui_ws.mkdir(parents=True)
    monkeypatch.setattr(workspace, "_home_path", lambda: fake_home)
    alice_home = profiles_root / "alice"
    _write_profile(
        alice_home,
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {webui_ws}\n",
    )
    _write_profile(profiles_root / "bravo", "terminal:\n  backend: local\n")
    saved = tmp_path / "plain-ws"
    saved.mkdir()

    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile",
        lambda name: profiles_root / (name or "alice"),
    )
    monkeypatch.setattr(
        workspace, "load_workspaces",
        lambda profile=None: (
            [{"path": str(webui_ws)}] if profile == "alice" else [{"path": str(saved)}]
        ),
    )
    return {
        "profiles_root": profiles_root,
        "webui_ws": webui_ws,
        "saved": saved,
    }


# ── models.get_session_for_file_ops ──────────────────────────────────────────


def _patch_file_ops_session_plumbing(monkeypatch, session):
    """Point get_session_for_file_ops' dependencies at the fake session."""
    import api.models as models

    active = getattr(session, "profile", None) or "alice"
    monkeypatch.setattr(models, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active)
    monkeypatch.setattr(profiles, "_profiles_match", lambda a, b: a == b)
    monkeypatch.setattr(
        workspace, "resolve_implicit_workspace_with_recovery",
        lambda w, f, profile=None: (Path("/workspace"), False),
    )
    return models


class TestGetSessionForFileOpsTranslation:
    def test_mapped_profile_attaches_translated_local_workspace(self, mapped_world, monkeypatch):
        import api.models as models

        session = SimpleNamespace(session_id="s1", workspace="/workspace", profile="alice")
        _patch_file_ops_session_plumbing(monkeypatch, session)

        got = models.get_session_for_file_ops("s1")

        assert got is session
        assert got.local_workspace == mapped_world["webui_ws"].resolve()
        # The agent-visible workspace field is NEVER rewritten.
        assert got.workspace == "/workspace"

    def test_workspace_field_never_rewritten_for_real_session(self, mapped_world, monkeypatch):
        import api.models as models

        real = models.Session(session_id="s2", workspace="/workspace", profile="alice")
        _patch_file_ops_session_plumbing(monkeypatch, real)

        got = models.get_session_for_file_ops("s2")

        assert got is real
        assert got.local_workspace == mapped_world["webui_ws"].resolve()
        assert got.workspace == "/workspace"
        # R1 guard: the translation is stored under an underscore-backed
        # attribute so Session.save()'s public-attribute serialization can
        # never persist it (a Path would also crash json.dumps).
        assert "_local_workspace" in vars(real)
        assert "local_workspace" not in vars(real)

    def test_sibling_webui_root_fails_closed(self, mapped_world, monkeypatch):
        import api.models as models

        sibling = mapped_world["profiles_root"] / "bob-ws"
        sibling.mkdir()
        session = SimpleNamespace(session_id="s3", workspace=str(sibling), profile="alice")
        _patch_file_ops_session_plumbing(monkeypatch, session)

        # alice HAS a map, but a sibling profile's root is not her agent
        # prefix and not in her trust roots -> fail closed (KeyError subclass,
        # answered 404 by the handlers). Falling back to the raw path here
        # would re-open the sibling-mount escape.
        with pytest.raises(models.SessionWorkspaceUnavailable):
            models.get_session_for_file_ops("s3")

    def test_unmapped_profile_keeps_resolved_agent_path(self, mapped_world, monkeypatch):
        import api.models as models

        # bravo declares no map: the translation tier is inert, so the
        # resolver returns the resolved agent path unchanged — the
        # single-container behaviour, byte-for-byte.
        session = SimpleNamespace(session_id="s4", workspace="/workspace", profile="bravo")
        _patch_file_ops_session_plumbing(monkeypatch, session)

        got = models.get_session_for_file_ops("s4")

        assert got.local_workspace is not None
        assert got.local_workspace == Path("/workspace")
        assert got.workspace == "/workspace"

    def test_external_state_db_view_attaches_none(self, monkeypatch, tmp_path):
        import api.models as models

        monkeypatch.setattr(models, "get_session", lambda sid, metadata_only=False: (_ for _ in ()).throw(KeyError(sid)))
        monkeypatch.setattr(models, "state_db_has_session", lambda sid: True)
        monkeypatch.setattr(models, "get_last_workspace", lambda: str(tmp_path / "last-ws"))

        got = models.get_session_for_file_ops("ext-1")

        assert isinstance(got, models._ExternalSessionView)
        assert got.session_id == "ext-1"
        assert got.workspace == str(tmp_path / "last-ws")
        # No profile on the external view -> translation cannot run -> None.
        assert got.local_workspace is None


# ── routes._file_ops_ws_root ─────────────────────────────────────────────────


class TestFileOpsWsRoot:
    def _import_routes(self):
        import api.routes as routes

        return routes

    def test_falls_back_to_workspace_without_local(self):
        routes = self._import_routes()
        # Profile-less legacy session: no translation attempted, direct path
        # preserved (the explicitly deferred case).
        s = SimpleNamespace(workspace="/w", profile=None)
        assert routes._file_ops_ws_root(s) == Path("/w")

    def test_uses_local_workspace_when_attached(self):
        routes = self._import_routes()
        s = SimpleNamespace(workspace="/w", local_workspace=Path("/local"), profile="alice")
        assert routes._file_ops_ws_root(s) == Path("/local")

    def test_none_local_workspace_falls_back_for_profileless(self):
        routes = self._import_routes()
        s = SimpleNamespace(workspace="/w", local_workspace=None, profile=None)
        assert routes._file_ops_ws_root(s) == Path("/w")


# ── End-to-end handlers (translated root) ────────────────────────────────────


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _fake_session_for(webui_ws: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="s1",
        workspace="/workspace",
        local_workspace=Path(webui_ws),
        profile="alice",
    )


class TestHandlersTranslatedEndToEnd:
    def test_file_create_writes_to_webui_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        routes._handle_file_create(
            handler,
            {"session_id": "s1", "path": "hello.txt", "content": "hi"},
        )

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["ok"] is True
        assert payload["path"] == "hello.txt"
        # The bytes landed on the WebUI-LOCAL side, not the agent-visible path.
        assert (webui_ws / "hello.txt").read_text(encoding="utf-8") == "hi"

    def test_file_save_and_delete_translate_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "exists.txt").write_text("v1", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        routes._handle_file_save(
            handler,
            {"session_id": "s1", "path": "exists.txt", "content": "v2"},
        )
        assert handler.status == 200
        assert (webui_ws / "exists.txt").read_text(encoding="utf-8") == "v2"
        assert handler.json_body()["size"] == 2

        handler2 = _FakeHandler()
        routes._handle_file_delete(
            handler2,
            {"session_id": "s1", "path": "exists.txt"},
        )
        assert handler2.status == 200
        assert not (webui_ws / "exists.txt").exists()

    def test_file_read_translates_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "readme.md").write_text("hello world", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        parsed = urlparse("/api/file/read?session_id=s1&path=readme.md")
        routes._handle_file_read(handler, parsed)

        assert handler.status == 200
        assert handler.json_body()["content"] == "hello world"

    def test_file_path_returns_webui_local_absolute(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "clip.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        routes._handle_file_path(
            handler,
            {"session_id": "s1", "path": "clip.txt"},
        )

        assert handler.status == 200
        assert Path(handler.json_body()["path"]) == (webui_ws / "clip.txt").resolve()

    def test_full_chain_real_models_feeds_handler(self, mapped_world, monkeypatch):
        """Real get_session_for_file_ops (Real Session + real resolver) -> handler."""
        import api.models as models
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        real = routes.Session(session_id="chain-1", workspace="/workspace", profile="alice")
        assert real.workspace == "/workspace"  # sanity: Session ctor resolved nothing
        _patch_file_ops_session_plumbing(monkeypatch, real)
        monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)

        handler = _FakeHandler()
        routes._handle_file_create(
            handler,
            {"session_id": "chain-1", "path": "chain.txt", "content": "via-chain"},
        )

        assert handler.status == 200
        assert (webui_ws / "chain.txt").read_text(encoding="utf-8") == "via-chain"
        # R1: translation rode on a Real cached Session object without leaking
        # into its public attribute space (save() would otherwise persist it).
        assert "local_workspace" not in vars(real)
        assert real.local_workspace == webui_ws.resolve()

# ── #7217 P2: maintainer-scope §5 dual-profile integration coverage ──────────
# All handlers below are driven through the SAME seams as the P1-a suite
# (mapped_world / _patch_file_ops_session_plumbing / _FakeHandler +
# routes.handle_get / direct handler calls). Positive cases assert the
# operation lands on the WebUI-LOCAL root; the cross-profile negative asserts
# an unmapped profile's session naming another profile's WebUI root fails
# closed (resolver rejection -> SessionWorkspaceUnavailable -> 404).

import io
import subprocess
import zipfile


@pytest.fixture
def isolated_ws_world(tmp_path, monkeypatch):
    """alice's mapped WebUI root lives OUTSIDE the shared home.

    Trust therefore comes only from alice's own saved-workspace list, so a
    bravo session naming that exact root must fail the trust re-check
    (Dragonk invariant: no global allow-root, profile roots are per-profile).
    """
    profiles_root = tmp_path / "profiles"
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    alice_ws = tmp_path / "alice-ws"
    alice_ws.mkdir()
    monkeypatch.setattr(workspace, "_home_path", lambda: fake_home)
    _write_profile(
        profiles_root / "alice",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {alice_ws}\n",
    )
    _write_profile(profiles_root / "bravo", "terminal:\n  backend: local\n")
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile",
        lambda name: profiles_root / (name or "alice"),
    )
    monkeypatch.setattr(
        workspace, "load_workspaces",
        lambda profile=None: (
            [{"path": str(alice_ws)}] if profile == "alice" else []
        ),
    )
    return {"alice_ws": alice_ws, "profiles_root": profiles_root}


class _BytesIOHandler(_FakeHandler):
    """Fake handler whose wfile is a seekable in-memory buffer (zip output)."""

    def __init__(self):
        super().__init__()
        self.wfile = io.BytesIO()


# ── rename / move ────────────────────────────────────────────────────────────


class TestRenameMoveTranslatedEndToEnd:
    def test_file_rename_translates_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "old.txt").write_text("v1", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        routes._handle_file_rename(
            handler,
            {"session_id": "s1", "path": "old.txt", "new_name": "new.txt"},
        )

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["ok"] is True
        # The reported path is relative to the translated (WebUI-local) root.
        assert payload["new_path"] == "new.txt"
        assert not (webui_ws / "old.txt").exists()
        assert (webui_ws / "new.txt").read_text(encoding="utf-8") == "v1"

    def test_file_move_translates_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "sub").mkdir()
        (webui_ws / "moved.txt").write_text("mv", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        routes._handle_file_move(
            handler,
            {"session_id": "s1", "path": "moved.txt", "dest_dir": "sub"},
        )

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["ok"] is True
        # dest_dir is interpreted against the WebUI-local root.
        assert payload["new_path"] == "sub/moved.txt"
        assert not (webui_ws / "moved.txt").exists()
        assert (webui_ws / "sub" / "moved.txt").read_text(encoding="utf-8") == "mv"

    def test_file_rename_fails_closed_for_agent_path_without_map(self, mapped_world, monkeypatch):
        """An alice session naming a path that translation rejects must not
        fall back to treating /workspace as a real local root."""
        import api.models as models
        import api.routes as routes

        session = SimpleNamespace(session_id="s9", workspace="/nowhere/deep", profile="alice")
        _patch_file_ops_session_plumbing(monkeypatch, session)
        monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)

        handler = _FakeHandler()
        routes._handle_file_rename(
            handler,
            {"session_id": "s9", "path": "x.txt", "new_name": "y.txt"},
        )
        # get_session_for_file_ops raised SessionWorkspaceUnavailable (KeyError)
        # -> handler answers 404, never a half-translated path.
        assert handler.status == 404


# ── raw / folder download ────────────────────────────────────────────────────


class TestRawAndFolderDownloadTranslated:
    def test_raw_file_download_serves_webui_local_bytes(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        blob = bytes(range(256))
        (webui_ws / "blob.bin").write_bytes(blob)
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        parsed = urlparse("/api/file/raw?session_id=s1&path=blob.bin")
        routes._handle_file_raw(handler, parsed)

        assert handler.status == 200
        # The downloaded bytes come from the WebUI-LOCAL root, byte-for-byte.
        assert bytes(handler.body) == blob
        # And the anchor served through is the translated root.
        assert any(h[0] == "Content-Disposition" for h in handler.sent_headers)

    def test_file_raw_target_anchors_to_webui_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        nested = webui_ws / "notes"
        nested.mkdir()
        (nested / "deep.bin").write_bytes(b"deep")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        anchor, target = routes._file_raw_target(_fake_session_for(webui_ws), "s1", "notes/deep.bin")

        assert anchor.resolve() == webui_ws.resolve()
        assert target == (webui_ws / "notes" / "deep.bin").resolve()

    def test_folder_download_zips_webui_local_directory(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        sub = webui_ws / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("alpha", encoding="utf-8")
        (sub / "b.bin").write_bytes(b"\x00\x01\x02")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _BytesIOHandler()
        parsed = urlparse("/api/folder/download?session_id=s1&path=sub")
        routes._handle_folder_download(handler, parsed)

        assert handler.status == 200
        zip_bytes = handler.wfile.getvalue()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = sorted(zf.namelist())
            assert names == ["a.txt", "b.bin"]
            assert zf.read("a.txt") == b"alpha"
            assert zf.read("b.bin") == b"\x00\x01\x02"

    def test_folder_download_missing_dir_404(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        parsed = urlparse("/api/folder/download?session_id=s1&path=ghost")
        routes._handle_folder_download(handler, parsed)

        assert handler.status == 404
        assert handler.json_body()["error"] == "not found"


# ── escape authorize / list / read / raw ─────────────────────────────────────


class TestEscapeArchiveTranslated:
    def test_escape_authorize_list_read_raw_anchored_on_translated_root(
        self, mapped_world, monkeypatch, tmp_path
    ):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "secret.txt").write_text("top secret", encoding="utf-8")
        (webui_ws / "escape").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))
        monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

        auth_handler = _FakeHandler()
        auth_handler.command = "POST"
        auth_handler.headers = {"Origin": "http://test.local"}
        routes._handle_escape_authorize(
            auth_handler,
            urlparse("/api/workspace/escape/authorize"),
            body={"session_id": "esc-1", "path": "escape"},
        )
        assert auth_handler.status == 200
        token = auth_handler.json_body()["token"]

        # list: the escape surface is walked with the translated root as anchor.
        # ``path`` is the surface path itself (the symlink name inside the
        # workspace) — list_authorized_escape_dir requires rel under it.
        list_handler = _FakeHandler()
        routes._handle_escape_list_dir(
            list_handler,
            urlparse(f"/api/workspace/escape/list?session_id=esc-1&token={token}&path=escape"),
        )
        assert list_handler.status == 200
        names = [e["name"] for e in list_handler.json_body()["entries"]]
        assert "secret.txt" in names

        # read: content served from the escaped target, anchored at translation.
        read_handler = _FakeHandler()
        routes._handle_escape_file_read(
            read_handler,
            urlparse(f"/api/workspace/escape/read?session_id=esc-1&token={token}&path=escape/secret.txt"),
        )
        assert read_handler.status == 200
        assert read_handler.json_body()["content"] == "top secret"
        assert read_handler.json_body()["escape_read_only"] is True

        # raw: exact bytes served through the same anchored grant.
        raw_handler = _FakeHandler()
        routes._handle_escape_file_raw(
            raw_handler,
            urlparse(f"/api/workspace/escape/raw?session_id=esc-1&token={token}&path=escape/secret.txt"),
        )
        assert raw_handler.status == 200
        assert bytes(raw_handler.body) == b"top secret"

    def test_escape_token_is_anchored_to_translated_root(self, mapped_world, monkeypatch, tmp_path):
        """Replaying the token through a DIFFERENT root fails closed (403):
        the grant is bound to the translated WebUI-local root, so the agent
        path cannot be swapped in to re-anchor the escape."""
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        (webui_ws / "escape").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))
        monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

        auth_handler = _FakeHandler()
        auth_handler.command = "POST"
        auth_handler.headers = {"Origin": "http://test.local"}
        routes._handle_escape_authorize(
            auth_handler,
            urlparse("/api/workspace/escape/authorize"),
            body={"session_id": "esc-2", "path": "escape"},
        )
        token = auth_handler.json_body()["token"]

        # A session whose local_workspace is a different dir: token rejected.
        other = tmp_path / "other-root"
        other.mkdir()
        other_session = SimpleNamespace(
            session_id="esc-2", workspace="/workspace", local_workspace=other, profile="alice"
        )
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: other_session)
        list_handler = _FakeHandler()
        routes._handle_escape_list_dir(
            list_handler,
            urlparse(f"/api/workspace/escape/list?session_id=esc-2&token={token}&path=escape"),
        )
        assert list_handler.status == 403


# ── cross-profile fail-closed (bravo names alice's WebUI root) ───────────────


class TestCrossProfileFailClosed:
    def test_bravo_session_naming_alice_webui_root_fails_closed(self, isolated_ws_world, monkeypatch):
        import api.models as models

        alice_ws = isolated_ws_world["alice_ws"]
        session = SimpleNamespace(session_id="br1", workspace=str(alice_ws), profile="bravo")
        _patch_file_ops_session_plumbing(monkeypatch, session)
        # The plumbing helper aligns the active profile WITH the session so the
        # resolver semantics can be tested in isolation; restore the real
        # production shape here: the active profile is alice, the requesting
        # session belongs to bravo. The file-ops authorization gate
        # (_profiles_match, models.get_session_for_file_ops) is what rejects a
        # foreign-profile session — fail-closed before any path is touched.
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alice")

        with pytest.raises(KeyError):
            models.get_session_for_file_ops("br1")

    def test_bravo_handler_answers_404_for_alice_root(self, isolated_ws_world, monkeypatch):
        import api.models as models
        import api.routes as routes

        alice_ws = isolated_ws_world["alice_ws"]
        (alice_ws / "secret.txt").write_text("alice-data", encoding="utf-8")
        session = SimpleNamespace(session_id="br2", workspace=str(alice_ws), profile="bravo")
        _patch_file_ops_session_plumbing(monkeypatch, session)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alice")
        monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)

        handler = _FakeHandler()
        parsed = urlparse("/api/file/read?session_id=br2&path=secret.txt")
        routes._handle_file_read(handler, parsed)

        # bravo is not the active profile -> authorization KeyError -> 404.
        # The file exists on disk but must NOT leak across profiles.
        assert handler.status == 404

    def test_mapped_session_naming_sibling_root_fails_closed(self, isolated_ws_world, monkeypatch):
        """The resolver-layer fail-closed: a MAPPED profile (alice) naming a
        sibling profile's WebUI root — an existing directory that is not in
        alice's trust roots — is rejected by the translation tier, raising
        SessionWorkspaceUnavailable (KeyError subclass), answered 404."""
        import api.models as models
        import api.routes as routes

        bob_ws = isolated_ws_world["profiles_root"] / "bob-ws"
        bob_ws.mkdir()
        (bob_ws / "bob.txt").write_text("bob-data", encoding="utf-8")
        session = SimpleNamespace(session_id="al2", workspace=str(bob_ws), profile="alice")
        _patch_file_ops_session_plumbing(monkeypatch, session)

        with pytest.raises(models.SessionWorkspaceUnavailable):
            models.get_session_for_file_ops("al2")

        monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)
        handler = _FakeHandler()
        parsed = urlparse("/api/file/read?session_id=al2&path=bob.txt")
        routes._handle_file_read(handler, parsed)
        assert handler.status == 404

    def test_alice_own_session_reads_isolated_root_control(self, isolated_ws_world, monkeypatch):
        """Positive control for the isolated fixture: alice's own mapped
        session can read the root — proving the 404 above is profile scoping,
        not a broken fixture."""
        import api.models as models
        import api.routes as routes

        alice_ws = isolated_ws_world["alice_ws"]
        (alice_ws / "secret.txt").write_text("alice-data", encoding="utf-8")
        session = SimpleNamespace(session_id="al1", workspace="/workspace", profile="alice")
        _patch_file_ops_session_plumbing(monkeypatch, session)
        monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)

        handler = _FakeHandler()
        parsed = urlparse("/api/file/read?session_id=al1&path=secret.txt")
        routes._handle_file_read(handler, parsed)

        assert handler.status == 200
        assert handler.json_body()["content"] == "alice-data"


# ── git write operations (stage / commit / commit-selected) ──────────────────


class TestGitWriteTranslatedEndToEnd:
    def _init_repo(self, repo: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "webui-test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "webui-test@example.com"], cwd=repo, check=True, capture_output=True)

    def _git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_git_stage_and_commit_land_in_webui_repo(self, mapped_world, monkeypatch):
        import api.routes as routes

        repo = mapped_world["webui_ws"]
        self._init_repo(repo)
        (repo / "feature.txt").write_text("from-webui", encoding="utf-8")
        monkeypatch.setenv("HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE", "1")
        session = SimpleNamespace(workspace="/workspace", profile="alice")
        monkeypatch.setattr(routes, "get_session", lambda sid: session)

        stage_handler = _FakeHandler()
        routes._handle_git_stage(stage_handler, {"session_id": "s1", "paths": ["feature.txt"]})
        assert stage_handler.status == 200
        assert stage_handler.json_body()["ok"] is True
        assert self._git(repo, "diff", "--cached", "--name-only") == "feature.txt"

        commit_handler = _FakeHandler()
        routes._handle_git_commit(commit_handler, {"session_id": "s1", "message": "webui commit"})
        assert commit_handler.status == 200
        short_sha = commit_handler.json_body()["commit"]

        assert self._git(repo, "log", "--format=%s", "-1") == "webui commit"
        assert self._git(repo, "rev-parse", "HEAD").startswith(short_sha)

    def test_git_commit_selected_lands_in_webui_repo(self, mapped_world, monkeypatch):
        import api.routes as routes

        repo = mapped_world["webui_ws"]
        self._init_repo(repo)
        (repo / "a.txt").write_text("a", encoding="utf-8")
        monkeypatch.setenv("HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE", "1")
        session = SimpleNamespace(workspace="/workspace", profile="alice")
        monkeypatch.setattr(routes, "get_session", lambda sid: session)

        routes._handle_git_stage(_FakeHandler(), {"session_id": "s1", "paths": ["a.txt"]})
        first = _FakeHandler()
        routes._handle_git_commit(first, {"session_id": "s1", "message": "first"})
        assert first.status == 200

        (repo / "b.txt").write_text("b", encoding="utf-8")
        routes._handle_git_stage(_FakeHandler(), {"session_id": "s1", "paths": ["b.txt"]})
        second = _FakeHandler()
        routes._handle_git_commit_selected(
            second, {"session_id": "s1", "message": "second", "paths": ["b.txt"]}
        )
        assert second.status == 200

        log = self._git(repo, "log", "--format=%s")
        assert log.splitlines() == ["second", "first"]


# ── lightweight handlers: create_dir / reveal / office-save / open-vscode ────


class TestLightweightHandlersTranslatedRoot:
    def test_create_dir_makes_dir_in_webui_root(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))

        handler = _FakeHandler()
        routes._handle_create_dir(handler, {"session_id": "s1", "path": "newdir"})

        assert handler.status == 200
        assert handler.json_body()["path"] == "newdir"
        assert (webui_ws / "newdir").is_dir()

    def test_file_reveal_uses_webui_local_target(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "show.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))
        seen = []
        monkeypatch.setattr(routes.subprocess, "Popen", lambda args, **kw: seen.append(args))

        handler = _FakeHandler()
        routes._handle_file_reveal(handler, {"session_id": "s1", "path": "show.txt"})

        assert handler.status == 200
        # On Linux the OS action opens the resolved parent — the WebUI-LOCAL
        # directory, never the agent-visible /workspace.
        assert seen and Path(seen[0][-1]) == webui_ws.resolve()

    def test_office_file_save_writes_webui_local_bytes(self, mapped_world, monkeypatch):
        import api.office_documents as office_docs
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "doc.docx").write_bytes(b"PK-old")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))
        # No real office parsing: stub the document transform; the route's
        # read/write still go through the anchored WebUI-local root.
        monkeypatch.setattr(
            office_docs, "save_office_document",
            lambda path, current, content: ({"preview": "p"}, b"PK-new"),
        )

        handler = _FakeHandler()
        routes._handle_office_file_save(handler, {"session_id": "s1", "path": "doc.docx", "content": "x"})

        assert handler.status == 200
        assert (webui_ws / "doc.docx").read_bytes() == b"PK-new"
        assert handler.json_body()["size"] == 6

    def test_open_vscode_uses_webui_local_path(self, mapped_world, monkeypatch):
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        (webui_ws / "code.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: _fake_session_for(webui_ws))
        seen = []
        monkeypatch.setattr(routes.subprocess, "Popen", lambda args, **kw: seen.append(args))
        monkeypatch.setattr(routes.shutil, "which", lambda cmd: "/usr/bin/code")

        handler = _FakeHandler()
        routes._handle_file_open_vscode(handler, {"session_id": "s1", "path": "code.txt"})

        assert handler.status == 200
        assert seen == [["/usr/bin/code", str((webui_ws / "code.txt").resolve())]]


# ── upload profile pin ───────────────────────────────────────────────────────


class TestUploadProfilePin:
    def test_workspace_upload_translates_agent_path_to_webui_root(
        self, mapped_world, monkeypatch
    ):
        """Upload must resolve through the #7217 translation tier FOR REAL.

        Re-gate round-2 finding: a resolver spy that returns the desired
        local path proves only profile forwarding, not translation. The
        real :func:`resolve_webui_workspace` therefore stays under test
        and is merely recorded: the agent-visible ``/workspace`` (absent on
        this host) must map onto the session profile's WebUI-local root,
        and the bytes must land THERE.
        """
        import api.upload as upload

        webui_ws = mapped_world["webui_ws"]
        monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "alice")
        monkeypatch.setattr(upload, "_profiles_match", lambda a, b: (a or "alice") == (b or "alice"))

        real_resolve = upload.resolve_webui_workspace
        calls = []

        def recording(workspace_path, *, profile=None, **kwargs):
            calls.append((workspace_path, profile))
            return real_resolve(workspace_path, profile=profile, **kwargs)

        monkeypatch.setattr(upload, "resolve_webui_workspace", recording)
        monkeypatch.setattr(
            upload, "_read_multipart_or_reject",
            lambda handler: ({"session_id": "s1", "path": ""}, {"hello.txt": ("hello.txt", b"hi")}),
        )
        monkeypatch.setattr(
            upload, "get_session",
            lambda sid: SimpleNamespace(workspace="/workspace", profile="alice"),
        )

        handler = _FakeHandler()
        upload.handle_workspace_upload(handler)

        # The session profile rides along on the translation-tier call — the
        # exact contract the P1 translation tier relies on at the upload
        # boundary — and the REAL translation (not a substituted path) put
        # the bytes on the mapped WebUI root.
        assert calls == [("/workspace", "alice")]
        assert handler.status == 200
        assert (webui_ws / "hello.txt").read_text(encoding="utf-8") == "hi"


# ── re-gate round 2: recovery order + create→list regression ─────────────────
# Reviewer replay (2026-10-22): with a valid map and an existing fallback,
# (1) create returned 200 but wrote the FALLBACK instead of the mapped local
# root, replacing both cached and persisted session.workspace; (2) the same
# session's listing then came back empty. Root cause: recovery's host-local
# existence check ran BEFORE the translation tier, so the agent-side path —
# absent on the WebUI host by design — was judged a deleted directory.
#
# These tests keep the REAL recovery tier and the REAL resolver under test
# (only the session lookup and the fallback source are provided) and drive
# the production handlers end to end.


@pytest.fixture
def regate_world(mapped_world, monkeypatch):
    """mapped_world plus an EXISTING trusted fallback directory.

    The fallback lives under the faked home, so it passes
    resolve_trusted_workspace trust check (A) — the reviewer's "existing
    fallback" condition, without which recovery cannot fire at all.
    """
    fallback = mapped_world["webui_ws"].parent / "home" / "fallback-ws"
    fallback.mkdir(parents=True)
    mapped_world["fallback"] = fallback
    return mapped_world


def _patch_real_recovery_plumbing(monkeypatch, session, fallback):
    """Point get_session_for_file_ops at *session* with the REAL recovery.

    Only the session lookup and the fallback source are substituted; the
    recovery tier (resolve_implicit_workspace_with_recovery), the
    translation tier (resolve_webui_workspace) and the persist helper all
    stay real — a stub here is exactly what hid the regression before.
    """
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    session_profile = getattr(session, "profile", None) or "alice"
    monkeypatch.setattr(models, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(models, "state_db_has_session", lambda sid: False)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: str(fallback))
    # The active profile must be the session's own owner so the REAL
    # _profiles_match (not a stub) authorizes it.
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: session_profile)
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)
    return models, routes


class TestRecoveryOrderRegression:
    def test_mapped_missing_agent_path_keeps_binding_and_writes_mapped_root(
        self, regate_world, monkeypatch
    ):
        """The reviewer's exact replay: mapped session, agent path absent on
        the host, fallback exists → create must land on the MAPPED root and
        the agent-visible binding must survive (no persist, no fallback)."""
        models, routes = _patch_real_recovery_plumbing(
            monkeypatch,
            SimpleNamespace(session_id="regate-1", workspace="/workspace", profile="alice"),
            regate_world["fallback"],
        )
        persist_calls = []

        def spy_persist(session, workspace, *, expected_workspace=None):
            # Mimic the real helper's binding rewrite so the OLD order
            # reproduces the reviewer's symptom (200 into the fallback)
            # instead of crashing on the None return.
            persist_calls.append(str(workspace))
            session.workspace = str(Path(workspace).expanduser().resolve())
            return session

        monkeypatch.setattr(models, "persist_recovered_workspace_binding", spy_persist)

        handler = _FakeHandler()
        routes._handle_file_create(
            handler,
            {"session_id": "regate-1", "path": "hello.txt", "content": "hi"},
        )

        assert handler.status == 200
        # Bytes on the mapped WebUI-local root, NOT the fallback.
        assert (regate_world["webui_ws"] / "hello.txt").read_text(encoding="utf-8") == "hi"
        assert not (regate_world["fallback"] / "hello.txt").exists()
        # Recovery never ran for the translated session: no persist, and the
        # agent-visible binding is byte-identical.
        assert persist_calls == []
        assert models.get_session("regate-1").workspace == "/workspace"

    def test_create_then_list_same_session_sees_created_file(
        self, regate_world, monkeypatch
    ):
        """Native create→list through the same session (reviewer's table).

        Both operations must resolve through the same WebUI-local root, so
        a file created via the translated tier is visible to the listing.
        """
        models, routes = _patch_real_recovery_plumbing(
            monkeypatch,
            SimpleNamespace(session_id="regate-2", workspace="/workspace", profile="alice"),
            regate_world["fallback"],
        )

        created = _FakeHandler()
        routes._handle_file_create(
            created,
            {"session_id": "regate-2", "path": "visible.txt", "content": "x"},
        )
        assert created.status == 200

        listed = _FakeHandler()
        parsed = urlparse("/api/file/list?session_id=regate-2&path=.")
        routes._handle_list_dir(listed, parsed)

        assert listed.status == 200
        payload = listed.json_body()
        assert any(e.get("name") == "visible.txt" for e in payload["entries"])
        # The listing root is the translated WebUI-local root.
        assert payload["workspace"] == str(regate_world["webui_ws"])
        assert payload["workspace_recovered"] is False

    def test_translated_session_missing_local_root_fails_closed(
        self, regate_world, monkeypatch
    ):
        """A mapped session whose WebUI-local root is genuinely gone fails
        closed (404) — it must NOT silently jump to the fallback and must
        NOT rewrite the agent-visible binding."""
        import shutil

        shutil.rmtree(regate_world["webui_ws"])
        models, routes = _patch_real_recovery_plumbing(
            monkeypatch,
            SimpleNamespace(session_id="regate-3", workspace="/workspace", profile="alice"),
            regate_world["fallback"],
        )

        handler = _FakeHandler()
        routes._handle_file_create(
            handler,
            {"session_id": "regate-3", "path": "nope.txt", "content": "x"},
        )

        assert handler.status == 404
        assert not (regate_world["fallback"] / "nope.txt").exists()

    def test_unmapped_profile_recovery_still_fires_and_persists(
        self, regate_world, monkeypatch
    ):
        """Unmapped (no declared map) sessions keep the pre-existing
        recovery semantics byte-for-byte: a genuinely missing workspace
        still recovers to the fallback and persists the new binding."""
        import json as _json

        import api.models as models

        session_dir = regate_world["fallback"].parent / "sessions"
        session_dir.mkdir()
        sidecar = session_dir / "regate-4.json"
        sidecar.write_text(
            _json.dumps(
                {
                    "session_id": "regate-4",
                    "workspace": "/gone",
                    "messages": [{"role": "user", "content": "preserve me"}],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)

        models, routes = _patch_real_recovery_plumbing(
            monkeypatch,
            SimpleNamespace(session_id="regate-4", workspace="/gone", profile="bravo"),
            regate_world["fallback"],
        )

        handler = _FakeHandler()
        routes._handle_file_create(
            handler,
            {"session_id": "regate-4", "path": "recovered.txt", "content": "x"},
        )

        assert handler.status == 200
        # The fallback is a real trusted directory (under the faked home).
        assert (regate_world["fallback"] / "recovered.txt").exists()
        # The binding was persisted to the recovered workspace — existing
        # unmapped semantics, unchanged by the translation tier.
        persisted = _json.loads(sidecar.read_text(encoding="utf-8"))
        assert persisted["workspace"] == str(regate_world["fallback"].resolve())
        assert persisted["messages"] == [{"role": "user", "content": "preserve me"}]


# ── re-gate round 3 (2026-10-22): upload trust restoration + CLI owner profile ──
# Reviewer findings on head 754350b8dd01453b2a2bb86c4341b71cdc71ffe1:
#
#  1. handle_workspace_upload routed EVERY profiled session through
#     resolve_webui_workspace, whose no-map branch returns the direct path
#     WITHOUT resolve_trusted_workspace validation — silently dropping the
#     upload consumer's prior trust check. Replay: save a workspace outside
#     the trusted home, persist a session there, remove it from the
#     profile's saved list → production resolver rejects, but the upload
#     handler still answered 200 and wrote the file (restoring the old
#     trust-validation component gives 400 with no write).
#
#  2. _handle_list_dir assigned cli_owner_profile ONLY inside the
#     all-profiles fallback. When the initial (active-profile) lookup
#     succeeded, cli_meta["profile"] was discarded, _list_profile stayed
#     None, and the CLI translation arm was skipped — the listing resolved
#     through profile-less trusted resolution and answered 404 on the
#     host-absent agent-visible path, for BOTH the default and a named
#     profile. The all-profiles-fallback branches returned 200 and listed
#     the mapped local file.
#
# Both suites keep the REAL resolvers, the REAL handlers and the REAL
# profile configs under test — only the session lookup, the metadata
# lookup and the multipart reader are provided.


@pytest.fixture
def trust_world(tmp_path, monkeypatch):
    """Two profiles with EXPLICIT, disjoint trust sets.

    alice: declares agent /workspace -> webui_ws (webui_ws sits under the
           faked home); saved workspace list = {alice_only}
    bravo: declares NO map; saved workspace list = {bravo_only}

    alice_only/bravo_only live OUTSIDE the faked home and outside the boot
    default, so ONLY the saved workspace list (trust rule B) can authorize
    them — the reviewer's saved-workspace-revocation shape, and the
    wrong-owner isolation probe (a path saved under one profile must never
    resolve under another).
    """
    profiles_root = tmp_path / "profiles"
    fake_home = tmp_path / "home"
    webui_ws = fake_home / "webui-ws"
    webui_ws.mkdir(parents=True)
    alice_only = tmp_path / "alice-only"
    alice_only.mkdir()
    bravo_only = tmp_path / "bravo-only"
    bravo_only.mkdir()

    monkeypatch.setattr(workspace, "_home_path", lambda: fake_home)
    _write_profile(
        profiles_root / "alice",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {webui_ws}\n",
    )
    _write_profile(profiles_root / "bravo", "terminal:\n  backend: local\n")
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile",
        lambda name: profiles_root / (name or "alice"),
    )

    state = {"alice": [str(alice_only)], "bravo": [str(bravo_only)]}

    def fake_load_workspaces(profile: str | None = None) -> list:
        return [{"path": p} for p in state.get(profile or "", [])]

    monkeypatch.setattr(workspace, "load_workspaces", fake_load_workspaces)
    return {
        "profiles_root": profiles_root,
        "fake_home": fake_home,
        "webui_ws": webui_ws,
        "alice_only": alice_only,
        "bravo_only": bravo_only,
        "saved_state": state,
    }


def _drive_workspace_upload(monkeypatch, session, active_profile):
    """Drive the REAL handle_workspace_upload with *session*.

    Only the multipart reader and the session lookup are substituted; the
    profile-visibility check uses the REAL _profiles_match. Returns the
    fake handler after the call.
    """
    import api.upload as upload

    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: active_profile)
    monkeypatch.setattr(
        upload, "_read_multipart_or_reject",
        lambda handler: (
            {"session_id": "up-1", "path": ""},
            {"hello.txt": ("hello.txt", b"hi")},
        ),
    )
    monkeypatch.setattr(upload, "get_session", lambda sid: session)
    handler = _FakeHandler()
    upload.handle_workspace_upload(handler)
    return handler


class TestUploadTrustValidationRestored:
    """Reviewer finding 1 (2026-10-22): unmapped-profile uploads lost their
    existing trust validation.

    The translation tier's no-map branch returns the direct path without
    resolve_trusted_workspace validation; routing every profiled session
    through it dropped the upload trust check — a workspace removed from
    its profile's saved list still answered 200 and wrote the file. The
    fix gates the tier on workspace_translation_applies and restores the
    consumer's original trusted resolution when translation does not apply.
    """

    def test_saved_workspace_revoked_upload_rejected_without_write(
        self, trust_world, monkeypatch
    ):
        """The reviewer's exact replay: saved workspace revoked → 400, no write.

        RED on the reviewed head: the no-map tier branch passed the revoked
        path through and the handler answered 200 + write.
        """
        bravo_only = trust_world["bravo_only"]
        trust_world["saved_state"]["bravo"].clear()  # the revocation
        handler = _drive_workspace_upload(
            monkeypatch,
            SimpleNamespace(workspace=str(bravo_only), profile="bravo"),
            "bravo",
        )
        assert handler.status == 400
        assert not (bravo_only / "hello.txt").exists()

    def test_mapped_profile_upload_still_translates_and_writes(
        self, trust_world, monkeypatch
    ):
        """Control: a mapped profile keeps translating — the bytes land on
        the WebUI-local root (the tier stays authoritative when a map
        actually applies)."""
        webui_ws = trust_world["webui_ws"]
        handler = _drive_workspace_upload(
            monkeypatch,
            SimpleNamespace(workspace="/workspace", profile="alice"),
            "alice",
        )
        assert handler.status == 200
        assert (webui_ws / "hello.txt").read_text(encoding="utf-8") == "hi"
        assert not (trust_world["alice_only"] / "hello.txt").exists()

    def test_nomap_profile_saved_workspace_upload_still_allowed(
        self, trust_world, monkeypatch
    ):
        """Control: a no-map profile whose workspace is still saved uploads
        normally through the consumer's original trusted resolution."""
        bravo_only = trust_world["bravo_only"]
        handler = _drive_workspace_upload(
            monkeypatch,
            SimpleNamespace(workspace=str(bravo_only), profile="bravo"),
            "bravo",
        )
        assert handler.status == 200
        assert (bravo_only / "hello.txt").read_text(encoding="utf-8") == "hi"

    def test_profileless_session_untrusted_workspace_rejected(
        self, trust_world, monkeypatch
    ):
        """Control: profile-less legacy sessions keep the pre-existing
        ambient trusted resolution — untrusted path, 400, no write."""
        alice_only = trust_world["alice_only"]
        handler = _drive_workspace_upload(
            monkeypatch,
            SimpleNamespace(workspace=str(alice_only), profile=None),
            None,
        )
        assert handler.status == 400
        assert not (alice_only / "hello.txt").exists()


def _patch_cli_list_dir_plumbing(monkeypatch, *, active_hit, fallback_hit):
    """Drive _handle_list_dir's CLI fallback with a scripted metadata lookup.

    The WebUI session lookup always misses (a CLI session not loaded in
    WebUI memory). *active_hit* / *fallback_hit* are the rows the first
    (active-profile) and second (all-profiles) metadata lookups return;
    None means "not found". Returns (routes_module, lookup_calls) where
    lookup_calls records which lookups ran.
    """
    import api.routes as routes

    lookup_calls = []

    def fake_lookup(session_id, *, all_profiles=False):
        lookup_calls.append(all_profiles)
        row = fallback_hit if all_profiles else active_hit
        return dict(row) if row else {}

    def missing_session(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", missing_session)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", fake_lookup)
    return routes, lookup_calls


class TestListDirCliOwnerProfile:
    """Reviewer finding 2 (2026-10-22): active-profile CLI lookup hits did
    not use their profile map.

    cli_owner_profile was assigned ONLY inside the all-profiles fallback,
    so an active-hit row discarded cli_meta["profile"], _list_profile
    stayed None, and the CLI translation arm was skipped — the listing
    resolved through profile-less trusted resolution and answered 404 on
    the host-absent agent-visible path, for the default and named
    profiles alike. The owner profile is now derived from EVERY successful
    metadata lookup.
    """

    def test_active_hit_mapped_profile_lists_translated_root(
        self, trust_world, monkeypatch
    ):
        """Active hit + mapped owner profile → list the translated root.

        RED on the reviewed head: the discarded owner profile sent the
        listing through profile-less trusted resolution → 404 on
        /workspace (absent on this host by design).
        """
        webui_ws = trust_world["webui_ws"]
        (webui_ws / "mapped-file.txt").write_text("mapped", encoding="utf-8")
        routes, lookup_calls = _patch_cli_list_dir_plumbing(
            monkeypatch,
            active_hit={
                "session_id": "cli-1",
                "workspace": "/workspace",
                "profile": "alice",
            },
            fallback_hit=None,
        )
        handler = _FakeHandler()
        routes._handle_list_dir(handler, urlparse("/api/list?session_id=cli-1&path=."))

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["workspace"] == str(webui_ws)
        assert any(e.get("name") == "mapped-file.txt" for e in payload["entries"])
        # The active hit alone answered — no all-profiles fallback needed.
        assert lookup_calls == [False]

    def test_active_hit_nomap_profile_lists_owner_trusted_root(
        self, trust_world, monkeypatch
    ):
        """Active hit + no-map owner profile → list through the OWNER's
        trusted resolution (saved-list authorization), not profile-less.

        RED on the reviewed head: same discarded-profile path → 404.
        """
        bravo_only = trust_world["bravo_only"]
        (bravo_only / "bravo-file.txt").write_text("bravo", encoding="utf-8")
        routes, _ = _patch_cli_list_dir_plumbing(
            monkeypatch,
            active_hit={
                "session_id": "cli-2",
                "workspace": str(bravo_only),
                "profile": "bravo",
            },
            fallback_hit=None,
        )
        handler = _FakeHandler()
        routes._handle_list_dir(handler, urlparse("/api/list?session_id=cli-2&path=."))

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["workspace"] == str(bravo_only)
        assert any(e.get("name") == "bravo-file.txt" for e in payload["entries"])

    def test_fallback_hit_uses_owner_profile_trust(self, trust_world, monkeypatch):
        """Control: the all-profiles fallback branch keeps deriving the
        owner profile (pre-existing behavior, unchanged)."""
        bravo_only = trust_world["bravo_only"]
        routes, lookup_calls = _patch_cli_list_dir_plumbing(
            monkeypatch,
            active_hit=None,
            fallback_hit={
                "session_id": "cli-3",
                "workspace": str(bravo_only),
                "profile": "bravo",
            },
        )
        handler = _FakeHandler()
        routes._handle_list_dir(handler, urlparse("/api/list?session_id=cli-3&path=."))

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["workspace"] == str(bravo_only)
        assert lookup_calls == [False, True]

    def test_wrong_owner_workspace_not_widened(self, trust_world, monkeypatch):
        """Wrong-owner isolation: alice_only is saved under ALICE only. A
        bravo-owned session naming it must NOT list through alice's trust
        — resolution stays scoped to the row's own owner profile."""
        routes, _ = _patch_cli_list_dir_plumbing(
            monkeypatch,
            active_hit=None,
            fallback_hit={
                "session_id": "cli-4",
                "workspace": str(trust_world["alice_only"]),
                "profile": "bravo",
            },
        )
        handler = _FakeHandler()
        routes._handle_list_dir(handler, urlparse("/api/list?session_id=cli-4&path=."))

        assert handler.status == 404

    def test_nomap_owner_revoked_workspace_not_listed(self, trust_world, monkeypatch):
        """No-map trust preservation: with the owner profile's saved entry
        revoked, the listing fails closed (404) instead of silently
        passing the untrusted path through."""
        trust_world["saved_state"]["bravo"].clear()
        routes, _ = _patch_cli_list_dir_plumbing(
            monkeypatch,
            active_hit={
                "session_id": "cli-5",
                "workspace": str(trust_world["bravo_only"]),
                "profile": "bravo",
            },
            fallback_hit=None,
        )
        handler = _FakeHandler()
        routes._handle_list_dir(handler, urlparse("/api/list?session_id=cli-5&path=."))

        assert handler.status == 404


# ── #8450 maintainer re-gate: JSON export right after a file listing ─────────
# get_session_for_file_ops caches the translated path on the (cached, shared)
# Session object as ``_local_workspace`` (a PosixPath). The export handler used
# to snapshot the whole instance dict, so once ANY listing had attached the
# view — on every profile, mapped or not — json.dumps raised ``TypeError:
# Object of type PosixPath is not JSON serializable`` and the route answered
# 500. RED on the reviewed head; the export must stay 200 with the transcript
# intact and never leak underscore-cached attributes.


class TestJsonExportAfterFileListing:
    def test_export_json_after_listing_attached_local_workspace(
        self, mapped_world, monkeypatch
    ):
        import api.models as models
        import api.routes as routes

        webui_ws = mapped_world["webui_ws"]
        # The cached session object that a listing path resolves and mutates…
        session = routes.Session(session_id="exp-1", workspace="/workspace", profile="alice")
        session.messages = [{"role": "user", "content": "hello"}]
        _patch_file_ops_session_plumbing(monkeypatch, session)
        listed = models.get_session_for_file_ops("exp-1")  # …attaches the view
        assert listed is session
        assert listed.local_workspace == webui_ws.resolve()
        assert "_local_workspace" in vars(listed)

        # …and the export handler reads the very same cached object.
        monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: listed)
        monkeypatch.setattr(routes, "get_active_profile_name", lambda: "alice")
        monkeypatch.setattr(routes, "_profiles_match", lambda row, active: row == active)

        handler = _FakeHandler()
        routes._handle_session_export(
            handler, urlparse("/api/session/export?session_id=exp-1&format=json")
        )

        assert handler.status == 200
        payload = handler.json_body()
        assert payload["session_id"] == "exp-1"
        # Transcript intact — the export is the readable JSON artifact.
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        # Underscore-backed caches (the PosixPath translation, and any future
        # cached attribute) never leak into the exported artifact.
        assert not any(key.startswith("_") for key in payload)
