"""Rendered workspace artifact category and action coverage for #6593."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_helpers import assert_layout_sane

REPO = Path(__file__).resolve().parent.parent
SOURCE_MODE = os.environ.get("ISSUE6593_SOURCE_MODE", "head")


def _repo_file(path):
    if SOURCE_MODE == "base":
        return subprocess.check_output(
            ["git", "show", f"origin/master:{path}"], cwd=REPO, text=True
        )
    return (REPO / path).read_text(encoding="utf-8")


WORKSPACE_JS = _repo_file("static/workspace.js")
STYLE_CSS = _repo_file("static/style.css")
I18N_JS = _repo_file("static/i18n.js")
RENDER_LINT = Path(
    r"C:\Users\Rod\.codex\plugins\cache\personal\pr\0.0.15\shared\lint\render-lint.js"
).read_text(encoding="utf-8").split("\nmodule.exports", 1)[0]


def _function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"could not extract {name}")


def _locale_blocks():
    matches = list(re.finditer(r"^  (?:'[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", I18N_JS, re.MULTILINE))
    return [
        I18N_JS[match.start() : (matches[index + 1].start() if index + 1 < len(matches) else I18N_JS.index("\n};", match.start()))]
        for index, match in enumerate(matches)
    ]


def _render_harness(items):
    renderer = _function(WORKSPACE_JS, "renderSessionArtifacts")
    css = STYLE_CSS.replace("</style>", "")
    return f"""
      <style>{css}</style>
      <style>
        body {{ margin: 0; background: #141327; color: #efe7dd; font-family: Inter, system-ui, sans-serif; }}
        .issue6593-shell {{ min-height: 100vh; display: flex; justify-content: flex-end; }}
        .issue6593-panel {{ width: 360px; min-width: 0; border-left: 1px solid rgba(255,255,255,.08); background: #17162b; }}
        #workspaceArtifacts {{ width: 100%; box-sizing: border-box; }}
      </style>
      <div class="issue6593-shell">
        <main class="issue6593-panel" data-active-tab="artifacts">
          <div id="workspaceArtifacts" class="workspace-artifacts"></div>
        </main>
      </div>
      <span id="workspaceArtifactsCount"></span>
      <script>
        const S = {{ session: {{ workspace: '/workspace' }}, artifacts: {items!r} }};
        const $ = id => document.getElementById(id);
        const esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const t = key => ({{
          workspace_artifact_category_modified: 'Modified Files',
          workspace_artifact_category_read: 'Files Read',
          workspace_artifact_category_web: 'Web Pages',
          workspace_artifact_category_media: 'Inline Media',
          workspace_artifact_source_session: 'session'
        }})[key] || key;
        const collectSessionArtifacts = () => S.artifacts;
        const openArtifactPath = path => window.opened = [...(window.opened || []), path];
        {_function(WORKSPACE_JS, "_normalizeArtifactUrl") if "function _normalizeArtifactUrl(" in WORKSPACE_JS else ""}
        {renderer}
        renderSessionArtifacts();
      </script>
    """


def test_grouped_sections_are_quiet_responsive_and_keyboard_usable():
    playwright = pytest.importorskip("playwright.sync_api")
    items = [
        {"category": "modified", "path": "src/app.py", "source": "mutated"},
        {"category": "read", "path": "README.md", "source": "read"},
        {"category": "web", "path": "https://example.com/docs", "source": "web"},
        {"category": "media", "path": "assets/chart.png", "source": "media"},
    ]
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.set_content(_render_harness(items))
        screenshot_path = os.environ.get("ISSUE6593_SCREENSHOT_PATH")
        if screenshot_path:
            page.screenshot(path=screenshot_path)
        groups = page.locator(".workspace-artifact-group")
        assert groups.count() == 4
        assert page.locator(".workspace-artifact-group-title > span:first-child").all_text_contents() == [
            "Modified Files", "Files Read", "Web Pages", "Inline Media"
        ]
        assert page.locator(".workspace-artifact-item").count() == 3
        assert page.locator(".workspace-artifact-link").count() == 1
        assert page.locator(".workspace-artifact-link").get_attribute("href") == "https://example.com/docs"
        assert page.locator(".workspace-artifact-link").get_attribute("rel") == "noopener noreferrer"
        assert page.locator(".workspace-artifact-link").get_attribute("target") == "_blank"
        for width in (1280, 768, 400):
            page.set_viewport_size({"width": width, "height": 600})
            violations = page.evaluate(
                """(lintSource) => {
                    eval(lintSource);
                    return collectRenderViolations('.issue6593-panel', {
                        checks: ['overlap', 'clip', 'container-escape', 'degenerate', 'raw-string', 'a11y']
                    });
                }""",
                RENDER_LINT,
            )
            assert violations == [], violations
            assert_layout_sane(page, ".issue6593-panel", checks=["overlap", "clip", "container-escape", "degenerate", "raw-string"])
        page.locator(".workspace-artifact-item").first.focus()
        assert page.evaluate("() => document.activeElement.classList.contains('workspace-artifact-item')")
        page.locator(".workspace-artifact-group > summary").first.focus()
        assert page.evaluate("() => document.activeElement.tagName === 'SUMMARY'")
        browser.close()


def test_category_labels_are_present_in_each_locale_and_use_group_styles():
    expected = (
        "workspace_artifact_category_modified",
        "workspace_artifact_category_read",
        "workspace_artifact_category_web",
        "workspace_artifact_category_media",
    )
    blocks = _locale_blocks()
    assert len(blocks) >= 14
    for block in blocks:
        for key in expected:
            assert re.search(rf"\b{key}:\s*'", block), key
    assert ".workspace-artifact-group" in STYLE_CSS
    assert ".workspace-artifact-link" in STYLE_CSS
