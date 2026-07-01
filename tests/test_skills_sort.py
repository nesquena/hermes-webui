"""Tests for the Skills panel sort toggle (Name / Modified) — salvage of #3557.

Validates:
- Backend exposes a per-skill ``mtime`` field from ``SKILL.md`` ``st_mtime``.
- Frontend has a Name/Modified sort toggle wired to ``setSkillsSort`` with
  localStorage persistence, defaulting to Name (existing category-grouped view).
- The Modified branch sorts by ``mtime`` descending (newest-modified first).
- i18n keys exist in every locale block.
"""
import os
import re


def _read(rel):
    with open(os.path.join(*rel.split("/")), encoding="utf-8") as f:
        return f.read()


# ── Backend: per-skill mtime ────────────────────────────────────────────────

def test_backend_skills_list_exposes_mtime():
    src = _read("api/routes.py")
    idx = src.find("def _skills_list_from_dir(")
    assert idx != -1, "_skills_list_from_dir must exist"
    body = src[idx:idx + 3000]
    assert "st_mtime" in body, "Backend must read SKILL.md st_mtime"
    assert '"mtime": mtime' in body, "Skill dict must include an mtime field"


# ── Frontend: sort toggle wiring ────────────────────────────────────────────

def test_sort_bar_present_in_html():
    html = _read("static/index.html")
    assert 'id="skillsSortBar"' in html, "Skills panel must have a sort bar"
    assert "setSkillsSort('name')" in html
    assert "setSkillsSort('modified')" in html
    assert 'data-i18n="skills_sort_label"' in html


def test_set_skills_sort_persists_to_localstorage():
    p = _read("static/panels.js")
    assert "function setSkillsSort(" in p, "setSkillsSort() must exist"
    assert "hermes-webui-skills-sort" in p, "Sort preference must use localStorage key"
    assert "localStorage.setItem('hermes-webui-skills-sort'" in p


def test_default_sort_is_name():
    p = _read("static/panels.js")
    m = re.search(r"let _skillsSort = .*?localStorage\.getItem\('hermes-webui-skills-sort'\)\s*\|\|\s*'name'", p)
    assert m, "Default sort must fall back to 'name' (existing category-grouped behavior)"


def test_modified_branch_sorts_by_mtime_desc():
    p = _read("static/panels.js")
    idx = p.find("function renderSkills(")
    body = p[idx:idx + 3000]
    assert "_skillsSort === 'modified'" in body, "renderSkills must branch on modified sort"
    assert "(b.mtime || 0) - (a.mtime || 0)" in body, "Modified sort must be mtime descending"


def test_sort_ui_synced_on_load():
    p = _read("static/panels.js")
    assert "_syncSkillsSortUI" in p, "Active sort button must be synced when panel loads"


# ── i18n parity ─────────────────────────────────────────────────────────────

def test_sort_i18n_keys_in_all_locales():
    i18n = _read("static/i18n.js")
    # 14 locale blocks; one occurrence per locale.
    for key in ("skills_sort_label", "skills_sort_name", "skills_sort_modified"):
        assert i18n.count(f"{key}:") == 14, f"{key} must exist in all 14 locale blocks"
