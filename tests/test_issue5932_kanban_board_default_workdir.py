"""Regression coverage for Kanban board default workdir plumbing."""

import importlib
import json
import re
import subprocess
from pathlib import Path

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


class _Boards:
    def __init__(self):
        self.boards = {"default": {"slug": "default", "name": "Default", "default_workdir": "/old"}}

    def _normalize_board_slug(self, slug):
        return str(slug).strip()

    def board_exists(self, slug):
        return slug in self.boards

    def create_board(self, slug, **kwargs):
        self.boards.setdefault(slug, {"slug": slug, "name": slug, "archived": False})
        self.boards[slug].update({key: value for key, value in kwargs.items() if value is not None})
        return dict(self.boards[slug])

    def write_board_metadata(self, slug, **kwargs):
        self.boards[slug].update({key: value for key, value in kwargs.items() if value is not None})
        return dict(self.boards[slug])

    def get_current_board(self):
        return "default"

    def set_current_board(self, slug):
        return None


def _bridge(monkeypatch):
    bridge = importlib.import_module("api.kanban_bridge")
    boards = _Boards()
    monkeypatch.setattr(bridge, "_kb", lambda: boards)
    return bridge, boards


def test_board_payload_validates_default_workdir_and_preserves_omission(monkeypatch):
    bridge, boards = _bridge(monkeypatch)
    resolved = Path("/trusted/project")
    calls = []
    monkeypatch.setattr(bridge, "resolve_trusted_workspace", lambda value: calls.append(value) or resolved)

    created = bridge._create_board_payload({"slug": "project", "default_workdir": "  /saved/project  "})
    assert created["board"]["default_workdir"] == str(resolved)
    assert calls == ["/saved/project"]

    bridge._update_board_payload("project", {"name": "Project"})
    assert boards.boards["project"]["default_workdir"] == str(resolved)
    bridge._update_board_payload("project", {"default_workdir": ""})
    assert boards.boards["project"]["default_workdir"] == ""
    assert calls == ["/saved/project"]

    monkeypatch.setattr(bridge, "resolve_trusted_workspace", lambda value: (_ for _ in ()).throw(ValueError("untrusted")))
    try:
        bridge._update_board_payload("project", {"default_workdir": "/outside"})
    except ValueError as exc:
        assert "untrusted" in str(exc)
    else:
        raise AssertionError("untrusted workdir must be rejected")


def test_board_modal_submit_includes_and_clears_default_workdir():
    assert 'id="kanbanBoardModalDefaultWorkdir"' in INDEX
    assert 'id="kanbanBoardModalOriginalDefaultWorkdir"' in INDEX
    assert 'list="kanbanBoardModalWorkdirs"' in INDEX
    assert "if (defaultWorkdir) payload.default_workdir = defaultWorkdir;" in PANELS
    assert "default_workdir" not in PANELS[PANELS.find("createKanbanTask"):PANELS.find("createKanbanTask") + 5000]
    assert "async function _loadKanbanBoardWorkdirOptions(){\n  await loadWorkspaceList();" in PANELS

    submit_src = extract_function(PANELS, "submitKanbanBoardModal", prefix="async function")
    script = f"""
const calls = [];
const fields = {{
  kanbanBoardModalError: {{ textContent: '' }},
  kanbanBoardModalMode: {{ value: 'rename' }},
  kanbanBoardModalName: {{ value: 'Board name' }},
  kanbanBoardModalSlugInput: {{ value: 'default' }},
  kanbanBoardModalDesc: {{ value: 'Board description' }},
  kanbanBoardModalIcon: {{ value: '📋' }},
  kanbanBoardModalColor: {{ value: '#7aa2ff' }},
  kanbanBoardModalDefaultWorkdir: {{ value: '' }},
  kanbanBoardModalOriginalDefaultWorkdir: {{ value: '' }},
  kanbanBoardModalSubmit: {{ disabled: false }},
  kanbanBoardModalSlug: {{ value: 'default' }},
}};
const document = {{
  getElementById(id) {{
    if (!fields[id]) throw new Error('missing element ' + id);
    return fields[id];
  }},
}};
function t() {{ return null; }}
async function api(url, opts) {{
  calls.push({{ url, body: JSON.parse(opts.body) }});
  return {{}};
}}
function closeKanbanBoardModal() {{}}
async function loadKanbanBoards() {{}}
{submit_src}
async function runCase(currentValue, originalValue) {{
  calls.length = 0;
  fields.kanbanBoardModalDefaultWorkdir.value = currentValue;
  fields.kanbanBoardModalOriginalDefaultWorkdir.value = originalValue;
  await submitKanbanBoardModal();
  return calls[0].body;
}}
(async () => {{
  const unchanged = await runCase('/saved/project', '/saved/project');
  const changed = await runCase('/saved/project-next', '/saved/project');
  const cleared = await runCase('', '/saved/project');
  console.log(JSON.stringify({{ unchanged, changed, cleared }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    payload = json.loads(result.stdout)
    assert payload["unchanged"] == {
        "name": "Board name",
        "description": "Board description",
        "icon": "📋",
        "color": "#7aa2ff",
    }
    assert payload["changed"]["default_workdir"] == "/saved/project-next"
    assert payload["cleared"]["default_workdir"] == ""


def test_default_board_settings_remain_reachable():
    assert "switcher.hidden = false;" in PANELS
    assert "const archiveDisabled = current === 'default';" in PANELS
    assert "if (current === 'default') return;  // default's slug is immutable" not in PANELS
    menu = PANELS[PANELS.find("const actions = `"):PANELS.find("const actions = `") + 1800]
    assert "openKanbanRenameBoard()" in menu
    assert "renameDisabled" not in menu


def test_new_board_keys_are_present_in_every_locale_block():
    blocks = re.findall(r"\n\s*(?:['\"][a-z]{2}(?:-[A-Z][A-Za-z]+)?['\"]|[a-z]{2}(?:-[A-Z]{2})?)\s*:\s*\{(.*?)\n\s*\},", I18N, re.S)
    assert len(blocks) >= 14
    for block in blocks:
        assert "kanban_board_settings:" in block
        assert "kanban_board_default_workdir:" in block
        assert "kanban_board_default_workdir_placeholder:" in block


def _locale_for_snippet(snippet):
    index = I18N.find(snippet)
    assert index != -1, f"missing snippet: {snippet}"
    locale = None
    for match in re.finditer(
        r"^\s*(?:'(?P<quoted>[a-z]{2}(?:-[A-Z][A-Za-z]+)?)'|(?P<plain>[a-z]{2}(?:-[A-Z]{2})?))\s*:\s*\{",
        I18N,
        re.M,
    ):
        if match.start() > index:
            break
        locale = match.group("quoted") or match.group("plain")
    assert locale, f"missing locale opener before snippet: {snippet}"
    return locale


def test_new_board_keys_are_localized_in_non_english_sample_locales():
    samples = {
        "es": "Ruta de workspace predeterminada",
        "de": "Standard-Workspace-Pfad",
        "pt": "Caminho padrão do workspace",
        "ko": "기본 워크스페이스 경로",
    }
    for locale, expected in samples.items():
        snippet = f"kanban_board_default_workdir: '{expected}'"
        assert _locale_for_snippet(snippet) == locale
