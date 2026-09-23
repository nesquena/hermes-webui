"""#3058 slice 3 rendered-layout proof for the delivered-steer row.

Drives the real `_anchorSceneNodeForRow` under Playwright against the real
`static/style.css`, painted into the real container chain the shipped renderer
uses — `.assistant-turn > .assistant-turn-blocks` for the transparent and
final-answer-only paths, and `.tool-worklog-group.live-worklog >
.tool-worklog-list` for the Compact Worklog rail. Both are flex columns, so
`align-self` and `max-width` from `.msg-row[data-role="user"]` are live there;
painting into a plain `div` instead would put the whole cascade outside the
proof, which is what an earlier version of this file did.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(os.environ.get("ISSUE3058_REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "tests"))
from _layout_helpers import assert_layout_sane  # noqa: E402

UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
HEADLESS = os.environ.get("ISSUE3058_HEADLESS", "1") != "0"
SCREENSHOT_PATH = os.environ.get("ISSUE3058_SCREENSHOT_PATH")

CHECKS = ["overlap", "clip", "container-escape", "degenerate"]
WIDTHS = [(1440, 900), (760, 900), (390, 844)]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

SHORT_STEER = "prefer the reconnect path"
LONG_STEER = (
    "please stop rewriting the parser and instead reconstruct the reconnect path so the "
    "stream-end recovery lease is never consulted from the command layer, then re-run the "
    "focused suite and report only what you actually observed"
)
UNBROKEN_STEER = "https://example.invalid/" + ("averyveryverylongunbreakablesegment" * 4)

ROWS = [
    {
        "role": "prose",
        "source_event_type": "token",
        "status": "completed",
        "row_id": "run-3058:1",
        "local_id": "live-prose:stream-3058:1",
        "text": "Starting on the parser rewrite now.",
        "payload": {},
    },
    {
        "role": "user",
        "kind": "control_boundary",
        "source_event_type": "steer_delivered",
        "status": "delivered",
        "row_id": "run-3058:steer-1",
        "local_id": "steer:stream-3058:1",
        "text": SHORT_STEER,
        "payload": {"delivered": True, "origin": "webui", "files": []},
    },
    {
        "role": "user",
        "kind": "control_boundary",
        "source_event_type": "steer_delivered",
        "status": "delivered",
        "row_id": "run-3058:steer-2",
        "local_id": "steer:stream-3058:2",
        "text": LONG_STEER,
        "payload": {
            "delivered": True,
            "origin": "webui",
            "files": ["docs/rfcs/webui-pending-intent-controls.md", "logs/stream-end-recovery.log"],
        },
    },
    {
        "role": "user",
        "kind": "control_boundary",
        "source_event_type": "steer_delivered",
        "status": "delivered",
        "row_id": "run-3058:steer-3",
        "local_id": "steer:stream-3058:3",
        "text": UNBROKEN_STEER,
        "payload": {"delivered": True, "origin": "webui", "files": []},
    },
    {
        "role": "prose",
        "source_event_type": "token",
        "status": "completed",
        "row_id": "run-3058:2",
        "local_id": "live-prose:stream-3058:2",
        "text": "Understood, switching to the reconnect path.",
        "payload": {},
    },
]


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


# The turn markup `_createAssistantTurn` builds, and the worklog rail
# `ensureActivityGroup` builds, reproduced with their real class names so the
# cascade the shipped renderer meets is the cascade under measurement.
def _turn(turn_id: str, live: bool, worklog: bool, mode: str = "") -> str:
    inner = (
        '<div class="tool-worklog-group live-worklog" data-tool-worklog-group="1" '
        'data-anchor-scene-owner="1"><div class="tool-worklog-list"></div></div>'
        if worklog
        else ""
    )
    live_attr = ' data-anchor-scene-live-owner="1"' if live else ""
    mode_attr = f' data-anchor-scene-live-mode="{mode}"' if mode else ""
    return (
        f'<div class="msg-row assistant-turn" id="{turn_id}" data-role="assistant"{live_attr}{mode_attr}>'
        '<div class="msg-role assistant"><div class="role-icon assistant">H</div>'
        '<span class="msg-role-name">Hermes</span></div>'
        f'<div class="assistant-turn-blocks">{inner}</div>'
        "</div>"
    )


def _harness() -> str:
    builder = _function(UI_JS, "_anchorSceneNodeForRow")
    css = STYLE_CSS.replace("</style>", "")
    turns = "".join(
        [
            _turn("liveWorklog", live=True, worklog=True),
            _turn("settledWorklog", live=False, worklog=True),
            _turn("transparentStream", live=True, worklog=False),
            _turn("finalAnswerOnly", live=True, worklog=False, mode="hide_all_activity"),
        ]
    )
    return f"""
        <style>{css}</style>
        <style>
          body {{ margin:0; background:#141327; color:#efe7dd; font-family:Inter, system-ui, sans-serif; }}
          .messages {{ padding:24px; box-sizing:border-box; }}
          #msgInner {{ display:flex; flex-direction:column; gap:24px; }}
        </style>
        <div class="messages"><div id="msgInner">{turns}</div></div>
        <script>
          const ROWS = {json.dumps(ROWS)};
          function esc(v) {{
            return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
              .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
          }}
          function renderMd(v) {{ return esc(v); }}
          function t(key) {{ return key === 'steer_delivered' ? 'Steer delivered' : key; }}
          function _thinkingActivityNode() {{ return document.createElement('div'); }}
          function buildToolCard() {{ return document.createElement('div'); }}
          function _activityStatusNode() {{ return document.createElement('div'); }}
          {builder}
          function paint(turnId, rows, settled) {{
            const turn = document.getElementById(turnId);
            const host = turn.querySelector('.tool-worklog-list')
              || turn.querySelector('.assistant-turn-blocks');
            for (const row of rows) {{
              const node = _anchorSceneNodeForRow(row, {{settled: settled}});
              if (node) host.appendChild(node);
            }}
          }}
          const steerRows = ROWS.filter(r => r.source_event_type === 'steer_delivered');
          paint('liveWorklog', ROWS, false);
          paint('settledWorklog', ROWS, true);
          paint('transparentStream', ROWS, false);
          // Final-answer-only paints the user's own input and nothing else.
          paint('finalAnswerOnly', steerRows, false);
        </script>
    """


# Reported per steer row: what the cascade actually resolved to, and how wide the
# row came out relative to the column it sits in. On base — where the only rule is
# a lone `.steer-delivered-row` class, outranked by `.msg-row[data-role="user"]` —
# these come back `flex-end` / `60%` / roughly a quarter of the column.
_MEASURE = """
nodes => nodes.map(n => {
  const cs = getComputedStyle(n);
  const parent = n.parentElement;
  const ps = getComputedStyle(parent);
  const inner = parent.clientWidth
    - parseFloat(ps.paddingLeft || 0) - parseFloat(ps.paddingRight || 0);
  return {
    alignSelf: cs.alignSelf,
    maxWidth: cs.maxWidth,
    width: n.getBoundingClientRect().width,
    inner: inner,
    parentDisplay: ps.display,
    parentFlexDirection: ps.flexDirection,
  };
})
"""


def test_3058_delivered_steer_row_stays_sane_across_widths_and_modes():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the manual local browser proof for #3058")

    scopes = ["#liveWorklog", "#settledWorklog", "#transparentStream", "#finalAnswerOnly"]
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=HEADLESS, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - browser binary missing path
            pytest.skip(f"chromium is unavailable for the #3058 layout proof: {exc}")
        page = browser.new_page(viewport={"width": WIDTHS[0][0], "height": WIDTHS[0][1]})
        page.set_content(_harness())

        rows = page.locator('[data-steer-delivery="delivered"]')
        # 3 steer rows in each of the 3 full-scene turns, plus 3 in final-answer-only.
        assert rows.count() == 12
        assert page.locator('[data-steer-delivery="delivered"][data-role="user"]').count() == 12
        assert (
            page.locator('#liveWorklog [data-steer-delivery="delivered"] .steer-delivered-label').first.text_content()
            == "Steer delivered"
        )
        # The Worklog rail really is the parent, not a bespoke test container.
        assert page.locator('#liveWorklog .tool-worklog-list > [data-steer-delivery="delivered"]').count() == 3
        assert page.locator('#transparentStream .assistant-turn-blocks > [data-steer-delivery="delivered"]').count() == 3
        # Ordering: assistant -> steer -> assistant inside one rail.
        order = page.locator("#liveWorklog .tool-worklog-list > *").evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('data-anchor-row-role'))"
        )
        assert order == ["prose", "user", "user", "user", "prose"]
        # Negative space: the row carries no interactive affordance.
        assert page.locator('[data-steer-delivery="delivered"] button, [data-steer-delivery="delivered"] a').count() == 0
        # Final-answer-only keeps the user's input visible.
        assert page.locator('#finalAnswerOnly [data-steer-delivery="delivered"]').count() == 3
        assert page.locator("#finalAnswerOnly .assistant-segment").count() == 0
        # ...and does not announce an assistant response that has not happened.
        assert page.locator("#finalAnswerOnly > .msg-role.assistant").is_visible() is False
        # The header is back the moment the turn holds anything else.
        assert page.locator("#liveWorklog > .msg-role.assistant").is_visible() is True

        for width, height in WIDTHS:
            page.set_viewport_size({"width": width, "height": height})
            for scope in scopes:
                assert_layout_sane(page, scope, checks=CHECKS)
            measured = rows.evaluate_all(_MEASURE)
            assert len(measured) == 12
            for index, m in enumerate(measured):
                # Guard the guard: if the parent stopped being a flex column, the
                # align-self assertion below would pass vacuously.
                assert m["parentDisplay"] == "flex", f"row {index} parent is not a flex container"
                assert m["parentFlexDirection"] == "column"
                assert m["alignSelf"] == "stretch", (
                    f"row {index} at width {width} resolved align-self {m['alignSelf']!r}; "
                    "the classic right-aligned user layout is winning"
                )
                assert m["maxWidth"] == "100%", (
                    f"row {index} at width {width} resolved max-width {m['maxWidth']!r}"
                )
                assert m["width"] >= m["inner"] - 1, (
                    f"row {index} at width {width} is {m['width']}px inside a {m['inner']}px column"
                )
            # No horizontal escape from any steer row, at any width.
            overflowing = rows.evaluate_all(
                "nodes => nodes.filter(n => n.scrollWidth > n.clientWidth + 1).length"
            )
            assert overflowing == 0, f"steer row overflows horizontally at width {width}"
            for label_class in ("steer-delivered-label", "msg-body", "steer-delivered-files"):
                clipped = page.locator(f'[data-steer-delivery="delivered"] .{label_class}').evaluate_all(
                    "nodes => nodes.filter(n => n.scrollWidth > n.clientWidth + 1).length"
                )
                assert clipped == 0, f".{label_class} clips at width {width}"

        if SCREENSHOT_PATH:  # pragma: no cover - capture-only path
            page.set_viewport_size({"width": WIDTHS[0][0], "height": WIDTHS[0][1]})
            page.screenshot(path=SCREENSHOT_PATH, full_page=True)
        browser.close()


def test_3058_served_branch_render_keeps_the_delivered_row_in_the_real_renderer():
    """The layout harness must also cross the served application boundary."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright is unavailable for the served #3058 proof")

    with tempfile.TemporaryDirectory(prefix="hermes-3058-served-") as temp:
        state = Path(temp)
        port = _free_port()
        env = os.environ.copy()
        for key in list(env):
            if key.endswith("_API_KEY"):
                env.pop(key, None)
        env.update(
            {
                "BROWSER": "echo",
                "HERMES_WEBUI_PORT": str(port),
                "HERMES_WEBUI_HOST": "127.0.0.1",
                "HERMES_WEBUI_STATE_DIR": str(state / "webui-state"),
                "HERMES_HOME": str(state / "hermes-home"),
                "HERMES_BASE_HOME": str(state / "hermes-home"),
                "HERMES_WEBUI_SKIP_ONBOARDING": "1",
                "HERMES_WEBUI_AGENT_DIR": str(state / "no-agent"),
            }
        )
        log_path = state / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "server.py")],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            base_url = f"http://127.0.0.1:{port}"
            try:
                deadline = time.time() + 30
                while time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                            if response.status == 200:
                                break
                    except (urllib.error.URLError, OSError):
                        time.sleep(0.25)
                else:
                    pytest.fail("served #3058 proof server did not become healthy")
                if proc.poll() is not None:
                    pytest.fail(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
                    )
                    page = browser.new_page(viewport={"width": 1024, "height": 600})
                    page.goto(base_url + "/", wait_until="domcontentloaded")
                    page.wait_for_selector("#msgInner", timeout=10000)
                    result = page.evaluate(
                        """() => {
                          const scene = {
                            version: 'activity_scene_v1', mode: 'compact_worklog',
                            identity: {stream_id: 'stream-3058', run_id: 'run-3058'},
                            lifecycle: {status: 'completed', terminal_state: 'completed'},
                            final_answer: 'answer', final_message_ref: 'assistant-3058',
                            activity_rows: [
                              {role: 'prose', kind: 'process_prose', source_event_type: 'token', status: 'completed',
                               row_id: 'prose-3058', local_id: 'prose-3058', text: 'before', payload: {}},
                              {role: 'user', kind: 'control_boundary', source_event_type: 'steer_delivered', status: 'delivered',
                               row_id: 'steer-3058', local_id: 'steer:stream-3058:1', text: 'keep this steer',
                               payload: {delivered: true, origin: 'webui', files: []}},
                            ], artifacts: [], side_effects: []
                          };
                          S.session = {session_id: 'served-3058', profile: 'default', title: 'served proof'};
                          S.activeProfile = 'default'; S.activeProfileIsDefault = true;
                          S.busy = false; S.toolCalls = [];
                          S.messages = [
                            {role: 'user', content: 'request', id: 'user-3058', turn_id: 'turn-3058'},
                            {role: 'assistant', content: 'answer', id: 'assistant-3058', _anchor_stream_id: 'stream-3058',
                             _anchor_activity_scene: scene}
                          ];
                          renderMessages();
                          const assistant = document.querySelector('.assistant-turn');
                          const direct = assistant && typeof _renderSettledAnchorSceneForMessage === 'function'
                            ? _renderSettledAnchorSceneForMessage(S.messages[1], assistant, 1) : false;
                          const group = assistant && assistant.querySelector('[data-anchor-settled-scene-owner="1"]');
                          const summary = group && group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
                          if (group && summary && group.classList.contains('tool-call-group-collapsed')) {
                            _toggleActivityGroup(summary);
                          }
                          const row = document.querySelector('[data-steer-delivery="delivered"]');
                          return {count: document.querySelectorAll('[data-steer-delivery="delivered"]').length,
                            role: row && row.getAttribute('data-role'), text: row && row.textContent,
                            scene: !!S.messages[1]._anchor_activity_scene, direct};
                        }"""
                    )
                    assert result["count"] == 1, json.dumps(result)
                    assert result["role"] == "user"
                    assert "keep this steer" in result["text"]
                    browser.close()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
