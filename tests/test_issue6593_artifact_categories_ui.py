"""Rendered workspace artifact category and action coverage for #6593."""

import os
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_helpers import assert_layout_sane

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "issue6593_artifact_categories.json"
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


def _artifact_bundle():
    consts = "\n".join(
        re.search(rf"const {name} = .*?;", WORKSPACE_JS).group(0)
        for name in (
            "ARTIFACT_IGNORE_RE",
            "ARTIFACT_MUTATION_TOOLS",
            "ARTIFACT_READ_TOOLS",
            "ARTIFACT_WEB_TOOLS",
            "ARTIFACT_CATEGORY_ORDER",
            "ARTIFACT_CATEGORY_LIMITS",
        )
    )
    return consts + "\n" + "\n".join(
        _function(WORKSPACE_JS, name)
        for name in (
            "_normalizeArtifactPath",
            "_normalizeArtifactUrl",
            "_normalizeArtifactTarget",
            "_normalizeArtifactMediaRef",
            "_looksLikeArtifactPath",
            "_parseArtifactJson",
            "_artifactCandidatesFromText",
            "_artifactCandidatesFromToolCall",
            "collectSessionArtifacts",
        )
    )


def _render_harness(payload):
    renderer = _function(WORKSPACE_JS, "renderSessionArtifacts")
    css = STYLE_CSS.replace("</style>", "")
    tool_calls = json.dumps(payload["tool_calls"])
    messages = json.dumps(payload["messages"])
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
        const S = {{
          toolCalls: {tool_calls},
          messages: {messages},
          session: {{ workspace: '/workspace', session_id: 'ui-proof' }}
        }};
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
        const openArtifactPath = path => window.opened = [...(window.opened || []), path];
        {_artifact_bundle()}
        const _issue6593CollectedArtifacts = collectSessionArtifacts();
        window.issue6593CollectedArtifacts = _issue6593CollectedArtifacts;
        collectSessionArtifacts = () => _issue6593CollectedArtifacts.map(item => ({{...item, source: item.category}}));
        {renderer}
        renderSessionArtifacts();
      </script>
    """


def test_grouped_sections_are_quiet_responsive_and_keyboard_usable():
    playwright = pytest.importorskip("playwright.sync_api")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    search_call = dict(fixture["tool_calls"][2])
    search_call["result"] = '{"files":["src/app.py"]}'
    payload = {
        "tool_calls": [fixture["tool_calls"][0], search_call, *fixture["tool_calls"][3:6]],
        "messages": [fixture["messages"][0], fixture["messages"][5]],
    }
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.set_content(_render_harness(payload))
        screenshot_path = os.environ.get("ISSUE6593_SCREENSHOT_PATH")
        if screenshot_path:
            page.screenshot(path=screenshot_path)
        groups = page.locator(".workspace-artifact-group")
        assert groups.count() == 4
        collected = page.evaluate("() => window.issue6593CollectedArtifacts")
        assert [item["category"] for item in collected] == [
            "modified", "read", "web", "web", "web", "media", "media"
        ]
        assert all("user" not in item["path"] for item in collected)
        assert page.locator(".workspace-artifact-group").evaluate_all(
            "nodes => nodes.map(node => node.dataset.artifactCategory)"
        ) == ["modified", "read", "web", "media"]
        assert page.locator(".workspace-artifact-group-title > span:first-child").all_text_contents() == [
            "Modified Files", "Files Read", "Web Pages", "Inline Media"
        ]
        assert page.locator(".workspace-artifact-item").count() == 4
        assert page.locator(".workspace-artifact-link").count() == 3
        assert page.locator("a[href^='javascript:']").count() == 0
        assert "private.example" not in page.locator("body").inner_text()
        assert page.locator(".workspace-artifact-link").first.get_attribute("href") == "https://example.com/search?q=artifacts"
        assert page.locator(".workspace-artifact-link").first.get_attribute("rel") == "noopener noreferrer"
        assert page.locator(".workspace-artifact-link").first.get_attribute("target") == "_blank"
        page.locator(".workspace-artifact-item").first.click()
        assert page.evaluate("() => window.opened") == ["src/app.py"]
        with page.expect_popup() as popup_info:
            page.locator(".workspace-artifact-link").first.click()
        popup = popup_info.value
        assert popup.url == "https://example.com/search?q=artifacts"
        popup.close()
        for width in (1280, 768, 400):
            page.set_viewport_size({"width": width, "height": 900})
            violations = page.evaluate(
                """(lintSource) => {
                    eval(lintSource);
                    return collectRenderViolations('.issue6593-panel', {
                        checks: ['overlap', 'clip', 'container-escape', 'raw-string', 'a11y']
                    });
                }""",
                RENDER_LINT,
            )
            assert violations == [], violations
            assert_layout_sane(page, ".issue6593-panel", checks=["overlap", "clip", "container-escape", "raw-string"])
        page.set_viewport_size({"width": 480, "height": 320})
        violations = page.evaluate(
            """(lintSource) => {
                eval(lintSource);
                return collectRenderViolations('.issue6593-panel', {
                    checks: ['overlap', 'clip', 'container-escape', 'raw-string', 'a11y']
                });
            }""",
            RENDER_LINT,
        )
        assert violations == [], violations
        assert_layout_sane(page, ".issue6593-panel", checks=["overlap", "clip", "container-escape", "raw-string"])
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
