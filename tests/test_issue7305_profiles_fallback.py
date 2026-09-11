"""#7305: GET /api/profiles must never 500 when the hermes-agent source is not
importable (two-container Docker / HERMES_WEBUI_CHAT_BACKEND=gateway).

Contract pinned here: when ``agent.skill_utils`` is missing, the WebUI reports
the skill stats as *unknown* — a stable ``(0, 0)`` — instead of counting skills
with a local partial re-implementation of the index walk. The profile picker
still renders, and it never claims a count it cannot verify (the UI omits the
skills line when the total is 0).

The fixture tree deliberately reproduces the shapes that a partial local scan
gets wrong: a SKILL.md whose declared ``name`` differs from its directory, two
directories declaring the same name, a declared name disabled in the profile
config, a platform-incompatible skill, and the paths the real index walk prunes
(support dirs of a skill package, dependency/VCS/metadata dirs, an inactive
org-mirror). ``test_agent_backed_path_counts_these_shapes_accurately`` is the
positive control: it runs the same tree through the real agent-backed path, so
the fixture cannot silently degrade into "a tree that never had a count to
lose".
"""

from __future__ import annotations

import builtins
import pathlib

import pytest

import api.profiles as profiles

# How a fixture entry relates to the reported counts.
COUNTED = "counted"  # yields a logical skill; contributes to compatible/enabled
WALK_PRUNED = "walk-pruned"  # never yielded by agent.skill_utils' index walk
PLATFORM_FILTERED = "platform-filtered"  # walked, but incompatible with this host

# path -> (declared frontmatter name, platform restriction or None, outcome)
SKILLS_TREE = {
    # Declared name differs from the directory name; that declared name is
    # disabled for the webui platform in config.yaml below.
    "alpha-dir/SKILL.md": ("my-skill", None, COUNTED),
    # A second directory declaring the same name — one logical skill.
    "beta-dir/SKILL.md": ("my-skill", None, COUNTED),
    # Platform-incompatible: must not be offered (or counted) on this host.
    "gamma-dir/SKILL.md": ("gamma-skill", "some-other-platform", PLATFORM_FILTERED),
    # A plain, enabled skill.
    "delta-dir/SKILL.md": ("delta-skill", None, COUNTED),
    # Support dir of a skill package: disclosure-only, not a standalone skill.
    "delta-dir/references/notes/SKILL.md": ("support-note", None, WALK_PRUNED),
    # Dependency / virtualenv / VCS / metadata dirs are excluded wholesale, and
    # an org mirror without its .active_org marker is token-gated shut.
    "node_modules/dep/SKILL.md": ("vendored-skill", None, WALK_PRUNED),
    ".venv/lib/pkg/SKILL.md": ("venv-skill", None, WALK_PRUNED),
    ".git/hooks/SKILL.md": ("hook-skill", None, WALK_PRUNED),
    "_org/other-org/org-skill/SKILL.md": ("org-skill", None, WALK_PRUNED),
}

# The one declared name the profile config disables for the webui platform.
DISABLED_NAMES = {"my-skill"}

CONFIG_YAML = (
    "profile:\n"
    "  name: default\n"
    "skills:\n"
    "  platform_disabled:\n"
    "    webui:\n"
    "      - my-skill\n"
)

CONFIG_WITHOUT_DISABLE = "profile:\n  name: default\n"


def _expected_walked() -> set[str]:
    return {
        rel
        for rel, (_, _, outcome) in SKILLS_TREE.items()
        if outcome != WALK_PRUNED
    }


def _expected_names(outcome: str) -> set[str]:
    return {name for name, _, entry_outcome in SKILLS_TREE.values() if entry_outcome == outcome}


