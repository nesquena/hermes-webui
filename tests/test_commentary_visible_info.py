import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
_LIFECYCLE_SPEC = importlib.util.spec_from_file_location(
    "browser_conversation_lifecycle",
    ROOT / "tests" / "browser_conversation_lifecycle.py",
)
assert _LIFECYCLE_SPEC and _LIFECYCLE_SPEC.loader
_LIFECYCLE = importlib.util.module_from_spec(_LIFECYCLE_SPEC)
_LIFECYCLE_SPEC.loader.exec_module(_LIFECYCLE)
_capture_page_errors = _LIFECYCLE._capture_page_errors
_start_webui_server = _LIFECYCLE._start_webui_server
_terminate_process = _LIFECYCLE._terminate_process


@pytest.mark.skipif(NODE is None, reason="node is required for commentary rendering regression")
def test_commentary_is_visible_assistant_information():
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "_commentary_is_visible_info.mjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "PASS commentary_is_visible_info" in result.stdout


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="browser commentary regression requires the CI browser toolchain",
)
def test_render_messages_projects_commentary_once_and_restores_deferred_worklog():
    playwright = pytest.importorskip("playwright.sync_api")
    progress = "Candidate identity verified; running the real restart now."
    distinct_reasoning = "Private reasoning that remains in Worklog."
    final_answer = "The restart completed successfully."
    messages = [
        {"role": "user", "content": "Continue the restart."},
        {
            "role": "assistant",
            "content": "",
            "reasoning": progress,
            "reasoning_content": progress,
            "codex_message_items": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": progress}],
                }
            ],
            "_activityBurstId": 9,
        },
        {
            "role": "assistant",
            "content": "Settled final answer remains visible.",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "lifecycle": {"terminal_state": "done"},
                "activity_rows": [
                    {
                        "row_id": "commentary-echo",
                        "role": "prose",
                        "kind": "process_prose",
                        "text": progress,
                        "status": "completed",
                        "display_hint": "main_prose",
                    },
                    {
                        "row_id": "distinct-reasoning",
                        "role": "thinking",
                        "kind": "reasoning",
                        "text": distinct_reasoning,
                        "status": "completed",
                        "display_hint": "collapsed_thinking",
                    },
                    {
                        "row_id": "distinct-tool",
                        "role": "tool",
                        "kind": "tool_call",
                        "text": "terminal",
                        "status": "completed",
                        "tool_call_id": "tool-1",
                        "tool": {
                            "name": "terminal",
                            "call_id": "tool-1",
                            "input": '{"command":"pwd"}',
                            "output": "/isolated/workspace",
                        },
                        "display_hint": "tool_row",
                    },
                ],
            },
        },
    ]

    repo_root = ROOT
    state_tmp = tempfile.TemporaryDirectory(
        prefix="hermes-commentary-render-", ignore_cleanup_errors=True
    )
    state_dir = Path(state_tmp.name)
    artifact_dir = state_dir / "artifacts"
    artifact_dir.mkdir()
    agent_dir = state_dir / "no-agent"
    workspace_dir = state_dir / "workspace"
    agent_dir.mkdir()
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        '"""Test-only agent stub."""\n', encoding="utf-8"
    )
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    for key in (
        "API_SERVER_KEY",
        "HERMES_WEBUI_PASSWORD",
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
    ):
        env.pop(key, None)
    env.update(
        {
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": str(state_dir / "webui-state"),
            "HERMES_HOME": str(state_dir / "hermes-home"),
            "HERMES_BASE_HOME": str(state_dir / "hermes-home"),
            "HERMES_CONFIG_PATH": str(state_dir / "hermes-home" / "config.yaml"),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace_dir),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )

    proc = log = browser = sync = context = None
    try:
        proc, log, _log_path, base_url = _start_webui_server(
            repo_root, env, artifact_dir
        )
        sync = playwright.sync_playwright().start()
        browser = sync.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        errors = _capture_page_errors(page)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)
        page.wait_for_function(
            "() => typeof renderMessages === 'function' && typeof S === 'object'",
            timeout=15000,
        )

        first = page.evaluate(
            """({messages, progress}) => {
              window._chatActivityDisplayMode = 'compact_worklog';
              window._simplifiedToolCalling = true;
              S.busy = false;
              S.activeStreamId = null;
              S.session = {session_id: 'commentary-render'};
              S.messages = messages;
              _sessionHtmlCache.delete('commentary-render');
              _sessionHtmlCacheSid = null;
              renderMessages();
              const visible = document.querySelector('[data-visible-commentary="1"]');
              const group = document.querySelector('[data-anchor-settled-scene-owner="1"]');
              return {
                visibleText: visible?.querySelector('.msg-body')?.innerText.trim() || '',
                visibleHidden: Boolean(visible?.hidden),
                visibleWorklogSource: Boolean(visible?.classList.contains('assistant-segment-worklog-source')),
                visibleCount: document.querySelectorAll('[data-visible-commentary="1"]').length,
                transcriptCount: ((document.querySelector('#msgInner')?.innerText || '').match(new RegExp(progress, 'g')) || []).length,
                deferred: group?.getAttribute('data-worklog-rows-deferred') || '',
                cacheHasSession: _sessionHtmlCache.has('commentary-render'),
              };
            }""",
            {"messages": messages, "progress": progress},
        )
        assert first == {
            "visibleText": progress,
            "visibleHidden": False,
            "visibleWorklogSource": False,
            "visibleCount": 1,
            "transcriptCount": 1,
            "deferred": "1",
            "cacheHasSession": True,
        }

        restored = page.evaluate(
            """({progress}) => {
              // Make this session look like a switch-back so renderMessages takes
              // its real _sessionHtmlCache restoration branch.
              _sessionHtmlCacheSid = 'other-session';
              document.querySelector('#msgInner').innerHTML = '';
              renderMessages();
              const group = document.querySelector('[data-anchor-settled-scene-owner="1"]');
              const wasDeferred = group?.getAttribute('data-worklog-rows-deferred') === '1';
              const materialized = _materializeDeferredWorklogRows(group);
              const rows = Array.from(group?.querySelectorAll('[data-anchor-scene-row="1"]') || []);
              const visible = document.querySelector('[data-visible-commentary="1"]');
              return {
                wasDeferred,
                materialized,
                roles: rows.map(row => row.getAttribute('data-anchor-row-role')),
                rowTexts: rows.map(row => row.innerText.trim()),
                visibleText: visible?.querySelector('.msg-body')?.innerText.trim() || '',
                visibleCount: document.querySelectorAll('[data-visible-commentary="1"]').length,
                progressThinkingCount: Array.from(document.querySelectorAll('[data-anchor-row-role="thinking"],.agent-activity-thinking,.thinking-card-row'))
                  .filter(row => row.innerText.includes(progress)).length,
              };
            }""",
            {"progress": progress},
        )
        assert restored["wasDeferred"] is True
        assert restored["materialized"] is True
        assert restored["visibleText"] == progress
        assert restored["visibleCount"] == 1
        assert restored["progressThinkingCount"] == 0
        assert "prose" not in restored["roles"]
        assert "thinking" in restored["roles"]
        assert "tool" in restored["roles"]
        assert any(distinct_reasoning in text for text in restored["rowTexts"])
        assert any("terminal" in text.lower() for text in restored["rowTexts"])

        final = page.evaluate(
            """({finalAnswer}) => {
              S.session = {session_id: 'commentary-final-answer'};
              S.messages = [
                {role:'user', content:'Finish the restart.'},
                {...S.messages[1], content:finalAnswer, _anchor_activity_scene:null},
              ];
              _sessionHtmlCache.delete('commentary-final-answer');
              _sessionHtmlCacheSid = null;
              renderMessages();
              const visibleBodies = Array.from(document.querySelectorAll('.assistant-segment:not([hidden]) .msg-body'))
                .map(node => node.innerText.trim()).filter(Boolean);
              return {
                visibleBodies,
                commentaryOwners: document.querySelectorAll('[data-visible-commentary="1"]').length,
              };
            }""",
            {"finalAnswer": final_answer},
        )
        assert final["visibleBodies"] == [final_answer]
        assert final["commentaryOwners"] == 0
        assert not errors, errors
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if sync is not None:
            sync.stop()
        _terminate_process(proc)
        if log is not None:
            log.close()
        state_tmp.cleanup()
