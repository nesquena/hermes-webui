#!/usr/bin/env python3
"""#8643 (behaviour 1): a running turn's Regenerate / Undo / Edit are dead buttons.

WHY THIS EXISTS
  `static/ui.js` paints the message action bar from `isLastAssistant` /
  `isEditableUser` alone. While a turn runs, the last visible row IS an
  assistant row, so Regenerate sits on it, and the last user row keeps its
  Edit. But every one of those actions refuses on `S.busy`
  (`editMessage`, `regenerateResponse`, `submitEdit`), so the tap did nothing
  and said nothing. On a phone the footer is always at full opacity
  (`.msg-foot .msg-actions{opacity:1}`), so no hover is needed to reach a
  button that cannot work.

  The maintainer's ask: hide (or disable with a tooltip) Regenerate, Undo and
  Edit while `S.busy`, and offer Regenerate only on the turn's final assistant
  message.

WHAT THIS CHECKS
  A persisted turn with an interim answer and a final answer, loaded through
  the normal browser session path. All three states are read inside ONE
  synchronous page evaluation — idle, then `S.busy = true` + `renderMessages()`,
  then back to idle — because a background sidebar refresh
  (`_reconcileActiveSessionIdleStateFromList`) clears `S.busy` on its own a
  few hundred milliseconds later, and the symptom is what the render paints
  while the turn is running.

  - idle: exactly one Regenerate, on the FINAL assistant segment of the turn
    and none on the interim one; one Edit on the last user row; Copy is still
    there, so the transcript itself rendered.
  - busy: no Regenerate and no Edit anywhere, while the transcript still
    renders (Copy survives, the final answer is still on screen) — the buttons
    are gone, not the messages.
  - idle again: both come back, so the gate is the run, not the row.

  Undo is pinned at the source instead (see
  tests/test_issue8643_busy_message_actions.py): the `undoBtn` const in
  renderMessages() is never inserted into `footHtml`, so no Undo button exists
  in the DOM to observe in any state.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state, and a transcript imported through the public
  API. `HERMES_DISABLE_LAZY_INSTALLS=1` is forced because there is no agent
  here: without it the server relaunches into a managed runtime that owes a
  full dependency sync before it can serve /health (tests/conftest.py sets the
  same variable for its own server).

USAGE
  python tests/browser_8643_busy_message_actions.py

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser_conversation_lifecycle import (  # noqa: E402
    _capture_page_errors,
    _start_webui_server,
    _terminate_process,
)


INTERIM_TEXT = "Interim answer: checking the workspace first."
FINAL_TEXT = "Final answer for the running-turn action buttons."
USER_TEXT = "Run the two-step job."

MESSAGES = [
    {"role": "user", "content": USER_TEXT},
    {
        "role": "assistant",
        "content": INTERIM_TEXT,
        "tool_calls": [
            {
                "id": "busy-actions-tool-1",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "busy-actions-tool-1", "content": "/workspace"},
    {"role": "assistant", "content": FINAL_TEXT},
]

# The tool-result row is not a renderable message, so the visible transcript is
# user(0) / interim assistant(1) / final assistant(3).
FINAL_ASSISTANT_IDX = "3"
INTERIM_ASSISTANT_IDX = "1"
LAST_USER_IDX = "0"


IMPORT_AND_LOAD_JS = """async ({messages, finalText}) => {
  const response = await fetch('/api/session/import', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: 'Running-turn action buttons', messages}),
  });
  const data = await response.json();
  if (!response.ok || !data.session || !data.session.session_id) {
    throw new Error(`session import failed: ${JSON.stringify(data)}`);
  }
  await loadSession(data.session.session_id);
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000;
    const check = () => {
      const text = (document.querySelector('#msgInner') || {}).innerText || '';
      if (text.includes(finalText)) return resolve();
      if (Date.now() >= deadline) return reject(new Error('loaded session never rendered the final answer'));
      setTimeout(check, 50);
    };
    check();
  });
  return data.session.session_id;
}
"""


# The action buttons, by the handler each one wires, per rendered row, plus the
# flag the render was supposed to be reading. One row is an assistant segment
# (`.assistant-segment[data-msg-idx]`) or a user row (`.msg-row[data-role]`).
ACTION_BUTTONS_JS = """() => {
  const rowsWith = (selector, needle) => Array.from(document.querySelectorAll(selector))
    .filter(row => row.querySelector('.msg-foot'))
    .map(row => ({
      idx: row.dataset.msgIdx,
      has: !!row.querySelector(`button[onclick*="${needle}"]`),
    }));
  const assistant = rowsWith('.assistant-segment[data-msg-idx]', 'regenerateResponse(this)');
  const users = rowsWith('.msg-row[data-role="user"]', 'editMessage(this)');
  const copy = Array.from(document.querySelectorAll('.assistant-segment[data-msg-idx] .msg-foot'))
    .filter(foot => foot.querySelector('button[onclick*="copyMsg(this)"]')).length;
  return {
    busy: !!S.busy,
    regenerate: assistant.filter(row => row.has).map(row => row.idx),
    assistantRows: assistant.map(row => row.idx),
    edit: users.filter(row => row.has).map(row => row.idx),
    copyCount: copy,
    renderedText: ((document.querySelector('#msgInner') || {}).innerText || '').slice(0, 200),
  };
}
"""

# Idle, busy, idle again — in one synchronous evaluation, so no background
# refresh can clear S.busy between the render and the reading of the DOM.
THREE_STATES_JS = """() => {
  const probe = %s;
  const idle = probe();
  S.busy = true;
  renderMessages();
  const busy = probe();
  S.busy = false;
  renderMessages();
  const restored = probe();
  return {idle, busy, restored};
}
""" % ACTION_BUTTONS_JS


def _failures_idle(state, label):
    problems = []
    if state["busy"]:
        problems.append(f"{label}: read with S.busy={state['busy']!r}, expected an idle render")
    if state["regenerate"] != [FINAL_ASSISTANT_IDX]:
        problems.append(
            f"{label}: Regenerate sits on rows {state['regenerate']}, expected only the turn's"
            f" final assistant row [{FINAL_ASSISTANT_IDX}]"
        )
    if INTERIM_ASSISTANT_IDX in state["regenerate"]:
        problems.append(
            f"{label}: Regenerate is offered on the interim answer (row {INTERIM_ASSISTANT_IDX})"
        )
    if state["edit"] != [LAST_USER_IDX]:
        problems.append(
            f"{label}: Edit sits on rows {state['edit']}, expected only the last user row"
            f" [{LAST_USER_IDX}]"
        )
    if not state["copyCount"]:
        problems.append(f"{label}: no Copy button rendered — the transcript did not render")
    if FINAL_TEXT not in state["renderedText"]:
        problems.append(f"{label}: the final answer is not on screen — the transcript did not render")
    return problems


def _failures_busy(state):
    problems = []
    if not state["busy"]:
        problems.append(f"busy: read with S.busy={state['busy']!r}, expected a running-turn render")
    for name in ("regenerate", "edit"):
        if state[name]:
            problems.append(
                f"busy: {name} button(s) still in the DOM on rows {state[name]} — a running turn"
                f" leaves dead buttons on screen"
            )
    if not state["copyCount"]:
        problems.append("busy: no Copy button rendered — the transcript itself is missing")
    if FINAL_TEXT not in state["renderedText"]:
        problems.append("busy: the final answer is no longer rendered — the transcript went away")
    return problems


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-8643-busy-actions-")
    state_dir = Path(state_tmp.name)
    agent_dir = state_dir / "no-agent"
    workspace_dir = state_dir / "workspace"
    agent_dir.mkdir()
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text('"""Test-only agent stub."""\n', encoding="utf-8")

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    for key in ("API_SERVER_KEY", "HERMES_WEBUI_PASSWORD"):
        env.pop(key, None)
    # No agent here, so keep the server from relaunching into a managed runtime
    # that owes a full dependency sync before /health answers (conftest.py does
    # the same for its own server).
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    env.update({
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state_dir / "webui-state"),
        "HERMES_HOME": str(state_dir / "hermes-home"),
        "HERMES_BASE_HOME": str(state_dir / "hermes-home"),
        "HERMES_CONFIG_PATH": str(state_dir / "hermes-home" / "config.yaml"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace_dir),
    })

    failures: list[str] = []
    proc = log = log_path = None
    try:
        proc, log, log_path, base_url = _start_webui_server(repo_root, env, state_dir)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(base_url=base_url)
            page = context.new_page()
            errors = _capture_page_errors(page)
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_selector("#msg", state="visible", timeout=15000)
            page.wait_for_function(
                "() => typeof window._autoScrollFollow === 'boolean'", timeout=15000
            )
            page.evaluate(IMPORT_AND_LOAD_JS, {"messages": MESSAGES, "finalText": FINAL_TEXT})
            states = page.evaluate(THREE_STATES_JS)

            idle_problems = _failures_idle(states["idle"], "idle")
            failures += idle_problems
            if not idle_problems:
                print("OK  idle — Regenerate only on the turn's final assistant row,"
                      " Edit on the last user row")

            busy_problems = _failures_busy(states["busy"])
            failures += busy_problems
            if not busy_problems:
                print("OK  busy — Regenerate and Edit are gone while the turn runs")

            restored_problems = _failures_idle(states["restored"], "idle again")
            failures += restored_problems
            if not restored_problems:
                print("OK  idle again — both buttons return once the turn is over")

            # The turn-final rule must hold in every state, busy or not.
            for label, state in states.items():
                if INTERIM_ASSISTANT_IDX in state["regenerate"]:
                    failures.append(
                        f"{label}: Regenerate is offered on the interim answer"
                        f" (row {INTERIM_ASSISTANT_IDX})"
                    )

            failures += [f"pageerror: {kind} {text}" for kind, text in errors]
            context.close()
            browser.close()
    except Exception as error:  # noqa: BLE001 — reported as a gate failure
        failures.append(f"driver error: {type(error).__name__}: {error}")
        if log_path is not None and Path(log_path).exists():
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
            failures.append("last server log tail:\n" + tail)
    finally:
        if proc is not None:
            _terminate_process(proc)
        if log is not None:
            log.close()
        state_tmp.cleanup()

    if failures:
        print("\nRUNNING-TURN ACTION BUTTONS GATE FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("\nRUNNING-TURN ACTION BUTTONS GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
