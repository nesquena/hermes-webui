"""Focused proof for delegated child runtime ownership in issue #6468."""

import ast
import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from api import delegated_child_runtime as runtime
from api import gateway_chat, profiles
from api import routes


def _isolated_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runtime, "_STORE", tmp_path / "delegated-child-runtime.json")
    monkeypatch.setattr(runtime, "_MAX_TERMINAL_RECORDS", 4)
    monkeypatch.setattr(runtime, "_LIVE_TTL_SECONDS", 60)
    monkeypatch.setattr(runtime, "_TERMINAL_TTL_SECONDS", 60)
    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default"})
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)
    runtime.reset_runtime_for_tests()


def _build_streaming_on_tool(*, record_event, publish_changed, session, session_id):
    source = Path("api/streaming.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    on_tool = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "on_tool"
    )
    branch_index = next(
        index
        for index, node in enumerate(on_tool.body)
        if isinstance(node, ast.If)
        and any(
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr == "startswith"
            and isinstance(candidate.func.value, ast.Name)
            and candidate.func.value.id == "event_type"
            and candidate.args
            and isinstance(candidate.args[0], ast.Constant)
            and candidate.args[0].value == "subagent."
            for candidate in ast.walk(node.test)
        )
    )
    callback = copy.deepcopy(on_tool)
    callback.name = "_on_tool_under_test"
    callback.decorator_list = []
    callback.body = [
        node
        for node in callback.body[: branch_index + 1]
        if not isinstance(node, ast.Nonlocal)
    ]
    test_module = ast.Module(body=[callback], type_ignores=[])
    ast.fix_missing_locations(test_module)
    namespace = {
        "_flush_reasoning_buffer": lambda: None,
        "record_subagent_event": record_event,
        "publish_session_list_changed": publish_changed,
        "s": session,
        "session_id": session_id,
    }
    exec(compile(test_module, "api/streaming.py", "exec"), namespace)
    return namespace["_on_tool_under_test"]


