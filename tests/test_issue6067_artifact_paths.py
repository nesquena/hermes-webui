import os
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(os.environ.get("ISSUE6067_REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "tests"))
from _layout_helpers import assert_layout_sane


WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
EXPECT_VISIBLE = os.environ.get("ISSUE6067_EXPECT_VISIBLE", "1") != "0"
HEADLESS = os.environ.get("ISSUE6067_HEADLESS", "1") != "0"
SCREENSHOT_PATH = os.environ.get("ISSUE6067_SCREENSHOT_PATH")

ARTIFACT_CASES = [
    {"path": "users.py", "source": "patch", "display": "users.py", "name": "users.py", "head": "", "tail": ""},
    {"path": "src/api/routes/users.py", "source": "tool", "display": "src/api/routes/users.py", "name": "users.py", "head": "src/api/", "tail": "routes/"},
    {"path": "dir/foo.py", "source": "single", "display": "dir/foo.py", "name": "foo.py", "head": "", "tail": "dir/"},
    {"path": "src/project/app/api/routes/handlers/users.py", "source": "", "display": "src/project/app/api/routes/handlers/users.py", "name": "users.py", "head": "src/project/app/api/routes/", "tail": "handlers/"},
    {"path": "/file.txt", "source": "root", "display": "/file.txt", "name": "file.txt", "head": "/", "tail": ""},
    {"path": "dir/", "source": "trail", "display": "dir/", "name": "", "head": "", "tail": "dir/"},
    {"path": "", "source": "empty", "display": "", "name": "", "head": "", "tail": ""},
    {"path": "src//users.py", "source": "gap", "display": "src//users.py", "name": "users.py", "head": "src/", "tail": "/"},
    {"path": "src/lastIndexOf/users.py", "source": "literal", "display": "src/lastIndexOf/users.py", "name": "users.py", "head": "src/", "tail": "lastIndexOf/"},
    {"path": "/workspace/über/<unsafe>/routes/quote&.py", "source": "<source>&", "display": "über/<unsafe>/routes/quote&.py", "name": "quote&.py", "head": "über/<unsafe>/", "tail": "routes/"},
    {"path": "2026-07-20-quarterly-revenue-analysis-with-regional-breakdown-and-confidence-intervals/summary.json", "source": "", "display": "2026-07-20-quarterly-revenue-analysis-with-regional-breakdown-and-confidence-intervals/summary.json", "name": "summary.json", "head": "", "tail": "2026-07-20-quarterly-revenue-analysis-with-regional-breakdown-and-confidence-intervals/"},
]


def _function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError(f"could not extract {name}")


def _locale_blocks():
    matches = list(re.finditer(r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", I18N_JS, re.MULTILINE))
    end = I18N_JS.index("\n};", matches[-1].start())
    return {
        match.group(1).strip("'"): I18N_JS[match.start() : (matches[index + 1].start() if index + 1 < len(matches) else end)]
        for index, match in enumerate(matches)
    }


def _filename_visible(locator) -> bool:
    return locator.evaluate(
        """node => {
            const range = document.createRange();
            range.selectNodeContents(node);
            const text = range.getBoundingClientRect();
            const box = node.getBoundingClientRect();
            return text.left >= box.left - 1 && text.right <= box.right + 1;
        }"""
    )


def _path_suffix_visible(locator, suffix: str) -> bool:
    return locator.evaluate(
        """(node, suffix) => {
            const textNode = node.firstChild;
            if(!textNode || textNode.nodeType !== Node.TEXT_NODE) return false;
            const value = textNode.textContent || '';
            const start = value.lastIndexOf(suffix);
            if(start < 0) return false;
            const range = document.createRange();
            range.setStart(textNode, start);
            range.setEnd(textNode, start + suffix.length);
            const text = range.getBoundingClientRect();
            const box = node.getBoundingClientRect();
            return text.left >= box.left - 1 && text.right <= box.right + 1;
        }""",
        suffix,
    )


def _directory_text_gap(locator) -> float:
    return locator.evaluate(
        """node => {
            const head = node.querySelector('.workspace-artifact-directory-head');
            const tail = node.querySelector('.workspace-artifact-directory-tail');
            if(!head || !tail) return Number.POSITIVE_INFINITY;
            const range = document.createRange();
            range.selectNodeContents(head);
            const text = range.getBoundingClientRect();
            const tailBox = tail.getBoundingClientRect();
            return tailBox.left - text.right;
        }"""
    )


def test_issue6067_session_source_key_is_english_fallback_owned():
    locale_blocks = _locale_blocks()
    assert re.search(r"\bworkspace_artifact_source_session:\s*'session'", locale_blocks["en"])
    for locale, block in locale_blocks.items():
        if locale == "en":
            continue
        assert not re.search(r"\bworkspace_artifact_source_session:\s*'", block), (
            f"workspace_artifact_source_session must be absent from non-English locale {locale!r}"
        )
    assert "_locale[key] ?? LOCALES.en[key]" in I18N_JS


def test_issue6067_artifact_filenames_remain_visible_across_artifact_widths():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run manual local browser proof for issue #6067")

    items = [{"path": case["path"], "source": case["source"]} for case in ARTIFACT_CASES]
    renderer = _function(WORKSPACE_JS, "renderSessionArtifacts")
    css = STYLE_CSS.replace("</style>", "")
    harness = f"""
        <style>{css}</style>
        <style>
          body {{
            margin: 0;
            background: #141327;
            color: #efe7dd;
            font-family: Inter, system-ui, sans-serif;
          }}
          .issue6067-proof-shell {{
            min-height: 100vh;
            display: flex;
            align-items: stretch;
            justify-content: space-between;
          }}
          .issue6067-proof-main {{
            flex: 1 1 auto;
            padding: 56px 48px;
            font-size: 24px;
          }}
          .issue6067-proof-panel {{
            width: 360px;
            min-width: 360px;
            border-left: 1px solid rgba(255,255,255,.08);
            background: #17162b;
          }}
          .issue6067-proof-tabs {{
            display: flex;
            padding: 18px 16px 0;
            gap: 12px;
          }}
          .issue6067-proof-tab {{
            flex: 1 1 0;
            padding: 12px 16px;
            border: 1px solid rgba(255,255,255,.12);
            border-radius: 14px;
            text-align: center;
            color: rgba(239,231,221,.72);
          }}
          .issue6067-proof-tab.active {{
            color: #efe7dd;
            border-color: rgba(255,255,255,.18);
          }}
          #workspaceArtifacts {{
            width: 100%;
            box-sizing: border-box;
          }}
        </style>
        <div class="issue6067-proof-shell">
          <div class="issue6067-proof-main">Artifacts proof fixture for issue #6067</div>
          <div class="issue6067-proof-panel rightpanel" data-active-tab="artifacts">
            <div class="issue6067-proof-tabs">
              <div class="issue6067-proof-tab">Files</div>
              <div id="workspaceArtifactsTab" class="issue6067-proof-tab active">Artifacts</div>
            </div>
            <div id="workspaceArtifacts" class="workspace-artifacts"></div>
          </div>
        </div>
        <span id="workspaceArtifactsCount"></span>
        <script>
          const S = {{session: {{workspace: '/workspace'}}, artifacts: {items!r}}};
          const opened = [];
          const $ = id => document.getElementById(id);
          const esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
          const t = key => key === 'workspace_artifact_source_session' ? 'session' : key;
          const collectSessionArtifacts = () => S.artifacts;
          const openArtifactPath = path => opened.push(path);
          {renderer}
          renderSessionArtifacts();
        </script>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.set_content(harness)
        if SCREENSHOT_PATH:
            page.screenshot(path=SCREENSHOT_PATH)
        page.set_viewport_size({"width": 1024, "height": 1400})
        buttons = page.locator(".workspace-artifact-item")
        assert buttons.count() == len(ARTIFACT_CASES)
        assert page.locator(".workspace-artifact-meta").all_text_contents() == [
            "patch", "tool", "single", "session", "root", "trail", "empty", "gap", "literal", "<source>&", "session"
        ]
        if EXPECT_VISIBLE:
            assert page.locator(".workspace-artifact-meta[data-i18n='workspace_artifact_source_session']").count() == 2
            for index, case in enumerate(ARTIFACT_CASES):
                button = buttons.nth(index)
                assert button.get_attribute("data-artifact-path") == case["path"]
                assert button.get_attribute("title") == case["display"]
                assert button.locator(".workspace-artifact-filename").inner_text() == case["name"]
                directory = button.locator(".workspace-artifact-directory")
                if case["head"] or case["tail"]:
                    assert directory.count() == 1
                    assert button.locator(".workspace-artifact-directory-head").inner_text() == case["head"]
                    assert button.locator(".workspace-artifact-directory-tail").inner_text() == case["tail"]
                else:
                    assert directory.count() == 0
            assert "<unsafe>" in buttons.nth(9).get_attribute("title")
        else:
            assert page.locator(".workspace-artifact-meta[data-i18n='workspace_artifact_source_session']").count() == 0
            assert page.locator(".workspace-artifact-path").all_text_contents() == [
                case["display"] for case in ARTIFACT_CASES
            ]

        for width in (180, 360, 519, 521):
            page.locator("#workspaceArtifacts").evaluate("(node, nextWidth) => node.style.width = nextWidth + 'px'", width)
            if EXPECT_VISIBLE:
                for index, case in enumerate(ARTIFACT_CASES):
                    if case["name"]:
                        assert _filename_visible(buttons.nth(index).locator(".workspace-artifact-filename"))
                assert buttons.nth(4).locator(".workspace-artifact-directory-head").inner_text() == "/"
                assert buttons.nth(7).locator(".workspace-artifact-directory-tail").inner_text() == "/"
                deep_head = buttons.nth(3).locator(".workspace-artifact-directory-head")
                assert deep_head.evaluate("node => node.scrollWidth >= node.clientWidth")
                if width in (360, 521):
                    short_directory = buttons.nth(1).locator(".workspace-artifact-directory")
                    short_head = buttons.nth(1).locator(".workspace-artifact-directory-head")
                    assert short_head.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
                    assert _directory_text_gap(short_directory) <= 2
            elif width == 180:
                clipped_path = buttons.nth(1).locator(".workspace-artifact-path")
                assert not _path_suffix_visible(clipped_path, "users.py")
            assert_layout_sane(page, "#workspaceArtifacts", checks=["overlap", "container-escape", "degenerate", "raw-string"])

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(".issue6067-proof-panel").evaluate(
            "node => { node.style.width = '300px'; node.classList.add('mobile-open'); }"
        )
        page.locator("#workspaceArtifacts").evaluate("node => node.style.removeProperty('width')")
        long_row = buttons.nth(10)
        long_tail = long_row.locator(".workspace-artifact-directory-tail")
        for selector in ("#workspaceArtifacts", ".workspace-artifact-item"):
            assert page.locator(selector).nth(10 if selector != "#workspaceArtifacts" else 0).evaluate(
                "node => node.scrollWidth === node.clientWidth"
            )
        assert long_row.locator(".workspace-artifact-filename").inner_text() == "summary.json"
        assert _filename_visible(long_row.locator(".workspace-artifact-filename"))
        assert long_tail.evaluate(
            "node => node.scrollWidth > node.clientWidth && getComputedStyle(node).textOverflow === 'ellipsis'"
        )
        assert_layout_sane(page, "#workspaceArtifacts", checks=["overlap", "clip", "container-escape", "degenerate", "raw-string"])

        for index in range(buttons.count()):
            buttons.nth(index).focus()
            assert page.evaluate("() => document.activeElement.classList.contains('workspace-artifact-item')")
            buttons.nth(index).click()
        assert page.evaluate("opened") == [case["path"] for case in ARTIFACT_CASES]
        browser.close()
