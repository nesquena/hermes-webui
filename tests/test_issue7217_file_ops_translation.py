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