def test_relayed_lifecycle_events_project_bounded_state_through_on_tool(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    forwarded = []
    invalidations = []

    def _record(profile, event_type, payload, **kwargs):
        forwarded.append((profile, event_type, dict(payload)))
        return runtime.record_subagent_event(profile, event_type, payload, **kwargs)

    class _Session:
        profile = "profile-a"
        session_id = "parent-a"

    on_tool = _build_streaming_on_tool(
        record_event=_record,
        publish_changed=lambda reason, profile=None, session_id=None: invalidations.append(
            (reason, profile, session_id)
        ),
        session=_Session(),
        session_id="parent-a",
    )

    on_tool(
        "subagent.start",
        "delegate_task",
        "preview",
        {"ignored": True},
        child_session_id="child-1",
        goal="must not be forwarded",
        task_count=3,
    )
    row = routes._sidebar_session_response_item(
        {
            "session_id": "child-1",
            "profile": "profile-a",
            "relationship_type": "child_session",
            "source_tag": "subagent",
            "title": "Child",
            "end_reason": "agent_close",
        }
    )

    assert row["runtime_state"] == "running"
    assert forwarded == [
        ("profile-a", "subagent.start", {"child_session_id": "child-1"})
    ]
    assert invalidations == [
        ("delegated_child_runtime_changed", "profile-a", "child-1")
    ]
    assert "goal" not in row
    assert "end_reason" not in row


def test_on_tool_invalidates_only_when_visible_runtime_state_changes(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    invalidations = []

    class _Session:
        profile = "profile-b"
        session_id = "parent-b"

    on_tool = _build_streaming_on_tool(
        record_event=runtime.record_subagent_event,
        publish_changed=lambda reason, profile=None, session_id=None: invalidations.append(
            (reason, profile, session_id)
        ),
        session=_Session(),
        session_id="parent-b",
    )

    on_tool("subagent.start", "delegate_task", "preview", {}, child_session_id="child-2")
    on_tool(
        "subagent.tool",
        "delegate_task",
        "preview",
        {},
        child_session_id="child-2",
        tool_name="read_file",
    )
    on_tool(
        "subagent.progress",
        "delegate_task",
        "preview",
        {},
        child_session_id="child-2",
        text="still running",
    )
    on_tool(
        "subagent.complete",
        "delegate_task",
        "preview",
        {},
        child_session_id="child-2",
        status="interrupted",
        summary="ignored",
    )

    assert invalidations == [
        ("delegated_child_runtime_changed", "profile-b", "child-2"),
        ("delegated_child_runtime_changed", "profile-b", "child-2"),
    ]
    assert runtime.child_runtime("profile-b", "child-2") == {
        "runtime_state": "cancelled",
        "runtime_reason": "interrupted",
    }


def test_unknown_terminal_status_stays_unknown_and_sticky(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)

    assert not runtime.record_subagent_event(
        "profile-b",
        "subagent.complete",
        {"child_session_id": "child-unknown", "status": "mystery"},
        owner_session_id="parent-b",
    )
    assert not runtime.record_subagent_event(
        "profile-b",
        "subagent.progress",
        {"child_session_id": "child-unknown"},
        owner_session_id="parent-b",
    )
    assert runtime.child_runtime("profile-b", "child-unknown") == {
        "runtime_state": "unknown",
        "runtime_reason": "mystery",
    }


def test_terminal_state_is_sticky_against_late_non_terminal_event(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)

    runtime.record_subagent_event("", "subagent.start", {"child_session_id": "child-2"})
    assert runtime.record_subagent_event(
        "", "subagent.complete", {"child_session_id": "child-2", "status": "completed"}
    )
    assert not runtime.record_subagent_event(
        "", "subagent.progress", {"child_session_id": "child-2", "text": "late"}
    )
    assert runtime.child_runtime("", "child-2") == {
        "runtime_state": "completed",
        "runtime_reason": "completed",
    }


def test_persisted_terminal_fallback_is_loaded_after_process_restart(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(runtime, "_STORE", state_dir / "delegated_child_runtime.json")

    runtime.record_subagent_event(
        "default",
        "subagent.start",
        {"child_session_id": "child-live"},
        owner_session_id="parent-restart",
    )
    runtime.record_subagent_event(
        "default",
        "subagent.complete",
        {"child_session_id": "child-complete", "status": "timeout"},
        owner_session_id="parent-restart",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "HERMES_HOME": str(tmp_path / "hermes"),
            "HERMES_WEBUI_STATE_DIR": str(state_dir),
            "HERMES_WEBUI_AGENT_DIR": str(tmp_path / "no-agent"),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from api.delegated_child_runtime import child_runtime; "
            "print(json.dumps({'live': child_runtime('default', 'child-live'), "
            "'complete': child_runtime('default', 'child-complete')}))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    restarted_state = json.loads(result.stdout.strip().splitlines()[-1])
    assert restarted_state == {
        "live": {"runtime_state": "unknown", "runtime_reason": ""},
        "complete": {"runtime_state": "failed", "runtime_reason": "timeout"},
    }


def test_missing_and_renamed_root_profiles_share_runtime_state(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default", "root-alias"})
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)

    assert runtime.record_subagent_event(
        None,
        "subagent.complete",
        {"child_session_id": "child-root", "status": "ok"},
        owner_session_id="parent-root",
    )

    expected = {"runtime_state": "completed", "runtime_reason": "ok"}
    assert runtime.child_runtime("default", "child-root") == expected
    assert runtime.child_runtime("root-alias", "child-root") == expected
    assert routes._sidebar_session_response_item(
        {
            "session_id": "child-root",
            "relationship_type": "child_session",
            "source_tag": "subagent",
            "profile": "default",
        }
    )["runtime_state"] == "completed"


def test_gateway_subagent_frames_do_not_claim_local_runtime_state(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    forwarded = []

    class _Response:
        def __iter__(self):
            yield b'data: {"event":"subagent.start","child_session_id":"child-gateway"}\n'
            yield b"data: [DONE]\n"

    gateway_chat._relay_gateway_run_events(
        _Response(),
        "parent-gateway",
        "stream-gateway",
        "run-gateway",
        "https://gateway.example",
        "",
        put_gateway_event=lambda event, payload: forwarded.append((event, payload)),
        cancel_event=threading.Event(),
    )

    assert forwarded == []
    assert runtime.child_runtime("default", "child-gateway") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }


def test_corrupt_persisted_store_degrades_to_unknown_then_recovers_on_next_write(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    runtime._STORE.write_text("{not json", encoding="utf-8")
    runtime._TERMINAL = None

    assert runtime.child_runtime("profile-corrupt", "child-corrupt") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }

    assert runtime.record_subagent_event(
        "profile-corrupt",
        "subagent.complete",
        {"child_session_id": "child-corrupt", "status": "ok"},
        owner_session_id="parent-corrupt",
    )
    store = json.loads(runtime._STORE.read_text(encoding="utf-8"))
    assert store["records"] == {
        "profile-corrupt\0child-corrupt": {
            "owner_session_id": "parent-corrupt",
            "runtime_reason": "ok",
            "runtime_state": "completed",
            "updated_at": runtime._TERMINAL[("profile-corrupt", "child-corrupt")]["updated_at"],
        }
    }


def test_stale_live_runtime_expires_without_followup_event(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "_LIVE_TTL_SECONDS", 120)
    monkeypatch.setattr(runtime, "_TERMINAL_TTL_SECONDS", 30)
    now = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: now[0])

    assert runtime.record_subagent_event(
        "profile-live",
        "subagent.start",
        {"child_session_id": "child-live"},
        owner_session_id="parent-live",
    )
    assert runtime.child_runtime("profile-live", "child-live") == {
        "runtime_state": "running",
        "runtime_reason": "subagent.start",
    }

    now[0] = 1035.0
    assert runtime.child_runtime("profile-live", "child-live") == {
        "runtime_state": "running",
        "runtime_reason": "subagent.start",
    }

    now[0] = 1121.0
    assert runtime.child_runtime("profile-live", "child-live") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }


def test_malformed_persisted_records_are_dropped_and_rewritten(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    now = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: now[0])
    runtime._STORE.write_text(
        json.dumps(
            {
                "records": {
                    "profile-good\0child-good": {
                        "runtime_state": "failed",
                        "runtime_reason": "timeout",
                        "updated_at": 990.0,
                        "owner_session_id": "parent-good",
                    },
                    "profile-bad\0child-bad": {
                        "runtime_state": "running",
                        "runtime_reason": "subagent.progress",
                        "updated_at": 990.0,
                    },
                    "profile-type\0child-type": {
                        "runtime_state": [],
                        "runtime_reason": "broken",
                        "updated_at": 990.0,
                    },
                    "profile-time\0child-time": {
                        "runtime_state": "failed",
                        "runtime_reason": "timeout",
                        "updated_at": "bad",
                    },
                    "missing-separator": {
                        "runtime_state": "completed",
                        "runtime_reason": "ok",
                        "updated_at": 990.0,
                    },
                    "profile-nondict\0child-nondict": "broken",
                }
            }
        ),
        encoding="utf-8",
    )
    runtime._TERMINAL = None

    assert runtime.child_runtime("profile-good", "child-good") == {
        "runtime_state": "failed",
        "runtime_reason": "timeout",
    }
    assert runtime.child_runtime("profile-bad", "child-bad") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    assert runtime.child_runtime("profile-type", "child-type") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    assert runtime.child_runtime("profile-time", "child-time") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    store = json.loads(runtime._STORE.read_text(encoding="utf-8"))
    assert store["records"] == {
        "profile-good\0child-good": {
            "owner_session_id": "parent-good",
            "runtime_reason": "timeout",
            "runtime_state": "failed",
            "updated_at": 990.0,
        }
    }


def test_persisted_terminal_records_are_bounded_and_expire(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "_MAX_TERMINAL_RECORDS", 2)
    monkeypatch.setattr(runtime, "_TERMINAL_TTL_SECONDS", 30)
    now = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: now[0])

    runtime.record_subagent_event("profile-c", "subagent.complete", {"child_session_id": "child-4", "status": "ok"})
    now[0] = 1010.0
    runtime.record_subagent_event("profile-c", "subagent.complete", {"child_session_id": "child-5", "status": "failed"})
    now[0] = 1020.0
    runtime.record_subagent_event("profile-c", "subagent.complete", {"child_session_id": "child-6", "status": "timeout"})
    runtime.clear_live_runtime()

    assert runtime.child_runtime("profile-c", "child-4") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    assert runtime.child_runtime("profile-c", "child-5") == {
        "runtime_state": "failed",
        "runtime_reason": "failed",
    }
    assert runtime.child_runtime("profile-c", "child-6") == {
        "runtime_state": "failed",
        "runtime_reason": "timeout",
    }

    now[0] = 1055.0
    runtime.clear_live_runtime()
    runtime._TERMINAL = None

    assert runtime.child_runtime("profile-c", "child-5") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    assert runtime.child_runtime("profile-c", "child-6") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    store = json.loads(runtime._STORE.read_text(encoding="utf-8"))
    assert store["records"] == {}


def test_cached_terminal_records_expire_without_reset(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "_TERMINAL_TTL_SECONDS", 30)
    now = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: now[0])

    runtime.record_subagent_event(
        "profile-cache",
        "subagent.complete",
        {"child_session_id": "child-cache", "status": "ok"},
        owner_session_id="parent-cache",
    )
    runtime.clear_live_runtime()

    now[0] = 1035.0
    assert runtime.child_runtime("profile-cache", "child-cache") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    store = json.loads(runtime._STORE.read_text(encoding="utf-8"))
    assert store["records"] == {}


