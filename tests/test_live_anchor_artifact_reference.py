"""Regression coverage for production turn-owned workspace artifacts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.artifact_references import derive_file_artifact_references


REPO = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_block(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    next_def = source.find("\n            def ", start + 1)
    assert next_def != -1, f"end of {name} not found"
    return source[start:next_def]


def test_successful_write_file_emits_only_bounded_reference_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "reports" / "final.md"
    result = json.dumps({
        "bytes_written": 19,
        "resolved_path": str(target),
        "files_modified": [str(target)],
        "output": "private generated body",
    })

    references = derive_file_artifact_references(
        "write_file",
        {"path": str(target), "content": "private input body"},
        result,
        workspace,
        tool_call_id="call-write",
    )

    assert references == [{
        "kind": "workspace_file",
        "path": "reports/final.md",
        "source_tool": "write_file",
        "tool_call_id": "call-write",
    }]
    encoded = json.dumps(references)
    assert "private generated body" not in encoded
    assert "private input body" not in encoded
    assert str(workspace) not in encoded


def test_failed_incomplete_or_read_only_results_do_not_become_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = str(workspace / "failed.md")

    cases = [
        ("write_file", {"path": path}, json.dumps({"error": "permission denied"})),
        ("write_file", {"path": path}, json.dumps({"resolved_path": path})),
        ("patch", {"path": path}, json.dumps({"success": False, "error": "stale"})),
        ("patch", {"path": path}, "truncated result"),
        ("read_file", {"path": path}, json.dumps({"bytes_written": 1})),
        ("terminal", {"command": f"touch {path}"}, json.dumps({"success": True})),
        ("create_file", {"path": path}, json.dumps({"bytes_written": 1})),
    ]

    for name, args, result in cases:
        assert derive_file_artifact_references(
            name, args, result, workspace, tool_call_id="call-failed"
        ) == []
    assert derive_file_artifact_references(
        "write_file", {"path": path}, json.dumps({"bytes_written": 1}), ""
    ) == []


def test_successful_patch_keeps_result_order_dedupes_and_rejects_unsafe_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    cache_path = workspace / "node_modules" / "generated.js"
    root_build_path = workspace / "build" / "bundle.js"
    nested_build_path = workspace / "docs" / "build" / "report.md"
    first = workspace / "src" / "app.py"
    second = workspace / "Makefile"
    result = json.dumps({
        "success": True,
        "files_modified": [
            str(first),
            str(first),
            str(outside),
            "../traversal.md",
            "https://example.com/not-a-file",
            str(cache_path),
            str(root_build_path),
            str(nested_build_path),
            {"path": str(workspace / "not-a-string.md")},
            str(second),
        ],
    })

    references = derive_file_artifact_references(
        "patch", {"mode": "patch"}, result, workspace, tool_call_id="call-patch"
    )

    assert [item["path"] for item in references] == [
        "src/app.py",
        "docs/build/report.md",
        "Makefile",
    ]


def test_surrounding_whitespace_path_does_not_take_decoy_ownership(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decoy = workspace / "report"
    actual = workspace / "report "
    decoy.write_text("decoy", encoding="utf-8")
    actual.write_text("actual", encoding="utf-8")

    assert derive_file_artifact_references(
        "write_file",
        {"path": str(actual)},
        json.dumps({"bytes_written": 6, "files_modified": [str(actual)]}),
        workspace,
        tool_call_id="call-space",
    ) == []

    assert derive_file_artifact_references(
        "write_file",
        {"path": str(decoy)},
        json.dumps({"bytes_written": 5, "files_modified": [str(decoy)]}),
        workspace,
        tool_call_id="call-decoy",
    ) == [{
        "kind": "workspace_file",
        "path": "report",
        "source_tool": "write_file",
        "tool_call_id": "call-decoy",
    }]


def test_artifact_derivation_limits_fail_closed_on_overflow(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    too_many_paths = [f"generated-{idx}.md" for idx in range(65)]
    assert derive_file_artifact_references(
        "patch",
        {"mode": "patch"},
        json.dumps({"success": True, "files_modified": too_many_paths}),
        workspace,
        tool_call_id="call-many",
    ) == []

    huge_result = json.dumps({
        "bytes_written": 1,
        "resolved_path": str(workspace / "huge.md"),
        "padding": "x" * (65 * 1024),
    })
    assert derive_file_artifact_references(
        "write_file",
        {"path": str(workspace / "huge.md")},
        huge_result,
        workspace,
        tool_call_id="call-huge-result",
    ) == []

    assert derive_file_artifact_references(
        "write_file",
        {"path": str(workspace / "tool-id.md")},
        json.dumps({"bytes_written": 1, "resolved_path": str(workspace / "tool-id.md")}),
        workspace,
        tool_call_id="t" * 257,
    ) == []

    patch = "\n".join(
        ["*** Begin Patch"]
        + [f"*** Add File: generated-{idx}.md" for idx in range(65)]
        + ["*** End Patch"]
    )
    assert derive_file_artifact_references(
        "patch",
        {"mode": "patch", "patch": patch},
        json.dumps({"success": True}),
        workspace,
        tool_call_id="call-patch-overflow",
    ) == []


def test_successful_patch_can_fall_back_to_v4a_targets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = {
        "mode": "patch",
        "patch": "\n".join([
            "*** Begin Patch",
            "*** Update File: src/app.py",
            "*** Add File: Makefile",
            "*** Move File: old.txt -> docs/new.txt",
            "*** End Patch",
        ]),
    }

    references = derive_file_artifact_references(
        "functions.patch",
        args,
        json.dumps({"success": True}),
        workspace,
        tool_call_id="call-v4a",
    )

    assert [item["path"] for item in references] == [
        "src/app.py",
        "Makefile",
        "old.txt",
        "docs/new.txt",
    ]


def test_existing_symlink_escape_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    link.symlink_to(outside, target_is_directory=True)

    references = derive_file_artifact_references(
        "write_file",
        {"path": "linked/secret.md"},
        json.dumps({"bytes_written": 1, "resolved_path": str(link / "secret.md")}),
        workspace,
        tool_call_id="call-link",
    )

    assert references == []


def test_foreign_drive_path_is_rejected_on_posix(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    references = derive_file_artifact_references(
        "write_file",
        {"path": r"C:\\foreign\\report.md"},
        json.dumps({"bytes_written": 1, "resolved_path": r"C:\\foreign\\report.md"}),
        workspace,
        tool_call_id="call-foreign-drive",
    )

    assert references == []


def test_backend_emits_artifact_after_tool_completion_and_keeps_cancel_journalable():
    modern = _function_block(STREAMING_PY, "on_tool_complete")
    legacy = _function_block(STREAMING_PY, "on_tool")

    for block in (modern, legacy):
        derive = block.index("derive_file_artifact_references(")
        tool_complete = block.index("put('tool_complete'")
        artifact = block.index("put('artifact_reference', artifact_reference)")
        assert tool_complete < derive < artifact

    assert "event not in ('cancel', 'error', 'artifact_reference')" in STREAMING_PY


def test_runtime_journal_snapshot_projects_artifact_references_for_reconnect(monkeypatch):
    from api import routes

    session_id = "session-artifact-snapshot"
    run_id = "run-artifact-snapshot"
    stream_id = "stream-artifact-snapshot"
    events = [
        {
            "event": "tool",
            "seq": 1,
            "event_id": f"{run_id}:1",
            "run_id": run_id,
            "created_at": 1.0,
            "payload": {"name": "write_file", "tid": "call-write"},
        },
        {
            "event": "tool_complete",
            "seq": 2,
            "event_id": f"{run_id}:2",
            "run_id": run_id,
            "created_at": 2.0,
            "payload": {"name": "write_file", "tid": "call-write", "preview": "ok"},
        },
        {
            "event": "artifact_reference",
            "seq": 3,
            "event_id": f"{run_id}:3",
            "run_id": run_id,
            "created_at": 3.0,
            "payload": {
                "kind": "workspace_file",
                "path": "reports/final.md",
                "source_tool": "write_file",
                "tool_call_id": "call-write",
                "content": "private body must not persist",
            },
        },
    ]

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda lookup_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "stream_id": stream_id,
            "last_seq": 3,
            "last_event_id": f"{run_id}:3",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda loaded_session_id, lookup_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]
    artifact = scene["artifacts"][0]

    assert snapshot["last_seq"] == 3
    assert scene["identity"]["run_id"] == run_id
    assert scene["identity"]["stream_id"] == stream_id
    assert artifact["event_id"] == f"{run_id}:3"
    assert artifact["run_id"] == run_id
    assert artifact["stream_id"] == stream_id
    assert artifact["seq"] == 3
    assert artifact["payload"] == {
        "kind": "workspace_file",
        "path": "reports/final.md",
        "source_tool": "write_file",
        "tool_call_id": "call-write",
    }
    assert "private body" not in json.dumps(scene)


def test_frontend_routes_artifact_sse_to_anchor_without_repainting_worklog():
    start = MESSAGES_JS.index("source.addEventListener('artifact_reference'")
    end = MESSAGES_JS.index("source.addEventListener('todo_state'", start)
    block = MESSAGES_JS[start:end]

    assert "S.session.session_id!==activeSid" in block
    assert "S.activeStreamId!==streamId" in block
    assert "_applyToAnchor('artifact_reference',d,e,null,{render:false});" in block
    assert "'tool_complete','artifact_reference','todo_state'" in MESSAGES_JS


@pytest.mark.skipif(NODE is None, reason="node is required for browser hydration coverage")
def test_frontend_hydrates_artifact_only_activity_scene():
    script = f"""
