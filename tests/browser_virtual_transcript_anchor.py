#!/usr/bin/env python3
"""Real-browser proof for the virtual-transcript viewport anchor (#8228).

The first round of this PR shipped a fast anchor path that was inert in
production: its cache-accumulation estimate drifted for rows above the render
window, the correctness gate then always fell back to the per-row scan, and
the mocked unit test could not tell — the mock's cache was always "complete"
and the arithmetic always landed exactly on the expected row.

This gate boots the REAL WebUI, imports a 120-message transcript through the
public API, turns transcript virtualization on through the page, scrolls to
three positions, and at every position proves against the live DOM:

  1. the fast path returns the SAME row as an independent reference predicate
     (first rendered row whose bottom edge crosses the container's top edge),
  2. the fast path performs ZERO fallback per-row reads
     (window.__vmAnchorFallbackRowReads stays 0) — an inert fast path re-reads
     rows through the fallback scan and red-flags immediately, which is what
     the mocked test failed to catch,
  3. with the fast path disabled, the per-row scan returns the same anchor and
     the fallback counter does bite (>= 1).

A final full-render (virtualization off) pass repeats 1-3, because the binary
search must work in both modes with no height-cache dependency.

The gate runs this proof on TWO transcripts: a plain question/answer one, and
a tool-call transcript where every turn's tool-call assistant message renders
a hidden `assistant-segment-worklog-source` row (display:none!important, all-
zero rect) still carrying data-msg-idx — the exact CORE-1 regression shape the
#8228 re-review reproduced (18 of 20 fast-anchor picks diverged from the
linear reference). The reference predicate skips those hidden rows, so the
gate proves the fast path lands on the same laid-out row as the per-row scan
with the hidden rows interleaved, and asserts the transcript actually produced
them (hidden > 0).

Exit 0 = proof passed. Non-zero = a real-browser failure; a screenshot and a
snapshot are written to the artifact directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from browser_conversation_lifecycle import (
    _capture_page_errors,
    _start_webui_server,
    _terminate_process,
)

MESSAGE_PAIRS = 60  # 120 renderable messages: past the 80-row virtualization threshold
LAST_TEXT = f"Answer {MESSAGE_PAIRS}"


def _long_transcript_messages() -> list[dict]:
    messages: list[dict] = []
    for i in range(1, MESSAGE_PAIRS + 1):
        messages.append(
            {
                "role": "user",
                "content": f"Question {i}: " + ("context line for a tall user row. " * 8),
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": f"Answer {i}: "
                + ("A fairly long answer paragraph so every row is tall enough to scroll. " * 8),
            }
        )
    return messages


def _tool_call_transcript_messages() -> list[dict]:
    """Tool-call transcript: every turn has a tool-call assistant message with
    NO visible text, so in the DEFAULT compact_worklog mode each one renders a
    hidden `assistant-segment-worklog-source` row that still carries
    data-msg-idx (display:none!important, all-zero rect) — the exact CORE-1
    shape the #8228 re-review reproduced (18 of 20 fast-anchor picks diverged
    from the linear reference on a 150-turn tool transcript). The gate must
    prove the binary search skips those rows and lands on the same laid-out
    row as the per-row scan."""
    messages: list[dict] = []
    for i in range(1, MESSAGE_PAIRS + 1):
        messages.append(
            {
                "role": "user",
                "content": f"Question {i}: " + ("context line for a tall user row. " * 8),
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "shell", "arguments": json.dumps({"cmd": f"echo {i}"})},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": f"Result {i}: " + ("a fairly long tool output line. " * 12),
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": f"Answer {i}: "
                + ("A fairly long answer paragraph so every row is tall enough to scroll. " * 8),
            }
        )
    return messages


_IMPORT_AND_LOAD_JS = """
async ({messages, lastText}) => {
  const response = await fetch('/api/session/import', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: 'Virtual transcript anchor browser proof', messages}),
  });
  const data = await response.json();
  if (!response.ok || !data.session || !data.session.session_id) {
    throw new Error('session import failed: ' + JSON.stringify(data));
  }
  const sid = data.session.session_id;
  await loadSession(sid);
  // #8228 greptile P2: loadSession lands on a BOUNDED TAIL (msg_limit=30).
  // MESSAGE_VIRTUAL_THRESHOLD_ROWS is 80, so a 30-row window never virtualizes:
  // every row renders and the old `rowCount < len(messages)` check passed only
  // because the transcript was truncated. Pull the COMPLETE transcript through
  // the documented msg_limit=all escape hatch (the same shape the outline jump
  // path uses) before the gate turns virtualization on.
  const full = await fetch(
    '/api/session?session_id=' + encodeURIComponent(sid) + '&messages=1&resolve_model=0&msg_limit=all'
  ).then((r) => r.json());
  if (full && full.session && Array.isArray(full.session.messages) && full.session.messages.length) {
    S.messages = full.session.messages;
    renderMessages({preserveScroll: false});
  }
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000;
    const check = () => {
      const text = (document.querySelector('#msgInner') || {}).innerText || '';
      if (text.includes(lastText)) return resolve();
      if (Date.now() >= deadline) return reject(new Error('loaded session never rendered'));
      setTimeout(check, 50);
    };
    check();
  });
  return sid;
}
"""

# Fast vs reference vs fallback at the current scroll position. Runs as one
# synchronous block so no rAF/scroll handler can interleave between the
# counter reset and the read (JS single thread).
_ANCHOR_PROBE_JS = """
() => {
  const container = document.getElementById('messages');
  const containerRect = container.getBoundingClientRect();
  const rows = container.querySelectorAll('[data-msg-idx]');
  // Independent reference: first LAID-OUT row whose bottom edge crosses the
  // container's top edge (the definition the per-row scan implements). Hidden
  // display:none rows (.assistant-segment-worklog-source / -anchor) have
  // all-zero rects and must be skipped — on a tool-call transcript in the
  // default compact_worklog mode they interleave with visible rows, which is
  // exactly the #8228 CORE-1 regression an unfiltered search hit.
  let refIdx = null;
  for (const row of rows) {
    if (row.classList.contains('assistant-segment-worklog-source')) continue;
    if (row.classList.contains('assistant-segment-anchor')) continue;
    const r = row.getBoundingClientRect();
    if (r.bottom > containerRect.top + 1) { refIdx = Number(row.dataset.msgIdx); break; }
  }
  // Fast path (default on): zero fallback reads expected.
  window.__vmAnchorFallbackRowReads = 0;
  const fast = _captureMessageViewportAnchor();
  const fastReads = window.__vmAnchorFallbackRowReads || 0;
  // Per-row fallback: same anchor expected, counter must bite.
  window.__vmFastAnchor = false;
  window.__vmAnchorFallbackRowReads = 0;
  const slow = _captureMessageViewportAnchor();
  const slowReads = window.__vmAnchorFallbackRowReads || 0;
  window.__vmFastAnchor = true;
  const pick = (a) => a ? {rawIdx: a.rawIdx, sessionIdx: a.sessionIdx, key: a.key} : null;
  return {
    refIdx,
    fast: pick(fast),
    fastReads,
    slow: pick(slow),
    slowReads,
    rowCount: rows.length,
    hiddenCount: document.querySelectorAll(
      '#messages [data-msg-idx].assistant-segment-worklog-source,' +
      '#messages [data-msg-idx].assistant-segment-anchor'
    ).length,
    scrollTop: Math.round(container.scrollTop),
    scrollHeight: Math.round(container.scrollHeight),
  };
}
"""


def _scroll_to(page, fraction: float) -> None:
    page.evaluate(
        """(fraction) => {
          const container = document.getElementById('messages');
          const max = Math.max(0, container.scrollHeight - container.clientHeight);
          container.scrollTop = Math.round(max * fraction);
          container.dispatchEvent(new Event('scroll'));
        }""",
        fraction,
    )
    # Let the scheduled virtualized re-render settle before probing.
    page.wait_for_timeout(500)


def _assert_anchor_proof(page, label: str, *, expect_virtualized: bool, renderable_rows: int) -> dict:
    probe = page.evaluate(_ANCHOR_PROBE_JS)
    assert probe["refIdx"] is not None, f"{label}: reference predicate found no covering row"
    assert probe["fast"] is not None, f"{label}: fast anchor returned null"
    assert probe["fast"]["rawIdx"] == probe["refIdx"], (
        f"{label}: fast anchor picked row {probe['fast']['rawIdx']} but the reference "
        f"predicate picks {probe['refIdx']} ({probe})"
    )
    assert probe["fastReads"] == 0, (
        f"{label}: fast path performed {probe['fastReads']} fallback per-row reads — "
        f"the fast path is inert ({probe})"
    )
    assert probe["slow"] is not None, f"{label}: fallback anchor returned null"
    assert probe["slow"]["rawIdx"] == probe["refIdx"], (
        f"{label}: fallback picked {probe['slow']['rawIdx']} but the reference picks "
        f"{probe['refIdx']} ({probe})"
    )
    assert probe["slowReads"] >= 1, f"{label}: fallback counter did not bite ({probe})"
    if expect_virtualized:
        assert probe["rowCount"] < renderable_rows, (
            f"{label}: virtualization is not active — all {probe['rowCount']} rows rendered "
            f"(renderable rows: {renderable_rows})"
        )
        assert probe["scrollTop"] > 0, f"{label}: scrolled position did not stick ({probe})"
    return probe


def _renderable_row_count(messages: list[dict]) -> int:
    """How many transcript rows these messages can actually mount.

    ``role: 'tool'`` payloads are folded into their assistant segment's
    worklog and never produce their own ``data-msg-idx`` row, so a
    240-message tool transcript only ever mounts 180 rows. Comparing
    ``rowCount`` against ``len(messages)`` therefore proved nothing: the
    virtualized assertion passed even with virtualization switched off
    (#8228 greptile P2).
    """
    return sum(1 for m in messages if str(m.get("role") or "") != "tool")


def _run_transcript_proof(page, messages: list[dict], last_text: str, label: str, *, expect_hidden: bool) -> None:
    renderable_rows = _renderable_row_count(messages)
    session_id = page.evaluate(
        _IMPORT_AND_LOAD_JS,
        {"messages": messages, "lastText": last_text},
    )
    assert session_id, f"{label}: imported session id missing"
    loaded_rows = page.evaluate(
        "() => (typeof S !== 'undefined' && Array.isArray(S.messages)) ? S.messages.length : -1"
    )
    assert loaded_rows >= renderable_rows, (
        f"{label}: the client holds {loaded_rows} of {renderable_rows} renderable rows — the "
        "transcript is still on a bounded tail, so every virtualization assertion below "
        "would be vacuous (#8228 greptile P2)"
    )

    # Turn transcript virtualization on through the page and stop following
    # the tail so a programmatic scroll sticks.
    page.evaluate(
        """() => {
          window._autoScrollFollow = false;
          window._virtualizeTranscript = true;
          renderMessages({preserveScroll: true});
        }"""
    )
    page.wait_for_timeout(600)

    if expect_hidden:
        hidden = page.evaluate(
            """() => document.querySelectorAll(
              '#messages [data-msg-idx].assistant-segment-worklog-source,' +
              '#messages [data-msg-idx].assistant-segment-anchor'
            ).length"""
        )
        assert hidden > 0, (
            f"{label}: tool-call transcript produced no hidden worklog rows — "
            f"the gate is not exercising the CORE-1 regression shape"
        )
        print(f"OK  {label}: {hidden} hidden display:none rows interleaved with laid-out rows")

    for fraction, pos in ((0.25, "quarter"), (0.5, "middle"), (0.75, "three-quarter")):
        _scroll_to(page, fraction)
        probe = _assert_anchor_proof(
            page, f"{label}/virtualized/{pos}", expect_virtualized=True, renderable_rows=renderable_rows
        )
        print(
            f"OK  {label} virtualized {pos}: fast anchor == reference (row {probe['refIdx']}), "
            f"0 fallback reads, {probe['rowCount']} rendered rows"
        )

    # Full-render mode: both paths must still agree (no cache dependency).
    page.evaluate(
        """() => {
          window._virtualizeTranscript = false;
          renderMessages({preserveScroll: true});
        }"""
    )
    page.wait_for_timeout(600)
    _scroll_to(page, 0.5)
    full = _assert_anchor_proof(
        page, f"{label}/full-render/middle", expect_virtualized=False, renderable_rows=renderable_rows
    )
    assert full["rowCount"] >= renderable_rows, (
        f"{label}: full-render mode mounted {full['rowCount']} of {renderable_rows} renderable rows — "
        "virtualization is still on, so this pass checked nothing"
    )
    print(
        f"OK  {label} full-render mode: fast anchor identical to the per-row scan "
        f"({full['rowCount']}/{renderable_rows} rows mounted)"
    )


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-virtual-anchor-")
    state_dir = Path(state_tmp.name)
    artifact_env = str(os.environ.get("VIRTUAL_ANCHOR_ARTIFACT_DIR") or "").strip()
    artifact_dir_owned = not bool(artifact_env)
    artifact_dir = Path(artifact_env) if artifact_env else Path(
        tempfile.mkdtemp(prefix="hermes-virtual-anchor-artifacts-")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = state_dir / "no-agent"
    workspace_dir = state_dir / "workspace"
    agent_dir.mkdir()
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text('"""Test-only agent stub."""\n', encoding="utf-8")
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
        # Both transcripts prove the anchor: the plain one (baseline coverage)
        # and the tool-call one (hidden worklog-source rows interleaved — the
        # #8228 CORE-1 regression shape the maintainer asked the gate to add).
        _run_transcript_proof(page, _long_transcript_messages(), LAST_TEXT, "plain", expect_hidden=False)
        _run_transcript_proof(
            page, _tool_call_transcript_messages(), LAST_TEXT, "tool-call", expect_hidden=True
        )

        if errors:
            raise AssertionError(f"unexpected browser errors: {errors!r}")
        context.close()
        browser.close()
        browser = None
        print("\nVIRTUAL TRANSCRIPT ANCHOR GATE PASSED")
        exit_code = 0
        return 0
    except Exception as error:
        print(f"\nVIRTUAL TRANSCRIPT ANCHOR GATE FAILED: {error}", file=sys.stderr)
        try:
            if page is not None:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
                (artifact_dir / "snapshot.json").write_text(
                    json.dumps(
                        {
                            "browser_errors": errors,
                            "virtualize": page.evaluate("() => window._virtualizeTranscript"),
                            "scroll": page.evaluate(
                                """() => {
                                  const c = document.getElementById('messages');
                                  return c ? {top: Math.round(c.scrollTop), height: Math.round(c.scrollHeight),
                                              clientHeight: c.clientHeight} : null;
                                }"""
                            ),
                            "rendered_rows": page.evaluate(
                                "() => document.querySelectorAll('#messages [data-msg-idx]').length"
                            ),
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