def test_child_runtime_keeps_pruned_state_when_store_cleanup_fails(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime.time, "time", lambda: 2000.0)
    runtime._TERMINAL = {
        ("profile-cache", "child-expired"): {
            "runtime_state": "completed",
            "runtime_reason": "ok",
            "updated_at": 1000.0,
            "owner_session_id": "parent-cache",
        }
    }

    def fail_write():
        raise OSError("state directory is unwritable")

    monkeypatch.setattr(runtime, "_persist_terminal", fail_write)

    assert runtime.child_runtime("profile-cache", "child-expired") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    assert runtime._TERMINAL == {}


def test_forget_runtime_owner_prunes_live_and_persisted_records(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)

    runtime.record_subagent_event(
        "profile-d",
        "subagent.complete",
        {"child_session_id": "child-7", "status": "ok"},
        owner_session_id="parent-d",
    )
    runtime.record_subagent_event(
        "profile-d",
        "subagent.complete",
        {"child_session_id": "child-8", "status": "failed"},
        owner_session_id="parent-e",
    )

    assert runtime.forget_runtime_owner("parent-d", profile="profile-d")
    runtime.clear_live_runtime()

    assert runtime.child_runtime("profile-d", "child-7") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }
    assert runtime.child_runtime("profile-d", "child-8") == {
        "runtime_state": "failed",
        "runtime_reason": "failed",
    }


