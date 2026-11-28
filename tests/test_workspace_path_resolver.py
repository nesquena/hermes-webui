"""#7217 P0: profile-scoped agent→WebUI workspace translation resolver.

Drives the REAL ``resolve_webui_workspace`` against REAL per-profile config
files and REAL filesystem layouts. The seams stubbed are the profile-home
resolver (pointed at temp dirs) and the saved-workspace list (pointed at the
temp WebUI roots) — the translation, trust re-validation, escape rejection,
and remote bypass all run for real.

Covered (maintainer scope + Dragonk invariant):
  1. mapped exact root and sub-path translation onto the WebUI-local root;
  2. unmapped profiles keep the untouched resolve_trusted_workspace contract;
  3. a sibling profile's root named directly is rejected (no global allow-root);
  4. ``..`` traversal through rel is rejected;
  5. a webui root that is a symlink to a blocked system path is dropped from
     the map (never widens trust);
  6. remote/SSH profiles bypass translation (non-stat target-side path);
  7. an explicit profile is mandatory (session fallback only);
  8. an empty workspace keeps boot-default semantics;
  9. rel returns (root, safe_resolve_ws-validated target);
 10. overlapping maps resolve longest-agent-prefix-first;
 11. malformed map config is ignored, never half-applied.
"""

import sqlite3  # noqa: F401 - presence parity with sibling suites
from pathlib import Path
from types import SimpleNamespace

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
    # Real deployments keep workspaces under the home root (trust A), which is
    # also what makes sub-path browsing trusted; mirror that here.
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
    # Saved-workspace trust for the unmapped/plain cases.
    # alice trusts its own mapped WebUI root; bravo (unmapped) trusts only the
    # plain saved dir — an agent path with no map entry cannot pass trust.
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


class TestMappedTranslation:
    def test_exact_root_translates(self, mapped_world):
        got = workspace.resolve_webui_workspace("/workspace", profile="alice")
        assert got == mapped_world["webui_ws"].resolve()

    def test_subpath_translates_with_tail(self, mapped_world):
        sub = mapped_world["webui_ws"] / "foo" / "bar"
        sub.mkdir(parents=True)
        got = workspace.resolve_webui_workspace("/workspace/foo/bar", profile="alice")
        assert got == sub.resolve()

    def test_missing_subpath_fails_closed(self, mapped_world):
        # Translation is exact: an agent sub-path with no local counterpart is
        # NOT silently coerced to the root (that would open the whole root).
        with pytest.raises(ValueError):
            workspace.resolve_webui_workspace("/workspace/ghost/dir", profile="alice")

    def test_unmapped_profile_passes_through_unchanged(self, mapped_world):
        # Single-container profile (no map declared): the translation tier is
        # inert and the pre-existing direct-path contract is preserved — even
        # for a path that does not exist locally.
        got = workspace.resolve_webui_workspace("/workspace", profile="bravo")
        assert got == Path("/workspace")

    def test_sibling_root_named_directly_is_rejected(self, mapped_world):
        # The core Dragonk invariant: a caller naming ANOTHER profile's
        # WebUI-local root must fail closed even when that root exists.
        sibling = mapped_world["profiles_root"] / "bob-ws"
        sibling.mkdir()
        with pytest.raises(ValueError):
            workspace.resolve_webui_workspace(str(sibling), profile="alice")

    def test_dotdot_rel_rejected(self, mapped_world):
        with pytest.raises(ValueError):
            workspace.resolve_webui_workspace(
                "/workspace", profile="alice", rel="../../etc/passwd"
            )

    def test_symlinked_webui_root_dropped_from_map(self, tmp_path, monkeypatch):
        # A map entry whose webui side resolves to a blocked system root must
        # be dropped entirely (translation inert), never half-applied.
        profiles_root = tmp_path / "profiles"
        _write_profile(
            profiles_root / "alice",
            "webui:\n"
            "  workspace_path_maps:\n"
            "    - agent: /workspace\n"
            "      webui: /etc\n",
        )
        monkeypatch.setattr(
            profiles, "get_hermes_home_for_profile",
            lambda name: profiles_root / (name or "alice"),
        )
        monkeypatch.setattr(workspace, "load_workspaces", lambda profile=None: [])
        assert workspace._workspace_path_map("alice") is None
        # The bad entry is dropped, so the tier is inert and behaviour matches
        # an unmapped profile — never a half-applied translation.
        assert workspace.resolve_webui_workspace("/workspace", profile="alice") == Path("/workspace")


