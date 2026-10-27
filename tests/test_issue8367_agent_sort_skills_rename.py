"""Regression tests for the Agent's ``_sort_skills`` -> ``sort_skills`` rename (PR #8367).

Agent commit 93fafc4d6ff (2026-10-18) made the skills tool's helpers public and kept no
underscore aliases, so WebUI's hard ``from tools.skills_tool import _sort_skills`` raised
ImportError and took down ``/api/skills`` and ``/api/skills/usage`` on every newer Agent.
WebUI pairs with whatever Agent the user has installed, so the listing must work with the
old spelling, the new spelling, and (defensively) neither.

These tests inject stand-in Agent modules so they run without hermes-agent installed (CI).
"""

import sys
import types
from pathlib import Path

import pytest

import api.routes as routes


def _fake_frontmatter(content: str):
    head, _, body = content.partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        if ":" in line and not line.startswith("---"):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body


def _install_fake_agent(monkeypatch, sort_attr):
    skill_utils = types.ModuleType("agent.skill_utils")
    skill_utils.iter_skill_index_files = lambda root, filename: sorted(Path(root).rglob(filename))
    skill_utils.get_external_skills_dirs = lambda: []
    monkeypatch.setitem(sys.modules, "agent.skill_utils", skill_utils)

    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool.MAX_DESCRIPTION_LENGTH = 1024
    skills_tool._EXCLUDED_SKILL_DIRS = frozenset({".git", ".hub"})
    skills_tool._parse_frontmatter = _fake_frontmatter
    skills_tool.skill_matches_platform = lambda frontmatter: True
    calls = []
    if sort_attr is not None:
        def _agent_sort(skills):
            calls.append(len(skills))
            return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))
        setattr(skills_tool, sort_attr, _agent_sort)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setattr(routes, "_get_disabled_skill_names_for_profile", lambda: set())
    return calls


def _write_skill(root: Path, rel: str, name: str) -> None:
    skill_dir = root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n# {name}\n",
        encoding="utf-8",
    )


def _seed(tmp_path: Path) -> Path:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "zeta", "zeta")
    _write_skill(skills_dir, "devops/beta", "beta")
    _write_skill(skills_dir, "devops/alpha", "alpha")
    _write_skill(skills_dir, "alpha-top", "alpha-top")
    return skills_dir


@pytest.mark.parametrize(
    "sort_attr",
    ["sort_skills", "_sort_skills", None],
    ids=["agent-after-93fafc4d", "agent-before-93fafc4d", "agent-without-helper"],
)
def test_skill_listing_works_with_every_agent_spelling(tmp_path, monkeypatch, sort_attr):
    calls = _install_fake_agent(monkeypatch, sort_attr)

    result = routes._skills_list_from_dir(_seed(tmp_path))

    assert result["success"] is True
    assert [(s["category"], s["name"]) for s in result["skills"]] == [
        (None, "alpha-top"),
        (None, "zeta"),
        ("devops", "alpha"),
        ("devops", "beta"),
    ]
    assert result["categories"] == ["devops"]
    # The Agent's own ordering helper is used whenever this Agent exports one.
    assert calls == ([] if sort_attr is None else [4])


def test_resolver_prefers_the_public_name_when_both_exist():
    module = types.ModuleType("tools.skills_tool")
    module.sort_skills = lambda skills: "public"
    module._sort_skills = lambda skills: "private"
    assert routes._resolve_agent_sort_skills(module)([]) == "public"


def test_resolver_ignores_a_non_callable_attribute():
    module = types.ModuleType("tools.skills_tool")
    module.sort_skills = None
    module._sort_skills = "not callable"
    ordered = routes._resolve_agent_sort_skills(module)(
        [{"name": "b", "category": "x"}, {"name": "a", "category": None}]
    )
    assert [s["name"] for s in ordered] == ["a", "b"]
