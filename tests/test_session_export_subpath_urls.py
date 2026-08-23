"""Regression coverage for browser session-export URLs under subpath mounts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
BOOT_JS = ROOT / "static" / "boot.js"
INDEX_HTML = ROOT / "static" / "index.html"
NODE = shutil.which("node")


def _boot_source() -> str:
    return BOOT_JS.read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    """Extract one product function for a small, browser-free execution harness."""
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name} must be defined in product source"
    brace = source.find("{", start)
    assert brace >= 0
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated {name}")


def _onclick_source(source: str, element_id: str) -> str:
    marker = f"$('{element_id}').onclick="
    start = source.find(marker)
    assert start >= 0, f"{element_id} must have an export click handler"
    arrow = source.find("()=>", start)
    assert arrow >= 0
    expression_start = arrow + len("()=>")
    if source[expression_start] != "{":
        end = source.find(";", expression_start)
        assert end >= 0
        return source[arrow:end]
    brace = expression_start
    assert brace >= 0
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[arrow : index + 1]
    raise AssertionError(f"unterminated {element_id} click handler")


def _run_export_actions(base_uri: str) -> list[dict[str, str]]:
    source = _boot_source()
    helper = _function_source(source, "_buildSessionExportUrl")
    html_export = _function_source(source, "exportSessionHTML")
    json_handler = _onclick_source(source, "btnExportJSON")
    html_handler = _onclick_source(source, "btnExportHTML")
    program = json.dumps(
        f"{helper}\n{html_export}\nreturn [{json_handler}, {html_handler}];"
    )
    script = f"""
const document = {{
  baseURI: {json.dumps(base_uri)},
  documentElement: {{classList: {{contains: () => true}}}},
  createElement: () => ({{click() {{ clicked.push(this); }}}}),
}};
const location = {{href: {json.dumps(base_uri)}}};
const clicked = [];
const elements = {{btnExportJSON: {{}}, btnExportHTML: {{}}}};
const $ = (id) => elements[id];
const S = {{session: {{session_id: 'session /?&=✓'}}}};
const getComputedStyle = () => ({{getPropertyValue: () => ''}});
const handlers = new Function(
  'document', 'location', 'URL', '$', 'S', 'getComputedStyle', 'btoa', 'unescape',
  {program},
)(document, location, URL, $, S, getComputedStyle, btoa, unescape);
handlers[0]();
handlers[1]();
const urls = clicked.map((anchor) => {{
  const url = new URL(anchor.href);
  return {{
  pathname: url.pathname,
  session_id: url.searchParams.get('session_id'),
  format: url.searchParams.get('format') || '',
  theme: url.searchParams.get('theme') || '',
  palette: Boolean(url.searchParams.get('palette')),
  }};
}});
console.log(JSON.stringify(urls));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is required for browser URL checks")
def test_export_builder_keeps_root_and_subpath_mounts() -> None:
    for base_uri, expected_path in (
        ("https://example.test/", "/api/session/export"),
        ("https://example.test/hermes-webui/", "/hermes-webui/api/session/export"),
    ):
        json_export, html_export = _run_export_actions(base_uri)
        assert json_export == {
            "pathname": expected_path,
            "session_id": "session /?&=✓",
            "format": "",
            "theme": "",
            "palette": False,
        }
        assert html_export == {
            "pathname": expected_path,
            "session_id": "session /?&=✓",
            "format": "html",
            "theme": "dark",
            "palette": True,
        }


@pytest.mark.skipif(NODE is None, reason="node is required for browser URL checks")
def test_export_actions_keep_the_app_base_from_a_session_deep_link() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(
        r"<script>(\(function\(\)\{var path=location\.pathname.*?\}\)\(\))</script>",
        source,
    )
    assert match, "index.html must establish its document base before app scripts load"
    script = f"""
let emitted = '';
const location = {{origin: 'https://example.test', pathname: '/hermes-webui/session/deep-link'}};
const document = {{write(value) {{ emitted += value; }}}};
{match.group(1)};
console.log(emitted);
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    base_match = re.search(r'href="([^"]+)"', result.stdout)
    assert base_match

    json_export, html_export = _run_export_actions(base_match.group(1))
    assert json_export["pathname"] == "/hermes-webui/api/session/export"
    assert html_export["pathname"] == "/hermes-webui/api/session/export"
