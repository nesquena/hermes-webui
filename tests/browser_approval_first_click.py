#!/usr/bin/env python3
"""Browser gate for #7091: the approval card must answer on the FIRST click.

Bug: on the local in-process backend the approval card's transport
`approval_id` is written by whichever producer rendered last (the chat-stream
SSE 'approval' event or the 1.5s fallback poll). When the server head advanced
between render and click, the card holds a stale id and the first click is
rejected by the #527 guard ("Approval response not accepted."); only the next
poll tick (~1.5-2s) re-syncs the card. The dismiss (X) button has the same
identity-desynchronization: it marks only the render-time id, so the next poll
re-renders the card and the X appears to do nothing.

Fix (client-side only, #527 guard untouched): `respondApproval` and
`dismissApprovalCard` reconcile the card against the authoritative
`/api/approval/pending` head before acting, using a LOGICAL identity (the
displayed description/command + the request/run/mirror ownership fields, minus
the volatile `approval_id`):

- same logical approval, stale transport id -> adopt the head's id and
  complete the original click once;
- different logical approval (queued A -> B while A is still visible) -> the
  click is NEVER transferred; the card re-renders to the head and a fresh
  deliberate click is required;
- nothing pending -> orphan card clears (server `stale_cleared` path).

This gate drives the REAL frontend functions (`showApprovalForSession`,
`respondApproval`, `dismissApprovalCard`) in a real Chromium against a real
WebUI server with isolated state, interleaving the two producers exactly as the
issue describes: the card is rendered from a stale snapshot while the server
head has already advanced. Approvals are seeded through the loopback-only
`/api/approval/inject_test` endpoint (the documented automated-test hook).

Run:
    python tests/browser_approval_first_click.py

Artifacts (screenshots + server log) land in $APPROVAL_ARTIFACT_DIR or a
temp dir; the run exits non-zero with the failure screenshot on any failure.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

NOT_ACCEPTED_TOAST = "Approval response not accepted."


# ── server boot scaffolding (same shape as browser_conversation_lifecycle.py) ─


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    base_url: str,
    timeout: float = 30.0,
    proc: subprocess.Popen | None = None,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def _terminate_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=8)


def _start_webui_server(repo_root: Path, env: dict, artifact_dir: Path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    run_env = dict(env)
    run_env["HERMES_WEBUI_PORT"] = str(port)
    log_path = artifact_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(repo_root / "server.py")],
        cwd=repo_root,
        env=run_env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    if _wait_for_health(base_url, proc=proc):
        return proc, log, log_path, base_url
    _terminate_process(proc)
    log.close()
    tail = ""
    if log_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    raise RuntimeError(f"WebUI server did not become healthy; log tail:\n{tail}")


def _post_json(base_url: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read(1024 * 1024))


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read(1024 * 1024))


# ── approval API helpers against the real server ─────────────────────────────


def _inject_approval(base_url: str, sid: str, command: str, key: str) -> None:
    query = urllib.parse.urlencode(
        {"session_id": sid, "pattern_key": key, "command": command}
    )
    _get_json(f"{base_url}/api/approval/inject_test?{query}")


def _pending_head(base_url: str, sid: str) -> dict | None:
    data = _get_json(f"{base_url}/api/approval/pending?session_id={urllib.parse.quote(sid)}")
    return data.get("pending")


def _respond(base_url: str, sid: str, approval_id: str, choice: str = "once") -> dict:
    return _post_json(
        base_url,
        "/api/approval/respond",
        {"session_id": sid, "choice": choice, "approval_id": approval_id},
    )


# ── page helpers: drive the REAL frontend functions ──────────────────────────


def _eval(page, expression: str, *args):
    return page.evaluate(expression, *args)


def _card_visible(page) -> bool:
    return _eval(
        page,
        "() => { const c = document.getElementById('approvalCard'); "
        "return !!c && c.classList.contains('visible'); }",
    )


def _wait_card_visible(page, timeout: float = 5.0) -> bool:
    """Poll until the approval card actually renders (bounded).

    showApprovalForSession bails at the belongs-to-active-session guard unless
    S.session matches the render target, and a fresh session can take a moment to
    be durable client-side. Polling keeps the gate deterministic instead of
    tripping on a transient not-yet-visible state.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _card_visible(page):
            return True
        time.sleep(0.1)
    return _card_visible(page)


def _card_command(page) -> str:
    return _eval(page, "() => (document.getElementById('approvalCmd') || {}).textContent || ''")


def _toast_text(page) -> str:
    return _eval(page, "() => (document.getElementById('toast') || {}).textContent || ''")