const fs=require('fs');
const src=fs.readFileSync({json.dumps(str(REPO / "static" / "messages.js"))},'utf8');
function extractFunc(name){{
  const start=src.indexOf('function '+name);
  if(start===-1) throw new Error(name+' not found');
  const params=src.indexOf('(',start);
  let depth=0,close=-1;
  for(let i=params;i<src.length;i+=1){{
    if(src[i]==='(') depth+=1;
    else if(src[i]===')'){{
      depth-=1;
      if(depth===0){{ close=i; break; }}
    }}
  }}
  const brace=src.indexOf('{{',close);
  depth=0;
  for(let i=brace;i<src.length;i+=1){{
    if(src[i]==='{{') depth+=1;
    else if(src[i]==='}}'){{
      depth-=1;
      if(depth===0) return src.slice(start,i+1);
    }}
  }}
  throw new Error(name+' body did not close');
}}

const activeSid='session-artifact-snapshot';
const streamId='stream-artifact-snapshot';
let _anchorShadowWarned=false;
let _anchorRegistry={{}};
const applied=[];
const _anchorApi={{
  applyAssistantTurnAnchorSourceEvent(registry,event,context){{
    applied.push({{event,context}});
    return {{applied:true}};
  }},
}};
eval(extractFunc('_hydrateAnchorRegistryFromActivityScene'));

