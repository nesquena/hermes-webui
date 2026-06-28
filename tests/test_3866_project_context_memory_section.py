import json
import pathlib
import shutil
import subprocess
from types import SimpleNamespace
from urllib.parse import urlencode

import api.profiles
import api.routes as routes
import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")


def project_context_for(workspace):
    return routes._read_active_project_context(pathlib.Path(workspace))


def test_project_context_reads_agents_md_from_active_workspace(tmp_path):
    workspace = tmp_path / "agents-only"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Agent Rules\n\nUse pytest here.", encoding="utf-8")

    data = project_context_for(workspace)

    assert "Use pytest here." in data["content"]
    assert data["path"].endswith("AGENTS.md")
    assert data["name"] == "AGENTS.md"
    assert data["shadowed"] == []


def test_project_context_prefers_hermes_md_and_reports_shadowed_agents(tmp_path):
    workspace = tmp_path / "priority"
    workspace.mkdir()
    (workspace / "HERMES.md").write_text("# Hermes Rules\n\nHermes wins.", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# Agent Rules\n\nAgents lose.", encoding="utf-8")

    data = project_context_for(workspace)

    assert "Hermes wins." in data["content"]
    assert "Agents lose." not in data["content"]
    assert data["path"].endswith("HERMES.md")
    assert [item["name"] for item in data["shadowed"]] == ["AGENTS.md"]
    assert data["shadowed"][0]["shadowed_by"] == "HERMES.md"


def test_project_context_walks_hermes_md_to_git_root_but_not_agents_md(tmp_path):
    root = tmp_path / "repo"
    child = root / "src" / "pkg"
    child.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".hermes.md").write_text("# Root Hermes\n\nRoot project rules.", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Root Agents\n\nRoot AGENTS should not be cwd-loaded.", encoding="utf-8")

    data = project_context_for(child)

    assert "Root project rules." in data["content"]
    assert data["path"].endswith(".hermes.md")
    assert data["path"].startswith(str(root))
    assert data["shadowed"] == []


def test_project_context_workspace_switch_re_resolves_same_session(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "AGENTS.md").write_text("First workspace rules.", encoding="utf-8")
    (second / "AGENTS.md").write_text("Second workspace rules.", encoding="utf-8")

    current = {"workspace": str(first)}

    def fake_get_session(_sid):
        return SimpleNamespace(workspace=current["workspace"])

    monkeypatch.setattr(routes, "get_session", fake_get_session)
    parsed = SimpleNamespace(query=urlencode({"session_id": "sid"}))

    before = routes._read_active_project_context(routes._memory_project_context_workspace(parsed))
    assert "First workspace rules." in before["content"]

    current["workspace"] = str(second)
    after = routes._read_active_project_context(routes._memory_project_context_workspace(parsed))
    assert "Second workspace rules." in after["content"]
    assert "First workspace rules." not in after["content"]
    assert after["workspace"] == str(second.resolve())


def test_project_context_absent_returns_empty_fields(tmp_path):
    workspace = tmp_path / "empty"
    workspace.mkdir()
    (workspace / ".git").mkdir()

    data = project_context_for(workspace)

    assert data["content"] == ""
    assert data["path"] == ""
    assert data["mtime"] is None
    assert data["shadowed"] == []


def test_project_context_content_is_redacted_in_memory_response(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "redacted"
    (home / "memories").mkdir(parents=True)
    workspace.mkdir()
    secret = "ghp_TestFakeCredential1234567890ab"
    (workspace / "AGENTS.md").write_text(
        f"# Agent Rules\n\nGitHub PAT: {secret}\nNormal note: keep me.",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(routes, "_memory_project_context_workspace", lambda _parsed: workspace)
    monkeypatch.setattr(routes, "_external_notes_sources_enabled", lambda: False)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    payload = routes._handle_memory_read(object(), SimpleNamespace(query=""))
    dumped = json.dumps(payload)

    assert secret not in dumped
    assert "Normal note: keep me." in payload["project_context"]


def test_memory_panel_defines_read_only_project_context_section():
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    assert "key: 'project_context'" in panels
    assert "readOnly: true" in panels
    assert "project_context_shadowed" in panels
    assert "/api/memory?session_id=" in panels


def test_memory_panel_references_all_memory_path_fields():
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    assert "function _memorySectionPath(key)" in panels
    assert "_memoryData.memory_path" in panels
    assert "_memoryData.user_path" in panels
    assert "_memoryData.soul_path" in panels
    assert "_memoryData.project_context_path" in panels
    assert "const sectionPath = _memorySectionPath(s.key)" in panels
    assert "if (sectionPath) el.title = sectionPath" in panels


def _memory_render_blocks():
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    helper_start = panels.index("function _memorySectionContent(key)")
    helper_end = panels.index("function _setMemoryHeaderButtons", helper_start)
    render_start = panels.index("function _renderMemoryDetail(section)")
    render_end = panels.index("function _renderMemoryEdit", render_start)
    return panels[helper_start:helper_end], panels[render_start:render_end]


def _run_memory_render_harness():
    helper_block, render_block = _memory_render_blocks()
    script = (
        "const helperBlock = "
        + json.dumps(helper_block)
        + ";\nconst renderBlock = "
        + json.dumps(render_block)
        + ";\n"
        + r"""
let _memoryData = {
  memory: 'Primary memory body',
  memory_path: 'C:/Users/Rod/.hermes/memories/MEMORY.md',
  memory_mtime: 1712345678,
  user: 'User memory body',
  user_path: 'C:/Users/Rod/.hermes/memories/USER.md',
  user_mtime: 1712345678,
  soul: 'Soul memory body',
  soul_path: 'C:/Users/Rod/.hermes/SOUL.md',
  soul_mtime: 1712345678,
  project_context: 'Project context body',
  project_context_path: 'D:/Repos/hermes-webui/AGENTS.md',
  project_context_name: 'AGENTS.md',
  project_context_mtime: 1712345678,
  project_context_shadowed: [{name: 'CLAUDE.md', shadowed_by: 'AGENTS.md'}],
};
let _memoryMode = '';
const nodes = {
  memoryDetailTitle: {textContent: '', style: {}},
  memoryDetailBody: {innerHTML: '', style: {}},
  memoryDetailEmpty: {style: {}},
};
function $(id) { return nodes[id] || null; }
function _memorySectionMeta(section) {
  return {key: section, label: section, emptyKey: section + '_empty'};
}
function _memorySectionLabel(meta) { return meta.label; }
function _memorySectionEmpty(meta) { return meta.emptyKey; }
function _setMemoryHeaderButtons() {}
function renderMd(content) { return 'rendered:' + content; }
function esc(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
eval(helperBlock + "\n" + renderBlock);
_renderMemoryDetail('memory');
const memoryHtml = nodes.memoryDetailBody.innerHTML;
_renderMemoryDetail('user');
const userHtml = nodes.memoryDetailBody.innerHTML;
_renderMemoryDetail('soul');
const soulHtml = nodes.memoryDetailBody.innerHTML;
_renderMemoryDetail('project_context');
const projectHtml = nodes.memoryDetailBody.innerHTML;
console.log(JSON.stringify({memoryHtml, userHtml, soulHtml, projectHtml, memoryMode: _memoryMode}));
"""
    )
    completed = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def _memory_button_render_blocks():
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    sections_start = panels.index("const MEMORY_SECTIONS = [")
    sections_end = panels.index("];", sections_start) + 2
    helper_start = panels.index("function _memorySectionPath(key)")
    helper_end = panels.index("function _setMemoryHeaderButtons", helper_start)
    loop_start = panels.index("for (const s of MEMORY_SECTIONS) {")
    loop_end = panels.index("    if (_currentMemorySection && _memoryMode !== 'edit') {", loop_start)
    return (
        panels[sections_start:sections_end],
        panels[helper_start:helper_end],
        panels[loop_start:loop_end].rsplit("    }", 1)[0],
    )


def _run_memory_button_harness():
    sections_block, helper_block, loop_block = _memory_button_render_blocks()
    script = (
        "const sectionsBlock = "
        + json.dumps(sections_block)
        + ";\nconst helperBlock = "
        + json.dumps(helper_block)
        + ";\nconst loopBlock = "
        + json.dumps(loop_block)
        + ";\n"
        + r"""
let _memoryData = {
  memory_path: 'C:/Users/Rod/.hermes/memories/MEMORY.md',
  user_path: 'C:/Users/Rod/.hermes/memories/USER.md',
  soul_path: 'C:/Users/Rod/.hermes/SOUL.md',
  project_context_path: 'D:/Repos/hermes-webui/AGENTS.md',
  external_notes_enabled: true,
};
let _currentMemorySection = 'memory';
const nodes = {
  memoryPanel: {
    innerHTML: '',
    appended: [],
    appendChild(node) { this.appended.push(node); },
  },
};
const document = {
  createElement() {
    return {
      title: '',
      type: '',
      className: '',
      innerHTML: '',
      onclick: null,
      classList: { add() {} },
      setAttribute(name, value) { this[name] = value; },
    };
  },
};
function _memorySectionLabel(meta) { return meta.key; }
function li() { return ''; }
function esc(value) { return String(value); }
function openMemorySection() {}
eval(
  sectionsBlock +
  "\n" +
  helperBlock +
  "\nfunction renderButtons() {\nconst panel = nodes.memoryPanel;\n" +
  loopBlock +
  "\n}\nrenderButtons();"
);
const buttons = nodes.memoryPanel.appended.map(btn => ({
  label: btn.innerHTML.replace(/<[^>]+>/g, ''),
  title: btn.title || '',
}));
console.log(JSON.stringify(buttons));
"""
    )
    completed = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def test_memory_detail_renders_path_for_non_project_sections():
    """Base-fails/head-passes regression for issue #4999.

    On base, `_renderMemoryDetail('memory')` ignores `memory_path`, so the
    rendered header omits the path row entirely. On head, the same render must
    show `MEMORY.md · <path>` using the existing pinned header row pattern.
    """
    if NODE is None:
        pytest.skip("node not on PATH")

    rendered = _run_memory_render_harness()

    assert "MEMORY.md" in rendered["memoryHtml"]
    assert "C:/Users/Rod/.hermes/memories/MEMORY.md" in rendered["memoryHtml"]
    assert "USER.md" in rendered["userHtml"]
    assert "C:/Users/Rod/.hermes/memories/USER.md" in rendered["userHtml"]
    assert "SOUL.md" in rendered["soulHtml"]
    assert "C:/Users/Rod/.hermes/SOUL.md" in rendered["soulHtml"]
    assert "AGENTS.md · D:/Repos/hermes-webui/AGENTS.md" in rendered["projectHtml"]
    assert "CLAUDE.md present, shadowed by AGENTS.md" in rendered["projectHtml"]


def test_memory_section_list_renders_hover_path_titles():
    """Base-fails/head-passes regression for issue #5045."""
    if NODE is None:
        pytest.skip("node not on PATH")

    rendered = {item["label"]: item["title"] for item in _run_memory_button_harness()}

    assert rendered["memory"] == "C:/Users/Rod/.hermes/memories/MEMORY.md"
    assert rendered["user"] == "C:/Users/Rod/.hermes/memories/USER.md"
    assert rendered["soul"] == "C:/Users/Rod/.hermes/SOUL.md"
    assert rendered["project_context"] == "D:/Repos/hermes-webui/AGENTS.md"
    assert rendered["external_notes"] == ""


def test_blank_session_workspace_does_not_resolve_to_server_cwd(monkeypatch):
    # Regression for the empty-workspace guard: a session with a blank workspace
    # (freshly-created/draft sessions) must return None rather than letting
    # Path("").resolve() surface the server's own CWD as project context.
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(workspace=""))
    parsed = SimpleNamespace(query="session_id=draft-session")

    assert routes._memory_project_context_workspace(parsed) is None


def test_project_context_reads_lowercase_agents_md(tmp_path):
    # Parity with the agent, which matches lowercase filename variants.
    workspace = tmp_path / "lowercase"
    workspace.mkdir()
    (workspace / "agents.md").write_text("# lower agents\n\nlowercase loads.", encoding="utf-8")

    data = project_context_for(workspace)

    # On case-insensitive filesystems the resolved name may normalize to the
    # uppercase candidate; the point of the fix is that the content loads at all
    # (on case-sensitive Linux CI, only the lowercase candidate matches).
    assert "lowercase loads." in data["content"]
    assert data["path"].lower().endswith("agents.md")


def test_project_context_strips_yaml_frontmatter(tmp_path):
    # Parity with the agent, which strips frontmatter before injecting context.
    workspace = tmp_path / "frontmatter"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "---\ntitle: Rules\nowner: me\n---\n\nActual body the agent injects.",
        encoding="utf-8",
    )

    data = project_context_for(workspace)

    assert "Actual body the agent injects." in data["content"]
    assert "title: Rules" not in data["content"]
