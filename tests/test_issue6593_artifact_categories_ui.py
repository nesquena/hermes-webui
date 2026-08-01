"""Rendered workspace artifact category and action coverage for #6593."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_helpers import assert_layout_sane

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "issue6593_artifact_categories.json"
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


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
                return source[start : index + 1]
    raise AssertionError(f"could not extract {name}")


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
    function_names = (
        "_normalizeArtifactPath",
        "_normalizeArtifactUrl",
        "_normalizeArtifactTarget",
        "_normalizeArtifactFilePath",
        "_normalizeArtifactMediaRef",
        "_normalizeArtifactWorkspacePath",
        "_normalizeArtifactOpenPath",
        "_parseArtifactJson",
        "_artifactToolId",
        "_artifactToolName",
        "_artifactToolArgs",
        "_artifactResultValues",
        "_artifactTextFromValue",
        "_artifactPartialFieldValues",
        "_artifactCandidatesFromText",
        "_artifactCandidatesFromToolCall",
        "_artifactToolResultPayload",
        "_artifactToolResultsById",
        "collectSessionArtifacts",
        "renderSessionArtifacts",
    )
    functions = "\n".join(_function(WORKSPACE_JS, name) for name in function_names)
    functions += "\nasync " + _function(WORKSPACE_JS, "openArtifactPath")
    return consts + "\n" + functions


def _actual_rightpanel_html():
    start = INDEX_HTML.index('<aside class="rightpanel">')
    end = INDEX_HTML.index("</aside>", start) + len("</aside>")
    return (
        INDEX_HTML[start:end]
        .replace(
            '<aside class="rightpanel">',
            '<aside class="rightpanel mobile-open" data-active-tab="artifacts">',
        )
        .replace('id="workspaceArtifacts" hidden', 'id="workspaceArtifacts"')
    )


def _render_harness(payload, workspace="/workspace"):
    return f"""
      <style>{STYLE_CSS} html, body {{ height: 100%; margin: 0; overflow: hidden; }} .rightpanel {{ height: 100vh; box-sizing: border-box; }}</style>
      <script>{I18N_JS}</script>
      {_actual_rightpanel_html()}
      <script>
        const S = {json.dumps({
            "toolCalls": payload["tool_calls"],
            "messages": payload["messages"],
            "session": {"workspace": workspace, "session_id": "ui-proof"},
        })};
        const $ = id => document.getElementById(id);
        const esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const switchWorkspacePanelTab = () => {{}};
        const _workspacePathExists = async () => true;
        const openFile = path => window.opened = [...(window.opened || []), path];
        const setStatus = () => {{}};
        const _workspaceArtifactDisclosureState = Object.create(null);
        {_artifact_bundle()}
        renderSessionArtifacts();
      </script>
    """


def test_grouped_sections_use_real_workspace_panel_and_safe_actions():
    playwright = pytest.importorskip("playwright.sync_api")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.set_content(_render_harness(payload))
        assert page.locator(".rightpanel").count() == 1
        assert page.locator(".rightpanel").evaluate("node => getComputedStyle(node).containerName") == "rightpanel"
        assert page.locator(".workspace-artifact-group").evaluate_all(
            "nodes => nodes.map(node => node.dataset.artifactCategory)"
        ) == ["modified", "read", "web", "media"]
        assert page.locator(".workspace-artifact-item").count() == 6
        assert page.locator(".workspace-artifact-link").count() == 5
        assert page.locator("a[href^='javascript:']").count() == 0
        assert "unsupported.png" not in page.locator("body").inner_text()
        page.locator(".workspace-artifact-item").first.click()
        assert page.evaluate("() => window.opened") == ["src/app.py"]
        media_link = page.locator(".workspace-artifact-link").filter(has_text="shot.png")
        assert media_link.get_attribute("href").startswith("api/media?path=%2Ftmp%2Fshot.png&")
        assert "session_id=ui-proof" in media_link.get_attribute("href")
        with page.expect_popup() as popup_info:
            page.locator(".workspace-artifact-link").first.click()
        popup = popup_info.value
        assert popup.url == "https://example.com/docs"
        popup.close()

        for viewport in ((1280, 900), (768, 900), (400, 900), (480, 320)):
            page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
            page.locator(".rightpanel").evaluate(
                "(node, narrow) => { node.style.setProperty('--mobile-rightpanel-width', narrow ? '210px' : ''); }",
                viewport == (480, 320),
            )
            assert_layout_sane(
                page,
                "#workspaceArtifacts",
                checks=["overlap", "clip", "container-escape", "raw-string", "a11y"],
            )
            if viewport == (480, 320):
                assert page.locator(".workspace-artifact-group > summary").first.evaluate(
                    "node => getComputedStyle(node).fontSize"
                ) == "10px"
        page.locator(".workspace-artifact-item").first.focus()
        assert page.evaluate("() => document.activeElement.classList.contains('workspace-artifact-item')")
        page.locator(".workspace-artifact-group > summary").first.focus()
        assert page.evaluate("() => document.activeElement.tagName === 'SUMMARY'")
        browser.close()


def test_category_labels_load_through_real_ru_and_de_locales():
    playwright = pytest.importorskip("playwright.sync_api")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    keys = [
        "workspace_artifact_category_modified",
        "workspace_artifact_category_read",
        "workspace_artifact_category_web",
        "workspace_artifact_category_media",
    ]
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 480, "height": 320})
        page.set_content(_render_harness(payload))
        for locale, lang in (("ru", "ru-RU"), ("de", "de-DE")):
            page.evaluate("locale => { setLocale(locale); applyLocaleToDOM(); }", locale)
            assert page.locator("html").get_attribute("lang") == lang
            expected = [page.evaluate("key => t(key)", key) for key in keys]
            actual = page.locator(".workspace-artifact-group-title > span:first-child").all_text_contents()
            assert actual == expected
            assert all(value not in keys for value in actual)
        browser.close()


def test_windows_file_media_uses_relative_drive_path_on_existing_media_route():
    playwright = pytest.importorskip("playwright.sync_api")
    payload = {
        "tool_calls": [],
        "messages": [{"role": "assistant", "content": "MEDIA:file:///C:/work/chart.png"}],
    }
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 480, "height": 320})
        page.set_content(_render_harness(payload, workspace=r"C:\work"))
        media_link = page.locator(".workspace-artifact-link").first
        assert media_link.get_attribute("href").startswith("api/media?path=C%3A%2Fwork%2Fchart.png&")
        assert "session_id=ui-proof" in media_link.get_attribute("href")
        browser.close()


def test_windows_workspace_prefix_is_removed_before_file_action():
    playwright = pytest.importorskip("playwright.sync_api")
    payload = {
        "tool_calls": [{"name": "write_file", "args": {"path": r"C:\work\src\app.py"}}],
        "messages": [],
    }
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 480, "height": 320})
        page.set_content(_render_harness(payload, workspace=r"C:\work"))
        page.locator(".workspace-artifact-item").first.click()
        assert page.evaluate("() => window.opened") == ["src/app.py"]
        browser.close()


def test_open_artifact_path_keeps_shared_deep_link_normalization():
    playwright = pytest.importorskip("playwright.sync_api")
    payload = {"tool_calls": [], "messages": []}
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 480, "height": 320})
        page.set_content(_render_harness(payload, workspace=r"C:\work"))
        for path, expected in (
            ("dist/report.html", "dist/report.html"),
            ("node_modules/pkg/README.md", "node_modules/pkg/README.md"),
            ("../shared/lib.py", "../shared/lib.py"),
        ):
            page.evaluate("async path => { window.opened = []; await openArtifactPath(path); }", path)
            assert page.evaluate("() => window.opened") == [expected]
        browser.close()


def test_category_disclosure_state_survives_artifact_rerender():
    playwright = pytest.importorskip("playwright.sync_api")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 480, "height": 320})
        page.set_content(_render_harness(payload))
        first = page.locator(".workspace-artifact-group").first
        assert first.get_attribute("open") == ""
        first.locator("summary").click()
        assert first.get_attribute("open") is None
        page.wait_for_timeout(10)
        page.evaluate("() => renderSessionArtifacts()")
        assert page.locator(".workspace-artifact-group").first.get_attribute("open") is None
        browser.close()
