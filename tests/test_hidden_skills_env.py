"""Tests for the HERMES_WEBUI_HIDDEN_SKILLS operator env var.

The hidden-skills knob lets a Docker / multi-tenant operator strip specific
skills out of the WebUI (Skills panel, cron skill picker, /api/skills/usage
breakdown) without forking the agent image. The hidden set is read once per
process and matched case-insensitively against each skill's resolved name
(frontmatter `name:` if present, else the containing directory name).

These tests are pure unit tests: they call the helpers directly, drive
``_skills_list_from_dir`` against a tmp_path skills dir, and reset the
process-global cache between cases so env mutations are observed cleanly.
"""
from pathlib import Path

import pytest

import api.routes as _routes_mod
from api.routes import _skill_name_is_hidden


# ─── Cache reset helper ────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_hidden_cache(monkeypatch):
    """Reset the module-level hidden-names cache around every test.

    The production helper memoizes the parsed env value once per process for
    performance. Tests deliberately mutate ``HERMES_WEBUI_HIDDEN_SKILLS`` to
    exercise the parser and the filter, so we have to clear the cache
    between cases or the second test would see the first test's value.
    """
    monkeypatch.delenv("HERMES_WEBUI_HIDDEN_SKILLS", raising=False)
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    yield
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None


# ─── Env parser ────────────────────────────────────────────────────────────
def test_unset_env_returns_empty():
    """No env var => no skills hidden; the helper stays cheap and inert."""
    from api.routes import _hidden_skill_names

    assert _hidden_skill_names() == frozenset()
    assert _skill_name_is_hidden("anything") is False


def test_empty_env_returns_empty():
    """An empty-string env var should NOT be treated as 'hide everything'."""
    import os
    from api.routes import _hidden_skill_names

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = ""
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    assert _hidden_skill_names() == frozenset()


def test_comma_separated_parsing():
    """Commas split entries; whitespace around each entry is stripped."""
    import os
    from api.routes import _hidden_skill_names

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "imcrobot-skill, internal-admin ,billing"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    assert _hidden_skill_names() == frozenset(
        {"imcrobot-skill", "internal-admin", "billing"}
    )


def test_whitespace_separated_parsing():
    """Tabs / multiple spaces act as a separator like commas do."""
    import os
    from api.routes import _hidden_skill_names

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "  imcrobot-skill\tinternal-admin   "
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    assert _hidden_skill_names() == frozenset(
        {"imcrobot-skill", "internal-admin"}
    )


def test_mixed_separators():
    """Commas and whitespace can be mixed freely."""
    import os
    from api.routes import _hidden_skill_names

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "a, b  c,d\te"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    assert _hidden_skill_names() == frozenset({"a", "b", "c", "d", "e"})


def test_cache_reuses_parsed_value():
    """The second call must hit the cache (parse only once per process)."""
    import os
    from api.routes import _hidden_skill_names

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "x"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    first = _hidden_skill_names()
    # Mutate the env after the first call. The cached value must NOT change.
    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "y, z"
    assert _hidden_skill_names() is first
    assert _hidden_skill_names() == frozenset({"x"})


# ─── Hidden-by-name predicate ──────────────────────────────────────────────
def test_hidden_predicate_case_insensitive():
    """The matcher is case-insensitive: 'IMCROBOT-SKILL' hides 'imcrobot-skill'."""
    import os

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "IMCROBOT-SKILL"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    assert _skill_name_is_hidden("imcrobot-skill") is True
    assert _skill_name_is_hidden("IMCROBOT-SKILL") is True
    assert _skill_name_is_hidden("imcrobot-skill  ") is True  # trim tolerance
    assert _skill_name_is_hidden("other-skill") is False


def test_hidden_predicate_exact_match_not_substring():
    """Hidden set is exact-match; 'foo' must NOT hide 'foobar'."""
    import os

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "foo"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None
    assert _skill_name_is_hidden("foo") is True
    assert _skill_name_is_hidden("foobar") is False
    assert _skill_name_is_hidden("xfoo") is False


def test_hidden_predicate_empty_name_never_matches():
    """An empty skill name must never match — the listing code already
    short-circuits blank names, but the predicate is exposed to other call
    sites too, so guard it independently."""
    assert _skill_name_is_hidden("") is False
    assert _skill_name_is_hidden("   ") is False


