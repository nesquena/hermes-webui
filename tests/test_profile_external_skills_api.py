"""Profile-scoped external skill discovery through the WebUI Skills API.

A named WebUI profile resolves its skills root from the per-request active
profile, but ``skills.external_dirs`` is read by the Agent helper through the
Hermes home. These tests pin the request-profile scope around that lookup so a
profile with ``external_dirs`` configured shows its shared skills, the
root/default profile resolves its own home (not a streaming turn's mirror), and
the response reports ``runtime_scope``. The fail-closed and legacy paths are
covered agent-free in ``test_profile_external_skills_scope.py``.
"""

from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import yaml

from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n",
        encoding="utf-8",
    )


def _write_config(home: Path, external_dirs: list[str]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {"skills": {"external_dirs": external_dirs}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _capture_json(monkeypatch, routes):
    captured = {}

    def fake_json(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    return captured


def _patch_active_profile(monkeypatch, profiles, name: str, home: Path, process_home: Path) -> None:
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: name)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: home)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: process_home)


def _patch_agent_routing(monkeypatch, *, routed: bool) -> None:
    """Pin the Agent's routed-profile predicate so the test does not depend on process state.

    A real Agent without ``serves_routed_profile`` degrades to the legacy process-wide
    lookup, which still resolves external roots from the bound home; the scope label
    assertion below accepts either outcome.
    """
    try:
        import agent.secret_scope as secret_scope
    except Exception:
        return
    if hasattr(secret_scope, "serves_routed_profile"):
        monkeypatch.setattr(secret_scope, "serves_routed_profile", lambda: routed)


def test_skills_api_reads_external_dirs_from_request_profile(monkeypatch, tmp_path):
    """A named profile's shared skills must not resolve from default HERMES_HOME."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "translation"
    shared_skills = tmp_path / "shared-skills"
    _write_skill(active_home / "skills", "translation-local")
    _write_skill(shared_skills, "shared-library")
    _write_config(default_home, [])
    _write_config(active_home, [str(shared_skills)])

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _patch_active_profile(monkeypatch, profiles, "translation", active_home, default_home)
    _patch_agent_routing(monkeypatch, routed=True)

    captured = _capture_json(monkeypatch, routes)
    handled = routes.handle_get(MagicMock(), urlparse("/api/skills"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["runtime_scope"] in {"profile", "legacy_process"}
    skills = {item["name"]: item for item in captured["payload"]["skills"]}
    assert set(skills) == {"translation-local", "shared-library"}
    # A flat skill under an external root is labeled with that root's name.
    assert skills["shared-library"]["category"] == shared_skills.name
    assert skills["translation-local"]["category"] is None


def test_skills_content_resolves_external_skill_from_request_profile(monkeypatch, tmp_path):
    """Skill detail lookup must search the same profile-scoped external roots."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "translation"
    shared_skills = tmp_path / "shared-skills"
    _write_skill(shared_skills, "shared-library")
    _write_config(default_home, [])
    _write_config(active_home, [str(shared_skills)])

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _patch_active_profile(monkeypatch, profiles, "translation", active_home, default_home)
    _patch_agent_routing(monkeypatch, routed=True)

    captured = _capture_json(monkeypatch, routes)
    handled = routes.handle_get(
        MagicMock(), urlparse("/api/skills/content?name=shared-library")
    )

    assert handled is True
    assert captured["payload"]["success"] is True
    assert captured["payload"]["name"] == "shared-library"


def test_skills_api_default_profile_reads_external_dirs_from_process_home(monkeypatch, tmp_path):
    """The root/default profile still resolves external dirs from process HERMES_HOME."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    shared_skills = tmp_path / "shared-skills"
    _write_skill(default_home / "skills", "default-local")
    _write_skill(shared_skills, "shared-library")
    _write_config(default_home, [str(shared_skills)])

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _patch_active_profile(monkeypatch, profiles, "default", default_home, default_home)
    _patch_agent_routing(monkeypatch, routed=False)

    captured = _capture_json(monkeypatch, routes)
    handled = routes.handle_get(MagicMock(), urlparse("/api/skills"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["runtime_scope"] in {"profile", "legacy_process"}
    assert {item["name"] for item in captured["payload"]["skills"]} == {
        "default-local",
        "shared-library",
    }