def _render_card(page, sid: str, pending: dict, count: int = 1) -> None:
    """Render the real approval card from a (possibly stale) pending snapshot."""
    _eval(
        page,
        "({sid, pending, count}) => { showApprovalForSession(sid, pending, count); }",
        {"sid": sid, "pending": pending, "count": count},
    )


def _click_allow_once(page) -> bool:
    """Drive the real respondApproval click handler (Allow once)."""
    return _eval(
        page,
        "async () => { const ok = await respondApproval('once'); return !!ok; }",
    )


def _click_dismiss(page) -> bool:
    """Drive the real dismissApprovalCard handler (the X button)."""
    return _eval(
        page,
        "async () => { await dismissApprovalCard(); return true; }",
    )


def _stop_approval_polling(page) -> None:
    """Freeze the 1.5s fallback poll so the stale-window stays deterministic."""
    _eval(page, "() => { stopApprovalPolling(); return true; }")


def _start_approval_polling(page, sid: str) -> None:
    _eval(page, "({sid}) => { startApprovalPolling(sid); return true; }", {"sid": sid})


def _fresh_active_session(page) -> str:
    """Start a genuinely new conversation and return its active session id.

    A prior scenario's poll wait can leave the active session empty/expired
    (showApprovalForSession bails at the belongs-to-active-session guard), so a
    scenario that must actually render a card establishes its own fresh session
    and freezes the fallback poll so the stale-window stays deterministic.

    We call `newSession()` directly rather than clicking `#btnNewChat`: the
    button short-circuits to "just focus the composer" when the current session
    is a reusable empty chat, which would return the SAME sid (and its prior
    dismissal/approval state) instead of a genuinely fresh session.
    """
    page.evaluate("async () => { await newSession(); return true; }")
    page.wait_for_function(
        "() => typeof S !== 'undefined' && !!S.session && !!S.session.session_id",
        timeout=10000,
    )
    sid = page.evaluate("S.session.session_id")
    assert sid, "no session id after creating a fresh conversation"
    _stop_approval_polling(page)
    return sid


# ── scenarios ────────────────────────────────────────────────────────────────


def _scenario_1_stale_transport_id_first_click_resolves(
    page, base_url: str, sid: str, artifact_dir: Path
) -> None:
    """Same logical approval, stale transport id: first click resolves once."""
    print("S1: stale transport id, same logical approval -> first click resolves")
    _inject_approval(base_url, sid, "rm -rf /tmp/approval-a", "dangerous_A")
    head_a = _pending_head(base_url, sid)
    assert head_a and head_a.get("approval_id"), f"precondition: head A missing: {head_a}"
    # The SSE producer delivers the head as-is; simulate the race where the card
    # rendered from a snapshot whose transport id is already stale.
    doctored = dict(head_a)
    doctored["approval_id"] = "stale-transport-id"
    _render_card(page, sid, doctored)
    assert _card_visible(page), "card must be visible after render"
    assert _card_command(page).strip() == "rm -rf /tmp/approval-a"

    ok = _click_allow_once(page)
    assert ok is True, "first click must resolve the approval"
    assert not _card_visible(page), "card must hide after a successful resolve"
    assert NOT_ACCEPTED_TOAST not in _toast_text(page), (
        f"first click must not surface '{NOT_ACCEPTED_TOAST}': {_toast_text(page)!r}"
    )
    assert _pending_head(base_url, sid) is None, (
        "the submitted id must be the authoritative head id (server resolved it)"
    )
    page.screenshot(path=str(artifact_dir / "s1-after-first-click-resolved.png"))


def _scenario_2_queued_a_to_b_never_transfers_click(
    page, base_url: str, sid: str, artifact_dir: Path
) -> None:
    """Queued A -> B while A is still visible: no auto-transfer, fresh click."""
    print("S2: queued A->B while A visible -> no auto-transfer, re-render, fresh click")
    _inject_approval(base_url, sid, "rm -rf /tmp/approval-a", "dangerous_A")
    _inject_approval(base_url, sid, "rm -rf /tmp/approval-b", "dangerous_B")
    head_a = _pending_head(base_url, sid)
    assert head_a and head_a.get("approval_id"), f"precondition: head A missing: {head_a}"
    approval_a_id = head_a["approval_id"]
    assert head_a.get("command") == "rm -rf /tmp/approval-a"
    _render_card(page, sid, head_a)
    assert _card_visible(page)

    # Another tab / the agent resolves A; the server head advances to B while
    # this tab still shows A (poll frozen).
    resolved = _respond(base_url, sid, approval_a_id)
    assert resolved.get("ok") is True, f"server-side resolve of A failed: {resolved}"
    head_b = _pending_head(base_url, sid)
    assert head_b and head_b.get("command") == "rm -rf /tmp/approval-b", (
        f"precondition: head must now be B: {head_b}"
    )
    approval_b_id = head_b["approval_id"]
    assert approval_b_id != approval_a_id

    # Click on the still-visible A card: must NOT transfer to B.
    ok_first = _click_allow_once(page)
    assert ok_first is False, "click on stale A must not auto-approve B"
    assert NOT_ACCEPTED_TOAST not in _toast_text(page), (
        f"reconcile must not burn a click with '{NOT_ACCEPTED_TOAST}': {_toast_text(page)!r}"
    )
    assert _card_visible(page), "card must stay visible, re-rendered to B"
    assert "approval-b" in _card_command(page), (
        f"card must re-render to the authoritative head B: {_card_command(page)!r}"
    )
    still_b = _pending_head(base_url, sid)
    assert still_b and still_b.get("approval_id") == approval_b_id, (
        "first click must not have resolved B"
    )
    page.screenshot(path=str(artifact_dir / "s2-after-rerender-to-b.png"))

    # A fresh deliberate click on the re-rendered B card resolves B.
    ok_second = _click_allow_once(page)
    assert ok_second is True, "fresh click on B must resolve"
    assert not _card_visible(page), "card must hide after resolving B"
    assert _pending_head(base_url, sid) is None, "B must be resolved server-side"
    page.screenshot(path=str(artifact_dir / "s2-after-b-resolved.png"))


