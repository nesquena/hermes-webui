#!/usr/bin/env python3
"""Real-server reload proof for stored equal-text reasoning with distinct IDs.

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


def main() -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
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
        seed = subprocess.run(
            [sys.executable, "-c", SEED],
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
                        _expand_settled_worklog(page)
                        snap = _activity_snapshot(page)
                        rows = [
                            row for row in snap["rows"] if row["role"] == "thinking"
                        ]
                        page.screenshot(
                            path=str(args.artifact_dir / f"{width}-{phase}.png"),
                            full_page=True,
                        )
                        observations.append(
                            {"phase": phase, "rows": rows, "snapshot": snap}
                        )
                        if [r["rowId"] for r in rows] != [
                            "reasoning-row-a",
                            "reasoning-row-b",
                        ]:
                            failures.append(
                                {"width": width, "phase": phase, "rows": rows}
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