const ok=_hydrateAnchorRegistryFromActivityScene({{
  version:'activity_scene_v1',
  identity:{{run_id:'run-artifact-snapshot', stream_id:streamId}},
  activity_rows:[],
  artifacts:[{{
    event_id:'run-artifact-snapshot:3',
    local_id:'artifact:run-artifact-snapshot:3',
    run_id:'run-artifact-snapshot',
    stream_id:streamId,
    seq:3,
    source_event_type:'artifact_reference',
    payload:{{
      kind:'workspace_file',
      path:'reports/final.md',
      source_tool:'write_file',
      tool_call_id:'call-write',
    }},
  }}],
}});
if(!ok) throw new Error('artifact-only scene was not hydrated');
if(applied.length!==1) throw new Error('expected one applied artifact event, got '+applied.length);
const item=applied[0];
if(item.event.source_event_type!=='artifact_reference') throw new Error('wrong source event');
if(item.event.path!=='reports/final.md') throw new Error('path did not hydrate');
if(item.event.run_id!=='run-artifact-snapshot') throw new Error('run identity lost');
if(item.context.stream_id!==streamId) throw new Error('stream context lost');
"""
    subprocess.run([NODE, "-e", script], check=True)


def test_server_terminal_reconciliation_persists_artifact_only_scene_without_browser(tmp_path, monkeypatch):
    from collections import OrderedDict

    from api import models, routes, streaming
    from api.artifact_references import anchor_artifact_event_from_payload
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session = Session(
        session_id="artifact-bg",
        messages=[
            {"role": "user", "content": "write a file", "timestamp": 1},
            {"role": "assistant", "content": "done", "timestamp": 2},
        ],
    )
    event = anchor_artifact_event_from_payload(
        {
            "kind": "workspace_file",
            "path": "reports/background.md",
            "source_tool": "write_file",
            "tool_call_id": "call-bg",
        },
        session_id=session.session_id,
        run_id="stream-bg",
        stream_id="stream-bg",
        event_id="stream-bg:7",
        seq=7,
    )

    assert streaming._reconcile_stream_artifacts_into_terminal_anchor_scene(
        session,
        "stream-bg",
        [event],
        terminal_state="completed",
        message_index=1,
    )
    session.save(skip_index=True)

    raw = json.loads((session_dir / "artifact-bg.json").read_text(encoding="utf-8"))
    assert "_anchor_activity_scene" not in raw["messages"][1]
    record = next(iter(raw["anchor_activity_scenes"].values()))
    assert record["message_index"] == 1
    assert record["stream_id"] == "stream-bg"
    assert record["scene"]["activity_rows"] == []
    assert record["scene"]["artifacts"][0]["payload"]["path"] == "reports/background.md"

    loaded = Session.load("artifact-bg")
    hydrated = routes._hydrate_anchor_activity_scenes(
        loaded.messages,
        loaded.anchor_activity_scenes,
    )
    assert hydrated[1]["_anchor_activity_scene"]["artifacts"][0]["payload"]["path"] == "reports/background.md"


def test_late_cancel_artifact_reconciles_onto_cancelled_message_once(tmp_path, monkeypatch):
    from collections import OrderedDict

    from api import models, routes, streaming
    from api.artifact_references import anchor_artifact_event_from_payload
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session = Session(
        session_id="artifact-cancel",
        messages=[{"role": "user", "content": "write then cancel", "timestamp": 1}],
    )
    streaming._persist_cancelled_turn(session, stream_id="stream-cancel")
    session.save(skip_index=True)

    event = anchor_artifact_event_from_payload(
        {
            "kind": "workspace_file",
            "path": "reports/cancelled.md",
            "source_tool": "write_file",
            "tool_call_id": "call-cancel",
        },
        session_id=session.session_id,
        run_id="stream-cancel",
        stream_id="stream-cancel",
        event_id="stream-cancel:9",
        seq=9,
    )
    assert streaming._reconcile_stream_artifacts_into_terminal_anchor_scene(
        session,
        "stream-cancel",
        [event],
        terminal_state="cancelled",
    )
    assert streaming._reconcile_stream_artifacts_into_terminal_anchor_scene(
        session,
        "stream-cancel",
        [event],
        terminal_state="cancelled",
    )
    session.save(skip_index=True)

    raw = json.loads((session_dir / "artifact-cancel.json").read_text(encoding="utf-8"))
    marker = raw["messages"][-1]
    assert marker["_anchor_stream_id"] == "stream-cancel"
    record = next(iter(raw["anchor_activity_scenes"].values()))
    artifacts = record["scene"]["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["payload"]["path"] == "reports/cancelled.md"
    assert record["scene"]["terminal_state"] == "cancelled"


def test_terminal_reconciliation_rejects_foreign_stream_owner(tmp_path, monkeypatch):
    from collections import OrderedDict

    from api import models, routes, streaming
    from api.artifact_references import anchor_artifact_event_from_payload
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session = Session(
        session_id="artifact-foreign",
        messages=[
            {"role": "user", "content": "first", "timestamp": 1},
            {
                "role": "assistant",
                "content": "newer answer",
                "timestamp": 2,
                "_anchor_stream_id": "stream-new",
            },
        ],
    )
    event = anchor_artifact_event_from_payload(
        {"kind": "workspace_file", "path": "reports/old.md", "source_tool": "write_file"},
        session_id=session.session_id,
        run_id="stream-old",
        stream_id="stream-old",
        event_id="stream-old:3",
        seq=3,
    )

    assert not streaming._reconcile_stream_artifacts_into_terminal_anchor_scene(
        session,
        "stream-old",
        [event],
        terminal_state="completed",
        message_index=1,
    )
    assert not session.anchor_activity_scenes


def test_terminal_reconciliation_rejects_explicit_foreign_artifact_owner(tmp_path):
    from api import streaming
    from api.artifact_references import anchor_artifact_event_from_payload
    from api.models import Session

    session = Session(
        session_id="artifact-foreign-owner",
        messages=[
            {"role": "user", "content": "write"},
            {
                "role": "assistant",
                "content": "done",
                "timestamp": 2,
                "_anchor_stream_id": "stream-good",
            },
        ],
    )
    foreign = anchor_artifact_event_from_payload(
        {
            "kind": "workspace_file",
            "path": "reports/foreign-owner.md",
            "source_tool": "write_file",
        },
        session_id=session.session_id,
        run_id="stream-good",
        stream_id="stream-foreign",
        event_id="stream-foreign:3",
        seq=3,
    )

    assert not streaming._reconcile_stream_artifacts_into_terminal_anchor_scene(
        session,
        "stream-good",
        [foreign],
        terminal_state="completed",
        message_index=1,
    )
    assert not session.anchor_activity_scenes


def test_returned_apperror_settles_tool_artifact_before_terminal_save(tmp_path, monkeypatch):
    import queue
    from collections import OrderedDict

    from api import config, models, routes, streaming
    from api.models import Session

    session_dir = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    session_dir.mkdir()
    workspace.mkdir()
    target = workspace / "reports" / "returned-error.md"
    target.parent.mkdir()

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(streaming, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    stream_id = "stream-returned-error"
    session = Session(session_id="artifact-returned-error", title="Returned error")
    session.pending_user_message = "write then fail"
    session.active_stream_id = stream_id
    session.save(skip_index=True)

    class ReturnedErrorAgent:
        def __init__(
            self,
            *,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
            **_kwargs,
        ):
            self.stream_delta_callback = stream_delta_callback
            self.reasoning_callback = reasoning_callback
            self.tool_progress_callback = tool_progress_callback
            self.clarify_callback = clarify_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            if self.tool_start_callback:
                self.tool_start_callback("call-returned", "write_file", {"path": str(target)})
            if self.tool_complete_callback:
                self.tool_complete_callback(
                    "call-returned",
                    "write_file",
                    {"path": str(target)},
                    json.dumps({"bytes_written": 1, "resolved_path": str(target)}),
                )
            return {
                "status": "error",
                "error": "returned terminal failure",
                "messages": kwargs.get("conversation_history") or [],
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: ReturnedErrorAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "RunJournalWriter", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "append_turn_journal_event_for_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "register_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "update_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "unregister_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "unregister_stream_owner", lambda *args, **kwargs: None)

    streaming.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text="write then fail",
            model="test-model",
            workspace=str(workspace),
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)

    raw = json.loads((session_dir / "artifact-returned-error.json").read_text(encoding="utf-8"))
    assert raw["messages"][-1]["_error"] is True
    assert raw["messages"][-1]["_anchor_stream_id"] == stream_id
    record = next(iter(raw["anchor_activity_scenes"].values()))
    assert record["message_index"] == len(raw["messages"]) - 1
    assert record["stream_id"] == stream_id
    assert record["scene"]["terminal_state"] == "error"
    assert record["scene"]["artifacts"][0]["payload"]["path"] == "reports/returned-error.md"


def test_repeated_tool_complete_ingress_persists_bounded_artifact_prefix(tmp_path, monkeypatch):
    import queue
    from collections import OrderedDict

    from api import config, models, routes, streaming
    from api.artifact_references import MAX_ANCHOR_ARTIFACT_BYTES, MAX_ANCHOR_ARTIFACT_REFERENCES
    from api.models import Session

    session_dir = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    session_dir.mkdir()
    workspace.mkdir()
    (workspace / "reports").mkdir()

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(streaming, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    stream_id = "stream-many-artifacts"
    session = Session(session_id="artifact-many-ingress", title="Many artifacts")
    session.pending_user_message = "write many files"
    session.active_stream_id = stream_id
    session.save(skip_index=True)

    class ManyArtifactsAgent:
        def __init__(
            self,
            *,
            tool_complete_callback=None,
            **_kwargs,
        ):
            self.tool_complete_callback = tool_complete_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            for idx in range(96):
                target = workspace / "reports" / f"{idx:03d}-artifact.md"
                if self.tool_complete_callback:
                    self.tool_complete_callback(
                        f"call-{idx:03d}",
                        "write_file",
                        {"path": str(target)},
                        json.dumps({"bytes_written": 1, "resolved_path": str(target)}),
                    )
            history = list(kwargs.get("conversation_history") or [])
            return {
                "completed": True,
                "final_response": "done",
                "messages": history + [
                    {"role": "user", "content": kwargs.get("persist_user_message") or ""},
                    {"role": "assistant", "content": "done"},
                ],
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: ManyArtifactsAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "RunJournalWriter", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "append_turn_journal_event_for_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "register_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "update_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "unregister_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "unregister_stream_owner", lambda *args, **kwargs: None)

    streaming.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text="write many files",
            model="test-model",
            workspace=str(workspace),
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)

    raw = json.loads((session_dir / "artifact-many-ingress.json").read_text(encoding="utf-8"))
    record = next(iter(raw["anchor_activity_scenes"].values()))
    artifacts = record["scene"]["artifacts"]
    encoded = json.dumps(artifacts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert 0 < len(artifacts) <= MAX_ANCHOR_ARTIFACT_REFERENCES
    assert len(artifacts) < 96
    assert len(encoded) <= MAX_ANCHOR_ARTIFACT_BYTES
    assert artifacts[0]["payload"]["path"] == "reports/000-artifact.md"
    assert artifacts[-1]["payload"]["path"] < "reports/096-artifact.md"