def _scenario_3_dismiss_syncs_to_authoritative_head(
    page, base_url: str, sid: str, artifact_dir: Path
) -> None:
    """Dismiss (X) with a stale id: re-renders B first; B then stays dismissed."""
    print("S3: dismiss with stale id -> re-render B first; B stays dismissed")
    _inject_approval(base_url, sid, "rm -rf /tmp/approval-a", "dangerous_A")
    _inject_approval(base_url, sid, "rm -rf /tmp/approval-b", "dangerous_B")
    head_a = _pending_head(base_url, sid)
    assert head_a and head_a.get("approval_id")
    _render_card(page, sid, head_a)
    assert _card_visible(page)

    resolved = _respond(base_url, sid, head_a["approval_id"])
    assert resolved.get("ok") is True
    head_b = _pending_head(base_url, sid)
    assert head_b and head_b.get("command") == "rm -rf /tmp/approval-b"

    # First X click on the stale A card: must NOT mark B dismissed behind the
    # user's back — it re-renders B and requires a fresh deliberate X click.
    _click_dismiss(page)
    assert _card_visible(page), "dismiss on a stale card must re-render B, not hide"
    assert "approval-b" in _card_command(page), _card_command(page)
    page.screenshot(path=str(artifact_dir / "s3-after-first-dismiss-rerender.png"))

    # Second X click on B dismisses B for real; the restarted poll must NOT
    # resurrect the card (the "X does nothing" symptom).
    _click_dismiss(page)
    assert not _card_visible(page), "dismiss of the authoritative head must hide the card"
    _start_approval_polling(page, sid)
    time.sleep(2.5)  # allow at least one 1.5s poll tick
    assert not _card_visible(page), (
        "dismissed approval must stay dismissed across poll ticks (X must not appear dead)"
    )
    page.screenshot(path=str(artifact_dir / "s3-after-poll-stays-hidden.png"))


