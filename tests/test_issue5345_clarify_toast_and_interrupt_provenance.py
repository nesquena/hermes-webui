"""Regression tests for issue #5345.

Two symptoms, both surfaced from front-end source guards (these are static-source
assertions — no server needed):

1. Misleading "Clarify endpoint unavailable. Please restart server." toast.
   `/api/clarify/pending` ALWAYS returns HTTP 200 when the route is present (it
   returns {"pending": null} for an unknown session — never 404). The old catch
   block fired the restart-server toast on ANY caught error whose message matched
   the broad regex ``/(^|\\b)(404|not found)(\\b|$)/i``. An unrelated stale-session
   404 ("Session not found") or transient error therefore produced a false
   missing-endpoint toast. The fix branches on the STRUCTURED HTTP status that
   `api()` attaches to the thrown Error (err.status) and only warns on a genuine
   route-not-found 404 whose body is not session-scoped.

2. Interrupt provenance. The issue asks that only explicit cancellation reach the
   backend and that cancellation source be observable. Passive lifecycle events
   (session switch / tab hide / page unload) already tear down only the LOCAL SSE
   transport via closeLiveStream() and never call /api/chat/cancel; the fix adds a
   provenance log to cancelStream()/cancelSessionStream() and threads a distinct
   `reason` from every explicit call site so a backend SIGINT/exit-130 can be
   attributed.

Backend fact locked by test_clarify_pending_never_404s: the handler returns 200
with {"pending": None} for an unknown session, so a real 404 can only be a
missing route (server predates the endpoint) or an unrelated session-scoped 404.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def _clarify_catch_block():
    """Return the source of the clarify fallback-poll catch block."""
    idx = MESSAGES_JS.find("function _startClarifyFallbackPoll")
    assert idx != -1, "_startClarifyFallbackPoll not found in messages.js"
    # Grab a generous window covering the catch/finally of the poll tick.
    return MESSAGES_JS[idx: idx + 5500]


# ── Part 1: clarify toast false positive ────────────────────────────────────

def test_clarify_toast_no_longer_uses_broad_message_regex():
    """The old broad ``(404|not found)`` message-scrape must be gone — that is the
    exact matcher that turned an unrelated stale-session 404 into a false
    'restart server' toast."""
    block = _clarify_catch_block()
    assert r"/(^|\b)(404|not found)(\b|$)/i.test(msg)" not in block, (
        "The broad clarify missing-endpoint regex is still present — an unrelated "
        "404/'not found' error message can still trigger a false restart-server toast."
    )


def test_clarify_toast_branches_on_structured_status():
    """The catch block must read the structured HTTP status (err.status) that
    api() attaches, not infer it from the message string."""
    block = _clarify_catch_block()
    assert "e.status" in block, (
        "clarify poll catch block must branch on the structured err.status "
        "(set by api()), not scrape the message text."
    )


def test_clarify_missing_endpoint_requires_404_and_non_session_body():
    """The missing-endpoint warning must be gated on status===404 AND a
    route-not-found body that is NOT session-scoped, so a 'Session not found'
    404 never surfaces the restart-server toast."""
    block = _clarify_catch_block()
    m = re.search(r"const\s+isMissingEndpoint\s*=([^;]+);", block, re.DOTALL)
    assert m, "expected an isMissingEndpoint guard expression in the catch block"
    guard = m.group(1)
    assert "status === 404" in guard or "status===404" in guard, (
        "missing-endpoint guard must require a 404 status"
    )
    assert "!isSessionScoped404" in guard.replace(" ", ""), (
        "missing-endpoint guard must EXCLUDE session-scoped 404s so a "
        "'Session not found' error is not mistaken for a missing endpoint."
    )


def test_stale_session_guard_is_not_tied_to_one_exact_backend_message():
    """The stale-session branch should not depend only on the exact
    'Session not found' wording: session-like 404 bodies and a current-session
    mismatch after a profile switch are expected stale polls."""
    block = _clarify_catch_block()
    m = re.search(r"const\s+isSessionScoped404\s*=([^;]+);", block, re.DOTALL)
    assert m, "expected an isSessionScoped404 guard expression in the catch block"
    guard = m.group(1).replace(" ", "")
    assert "status===404" in guard, "stale-session guard must require a 404 status"
    assert "/session/i.test(msg)" in guard, (
        "stale-session guard should classify session-scoped 404 bodies without "
        "requiring the exact 'Session not found' wording"
    )
    assert "currentSid!==null&&currentSid!==sid" in guard, (
        "stale-session guard should also cover the profile-switch race where the "
        "active session changed while the old poll request was in flight"
    )


def test_stale_session_404_handled_before_missing_endpoint():
    """A session-scoped 404 must be treated as a stale-session poll (stop +
    hide silently), and that branch must appear BEFORE the missing-endpoint
    warning branch so the warning can never fire for it."""
    block = _clarify_catch_block()
    stale_idx = block.find("isSessionScoped404")
    warn_idx = block.find("isMissingEndpoint")
    assert stale_idx != -1, "stale-session 404 branch (isSessionScoped404) not found"
    assert warn_idx != -1, "missing-endpoint guard not found"
    assert stale_idx < warn_idx, (
        "the stale-session 404 branch must be evaluated before the "
        "missing-endpoint warning so a session-scoped 404 never warns."
    )


def test_expected_handled_clarify_404s_do_not_emit_poll_failed_warn():
    """Routine stale-session 404s and handled missing-endpoint 404s should return
    before the generic warn-level 'pending poll failed' diagnostic. Expected
    profile-switch teardown must not create warn-level DevTools noise."""
    block = _clarify_catch_block()
    stale_idx = block.find("if (isSessionScoped404)")
    missing_idx = block.find("if (isMissingEndpoint)")
    warn_idx = block.find('console.warn("[clarify] pending poll failed"')
    assert stale_idx != -1, "expected stale-session branch"
    assert missing_idx != -1, "expected missing-endpoint branch"
    assert warn_idx != -1, "expected generic unexpected-failure warning"
    assert stale_idx < missing_idx < warn_idx, (
        "generic warn-level clarify poll logging must come only after handled "
        "stale-session and missing-endpoint branches return"
    )


def test_clarify_poll_failure_is_logged_with_context():
    """Clarify poll failures must be observable: path, status, polling session
    id, and current session id (maintainer question #2)."""
    block = _clarify_catch_block()
    assert "[clarify] pending poll failed" in block, (
        "clarify poll failure should log a structured diagnostic line"
    )
    for field in ("path", "status", "pollingSessionId", "currentSessionId"):
        assert field in block, f"clarify failure log must include {field!r}"


# ── Part 2: interrupt provenance ────────────────────────────────────────────

def test_cancel_stream_accepts_reason_param():
    """cancelStream must take a `reason` so explicit-cancel provenance is
    attributable in the logs."""
    m = re.search(r"async\s+function\s+cancelStream\s*\(([^)]*)\)", BOOT_JS)
    assert m, "cancelStream declaration not found in boot.js"
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    assert "reason" in params, (
        f"cancelStream must accept a `reason` parameter; got params {params}"
    )


def test_cancel_stream_logs_provenance():
    """cancelStream / cancelSessionStream must log the cancellation reason so a
    backend SIGINT/exit-130 can be attributed to an explicit user action."""
    assert "[stream] cancel requested" in BOOT_JS, (
        "cancelStream must log a '[stream] cancel requested' provenance line"
    )
    # both explicit backend-cancel functions log it
    assert BOOT_JS.count("[stream] cancel requested") >= 2, (
        "both cancelStream and cancelSessionStream should log provenance"
    )


def test_explicit_cancel_call_sites_pass_a_reason():
    """Every explicit cancelStream() call site should pass a descriptive reason
    string (composer-stop, slash-stop, slash-interrupt, busy-interrupt) so the
    provenance log is meaningful rather than a bare 'explicit-cancel'."""
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    commands = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
    messages = MESSAGES_JS
    combined = ui + commands + messages
    # No bare await cancelStream() with empty args at the explicit call sites.
    bare = re.findall(r"await\s+cancelStream\(\s*\)", combined)
    assert not bare, (
        f"found {len(bare)} bare cancelStream() call(s) with no reason — each "
        "explicit call site must pass a provenance reason string."
    )
    for reason in ("composer-stop", "slash-stop", "slash-interrupt", "busy-interrupt"):
        assert f"cancelStream('{reason}')" in combined or f'cancelStream("{reason}")' in combined, (
            f"expected an explicit cancelStream call with reason {reason!r}"
        )


# ── Backend invariant that the front-end fix depends on ─────────────────────

def test_clarify_pending_never_404s():
    """The whole Part-1 fix rests on /api/clarify/pending returning 200 (with
    {"pending": None}) for any session — a 404 from that path is ALWAYS either a
    missing route or an unrelated error. Lock the handler shape."""
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    m = re.search(
        r"def _handle_clarify_pending\(handler, parsed\):(.*?)\ndef ",
        routes,
        re.DOTALL,
    )
    assert m, "_handle_clarify_pending not found"
    handler_src = m.group(1)
    assert "404" not in handler_src, (
        "_handle_clarify_pending must never return 404 — it returns 200 with "
        "{'pending': None} for unknown sessions. If this changes, the front-end "
        "clarify toast logic in messages.js must be revisited."
    )
    assert '{"pending": None}' in handler_src or "{'pending': None}" in handler_src, (
        "_handle_clarify_pending should return {'pending': None} for no pending clarify"
    )
