#!/usr/bin/env python3
"""Real-server reload proof for preserved reasoning text and durable IDs.

Seed a disposable sidecar through Session.save(), then exercise the real HTTP
loader and unmodified browser renderer. No Agent/provider request is performed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from browser_conversation_lifecycle import (
    _activity_snapshot,
    _capture_page_errors,
    _expand_settled_worklog,
    _start_webui_server,
    _terminate_process,
)

SEED = r"""
from api.models import Session
from api.routes import _assistant_anchor_scene_message_ref
import os
messages = [
    {"role": "user", "content": "Show both recorded reasoning steps.", "_ts": 1},
    {"role": "assistant", "content": "Two distinct reasoning events were recorded.", "_ts": 2},
]
s = Session(session_id="reasoning-proof", title="Reasoning identity proof", profile="default",
            workspace=os.environ["HERMES_WEBUI_DEFAULT_WORKSPACE"], messages=messages)
rows = []
for name in ("a", "b", "a"):
    rows.append({"row_id": "reasoning-row-"+name, "event_id": "reasoning-event-"+name,
                 "local_id": "reasoning-local-"+name, "role": "thinking", "kind": "reasoning",
                 "source_event_type": "reasoning", "status": "completed",
                 "text": "Checking the same condition.",
                 "identity": {"event_id": "reasoning-event-"+name}})
ref = _assistant_anchor_scene_message_ref(messages[-1])
s.anchor_activity_scenes = {ref: {"message_index": 1, "message_ref": ref, "stream_id": "stream-proof",
    "scene": {"version": "activity_scene_v1", "mode": "compact_worklog", "activity_rows": rows,
              "identity": {"session_id": s.session_id, "stream_id": "stream-proof", "run_id": "run-proof"},
              "final_answer": messages[-1]["content"], "terminal_state": "completed"}}}
s.path.parent.mkdir(parents=True, exist_ok=True)
s.save()
"""



MARKDOWN_CASES = {
    "fenced-code": ("```python\nprint(42)\n```", "```python print(42) ```"),
    "indented-code": ("    print(42)\n    print(43)", "print(42)\n    print(43)"),
    "lists": ("- a\n- b\n\n1. c", "- a - b 1. c"),
}

MARKDOWN_SEED = r"""
from api.models import Session
from api.routes import _assistant_anchor_scene_message_ref
import os
transcript = os.environ["HERMES_PROOF_TRANSCRIPT"]
saved = os.environ["HERMES_PROOF_SAVED_PROSE"]
messages = [
    {"role": "user", "content": "Keep the recorded Markdown structure.", "_ts": 1},
    {"role": "assistant", "content": transcript, "_ts": 2,
     "tool_calls": [{"id": "call-proof", "name": "read_file", "output": "Full transcript tool output"}]},
    {"role": "assistant", "content": "Final answer", "_ts": 3},
]
s = Session(session_id="reasoning-proof", title="Transcript Markdown proof", profile="default",
            workspace=os.environ["HERMES_WEBUI_DEFAULT_WORKSPACE"], messages=messages)
row = {"row_id": "saved-prose-row", "event_id": "saved-prose-event", "role": "prose",
       "kind": "process_prose", "source_event_type": "token", "status": "running",
       "stream_id": "stream-proof", "text": saved, "payload": {"text": saved},
       "identity": {"event_id": "saved-prose-event"}}
ref = _assistant_anchor_scene_message_ref(messages[-1])
s.anchor_activity_scenes = {ref: {"message_index": 2, "message_ref": ref, "stream_id": "stream-proof",
    "scene": {"version": "activity_scene_v1", "mode": "compact_worklog", "activity_rows": [row],
              "identity": {"session_id": s.session_id, "stream_id": "stream-proof", "run_id": "run-proof"},
              "final_answer": "Final answer", "terminal_state": "completed"}}}
