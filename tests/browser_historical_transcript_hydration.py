#!/usr/bin/env python3
"""Public browser proof for ID-linked historical tool transcript hydration.

This boots the real WebUI with isolated state, imports a persisted transcript
through the public API, and loads it through the normal browser session path.
The supported OpenAI-compatible declaration/result/final chain must hydrate to
one Anchor Worklog and remain equivalent after hard reload.
"""

from __future__ import annotations

import json
import os
import shutil
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


FINAL_TEXT = "Historical hydration proof final answer."
TOOL_IDS = ("historical-tool-first", "historical-tool-second")
TEST_BITE = os.environ.get("HISTORICAL_HYDRATION_TEST_BITE", "").strip()


def _historical_messages() -> list[dict]:
    first_result_id = (
        "missing-historical-tool-id"
        if TEST_BITE == "break-tool-link"
        else TOOL_IDS[0]
    )
    return [
        {"role": "user", "content": "Inspect both historical files."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": TOOL_IDS[0],
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command":"pwd"}',
                    },
                },
                {
                    "id": TOOL_IDS[1],
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": TOOL_IDS[1],
            "content": "README historical contents",
        },
        {
            "role": "tool",
            "tool_call_id": first_result_id,
            "content": "/isolated/workspace",
        },
        {"role": "assistant", "content": FINAL_TEXT},
    ]


def _assert_hydrated(snapshot: dict) -> None:
    assert snapshot["live"] is False, snapshot
    assert snapshot["groupCount"] == 1, snapshot
    assert snapshot["clientState"]["busy"] is False, snapshot
    rows = [row for row in snapshot["rows"] if row["role"] == "tool"]
    assert [row["tool"] for row in rows] == ["terminal", "read_file"], snapshot
    assert len(rows) == 2, snapshot
    assert sum(FINAL_TEXT in text for text in snapshot["visibleFinal"]) == 1, snapshot
    assert snapshot["transcript"].count(FINAL_TEXT) == 1, snapshot


def _semantic_snapshot(snapshot: dict) -> dict:
    return {
        "tools": [
            {"tool": row["tool"], "text": row["text"]}
            for row in snapshot["rows"]
            if row["role"] == "tool"
        ],
        "final": snapshot["visibleFinal"],
    }


def _import_and_load(page) -> str:
    return page.evaluate(
        """async ({messages, finalText}) => {
          const response = await fetch('/api/session/import', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              title: 'Historical hydration browser proof',
              messages,
            }),
          });
          const data = await response.json();
          if (!response.ok || !data.session || !data.session.session_id) {
            throw new Error(`session import failed: ${JSON.stringify(data)}`);
          }
          await loadSession(data.session.session_id);
          await new Promise((resolve, reject) => {
            const deadline = Date.now() + 10000;
            const check = () => {
              const text = (document.querySelector('#msgInner') || {}).innerText || '';
              if (text.includes(finalText)) return resolve();
              if (Date.now() >= deadline) return reject(new Error('loaded session never rendered final text'));
              setTimeout(check, 50);
            };
            check();
          });
          return data.session.session_id;
        }""",
        {"messages": _historical_messages(), "finalText": FINAL_TEXT},
    )


def main() -> int:
    if TEST_BITE not in {"", "break-tool-link"}:
        raise ValueError(
            f"Unsupported HISTORICAL_HYDRATION_TEST_BITE {TEST_BITE!r}; "
            "expected one of '', 'break-tool-link'"
        )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-historical-hydration-")
    state_dir = Path(state_tmp.name)
    artifact_env = str(os.environ.get("HISTORICAL_HYDRATION_ARTIFACT_DIR") or "").strip()
    artifact_dir_owned = not bool(artifact_env)
    artifact_dir = Path(artifact_env) if artifact_env else Path(
        tempfile.mkdtemp(prefix="hermes-historical-hydration-artifacts-")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = state_dir / "no-agent"
    workspace_dir = state_dir / "workspace"
    agent_dir.mkdir()
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text("\"\"\"Test-only agent stub.\"\"\"\n", encoding="utf-8")
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
    env.update({
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
    })

    proc = log = log_path = playwright = browser = page = None
    errors: list[tuple[str, str]] = []
    exit_code = 1
    try:
        proc, log, log_path, base_url = _start_webui_server(repo_root, env, artifact_dir)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        errors = _capture_page_errors(page)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)
        page.wait_for_function(
            "() => typeof window._autoScrollFollow === 'boolean'",
            timeout=15000,
        )
        session_id = _import_and_load(page)
        assert session_id, "imported session id missing"
        _expand_settled_worklog(page)
        hydrated = _activity_snapshot(page)
        _assert_hydrated(hydrated)
        print("OK  historical load: ID-linked transcript hydrated to one Anchor Worklog")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "text => (document.querySelector('#msgInner') || {}).innerText?.includes(text)",
            arg=FINAL_TEXT,
            timeout=15000,
        )
        _expand_settled_worklog(page)
        reloaded = _activity_snapshot(page)
        _assert_hydrated(reloaded)
        assert _semantic_snapshot(reloaded) == _semantic_snapshot(hydrated), {
            "hydrated": _semantic_snapshot(hydrated),
            "reloaded": _semantic_snapshot(reloaded),
        }
        print("OK  hard reload: historical Anchor Worklog remains semantically identical")
        if errors:
            raise AssertionError(f"unexpected browser errors: {errors!r}")
        context.close()
        browser.close()
        browser = None
        print("\nHISTORICAL TRANSCRIPT HYDRATION GATE PASSED")
        exit_code = 0
        return 0
    except Exception as error:
        print(f"\nHISTORICAL TRANSCRIPT HYDRATION GATE FAILED: {error}", file=sys.stderr)
        try:
            if page is not None:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
                (artifact_dir / "snapshot.json").write_text(
                    json.dumps(
                        {
                            "test_bite": TEST_BITE or None,
                            "browser_errors": errors,
                            "messages": page.evaluate(
                                """() => (S.messages || []).map((message) => ({
                                  role: message && message.role,
                                  content: message && message.content,
                                  tool_call_id: message && message.tool_call_id,
                                  tool_calls: Array.isArray(message && message.tool_calls)
                                    ? message.tool_calls.map((call) => call && call.id)
                                    : null,
                                  has_anchor_scene: Boolean(message && message._anchor_activity_scene),
                                }))"""
                            ),
                            "dom": _activity_snapshot(page),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except Exception as artifact_error:
            print(f"Could not capture browser artifacts: {artifact_error}", file=sys.stderr)
        print(f"Artifacts: {artifact_dir}", file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        _terminate_process(proc)
        if log is not None:
            log.close()
        if proc is not None and proc.returncode not in (None, 0, -15):
            print(f"WebUI server exit code: {proc.returncode}", file=sys.stderr)
        if log_path is not None and log_path.exists() and proc is not None and proc.returncode not in (None, 0, -15):
            print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
        state_tmp.cleanup()
        if artifact_dir_owned and exit_code == 0:
            shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