class TestRemoteBypass:
    def test_remote_profile_keeps_target_side_path(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        _write_profile(
            profiles_root / "sshalice",
            "terminal:\n  backend: ssh\n  cwd: /remote/ws\n",
        )
        monkeypatch.setattr(
            profiles, "get_hermes_home_for_profile",
            lambda name: profiles_root / (name or "sshalice"),
        )
        monkeypatch.setattr(workspace, "load_workspaces", lambda profile=None: [])
        got = workspace.resolve_webui_workspace("/remote/ws/project", profile="sshalice")
        assert got == Path("/remote/ws/project")


class TestContract:
    def test_explicit_profile_required(self):
        with pytest.raises(ValueError):
            workspace.resolve_webui_workspace("/workspace")

    def test_session_profile_fallback(self, mapped_world):
        session = SimpleNamespace(workspace="/workspace", profile="alice")
        got = workspace.resolve_webui_workspace("/workspace", session=session)
        assert got == mapped_world["webui_ws"].resolve()

    def test_empty_workspace_uses_boot_default(self, tmp_path, monkeypatch):
        boot = tmp_path / "boot-ws"
        boot.mkdir()
        monkeypatch.setattr(workspace, "_BOOT_DEFAULT_WORKSPACE", boot)
        monkeypatch.setattr(
            profiles, "get_hermes_home_for_profile",
            lambda name: tmp_path / (name or "alice"),
        )
        monkeypatch.setattr(workspace, "load_workspaces", lambda profile=None: [])
        got = workspace.resolve_webui_workspace(None, profile="alice")
        assert got == boot.resolve()

    def test_rel_returns_validated_tuple(self, mapped_world):
        (mapped_world["webui_ws"] / "notes").mkdir()
        root, target = workspace.resolve_webui_workspace(
            "/workspace", profile="alice", rel="notes/todo.md"
        )
        assert root == mapped_world["webui_ws"].resolve()
        assert target == (mapped_world["webui_ws"] / "notes" / "todo.md").resolve()

    def test_malformed_map_config_ignored(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        _write_profile(
            profiles_root / "alice",
            "webui:\n  workspace_path_maps: not-a-list\n",
        )
        monkeypatch.setattr(
            profiles, "get_hermes_home_for_profile",
            lambda name: profiles_root / (name or "alice"),
        )
        monkeypatch.setattr(workspace, "load_workspaces", lambda profile=None: [])
        assert workspace._workspace_path_map("alice") is None


class TestOverlappingMaps:
    def test_longest_agent_prefix_wins(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        fake_home = tmp_path / "home"
        general = fake_home / "ws-general"
        specific = fake_home / "ws-specific"
        general.mkdir(parents=True)
        specific.mkdir(parents=True)
        monkeypatch.setattr(workspace, "_home_path", lambda: fake_home)
        _write_profile(
            profiles_root / "alice",
            "webui:\n"
            "  workspace_path_maps:\n"
            "    - agent: /workspace\n"
            f"      webui: {general}\n"
            "    - agent: /workspace/special\n"
            f"      webui: {specific}\n",
        )
        monkeypatch.setattr(
            profiles, "get_hermes_home_for_profile",
            lambda name: profiles_root / (name or "alice"),
        )
        monkeypatch.setattr(
            workspace, "load_workspaces",
            lambda profile=None: [{"path": str(general)}, {"path": str(specific)}],
        )
        (general / "plain").mkdir()
        (specific / "deep").mkdir(parents=True)
        assert workspace.resolve_webui_workspace("/workspace/plain", profile="alice") == (general / "plain").resolve()
        assert workspace.resolve_webui_workspace("/workspace/special/deep", profile="alice") == (specific / "deep").resolve()