def test_forget_runtime_owner_matches_renamed_root_profile(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default", "root-alias"})
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)
    runtime.record_subagent_event(
        "default",
        "subagent.complete",
        {"child_session_id": "child-root", "status": "ok"},
        owner_session_id="parent-root",
    )

    assert runtime.forget_runtime_owner("parent-root", profile="root-alias")
    runtime.clear_live_runtime()

    assert runtime.child_runtime("default", "child-root") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }


def test_events_without_child_or_owner_are_ignored(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)

    assert not runtime.record_subagent_event(
        "profile-e",
        "subagent.start",
        {"status": "ok"},
        owner_session_id="parent-e",
    )
    assert not runtime.forget_runtime_owner("", profile="profile-e")
    assert runtime.child_runtime("profile-e", "child-missing") == {
        "runtime_state": "unknown",
        "runtime_reason": "",
    }


def test_unowned_child_stays_unknown_and_fork_renderer_stays_separate(monkeypatch, tmp_path):
    _isolated_runtime(monkeypatch, tmp_path)
    unknown = routes._sidebar_session_response_item(
        {
            "session_id": "child-4",
            "relationship_type": "child_session",
            "source_tag": "subagent",
            "profile": "profile-c",
            "active_stream_id": "parent-owned",
            "end_reason": "agent_close",
        }
    )
    assert unknown["runtime_state"] == "unknown"

    fork = routes._sidebar_session_response_item(
        {
            "session_id": "fork-1",
            "relationship_type": "child_session",
            "session_source": "fork",
            "profile": "profile-c",
        }
    )
    assert "runtime_state" not in fork


def test_session_delete_route_prunes_delegated_child_runtime_store():
    source = Path("api/routes.py").read_text(encoding="utf-8")
    delete_start = source.index('if parsed.path == "/api/session/delete":')
    publish_idx = source.index('_publish_session_list_changed("session_delete"', delete_start)
    delete_block = source[delete_start:publish_idx]
    assert "forget_runtime_owner(sid, profile=event_profile)" in delete_block