def _scenario_4_dismissed_successor_in_map_never_resolves(
    page, base_url: str, artifact_dir: Path
) -> None:
    """Dismissed successor B lands in the pending map but never renders (the
    cross-tab-dismissal path), leaving A as the visible card. A's click must
    not resolve B: the capture sources the RENDERED card, not the mutable map.
    """
    print("S4: dismissed successor in map + visible A -> A's click must not resolve B")
    # A prior scenario's poll wait can leave the active session empty/expired, so
    # establish a FRESH active session here (and freeze its poll) — otherwise
    # showApprovalForSession bails at the belongs-to-active-session guard and A
    # never renders, which makes the scenario vacuous for its own target path.
    sid = _fresh_active_session(page)

    _inject_approval(base_url, sid, "rm -rf /tmp/approval-a", "dangerous_A")
    head_a = _pending_head(base_url, sid)
    assert head_a and head_a.get("approval_id")
    _render_card(page, sid, head_a)
    assert _wait_card_visible(page), "card must be visible after render on the fresh session"
    assert "approval-a" in _card_command(page), _card_command(page)

    # Advance the server head to B (queued), then resolve A server-side so B
    # becomes the head while the card still shows A (the poll is frozen).
    _inject_approval(base_url, sid, "rm -rf /tmp/approval-b", "dangerous_B")
    resolved = _respond(base_url, sid, head_a["approval_id"])
    assert resolved.get("ok") is True
    head_b = _pending_head(base_url, sid)
    assert head_b and "approval-b" in head_b.get("command", "")
    approval_b_id = head_b["approval_id"]

    # Cross-tab-style: mark B dismissed, then render it. _rememberApprovalPending
    # stores B in the map, but the dismissed check suppresses the render, so A
    # stays the visibly rendered card while the map holds B.
    _eval(
        page,
        "({sid, id, pending}) => { _markApprovalDismissed(sid, id); showApprovalForSession(sid, pending, 1); return true; }",
        {"sid": sid, "id": approval_b_id, "pending": head_b},
    )
    assert _card_visible(page), "suppressed B must leave A visibly rendered"
    assert "approval-a" in _card_command(page), _card_command(page)

    # Capture the actual /api/approval/respond request payloads so the test
    # asserts on the observable network behaviour (fail-closed), not just the
    # server head afterwards: A's click must never POST approval-b's id.
    respond_payloads: list[dict] = []

    def _on_request(request) -> None:
        if request.method == "POST" and request.url.endswith("/api/approval/respond"):
            try:
                respond_payloads.append(json.loads(request.post_data or "{}"))
            except Exception:
                respond_payloads.append({})

    page.on("request", _on_request)

    # Click Allow once on the visible A: must NOT resolve B, and must NOT POST
    # approval-b's id (either no respond POST fires, or it carries A's id).
    _click_allow_once(page)
    assert all(
        payload.get("approval_id") != approval_b_id for payload in respond_payloads
    ), (
        "A's click must never POST approval-b's id; got "
        + repr(respond_payloads)
    )
    still = _pending_head(base_url, sid)
    assert still and "approval-b" in still.get("command", ""), (
        "A's click must not resolve the never-rendered successor B"
    )
    page.screenshot(path=str(artifact_dir / "s4-after-click-keeps-b-pending.png"))


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-approval-gate-")
    state_dir = Path(state_tmp.name)
    artifact_env = str(os.environ.get("APPROVAL_ARTIFACT_DIR") or "").strip()
    artifact_dir_owned = not bool(artifact_env)
    artifact_dir = Path(artifact_env) if artifact_env else Path(
        tempfile.mkdtemp(prefix="hermes-approval-artifacts-")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    agent_dir = state_dir / "no-agent"
    agent_dir.mkdir(parents=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        '"""Empty agent stub for the approval-first-click browser gate."""\n',
        encoding="utf-8",
    )

    env = os.environ.copy()
    # The gate must own the WebUI environment completely: strip every WebUI
    # setting/credential from the host so no portal/ops configuration (password,
    # trusted-auth header, OIDC, passkeys, provider API keys) can leak into the
    # sandboxed server or silently flip auth/backends on.
    for key in list(env):
        if key.upper().startswith("HERMES_WEBUI_") or key.endswith("_API_KEY"):
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
    # Default (local in-process) backend: no HERMES_WEBUI_CHAT_BACKEND, no
    # gateway URL — exactly the configuration #7091 was reported on.

    proc = None
    log = None
    log_path = None
    exit_code = 1
    playwright = None
    browser = None
    page = None
    try:
        proc, log, log_path, base_url = _start_webui_server(repo_root, env, artifact_dir)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        page.on("pageerror", lambda error: print(f"pageerror: {error}", file=sys.stderr))

        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)
        page.locator("#btnNewChat").click()
        page.wait_for_function(
            "() => typeof S !== 'undefined' && !!S.session && !!S.session.session_id",
            timeout=10000,
        )
        sid = page.evaluate("S.session.session_id")
        assert sid, "no session id after creating a conversation"
        # Freeze the fallback poll so the stale render window is deterministic.
        _stop_approval_polling(page)

        _scenario_1_stale_transport_id_first_click_resolves(page, base_url, sid, artifact_dir)
        _scenario_2_queued_a_to_b_never_transfers_click(page, base_url, sid, artifact_dir)
        _scenario_3_dismiss_syncs_to_authoritative_head(page, base_url, sid, artifact_dir)
        _scenario_4_dismissed_successor_in_map_never_resolves(page, base_url, artifact_dir)

        context.close()
        browser.close()
        browser = None
        print("APPROVAL FIRST-CLICK GATE PASSED")
        exit_code = 0
        return 0
    except Exception as error:
        print(f"\nAPPROVAL FIRST-CLICK GATE FAILED: {error}", file=sys.stderr)
        try:
            if page is not None:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
        except Exception as artifact_error:
            print(f"Could not capture failure screenshot: {artifact_error}", file=sys.stderr)
        print(f"Artifacts: {artifact_dir}", file=sys.stderr)
        exit_code = 1
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
            print(
                log_path.read_text(encoding="utf-8", errors="replace")[-2000:],
                file=sys.stderr,
            )
        state_tmp.cleanup()
        if artifact_dir_owned and exit_code == 0:
            shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
