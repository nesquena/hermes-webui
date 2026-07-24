"""Regression tests for delegated child runtime state in the sidebar (#6468)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def test_sidebar_response_item_adds_bounded_runtime_fields(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "_session_attention_summary", lambda _sid: {"kind": "approval", "count": 1})
    row = routes._sidebar_session_response_item({
        "session_id": "child-1",
        "parent_session_id": "parent-1",
        "title": "Child session",
        "active_stream_id": "stream-1",
        "is_streaming": True,
        "ended_at": 123,
        "end_reason": "tool-limit",
        "transcript": "must stay out of bounded payload",
    })
    assert row["runtime_state"] == "waiting"
    assert row["runtime_reason"] == "approval"
    assert row["attention"]["kind"] == "approval"
    assert "transcript" not in row


def test_sidebar_response_item_leaves_parent_runtime_fields_empty(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "_session_attention_summary", lambda _sid: None)
    row = routes._sidebar_session_response_item({
        "session_id": "parent-1",
        "title": "Parent session",
        "is_streaming": True,
        "ended_at": 123,
        "end_reason": "tool-limit",
    })
    assert row["runtime_state"] is None
    assert row["runtime_reason"] is None


@pytest.mark.parametrize(
    ("session", "attention", "expected_state", "expected_reason"),
    [
        ({"active_stream_id": "run-1"}, None, "running", "live"),
        ({"ended_at": 42}, None, "completed", "completed"),
        ({"ended_at": 42, "end_reason": "tool-limit"}, None, "failed", "tool_limit"),
        ({"ended_at": 42, "end_reason": "cancelled"}, None, "cancelled", "cancelled"),
        ({"ended_at": 42, "end_reason": "approval"}, None, "unknown", "unknown"),
        ({"ended_at": 42, "end_reason": "clarify"}, None, "unknown", "unknown"),
        ({"ended_at": 42, "end_reason": "agent_close"}, None, "unknown", "unknown"),
        ({"ended_at": 42, "end_reason": "mystery_shutdown"}, None, "unknown", "unknown"),
        ({"ended_at": 42, "end_reason": "unknown"}, None, "unknown", "unknown"),
        ({}, None, "unknown", None),
        ({}, {"kind": "clarify", "count": 1}, "waiting", "clarify"),
    ],
)
def test_sidebar_runtime_normalization_variants(session, attention, expected_state, expected_reason):
    import api.routes as routes

    state, reason = routes._sidebar_child_runtime_fields(session, attention=attention)
    assert state == expected_state
    assert reason == expected_reason


def test_sidebar_allowlist_includes_runtime_fields():
    import api.routes as routes

    assert "runtime_state" in routes._SIDEBAR_SESSION_RESPONSE_FIELDS
    assert "runtime_reason" in routes._SIDEBAR_SESSION_RESPONSE_FIELDS


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def _node_preamble() -> str:
    src = SESSIONS_JS.read_text(encoding="utf-8")
    helper_src = "\n".join(
        [
            extract_function(src, "_delegatedChildRuntimeMeta"),
            extract_function(src, "_renderDelegatedChildRuntimeBadge"),
            extract_function(src, "_renderGenericDelegatedChildRow"),
        ]
    )
    return f"""