def test_renderer_uses_generic_badge_only_for_delegated_children(tmp_path):
    source = Path("static/sessions.js").read_text(encoding="utf-8")
    helper_start = source.index("function _delegatedChildRuntimeBadge(child){")
    helper_end = source.index("function _attachChildSessionsToSidebarRows(", helper_start)
    helper = source[helper_start:helper_end]
    badge_insert = source.index("if(runtimeBadge) row.appendChild(runtimeBadge);", helper_end)
    render_start = source.rfind("const row=document.createElement('button');", 0, badge_insert)
    render_end = source.index("childList.appendChild(row);", badge_insert)
    render_snippet = source[render_start:render_end + len("childList.appendChild(row);")]
    script = f"""
const helper = {helper!r};
const renderSnippet = {render_snippet!r};
const labels = {{
  delegated_child_runtime_running: 'Running',
  delegated_child_runtime_completed: 'Done',
  delegated_child_runtime_failed: 'Failed',
  delegated_child_runtime_cancelled: 'Cancelled',
  delegated_child_runtime_unknown: 'Unknown',
}};
function t(key, value) {{
  if (key === 'delegated_child_runtime_tooltip') return `Child runtime: ${{value}}`;
  return labels[key] || key;
}}
function textNode(text) {{
  return {{ nodeType: 'text', textContent: String(text) }};
}}
function element(tag) {{
  return {{
    tagName: tag,
    type: '',
    className: '',
    textContent: '',
    title: '',
    attrs: {{}},
    children: [],
    appendChild(child) {{
      this.children.push(child);
      return child;
    }},
    setAttribute(name, value) {{
      this.attrs[name] = String(value);
    }},
  }};
}}
const document = {{
  createElement(tag) {{
    return element(tag);
  }},
  createTextNode(text) {{
    return textNode(text);
  }},
}};
const badgeFactory = new Function('document', 't', helper + '; return _delegatedChildRuntimeBadge;');
const delegatedBadge = badgeFactory(document, t)({{
  relationship_type: 'child_session',
  source_tag: 'subagent',
  session_id: 'child-5',
  runtime_state: 'failed',
}});
const forkBadge = badgeFactory(document, t)({{
  relationship_type: 'child_session',
  source_tag: 'subagent',
  session_source: 'fork',
  session_id: 'fork-2',
  runtime_state: 'running',
}});
const unknownBadge = badgeFactory(document, t)({{
  relationship_type: 'child_session',
  source_tag: 'subagent',
  runtime_state: 'unknown',
}});
const missingStateBadge = badgeFactory(document, t)({{
  relationship_type: 'child_session',
  source_tag: 'subagent',
}});
if (!delegatedBadge) throw new Error('delegated child should render a badge');
if (forkBadge !== null) throw new Error('fork child must stay on the dedicated renderer path');
if (unknownBadge !== null) throw new Error('unknown child state should not render a badge');
if (missingStateBadge !== null) throw new Error('missing child state should not render a badge');
const openCalls = [];
const childList = {{
  children: [],
  appendChild(child) {{
    this.children.push(child);
    return child;
  }},
}};
const child = {{
  relationship_type: 'child_session',
  source_tag: 'subagent',
  session_id: 'child-5',
  runtime_state: 'running',
}};
const renderChild = new Function(
  'document',
  'child',
  'childList',
  'activeSidForSidebar',
  'childLabelFor',
  '_delegatedChildRuntimeBadge',
  'openChildSession',
  '_wireSessionNewTabListeners',
  '_consumeSessionNewTabClick',
  renderSnippet + '; return childList.children[0];'
);
(async () => {{
  const row = renderChild(
    document,
    child,
    childList,
    null,
    () => 'Delegated child',
    badgeFactory(document, t),
    async(rowChild) => {{
      openCalls.push(rowChild.session_id);
    }},
    () => {{}},
    () => false,
  );
  await row.onclick({{ stopPropagation() {{}} }});
  const badge = row.children[1];
  console.log(JSON.stringify({{
    delegatedBadgeClass: delegatedBadge.className,
    delegatedBadgeTitle: delegatedBadge.title,
    delegatedBadgeLabel: delegatedBadge.attrs['aria-label'],
    delegatedBadgeText: delegatedBadge.textContent,
    rowTag: row.tagName,
    rowType: row.type,
    rowTitle: row.title,
    rowChildren: row.children.map((childNode) => childNode.nodeType === 'text' ? childNode.textContent : childNode.className),
    openCalls,
  }}));
}})().catch((error) => {{
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}});
"""
    script_path = tmp_path / "delegated_child_runtime_renderer_test.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    metrics = json.loads(result.stdout.strip().splitlines()[-1])
    assert metrics["delegatedBadgeClass"] == "session-child-runtime session-child-runtime-failed"
    assert metrics["delegatedBadgeText"] == "Failed"
    assert metrics["delegatedBadgeTitle"] == "Child runtime: Failed"
    assert metrics["delegatedBadgeLabel"] == "Child runtime: Failed"
    assert metrics["rowTag"] == "button"
    assert metrics["rowType"] == "button"
    assert metrics["rowTitle"] == "Open child session"
    assert metrics["rowChildren"] == [
        "session-child-session-label",
        "session-child-runtime session-child-runtime-running",
    ]
    style = Path("static/style.css").read_text(encoding="utf-8")
    assert ".session-child-session-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}" in style
    assert metrics["openCalls"] == ["child-5"]