s.path.parent.mkdir(parents=True, exist_ok=True)
s.save()
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument("--filtered-reasoning", choices=("visible-prose", "final-answer"))
    scenario.add_argument("--no-tool-reasoning", choices=("missing-metadata", "empty-metadata"))
    scenario.add_argument("--transcript-markdown", choices=("fenced-code", "indented-code", "lists"))
    args = parser.parse_args()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    failures = []
    with tempfile.TemporaryDirectory(prefix="hermes-reasoning-proof-") as tmp:
        root = Path(tmp)
        agent, workspace = root / "no-agent", root / "workspace"
        agent.mkdir()
        workspace.mkdir()
        (agent / "run_agent.py").write_text('"""Test-only Agent stub."""\n')
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        for key in (
            "API_SERVER_KEY",
            "HERMES_WEBUI_PASSWORD",
            "HERMES_WEBUI_EXTENSION_DIR",
            "HERMES_WEBUI_EXTENSION_MANIFEST",
        ):
            env.pop(key, None)
        env.update(
            {
                "HERMES_HOME": str(root / "hermes"),
                "HERMES_BASE_HOME": str(root / "hermes"),
                "HERMES_CONFIG_PATH": str(root / "hermes" / "config.yaml"),
                "HERMES_WEBUI_STATE_DIR": str(root / "state"),
                "HERMES_WEBUI_AGENT_DIR": str(agent),
                "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
                "HERMES_WEBUI_HOST": "127.0.0.1",
                "HERMES_WEBUI_SKIP_ONBOARDING": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        seed_source = SEED
        expected_row_ids = ["reasoning-row-a", "reasoning-row-b"]
        expected_thoughts = [
            ("reasoning-event-a", "Checking the same condition.", "completed"),
            ("reasoning-event-b", "Checking the same condition.", "completed"),
        ]
        if args.transcript_markdown:
            transcript, saved_prose = MARKDOWN_CASES[args.transcript_markdown]
            env.update(HERMES_PROOF_TRANSCRIPT=transcript, HERMES_PROOF_SAVED_PROSE=saved_prose)
            seed_source = MARKDOWN_SEED
            expected_row_ids = []
            expected_thoughts = []
        if args.no_tool_reasoning:
            metadata = ', "reasoning_content": ""' if args.no_tool_reasoning == "empty-metadata" else ""
            seed_source = seed_source.replace(
                '    {"role": "assistant", "content": "Two distinct reasoning events were recorded.", "_ts": 2},',
                '    {"role": "assistant", "content": [{"type": "thinking", "thinking": "Transcript thought"}, '
                '{"type": "text", "text": "Final answer"}]' + metadata + ', "_ts": 2},',
            ).replace('("a", "b", "a")', '("saved",)').replace(
                '"reasoning-row-"+name', '"saved-row"'
            ).replace('"reasoning-event-"+name', '"saved-event"').replace(
                '"status": "completed"', '"stream_id": "stream-proof", "status": "running"'
            ).replace('"Checking the same condition."', '"Distinct saved thought"')
            expected_row_ids = ["saved-row"]
            expected_thoughts = [("saved-event", "Distinct saved thought", "completed")]
        if args.filtered_reasoning:
            final = "Two distinct reasoning events were recorded."
            reasoning = "Visible progress" if args.filtered_reasoning == "visible-prose" else final
            seed_source = seed_source.replace(
                '    {"role": "assistant", "content": "Two distinct reasoning events were recorded.", "_ts": 2},',
                f'    {{"role": "assistant", "content": "Visible progress", "reasoning_content": {reasoning!r}, "_ts": 2}},\n'
                f'    {{"role": "assistant", "content": {final!r}, "_ts": 3}},',
            ).replace('"message_index": 1', '"message_index": 2').replace(
                '"status": "completed"', '"stream_id": "stream-proof", "status": "running"'
            )
        seed = subprocess.run(
            [sys.executable, "-c", seed_source],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if seed.returncode:
            raise RuntimeError("Disposable session seed failed: " + seed.stderr[-2000:])
        proc = log = None
        try:
            proc, log, _, url = _start_webui_server(repo, env, args.artifact_dir)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                for width in (1280, 390):
                    context = browser.new_context(
                        base_url=url, viewport={"width": width, "height": 900}
                    )
                    # Match the existing full-app browser gates: public static assets
                    # may load normally; the isolated server has no provider credentials.
                    page = context.new_page()
                    errors = _capture_page_errors(page)
                    page.goto("/", wait_until="domcontentloaded")
                    page.wait_for_selector("#msg", timeout=15000)
                    page.wait_for_function(
                        "() => typeof window._autoScrollFollow === 'boolean'",
                        timeout=15000,
                    )
                    page.evaluate(
                        "async () => { await loadSession('reasoning-proof'); }"
                    )
                    observations = []
                    for phase in ("load", "reload"):
                        if phase == "reload":
                            page.reload(wait_until="domcontentloaded")
                            page.wait_for_function(
                                "() => typeof S !== 'undefined' && S.session?.session_id === 'reasoning-proof' && S.messages?.some(m => m._anchor_activity_scene)",
                                timeout=15000,
                            )
                        if args.filtered_reasoning or args.no_tool_reasoning:
                            # Missing Thinking rows are the intended negative
                            # result; capture it rather than timing out waiting
                            # for a worklog that the broken baseline omitted.
                            page.evaluate("""() => {
                                for (const group of document.querySelectorAll(
                                    '.assistant-turn [data-anchor-settled-scene-owner="1"]'
                                )) {
                                    const summary = group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
                                    if (group.classList.contains('tool-call-group-collapsed') && summary) {
                                        _toggleActivityGroup(summary);
                                    }
                                    if (group.getAttribute('data-worklog-rows-deferred') === '1') {
                                        _materializeDeferredWorklogRows(group);
                                    }
                                }
                            }""")
                        else:
                            _expand_settled_worklog(page)
                        if args.no_tool_reasoning:
                            for header in page.locator('.thinking-card:not(.open) .thinking-card-header').all():
                                header.click()
                        snap = _activity_snapshot(page)
                        rows = [
                            row for row in snap["rows"] if row["role"] == "thinking"
                        ]
                        scene_thinking = page.evaluate("""() => S.messages.flatMap(message =>
                            (message._anchor_activity_scene?.activity_rows || [])
                                .filter(row => row.role === 'thinking')
                                .map(row => [row.event_id, row.text, row.status]))""")
                        markdown = None
                        markdown_valid = True
                        if args.transcript_markdown:
                            markdown = page.evaluate("""texts => {
                                const prose = document.querySelector('[data-anchor-row-id="saved-prose-row"]');
                                const reference = document.createElement('div');
                                reference.innerHTML = renderMd(texts[0]);
                                return {
                                    sceneProse: S.messages.flatMap(message =>
                                        (message._anchor_activity_scene?.activity_rows || [])
                                            .filter(row => row.role === 'prose')
                                            .map(row => [row.event_id, row.row_id, row.text, row.payload?.text])),
                                    rawText: prose?.dataset.rawText,
                                    referenceHtml: reference.innerHTML,
                                    renderedHtml: prose?.querySelector('.msg-body')?.innerHTML,
                                    referenceCodeBlocks: Array.from(reference.querySelectorAll('pre code')).map(el => el.textContent),
                                    codeBlocks: Array.from(prose?.querySelectorAll('pre code') || []).map(el => el.textContent),
                                    unordered: Array.from(prose?.querySelectorAll('ul > li') || []).map(el => el.textContent.trim()),
                                    ordered: Array.from(prose?.querySelectorAll('ol > li') || []).map(el => el.textContent.trim()),
                                };
                            }""", MARKDOWN_CASES[args.transcript_markdown])
                            transcript = MARKDOWN_CASES[args.transcript_markdown][0]
                            markdown_valid = (
                                markdown["sceneProse"] == [["saved-prose-event", "saved-prose-row", transcript, transcript]]
                                and markdown["rawText"] == (transcript.strip() if args.transcript_markdown == "indented-code" else transcript)
                                and snap["visibleFinal"] == ["Final answer"]
                                and len([row for row in snap["rows"] if row["role"] == "tool"]) == 1
                                and (
                                    markdown["codeBlocks"] == ["print(42)"]
                                    if args.transcript_markdown == "fenced-code"
                                    else (markdown["codeBlocks"] == markdown["referenceCodeBlocks"]
                                          and markdown["renderedHtml"] == markdown["referenceHtml"])
                                    if args.transcript_markdown == "indented-code"
                                    else markdown["unordered"] == ["a", "b"] and markdown["ordered"] == ["c"]
                                )
                            )
                        page.screenshot(
                            path=str(args.artifact_dir / f"{width}-{phase}.png"),
                            full_page=True,
                        )
                        observations.append(
                            {"phase": phase, "rows": rows, "scene_thinking": scene_thinking, "markdown": markdown, "snapshot": snap}
                        )
                        if (
                            [r["rowId"] for r in rows] != expected_row_ids
                            or scene_thinking != [list(thought) for thought in expected_thoughts]
                            or (args.no_tool_reasoning and snap["visibleFinal"] != ["Final answer"])
                            or not markdown_valid
                        ):
                            failures.append(
                                {"width": width, "phase": phase, "rows": rows, "scene_thinking": scene_thinking, "markdown": markdown}
                            )
                    (args.artifact_dir / f"{width}.json").write_text(
                        json.dumps(observations, indent=2)
                    )
                    if errors:
                        failures.append({"width": width, "browser_errors": errors})
                    context.close()
                browser.close()
        finally:
            _terminate_process(proc)
            if log is not None:
                log.close()
    assert not failures, json.dumps(failures, indent=2)
    print("REASONING HYDRATION: desktop/narrow load + hard reload passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
