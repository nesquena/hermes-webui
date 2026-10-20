#!/usr/bin/env python3
"""Real-send regression gate for the #7855 goal-continuation ID handoff.

The #7855 round-3 review marked the PR BLOCKING (BRICK) because every ordinary
chat send threw ``ReferenceError: _drainingGoalContinuationId is not defined``:
the variable (and its setter) were declared INSIDE ``attachLiveStream()`` while
``send()`` read and cleared them from a sibling top-level scope. The PR's own
tests were source-presence assertions (``assert "..." in src``), so they stayed
green while the browser crashed on every send — exactly the blind spot
``node --check`` and fragment-extraction tests share.

This gate drives the REAL path instead: boot the real WebUI server, open a real
Chromium page, run a genuine send() from the composer, and assert that
``/api/chat/start`` actually received a body. It fails with the same
``request body: None`` tell the CI ``live-to-final`` job reported if the send
path throws before the fetch.

Covered end-to-end (maintainer's checklist, extended in round 6):
  1. ordinary send posts a chat/start body (the round-3 BRICK regression itself);
  2. a genuine user turn posts NO continuation ID while a continuation is queued;
  3. the queued-continuation drain hands its ID to that send and it is posted;
  4. a re-entrant genuine send (arriving while a continuation is parked in
     uploadPendingFiles) must requeue WITHOUT any continuation ID — round 6 CORE;
  5. a refresh-restored continuation is a text-bound, one-shot draft: sending the
     restored text unchanged keeps the goal, REPLACING it fails closed, and a
     consumed draft leaves nothing behind;
  6. a session boundary drops an unconsumed restored draft;
  7. a failed start restores the draft text AND its continuation ID.

The round-5 CORE race (a genuine turn parked mid-await while the drain publishes
the ID) is not reproducible through page timing, so it lives in
tests/test_7855_goal_continuation_binding.py, which replays that exact sequence
against the real send() in a Node VM — extended in round 6 for the re-entrant
guard, the failed-start restore, and both busy-queue modes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIVED_BODIES: list[dict] = []
BODY_LOCK = threading.Lock()


def _bodies_with_key(key: str) -> list[dict]:
    with BODY_LOCK:
        return [b for b in RECEIVED_BODIES if key in b]


def _last_body_with_key(key: str) -> dict | None:
    bodies = _bodies_with_key(key)
    return bodies[-1] if bodies else None


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    # Reuse the lifecycle gate's proven server bootstrap so this gate starts
    # the same isolated instance the CI jobs exercise.
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        import browser_conversation_lifecycle as lifecycle
    except ImportError as exc:  # pragma: no cover - environment problem
        print(f"SETUP FAIL: cannot import lifecycle harness ({exc})", file=sys.stderr)
        return 2

    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-7855-gate-")
    state_dir = Path(state_tmp.name)
    artifact_dir = state_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = state_dir / "no-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "run_agent.py").write_text('"""No-op agent stub."""\n', encoding="utf-8")
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir()

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    for key in (
        "API_SERVER_KEY",
        "HERMES_WEBUI_PASSWORD",
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
        "HERMES_TEST_TERMINAL_BARRIER_DIR",
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

    proc = None
    log = None
    playwright = None
    browser = None
    page = None
    failures: list[str] = []
    try:
        proc, log, log_path, base_url = lifecycle._start_webui_server(
            REPO_ROOT, env, artifact_dir
        )
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=20000)

        # Capture every /api/chat/start POST body straight from the request —
        # no relay server in between (a blocking forward inside a route handler
        # deadlocks the page). Answer with a minimal stream_id body so send()
        # proceeds exactly as it would in production.
        def _capture_chat_start(route):
            raw = route.request.post_data or ""
            parsed = None
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            with BODY_LOCK:
                RECEIVED_BODIES.append(parsed if isinstance(parsed, dict) else {})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"stream_id": "gate-stream-7855", "session_id": "gate-session"}),
            )

        page.route("**/api/chat/start", _capture_chat_start)

        # ── Case 1: the BRICK itself — an ordinary send must reach the POST ──
        # The route handler installed above is the only thing between the
        # browser and the request; if send() throws before fetch (the BRICK),
        # no body is ever recorded and this case fails exactly like the CI job
        # did ("request body: None").
        page.locator("#msg").fill("ordinary send without any continuation")
        page.wait_for_timeout(300)
        page.locator("#btnSend").click()
        page.wait_for_timeout(2000)
        body = _wait_for_recorded(timeout=20)
        if body is None:
            failures.append(
                "case1 ordinary-send: /api/chat/start was never reached "
                f"(page errors: {page_errors[-3:]!r}) — this is the #7855 "
                "BRICK shape (send() threw before the fetch)"
            )
        else:
            if body.get("goal_continuation_id") not in (None, ""):
                failures.append(
                    "case1 ordinary-send: a genuine user turn must not carry a "
                    f"continuation ID, got {body.get('goal_continuation_id')!r}"
                )
            else:
                print("OK  case1 ordinary send posts a chat/start body, no continuation ID")

        # ── Case 2: a genuine turn carries no continuation ID ──
        # The end-to-end invariant on the real page. The RACE itself — a genuine
        # turn parked mid-await while a drain publishes the ID — is owned by
        # tests/test_7855_goal_continuation_binding.py, which replays that exact
        # sequence against the real send() in a Node VM where the timing is
        # deterministic. Here we only assert the shipped invariant.
        page.evaluate(
            """() => {
              window.__case2Id = 'cont-case2-pending';
              if (typeof queueSessionMessage === 'function') {
                queueSessionMessage(S.session.session_id, {
                  text: 'pending continuation body',
                  files: [],
                  model: S.session.model,
                  model_provider: S.session.model_provider,
                  profile: S.activeProfile || 'default',
                  goal_continuation_id: window.__case2Id,
                });
              } else { window.__case2QueueMissing = true; }
            }"""
        )
        if page.evaluate("() => !!window.__case2QueueMissing"):
            failures.append("case2 genuine-turn: queueSessionMessage is not reachable at module scope")
        else:
            page.locator("#msg").fill("genuine user turn while a continuation is pending")
            page.locator("#btnSend").click()
            second = _wait_for_recorded(timeout=20, index=1)
            if second is None:
                failures.append("case2 genuine-turn: the genuine send never reached /api/chat/start")
            elif second.get("goal_continuation_id"):
                failures.append(
                    "case2 genuine-turn: a genuine user turn carried a continuation ID "
                    f"({second.get('goal_continuation_id')!r})"
                )
            else:
                print("OK  case2 genuine turn posts no ID while a continuation is pending")

        # ── Case 3: the queued-continuation drain hands the ID to send() ──
        # Drive the real queue: push an entry that carries a continuation
        # ID through queueSessionMessage, then let the drain path run send()
        # (the ui.js setTimeout body) and assert the posted body carries the
        # ID — the exact handoff the round-3 review called out.
        page.evaluate(
            """() => {
              window.__case3Id = 'cont-case3-drain';
              if (typeof queueSessionMessage === 'function') {
                queueSessionMessage(S.session.session_id, {
                  text: 'queued continuation text',
                  files: [],
                  model: S.session.model,
                  model_provider: S.session.model_provider,
                  profile: S.activeProfile || 'default',
                  goal_continuation_id: window.__case3Id,
                });
              } else {
                window.__case3QueueMissing = true;
              }
            }"""
        )
        queue_missing = page.evaluate("() => !!window.__case3QueueMissing")
        if queue_missing:
            failures.append(
                "case3 queue-drain: queueSessionMessage is not reachable at module scope"
            )
        else:
            page.wait_for_timeout(400)  # let the drain settle + send fire
            drained_body = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                drained_body = _last_body_with_key("goal_continuation_id_value_marker") or _find_body_with_value(
                    page, "cont-case3-drain"
                )
                if drained_body:
                    break
                page.wait_for_timeout(250)
            if drained_body:
                print("OK  case3 queued continuation drain posts its continuation ID")
            else:
                # Not a hard failure on its own: the drain may need a finished
                # stream to fire. Record it as a gap instead of masking it.
                failures.append(
                    "case3 queue-drain: no posted body carried the continuation ID "
                    "(drain path did not hand cont-case3-drain to send())"
                )

        # ── Case 4: a re-entrant genuine turn must NOT inherit the parked ID ──
        # The in-flight send is a parked continuation holding the lock; a genuine
        # send that arrives re-entrantly is queued FIRST. Inspect the queued entry
        # (not the POST) to prove it carries no continuation ID.
        # Fill the composer first — the re-entrant guard reads the live text.
        page.locator("#msg").fill("message sent during the drain window")
        page.wait_for_timeout(200)
        page.evaluate(
            """() => {
              window.__case4Result = null;
              const sid = S.session.session_id;
              // Force the concurrent-send branch: pretend a send is in flight for
              // this session, so send() takes the requeue exit. There is no shared
              // continuation slot to seed (round 6 removed it) — a re-entrant
              // genuine turn may only use its OWN token, which this call has none of.
              _sendInProgress = true;
              _sendInProgressSid = sid;
              const before = (typeof _readPersistedSessionQueue === 'function')
                ? _readPersistedSessionQueue(sid) : [];
              const beforeCount = Array.isArray(before) ? before.length : 0;
              Promise.resolve(send()).then(() => {
                const after = (typeof _readPersistedSessionQueue === 'function')
                  ? _readPersistedSessionQueue(sid) : [];
                const entries = Array.isArray(after) ? after : [];
                window.__case4Result = {
                  beforeCount,
                  carriedId: entries.some(e => e && e.goal_continuation_id),
                  entries: entries.map(e => (e && e.goal_continuation_id) || null),
                };
              }).catch(err => { window.__case4Result = 'error: ' + String(err); });
            }"""
        )
        case4 = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            case4 = page.evaluate("() => window.__case4Result")
            if case4:
                break
            page.wait_for_timeout(200)
        if isinstance(case4, str):
            failures.append(f"case4 requeue: send() raised {case4}")
        elif isinstance(case4, dict):
            if case4.get("carriedId"):
                failures.append(
                    "case4 reentrant-genuine: the re-queued entry carries a continuation "
                    f"ID it never owned (entries={case4.get('entries')!r}) — the genuine "
                    "turn would consume the parked continuation's pending record"
                )
            else:
                print("OK  case4 a re-entrant genuine turn requeues without any continuation ID")
        else:
            failures.append(f"case4 requeue: unexpected probe result {case4!r}")
        page.evaluate("() => { _sendInProgress = false; _sendInProgressSid = null; }")

        # ── Case 5: the restored-continuation draft (reviewer's probe #3) ──
        # A refresh-restore used to publish its ID to a global slot, so a user
        # who replaced the restored text had their OWN message post the stale
        # ID. The draft is now bound to the exact restored text: send the text
        # unchanged and the continuation survives; replace it and the token dies.
        draft = page.evaluate(
            """() => {
              if (typeof _setRestoredGoalContinuationDraft !== 'function') return 'setter-missing';
              if (typeof _takeRestoredDraftGoalContinuationId !== 'function') return 'reader-missing';
              const _msg = document.getElementById('msg');
              _setRestoredGoalContinuationDraft('cont-case5-draft', 'restored continuation text');
              const marked = !!( _msg.dataset.goalContinuationId === 'cont-case5-draft' );
              // (a) user REPLACED the text -> fail closed, no token
              const replaced = _takeRestoredDraftGoalContinuationId('my own totally different message');
              const afterReplaced = _msg.dataset.goalContinuationId || null;
              // (b) user left the text alone -> token survives, one-shot
              _setRestoredGoalContinuationDraft('cont-case5-draft', 'restored continuation text');
              const unchanged = _takeRestoredDraftGoalContinuationId('restored continuation text');
              const afterUnchanged = _msg.dataset.goalContinuationId || null;
              return {marked, replaced, afterReplaced, unchanged, afterUnchanged};
            }"""
        )
        if draft in ("setter-missing", "reader-missing"):
            failures.append(f"case5 restored-draft: draft accessors not reachable at module scope ({draft})")
        elif not isinstance(draft, dict):
            failures.append(f"case5 restored-draft: unexpected probe result {draft!r}")
        else:
            if not draft.get("marked"):
                failures.append("case5 restored-draft: the draft ID was not recorded on the composer")
            if draft.get("replaced"):
                failures.append(
                    "case5 restored-draft: a REPLACED draft still handed out the stale ID "
                    f"({draft.get('replaced')!r}) — it must fail closed"
                )
            if draft.get("afterReplaced"):
                failures.append("case5 restored-draft: a consumed draft left its ID on the composer")
            if draft.get("unchanged") != "cont-case5-draft":
                failures.append(
                    "case5 restored-draft: sending the restored text unchanged lost the "
                    f"continuation ({draft.get('unchanged')!r})"
                )
            if draft.get("afterUnchanged"):
                failures.append("case5 restored-draft: the unchanged draft was not consumed (not one-shot)")
            if not failures:
                print("OK  case5 restored draft is text-bound, one-shot, and fails closed when replaced")

        # ── Case 6: session boundary drops an unconsumed draft ──
        boundary = page.evaluate(
            """() => {
              if (typeof _setRestoredGoalContinuationDraft !== 'function') return 'setter-missing';
              if (typeof _clearRestoredGoalContinuationDraft !== 'function') return 'clear-missing';
              _setRestoredGoalContinuationDraft('cont-case6-boundary', 'stale draft text');
              _clearRestoredGoalContinuationDraft();
              const _msg = document.getElementById('msg');
              return {leftover: _msg.dataset.goalContinuationId || null};
            }"""
        )
        if boundary in ("setter-missing", "clear-missing"):
            failures.append(f"case6 boundary: draft accessors not reachable at module scope ({boundary})")
        elif not isinstance(boundary, dict):
            failures.append(f"case6 boundary: unexpected probe result {boundary!r}")
        elif boundary.get("leftover"):
            failures.append(
                f"case6 boundary: a session boundary left the stale draft ID behind ({boundary['leftover']!r})"
            )
        else:
            print("OK  case6 session boundary drops an unconsumed restored draft")

        # ── Case 7: a failed start restores the draft text AND its ID ──
        # Rejected POSTs never admit a turn, so the continuation ID must come back
        # with the text: restore only the text and the retry becomes an ordinary
        # turn that silently ends the goal loop (round 6 item 2).
        failed_start = page.evaluate(
            """() => {
              if (typeof _restoreComposerDraftAfterFailedSend !== 'function') return 'helper-missing';
              if (typeof _setRestoredGoalContinuationDraft !== 'function') return 'setter-missing';
              if (typeof _takeRestoredDraftGoalContinuationId !== 'function') return 'reader-missing';
              const _msg = document.getElementById('msg');
              // An empty composer: the restore owns it (the pre-send wipe ran).
              _msg.value = '';
              _msg.dataset.goalContinuationId = '';
              _msg.dataset.goalContinuationText = '';
              const ok = _restoreComposerDraftAfterFailedSend(
                'draft restored after a failed start', [], S.session.session_id,
                Promise.resolve(), 'cont-case7-failedstart');
              const idAfter = _msg.dataset.goalContinuationId || null;
              const readBack = _takeRestoredDraftGoalContinuationId(_msg.value);
              return {ok, text: _msg.value, idAfter, readBack};
            }"""
        )
        if failed_start in ("helper-missing", "setter-missing", "reader-missing"):
            failures.append(f"case7 failed-start restore: helper not reachable ({failed_start})")
        elif not isinstance(failed_start, dict):
            failures.append(f"case7 failed-start restore: unexpected probe result {failed_start!r}")
        else:
            if not failed_start.get("ok"):
                failures.append("case7 failed-start restore: the draft text was not restored")
            if failed_start.get("text") != "draft restored after a failed start":
                failures.append(
                    "case7 failed-start restore: restored the wrong text "
                    f"({failed_start.get('text')!r})"
                )
            if failed_start.get("idAfter") != "cont-case7-failedstart":
                failures.append(
                    "case7 failed-start restore: the draft was restored WITHOUT its "
                    f"continuation ID ({failed_start.get('idAfter')!r}) — the retry would "
                    "post an empty ID and end the goal loop"
                )
            if failed_start.get("readBack") != "cont-case7-failedstart":
                failures.append(
                    "case7 failed-start restore: a retry of the restored text could not "
                    f"read its ID back ({failed_start.get('readBack')!r})"
                )
            if not failures:
                print("OK  case7 a failed start restores the draft text and its continuation ID")

        # ── The tell-tale check: NO ReferenceError may have hit the page ──
        ref_errors = [
            e for e in page_errors if "is not defined" in e or "ReferenceError" in e
        ]
        if ref_errors:
            failures.append(
                "page threw ReferenceError(s) during the gate: "
                f"{ref_errors[:3]!r} — a send path is still scope-broken"
            )
    except Exception as exc:  # noqa: BLE001 - gate must report, not crash blind
        failures.append(f"gate raised: {type(exc).__name__}: {exc}")
    finally:
        for closer_name in ("page", "browser"):
            closer = locals().get(closer_name)
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # pragma: no cover
            pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # pragma: no cover
                try:
                    proc.kill()
                except Exception:
                    pass
        if log is not None:
            try:
                log.close()
            except Exception:  # pragma: no cover
                pass
        try:
            state_tmp.cleanup()
        except Exception:  # pragma: no cover
            pass

    with BODY_LOCK:
        total_bodies = len(RECEIVED_BODIES)
    if failures:
        print(f"FAIL {len(failures)} issue(s) with {total_bodies} recorded chat/start bodies:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"PASS real-send gate: {total_bodies} chat/start bodies recorded, no reference errors")
    return 0


def _wait_for_recorded(timeout: float, index: int = 0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with BODY_LOCK:
            bodies = list(RECEIVED_BODIES)
        if len(bodies) > index:
            return bodies[index]
        time.sleep(0.25)
    return None


def _find_body_with_value(page, value: str) -> dict | None:
    """Match the recorded body whose message text/ID contains the drain value."""
    with BODY_LOCK:
        bodies = list(RECEIVED_BODIES)
    for body in reversed(bodies):
        if value in json.dumps(body):
            page.evaluate("() => {}")  # keep the page reference honest
            return body
    return None


if __name__ == "__main__":
    raise SystemExit(main())