const translations = {{
  session_child_runtime_waiting: 'Waiting',
  session_child_runtime_running: 'Running',
  session_child_runtime_completed: 'Done',
  session_child_runtime_failed: 'Failed',
  session_child_runtime_cancelled: 'Canceled',
  session_child_runtime_unknown: 'Unknown',
  session_attention_approval_title: 'Waiting for permission decision',
  session_attention_clarify_title: 'Waiting for your answer',
}};
globalThis.t = (key) => translations[key] || key;
class FakeClassList {{
  constructor(owner) {{ this.owner = owner; this.items = new Set(); }}
  add(...tokens) {{
    for (const token of String(this.owner.className || '').split(/\\s+/)) if (token) this.items.add(token);
    for (const token of tokens) if (token) this.items.add(token);
    this.owner.className = [...this.items].join(' ');
  }}
  contains(token) {{ return this.items.has(token); }}
}}
class FakeElement {{
  constructor(tag) {{
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.dataset = {{}};
    this.attributes = {{}};
    this.className = '';
    this.classList = new FakeClassList(this);
    this.textContent = '';
    this.title = '';
    this.type = '';
    this.onclick = null;
  }}
  appendChild(child) {{ this.children.push(child); return child; }}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  getAttribute(name) {{ return this.attributes[name] ?? null; }}
}}
globalThis.document = {{ createElement: (tag) => new FakeElement(tag) }};
{helper_src}
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_generic_child_renderer_shows_runtime_badge_and_preserves_open_behavior():
    source = _node_preamble() + """
let stopped = 0;
const child = {
  session_id: 'child-1',
  runtime_state: 'failed',
  runtime_reason: 'tool_limit',
};
const row = _renderGenericDelegatedChildRow(
  child,
  null,
  async (item) => { globalThis.opened = item.session_id; },
  () => '-> Child session - now'
);
Promise.resolve(row.onclick({ stopPropagation() { stopped += 1; } })).then(() => {
  const badge = row.children[1];
  console.log(JSON.stringify({
    className: row.className,
    labelText: row.children[0].textContent,
    badgeText: badge.textContent,
    badgeClass: badge.className,
    tooltip: badge.title,
    runtimeState: badge.dataset.runtimeState,
    runtimeReason: badge.dataset.runtimeReason,
    opened: globalThis.opened,
    stopped,
  }));
});
"""
    out = _run_node(source)
    assert out["className"] == "session-child-session"
    assert out["labelText"] == "-> Child session - now"
    assert out["badgeText"] == "Failed"
    assert "session-child-runtime-badge-failed" in out["badgeClass"]
    assert out["tooltip"] == "Failed"
    assert out["runtimeState"] == "failed"
    assert out["runtimeReason"] == "tool_limit"
    assert out["opened"] == "child-1"
    assert out["stopped"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_waiting_child_badge_uses_attention_tooltip():
    source = _node_preamble() + """
const row = _renderGenericDelegatedChildRow(
  { session_id: 'child-2', runtime_state: 'waiting', runtime_reason: 'approval' },
  null,
  async () => {},
  () => '-> Approval child'
);
const badge = row.children[1];
console.log(JSON.stringify({
  badgeText: badge.textContent,
  tooltip: badge.title,
  runtimeState: badge.dataset.runtimeState,
  runtimeReason: badge.dataset.runtimeReason,
}));
"""
    out = _run_node(source)
    assert out == {
        "badgeText": "Waiting",
        "tooltip": "Waiting for permission decision",
        "runtimeState": "waiting",
        "runtimeReason": "approval",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_nested_delegated_children_reuse_same_runtime_badge_helper():
    source = _node_preamble() + """
const top = _renderGenericDelegatedChildRow(
  { session_id: 'child-top', runtime_state: 'running', runtime_reason: 'live' },
  null,
  async () => {},
  () => '-> Top child'
);
const nested = _renderGenericDelegatedChildRow(
  { session_id: 'child-nested', runtime_state: 'completed', runtime_reason: 'completed' },
  null,
  async () => {},
  () => '-> Nested child'
);
console.log(JSON.stringify({
  top: {
    label: top.children[1].textContent,
    state: top.children[1].dataset.runtimeState,
  },
  nested: {
    label: nested.children[1].textContent,
    state: nested.children[1].dataset.runtimeState,
  },
}));
"""
    out = _run_node(source)
    assert out == {
        "top": {"label": "Running", "state": "running"},
        "nested": {"label": "Done", "state": "completed"},
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_fork_child_path_stays_on_existing_renderer_contract():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "if(child.session_source==='fork'){" in src
    assert "session-state-indicator session-child-session-state" in src
    assert "_renderGenericDelegatedChildRow(child, activeSidForSidebar, openChildSession, childLabelFor)" in src
