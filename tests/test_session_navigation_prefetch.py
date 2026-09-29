"""Regression coverage for fresh, bounded session-navigation loads."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def _function_block(source: str, name: str) -> str:
    markers = (f"async function {name}(", f"function {name}(")
    starts = [source.find(marker) for marker in markers]
    start = min(index for index in starts if index >= 0)
    brace = source.index("{", start)
    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for index in range(brace, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            continue
        if char in ("'", '"', "`"):
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def test_hover_and_focus_do_not_issue_or_cache_session_responses():
    """A hover burst must remain a pure UI interaction with no response cache."""
    source = SESSIONS_JS.read_text(encoding="utf-8")
    render_one = _function_block(source, "_renderOneSession")

    assert "_sessionNavCache" not in source
    assert "_prefetchSessionForNav" not in source
    assert "_apiSessionNav" not in source
    assert "_sessionNavRowIsStreaming" not in source
    assert "onpointerenter" not in render_one
    assert "onfocusin" not in render_one


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_becoming_streaming_between_hover_and_click_uses_fresh_click_requests():
    """The real click request path must not consume an earlier idle snapshot."""
    source = SESSIONS_JS.read_text(encoding="utf-8")
    start_requests = _function_block(source, "_startFreshSessionNavigationRequests")
    driver = f"""
const _INITIAL_MSG_LIMIT = 30;
const _INITIAL_TAIL_MSG_LIMIT = _INITIAL_MSG_LIMIT;
const calls = [];
function api(url, opts) {{
  calls.push({{url, opts: opts || null}});
  if(url.includes('messages=0')) {{
    return Promise.resolve({{session: {{session_id:'target', active_stream_id:'stream-new'}}}});
  }}
  return Promise.resolve({{session: {{session_id:'target', messages:[{{role:'assistant', content:'fresh-live-tail'}}]}}}});
}}
{start_requests}
(async () => {{
  const row = {{}};
  for(let i=0;i<50;i++) {{
    if(typeof row.onpointerenter === 'function') row.onpointerenter({{pointerType:'mouse'}});
    if(typeof row.onfocusin === 'function') row.onfocusin();
  }}
  const callsBeforeClick = calls.length;
  const requests = _startFreshSessionNavigationRequests('target');
  const metadata = await requests.metadata;
  const messages = await requests.messages;
  console.log(JSON.stringify({{callsBeforeClick, calls, metadata, messages}}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    proc = subprocess.run(
        [NODE, "-e", driver],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    assert observed["callsBeforeClick"] == 0
    assert observed["calls"] == [
        {
            "url": "/api/session?session_id=target&messages=0&resolve_model=0",
            "opts": None,
        },
        {
            "url": "/api/session?session_id=target&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1",
            "opts": {"timeoutMs": 120000},
        },
    ]
    assert observed["metadata"]["session"]["active_stream_id"] == "stream-new"
    assert observed["messages"]["session"]["messages"][0]["content"] == "fresh-live-tail"


def test_load_session_starts_both_requests_then_assigns_metadata_before_transcript():
    source = SESSIONS_JS.read_text(encoding="utf-8")
    load_session = _function_block(source, "loadSession")

    request_start = load_session.index("const _freshNavigationRequests =")
    metadata_await = load_session.index("data = await _metadataRequest")
    metadata_assignment = load_session.index("S.session=data.session")
    transcript_await = load_session.index("messageRequest:_freshMessagesRequest")

    assert request_start < metadata_await < metadata_assignment < transcript_await
    assert load_session.count("messageRequest:_freshMessagesRequest") == 2


def test_initial_tail_and_older_pagination_remain_thirty_messages():
    source = SESSIONS_JS.read_text(encoding="utf-8")
    load_older = _function_block(source, "_loadOlderMessages")

    assert "const _INITIAL_MSG_LIMIT = 30;" in source
    assert "const _INITIAL_TAIL_MSG_LIMIT = _INITIAL_MSG_LIMIT;" in source
    assert "msg_limit=${_INITIAL_MSG_LIMIT}" in load_older


def test_session_visit_model_freshness_default_remains_five_minutes():
    from api import config

    assert config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS == 300.0