# ─── Integration: filter inside _skills_list_from_dir ──────────────────────
def _make_skill(skills_dir: Path, name: str, description: str = "A skill") -> None:
    """Helper: drop a real SKILL.md under skills_dir/<name>/."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n{description}\n",
        encoding="utf-8",
    )


def test_list_from_dir_filters_hidden_skills(tmp_path, monkeypatch):
    """A hidden skill is dropped from the listing; non-hidden ones survive."""
    import os
    from api.routes import _skills_list_from_dir

    monkeypatch.setattr("api.routes._get_disabled_skill_names_for_profile", lambda: set())
    monkeypatch.setattr("api.routes._active_skill_search_dirs", lambda d: [d])

    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "imcrobot-skill", "internal one")
    _make_skill(skills_dir, "public-skill", "visible one")
    _make_skill(skills_dir, "another-public", "also visible")

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "imcrobot-skill"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None

    result = _skills_list_from_dir(skills_dir)
    names = [s["name"] for s in result["skills"]]
    assert "imcrobot-skill" not in names
    assert sorted(names) == ["another-public", "public-skill"]


def test_list_from_dir_no_env_returns_all(tmp_path, monkeypatch):
    """Without the env var set, listing is unchanged (regression guard)."""
    from api.routes import _skills_list_from_dir

    monkeypatch.setattr("api.routes._get_disabled_skill_names_for_profile", lambda: set())
    monkeypatch.setattr("api.routes._active_skill_search_dirs", lambda d: [d])

    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "imcrobot-skill")
    _make_skill(skills_dir, "public-skill")

    result = _skills_list_from_dir(skills_dir)
    assert {s["name"] for s in result["skills"]} == {"imcrobot-skill", "public-skill"}


def test_list_from_dir_hidden_uses_directory_name(tmp_path, monkeypatch):
    """When a SKILL.md has no `name:` frontmatter, the directory name is
    what gets matched against the hidden set. This is the operator's
    documented contract: 'matches the skill's frontmatter `name:` if
    present, else the containing directory name'."""
    import os
    from api.routes import _skills_list_from_dir

    monkeypatch.setattr("api.routes._get_disabled_skill_names_for_profile", lambda: set())
    monkeypatch.setattr("api.routes._active_skill_search_dirs", lambda d: [d])

    skills_dir = tmp_path / "skills"
    # No `name:` frontmatter — listing falls back to skill_dir.name.
    skill_dir = skills_dir / "legacy-named-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: no name in frontmatter\n---\nbody\n",
        encoding="utf-8",
    )

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "legacy-named-skill"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None

    result = _skills_list_from_dir(skills_dir)
    assert result["skills"] == []


def test_list_from_dir_multiple_hidden(tmp_path, monkeypatch):
    """Multiple hidden entries all get filtered; the rest come through."""
    import os
    from api.routes import _skills_list_from_dir

    monkeypatch.setattr("api.routes._get_disabled_skill_names_for_profile", lambda: set())
    monkeypatch.setattr("api.routes._active_skill_search_dirs", lambda d: [d])

    skills_dir = tmp_path / "skills"
    for n in ("imcrobot-skill", "internal-admin", "billing", "user-facing"):
        _make_skill(skills_dir, n)

    os.environ["HERMES_WEBUI_HIDDEN_SKILLS"] = "imcrobot-skill, internal-admin, billing"
    _routes_mod._HIDDEN_SKILL_NAMES_CACHE = None

    result = _skills_list_from_dir(skills_dir)
    assert [s["name"] for s in result["skills"]] == ["user-facing"]


# ─── Defensive behaviour ───────────────────────────────────────────────────
def test_predicate_tolerates_broken_cache(monkeypatch):
    """A buggy cache accessor must NOT silently hide everything. Defence in
    depth so a future refactor can't accidentally ship a 'hide all' bug."""
    from api.routes import _skill_name_is_hidden

    def boom():
        raise RuntimeError("simulated cache failure")

    monkeypatch.setattr(_routes_mod, "_hidden_skill_names", boom)
    assert _skill_name_is_hidden("any-name") is False
    assert _skill_name_is_hidden("imcrobot-skill") is False