@pytest.fixture()
def agent_absent(monkeypatch):
    """Simulate the agent-less deployment: importing anything under ``agent.``
    raises ``ImportError``, even when the source tree is present locally."""
    real_import = builtins.__import__

    def _no_agent(name, *args, **kwargs):
        if name == "agent" or str(name).startswith("agent."):
            raise ImportError(f"{name} not mounted (simulated two-container Docker)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_agent)


@pytest.fixture()
def hermes_cli_absent(monkeypatch):
    """Simulate the same deployment's other half: no ``hermes_cli`` either."""
    real_import = builtins.__import__

    def _no_cli(name, *args, **kwargs):
        if name == "hermes_cli" or str(name).startswith("hermes_cli."):
            raise ImportError(f"{name} not mounted (simulated two-container Docker)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_cli)


def _write_profile(tmp_path: pathlib.Path, dir_name: str = "home") -> pathlib.Path:
    """Materialise SKILLS_TREE + config.yaml as a profile home."""
    home = tmp_path / dir_name
    skills = home / "skills"
    for rel, (name, platform, _outcome) in SKILLS_TREE.items():
        skill_md = skills / rel
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        lines = ["---", f"name: {name}", "description: issue 7305 fixture"]
        if platform:
            lines += ["platforms:", f"  - {platform}"]
        lines += ["---", "body", ""]
        skill_md.write_text("\n".join(lines), encoding="utf-8")
    (home / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    return home


def test_skills_stats_are_unknown_without_agent_skill_utils(tmp_path, agent_absent):
    """Without agent.skill_utils the stats are (0, 0) — never a guessed count,
    no matter which fixtures a partial local scan would have counted."""
    home = _write_profile(tmp_path)

    assert profiles._get_profile_skills_stats(home) == (0, 0)


def test_default_profile_dict_never_raises_without_agent(tmp_path, agent_absent, monkeypatch):
    """_default_profile_dict() (the fallback row for GET /api/profiles) must
    return a usable row with unknown skill stats instead of raising."""
    home = _write_profile(tmp_path)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)

    row = profiles._default_profile_dict()

    assert row["name"] == "default"
    assert row["path"] == str(home)
    assert row["is_default"] is True
    assert row["skill_count"] == 0
    assert row["enabled_skills"] == 0
    assert row["total_skills"] == 0


def test_list_profiles_api_isolated_mode_without_agent_or_cli(
    tmp_path, agent_absent, hermes_cli_absent, monkeypatch
):
    """The #7305 path end to end: isolated profile mode with neither
    agent.skill_utils nor hermes_cli importable must still return the
    default-only row instead of propagating ImportError out of the route."""
    home = _write_profile(tmp_path, dir_name="default")
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(home))

    rows = profiles.list_profiles_api()

    assert [r["name"] for r in rows] == ["default"]
    assert rows[0]["is_active"] is True
    assert rows[0]["skill_count"] == 0
    assert rows[0]["enabled_skills"] == 0
    assert rows[0]["total_skills"] == 0


def test_agent_backed_path_counts_these_shapes_accurately(tmp_path):
    """Positive control (agent source present): the fixture above is a real
    semantic trap, not a tree with nothing to count.

    - the index walk yields exactly the non-pruned SKILL.md files, so the
      support/dependency/VCS/org-mirror files a naive local ``os.walk`` would
      pick up are invisible to the count;
    - the platform-incompatible and duplicate-declared-name files do not add to
      the compatible total, which therefore stays below the walk's file count;
    - identity comes from frontmatter ``name``: the disabled ``my-skill`` (a
      declared name, not a directory) is compatible but not enabled;
    - dropping that config disable flips it to enabled without changing the
      compatible total.
    """
    pytest.importorskip("agent.skill_utils")
    from agent.skill_utils import iter_skill_index_files

    home = _write_profile(tmp_path)
    skills = home / "skills"

    walked = {
        p.relative_to(skills).as_posix() for p in iter_skill_index_files(skills, "SKILL.md")
    }
    on_disk = {p.relative_to(skills).as_posix() for p in skills.rglob("SKILL.md")}

    expected_walked = _expected_walked()
    expected_compatible = _expected_names(COUNTED)
    expected_enabled = expected_compatible - DISABLED_NAMES

    # The fixture must contain paths a naive scan would over-count...
    assert on_disk > walked, "fixture must include SKILL.md files the index walk prunes"
    # ...and the walk must still see exactly the paths it is documented to keep.
    assert walked == expected_walked

    enabled, compatible = profiles._compute_profile_skills_stats(home)

    assert compatible == len(expected_compatible)
    assert compatible < len(walked)
    assert enabled == len(expected_enabled)

    (home / "config.yaml").write_text(CONFIG_WITHOUT_DISABLE, encoding="utf-8")
    enabled_all, compatible_all = profiles._compute_profile_skills_stats(home)

    assert compatible_all == compatible
    assert enabled_all == len(expected_compatible)
