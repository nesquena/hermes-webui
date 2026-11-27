"""#8450: a mapped session working BELOW its mapped root must resolve.

greptile P1 of 2026-11-27 12:35 (comment 4719447865, "Mapped
subdirectories are refused"):

    If ``/workspace`` maps to a saved directory such as ``/data/alice``,
    resolving ``/workspace/project`` checks ``/data/alice/project`` as an
    INDEPENDENT workspace. ``resolve_trusted_workspace`` accepts saved paths
    only by EXACT match. Unless that subdirectory is separately saved or
    falls under the home or boot default, it rejects the existing directory.
    File and Git operations then fail for a valid session working below the
    mapped root.

    Validate the matched WebUI root against the profile's trust rules, then
    check that the resolved subdirectory stays inside it.

The mapped root here deliberately lives OUTSIDE the home directory and
OUTSIDE the boot default — the self-hosted shape this finding is about —
so the only thing that can admit the sub-directory is the mapped root
itself.
"""

from pathlib import Path

import pytest

workspace_mod = pytest.importorskip("api.workspace")
profiles = pytest.importorskip("api.profiles")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mapped_root_outside_home(tmp_path, monkeypatch):
    """A profile whose map points at a saved directory outside home.

    ``mapped_root`` holds ``project/`` (a sub-directory a session may work
    in) and a sibling ``outside/`` that the map does NOT cover.
    """
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    alice_home = home / "profiles" / "alice"
    alice_home.mkdir(parents=True, exist_ok=True)

    mapped_root = tmp_path / "data" / "alice"
    (mapped_root / "project").mkdir(parents=True, exist_ok=True)
    (mapped_root / "project" / "file.txt").write_text("IN-SUBDIR", encoding="utf-8")

    sibling = tmp_path / "data" / "bob"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "secret.txt").write_text("BOB", encoding="utf-8")

    _write(
        alice_home / "config.yaml",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {mapped_root}\n",
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(workspace_mod, "_home_path", lambda: home)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    # The profile's saved workspace list holds the mapped root only.
    monkeypatch.setattr(
        workspace_mod,
        "load_workspaces",
        lambda profile=None: [{"path": str(mapped_root)}],
    )

    return {
        "home": home,
        "alice_home": alice_home,
        "mapped_root": mapped_root,
        "sibling": sibling,
    }


def test_mapped_subdirectory_resolves_inside_the_trusted_root(
    mapped_root_outside_home, monkeypatch
):
    """A session working below the mapped root must resolve."""
    resolved = workspace_mod.resolve_webui_workspace(
        "/workspace/project", profile="alice"
    )

    assert Path(resolved) == (mapped_root_outside_home["mapped_root"] / "project").resolve(), {
        "resolved": str(resolved),
    }


def test_mapped_root_itself_still_resolves(mapped_root_outside_home, monkeypatch):
    """The exact mapped root keeps resolving (no regression)."""
    resolved = workspace_mod.resolve_webui_workspace("/workspace", profile="alice")

    assert Path(resolved) == mapped_root_outside_home["mapped_root"].resolve(), {
        "resolved": str(resolved),
    }


def test_deep_mapped_subdirectory_resolves(mapped_root_outside_home, monkeypatch):
    """A deeper sub-directory resolves as well."""
    deep = mapped_root_outside_home["mapped_root"] / "project" / "deep"
    deep.mkdir(parents=True, exist_ok=True)

    resolved = workspace_mod.resolve_webui_workspace(
        "/workspace/project/deep", profile="alice"
    )
    assert Path(resolved) == deep.resolve(), {"resolved": str(resolved)}


def test_mapped_subdirectory_cannot_escape_the_mapped_root(
    mapped_root_outside_home, monkeypatch
):
    """A traversal out of the mapped root stays blocked."""
    with pytest.raises(ValueError):
        workspace_mod.resolve_webui_workspace(
            "/workspace/../bob", profile="alice"
        )


def test_unmapped_profile_path_is_still_refused(
    mapped_root_outside_home, monkeypatch
):
    """A path outside every map keeps failing closed."""
    with pytest.raises(ValueError):
        workspace_mod.resolve_webui_workspace(
            str(mapped_root_outside_home["sibling"]), profile="alice"
        )


def test_missing_mapped_subdirectory_still_fails_closed(
    mapped_root_outside_home, monkeypatch
):
    """A missing local counterpart is NOT coerced to the mapped root.

    The pre-existing contract (test_workspace_path_resolver.py::
    test_missing_subpath_fails_closed) is that translation is exact: a
    sub-path with no local counterpart fails closed rather than opening
    the whole root. The sub-directory relaxation must not weaken it.
    """
    with pytest.raises(ValueError):
        workspace_mod.resolve_webui_workspace(
            "/workspace/ghost/dir", profile="alice"
        )


def test_symlink_out_of_the_mapped_root_is_still_refused(
    mapped_root_outside_home, monkeypatch
):
    """The relaxation must not become a symlink escape.

    A sub-directory of the mapped root is admitted WITHOUT being trusted in
    its own right, so a symlink inside the mapped root pointing OUTSIDE it
    must still be refused.
    """
    escape = mapped_root_outside_home["mapped_root"] / "escape"
    escape.symlink_to(mapped_root_outside_home["sibling"])

    with pytest.raises(ValueError):
        workspace_mod.resolve_webui_workspace("/workspace/escape", profile="alice")
