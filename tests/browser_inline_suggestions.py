#!/usr/bin/env python3
"""
Headless browser gate for inline follow-up suggestion buttons.

SPEC: SPEC-inline-suggestions.md

WHAT IT CHECKS, in a real browser against the real server.py (agent-free,
send() stubbed with a recorder the same way tests/browser_clarify_enter_submit.py
stubs respondClarify):

  1. a message whose body contains a ```suggestions``` block shows no visible
     raw code block and exactly N buttons, the first one primary (and the
     label alone — not the kbd hint — is each button's accessible name);
  2. clicking the second (secondary) button sends exactly one user message
     whose text equals that button's label, and disables the whole group;
  3. an existing composer draft survives a suggestion click (sent, then
     restored);
  4. Ctrl+Enter (Meta+Enter on mac) with an EMPTY composer activates the
     primary suggestion of the newest assistant message — and does nothing
     when the composer has text;
  5. re-rendering a message in place (same DOM node, fresh innerHTML with the
     same ```suggestions``` block — the shape an in-place edit/re-render
     produces) does not duplicate the button group;
  6. an invalid block (non-JSON, or an array with a non-string entry) renders
     no buttons and leaves the raw block visible (fail-safe).

Exercises both markup shapes suggestions.js has to recognise: the
static/history renderMd() shape (`<div class="pre-header">` +
`<pre><code class="language-suggestions">`) and the live-stream smd shape
(`<pre><code class="suggestions">`, no pre-header).

SCOPE
  Agent-free, like tests/browser_smoke.py and tests/browser_clarify_enter_submit.py:
  the real server.py on an ephemeral port with isolated temp state. send() is
  replaced with a recorder so no agent turn is ever started.

USAGE
  python tests/browser_inline_suggestions.py
  (Requires: playwright + chromium. Set INLINE_SUGGESTIONS_PORT to pin the port.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# Known-benign console noise unrelated to this feature (see tests/browser_smoke.py).
BENIGN = [
    "favicon",
    "manifest.json",
    "serviceworker",
    "sw.js",
    "the server responded with a status of 404",
]

SETUP_JS = """() => {
  window.__sendCalls = [];
  window.send = function(){
    window.__sendCalls.push(document.getElementById('msg').value);
    return Promise.resolve();
  };
  const empty = document.getElementById('emptyState');
  if (empty) empty.style.display = 'none';
  return true;
}"""

# args: [rowId, rawFenceBody, bare]
#   bare=false -> the renderMd() history shape: <div class="pre-header"> + <pre><code class="language-suggestions">
#   bare=true  -> the smd live-stream shape: <pre><code class="suggestions"> (no pre-header)
INJECT_ROW_JS = """(args) => {
  const [rowId, raw, bare] = args;
  const inner = document.getElementById('msgInner');
  const row = document.createElement('div');
  row.className = 'assistant-segment';
  row.setAttribute('data-test-row', rowId);
  const body = document.createElement('div');
  body.className = 'msg-body';
  const codeClass = bare ? 'suggestions' : 'language-suggestions';
  const header = bare ? '' : '<div class="pre-header">suggestions</div>';
  body.innerHTML = '<p>Reply for ' + rowId + '</p>' + header +
    '<pre><code class="' + codeClass + '"></code></pre>';
  row.appendChild(body);
  inner.appendChild(row);
  row.querySelector('code').textContent = raw;
  return true;
}"""

# Simulates an in-place re-render: same row/msg-body DOM node, fresh
# innerHTML (the shape ui.js's edit-message path produces: body.innerHTML =
# renderMd(text) on the SAME body element, not a replaced one).
RERENDER_ROW_JS = """(args) => {
  const [rowId, raw, bare] = args;
  const row = document.querySelector('[data-test-row="' + rowId + '"]');
  if (!row) return false;
  const body = row.querySelector('.msg-body');
  const codeClass = bare ? 'suggestions' : 'language-suggestions';
  const header = bare ? '' : '<div class="pre-header">suggestions</div>';
  body.innerHTML = '<p>Reply for ' + rowId + ' (re-rendered)</p>' + header +
    '<pre><code class="' + codeClass + '"></code></pre>';
  body.querySelector('code').textContent = raw;
  return true;
}"""

GROUP_STATE_JS = """(rowId) => {
  const row = document.querySelector('[data-test-row="' + rowId + '"]');
  if (!row) return null;
  const body = row.querySelector('.msg-body');
  const pre = body.querySelector('pre');
  const header = body.querySelector('.pre-header');
  const groups = row.querySelectorAll('.inline-suggestions');
  const group = groups[groups.length - 1] || null;
  const buttons = group ? Array.from(group.querySelectorAll('.inline-suggestion-btn')) : [];
  const labelOf = (b) => {
    const clone = b.cloneNode(true);
    const kbd = clone.querySelector('.inline-suggestion-kbd');
    if (kbd) kbd.remove();
    return clone.textContent;
  };
  return {
    groupCount: groups.length,
    bodyNextIsGroup: group ? body.nextElementSibling === group : null,
    preHidden: pre ? (getComputedStyle(pre).display === 'none') : null,
    preHasHiddenClass: pre ? pre.classList.contains('suggestions-block-hidden') : null,
    headerHidden: header ? (getComputedStyle(header).display === 'none') : null,
    buttonCount: buttons.length,
    labels: buttons.map(labelOf),
    firstIsPrimary: buttons.length ? buttons[0].classList.contains('primary') : null,
    restAreSecondary: buttons.slice(1).every((b) => b.classList.contains('secondary')),
    disabledStates: buttons.map((b) => b.disabled),
    ariaDisabledStates: buttons.map((b) => b.getAttribute('aria-disabled')),
    groupRole: group ? group.getAttribute('role') : null,
    groupAriaLabel: group ? group.getAttribute('aria-label') : null,
  };
}"""

CLICK_BUTTON_JS = """(args) => {
  const [rowId, index] = args;
  const row = document.querySelector('[data-test-row="' + rowId + '"]');
  if (!row) return false;
  const groups = row.querySelectorAll('.inline-suggestions');
  const group = groups[groups.length - 1];
  if (!group) return false;
  const buttons = group.querySelectorAll('.inline-suggestion-btn');
  const btn = buttons[index];
  if (!btn) return false;
  btn.click();
  return true;
}"""

SET_COMPOSER_JS = """(text) => { document.getElementById('msg').value = text; return true; }"""
COMPOSER_VALUE_JS = "() => document.getElementById('msg').value"
SEND_CALLS_JS = "() => window.__sendCalls.slice()"
FOCUS_MSG_JS = "() => { document.getElementById('msg').focus(); return document.activeElement === document.getElementById('msg'); }"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _is_benign(text):
    t = text.lower()
    return any(p.lower() in t for p in BENIGN)


def _wait(page, predicate_js, timeout=5000, arg=None):
    page.wait_for_function(predicate_js, arg=arg, timeout=timeout)


def _run_checks(page, failures):
    def check(ok, msg):
        if ok:
            print(f"OK  {msg}")
        else:
            failures.append(f"  {msg}")

    page.evaluate(SETUP_JS)

    # ── Check 1: valid block -> no visible raw code, exactly N buttons, first primary ──
    labels_row1 = ["Ja, einrichten", "Erst nur Advisor"]
    page.evaluate(INJECT_ROW_JS, ["row1", _json(labels_row1), False])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 2; }", arg="row1")
    state = page.evaluate(GROUP_STATE_JS, "row1")
    check(state["groupCount"] == 1, "check1: exactly one suggestions group rendered")
    check(state["bodyNextIsGroup"] is True, "check1: group sits directly after .msg-body, inside the message")
    check(state["preHasHiddenClass"] is True, "check1: raw <pre> carries the hiding class")
    check(state["preHidden"] is True, "check1: raw <pre> is not visible (display:none)")
    check(state["headerHidden"] is True, "check1: pre-header is hidden too")
    check(state["buttonCount"] == 2, "check1: exactly N=2 buttons rendered")
    check(state["labels"] == labels_row1, "check1: button labels match the JSON array in order")
    check(state["firstIsPrimary"] is True, "check1: first button is primary")
    check(state["restAreSecondary"] is True, "check1: remaining buttons are secondary")
    check(state["groupRole"] == "group", 'check1: container has role="group"')
    check(bool(state["groupAriaLabel"]), "check1: container has an aria-label")
    for label in labels_row1:
        count = page.get_by_role("button", name=label, exact=True).count()
        check(count == 1, f"check1: a11y accessible name for button is exactly the label {label!r} (kbd hint excluded)")

    # ── Check 2: clicking button 2 sends exactly once, with that label; group then disabled ──
    page.evaluate(CLICK_BUTTON_JS, ["row1", 1])
    _wait(page, "() => window.__sendCalls.length === 1")
    sends = page.evaluate(SEND_CALLS_JS)
    check(sends == [labels_row1[1]], f"check2: exactly one send() call with the clicked label (got {sends!r})")
    state = page.evaluate(GROUP_STATE_JS, "row1")
    check(all(state["disabledStates"]), "check2: all buttons of the clicked message are disabled")
    check(all(v == "true" for v in state["ariaDisabledStates"]), "check2: all buttons carry aria-disabled=true")
    # Defensive: a second click must not send again (no double send).
    page.evaluate(CLICK_BUTTON_JS, ["row1", 0])
    page.wait_for_timeout(200)
    sends_after = page.evaluate(SEND_CALLS_JS)
    check(sends_after == sends, "check2: clicking an already-sent group's other button does not send again")

    # ── Check 3: an existing composer draft survives a suggestion click ──
    labels_row2 = ["Draft-safe option"]
    page.evaluate(INJECT_ROW_JS, ["row2", _json(labels_row2), True])  # live-stream (bare) shape
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row2")
    draft = "a draft the user was typing"
    page.evaluate(SET_COMPOSER_JS, draft)
    page.evaluate(CLICK_BUTTON_JS, ["row2", 0])
    _wait(page, "() => window.__sendCalls.length === 2")
    sends = page.evaluate(SEND_CALLS_JS)
    check(sends[1] == labels_row2[0], "check3: the suggestion label (not the draft) was sent")
    _wait(page, "(expected) => document.getElementById('msg').value === expected", arg=draft)
    restored = page.evaluate(COMPOSER_VALUE_JS)
    check(restored == draft, f"check3: composer draft was restored after send (got {restored!r})")

    # ── Check 4: Ctrl+Enter / Cmd+Enter with an EMPTY composer activates the newest primary ──
    labels_row3 = ["Newest primary", "Other option"]
    page.evaluate(INJECT_ROW_JS, ["row3", _json(labels_row3), False])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 2; }", arg="row3")
    page.evaluate(SET_COMPOSER_JS, "")
    page.evaluate(FOCUS_MSG_JS)
    chord = "Meta+Enter" if sys.platform == "darwin" else "Control+Enter"
    page.keyboard.press(chord)
    _wait(page, "() => window.__sendCalls.length === 3")
    sends = page.evaluate(SEND_CALLS_JS)
    check(sends[2] == labels_row3[0], f"check4: {chord} activated the newest message's primary suggestion")
    state = page.evaluate(GROUP_STATE_JS, "row3")
    check(all(state["disabledStates"]), "check4: the activated group is disabled afterwards (no double send)")

    # Negative: with a non-empty composer, the chord is the app's own existing
    # composer send-shortcut (unrelated to this feature) and is expected to
    # send the TYPED text under the default send-key setting — what must NOT
    # happen is OUR suggestion mechanism activating instead: the newest
    # group's primary must stay untouched (not disabled) and the literal
    # composer text, not the suggestion label, is whatever got sent.
    labels_row4 = ["Should not auto-fire"]
    page.evaluate(INJECT_ROW_JS, ["row4", _json(labels_row4), False])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row4")
    composer_text = "not empty"
    page.evaluate(SET_COMPOSER_JS, composer_text)
    page.evaluate(FOCUS_MSG_JS)
    page.keyboard.press(chord)
    page.wait_for_timeout(250)
    sends_after = page.evaluate(SEND_CALLS_JS)
    check(
        labels_row4[0] not in sends_after,
        f"check4: {chord} with a non-empty composer never sends the suggestion label (got {sends_after!r})",
    )
    check(
        sends_after == sends + [composer_text] or sends_after == sends,
        f"check4: {chord} with a non-empty composer only ever sends the literal typed text, if anything (got {sends_after!r})",
    )
    state = page.evaluate(GROUP_STATE_JS, "row4")
    check(
        not any(state["disabledStates"]),
        "check4: the newest group's own suggestion buttons are untouched by a non-empty-composer chord",
    )

    # ── Check 5: re-rendering the same message in place must not duplicate buttons ──
    labels_row5 = ["Rebuilt option A", "Rebuilt option B"]
    page.evaluate(INJECT_ROW_JS, ["row5", _json(labels_row5), False])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 2; }", arg="row5")
    before = page.evaluate(GROUP_STATE_JS, "row5")
    check(before["groupCount"] == 1, "check5: one group before re-render")
    page.evaluate(RERENDER_ROW_JS, ["row5", _json(labels_row5), False])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "const g = r ? r.querySelectorAll('.inline-suggestions') : []; "
                "return g.length === 1 && g[0].querySelectorAll('.inline-suggestion-btn').length === 2; }",
          arg="row5")
    # Give any further (incorrect) duplicate-adding mutation a moment to show up.
    page.wait_for_timeout(250)
    after = page.evaluate(GROUP_STATE_JS, "row5")
    check(after["groupCount"] == 1, f"check5: still exactly one group after an in-place re-render (got {after['groupCount']})")
    check(after["buttonCount"] == 2, f"check5: still exactly N=2 buttons after re-render (got {after['buttonCount']})")
    check(after["labels"] == labels_row5, "check5: re-rendered group has fresh, correctly-ordered labels")

    # ── Check 6: invalid blocks render no buttons and stay visible ──
    page.evaluate(INJECT_ROW_JS, ["row6a", "not json", False])
    page.evaluate(INJECT_ROW_JS, ["row6b", _json(["a", 5]), False])
    page.wait_for_timeout(300)
    for row_id, desc in (("row6a", "non-JSON body"), ("row6b", 'array with a non-string entry (["a", 5])')):
        state = page.evaluate(GROUP_STATE_JS, row_id)
        check(state["groupCount"] == 0, f"check6: no buttons rendered for invalid block ({desc})")
        check(state["preHasHiddenClass"] is False, f"check6: raw block NOT marked hidden for invalid block ({desc})")
        check(state["preHidden"] is False, f"check6: raw block stays visible for invalid block ({desc})")

    _review_checks(page, check)


INJECT_USER_ROW_JS = """(args) => {
  const [rowId, raw] = args;
  const inner = document.getElementById('msgInner');
  const row = document.createElement('div');
  row.className = 'msg-row';
  row.setAttribute('data-role', 'user');
  row.setAttribute('data-test-row', rowId);
  const body = document.createElement('div');
  body.className = 'msg-body';
  body.innerHTML = '<p>Pasted example for ' + rowId + '</p>' +
    '<div class="pre-header">suggestions</div>' +
    '<pre><code class="language-suggestions"></code></pre>';
  row.appendChild(body);
  inner.appendChild(row);
  row.querySelector('code').textContent = raw;
  return true;
}"""


def _review_checks(page, check):
    """Regressions from the review on PR #9361 (busy flag, liveness, user rows,
    draft ownership). Runs after the SPEC checks, on the same page."""
    # ── Check 7: the busy flag really disables buttons ──
    page.evaluate(INJECT_ROW_JS, ["row7", _json(["Weiter"]), False])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row7")
    page.evaluate("() => { S.busy = true; return S.busy; }")
    page.wait_for_timeout(400)  # BUSY_SYNC_MS is 150ms
    disabled_busy = page.evaluate(
        "() => document.querySelector('[data-test-row=\"row7\"] .inline-suggestion-btn').disabled")
    check(disabled_busy is True, "check7: a running turn (S.busy) disables the suggestion buttons")
    page.evaluate("() => { S.busy = false; return S.busy; }")
    page.wait_for_timeout(400)
    enabled_after = page.evaluate(
        "() => !document.querySelector('[data-test-row=\"row7\"] .inline-suggestion-btn').disabled")
    check(enabled_after is True, "check7: buttons come back once the turn finished")

    # ── Check 8: a fenced example in a USER message is never turned into buttons ──
    page.evaluate(INJECT_USER_ROW_JS, ["row8", _json(["Yes", "No"])])
    page.wait_for_timeout(350)
    user_state = page.evaluate(GROUP_STATE_JS, "row8")
    check(user_state["groupCount"] == 0, "check8: user-authored fence renders no reply buttons")
    check(user_state["preHidden"] is False, "check8: user-authored fence stays visible as content")

    # ── Check 9: only the last message keeps live buttons ──
    older_disabled = page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-test-row=\"row7\"] .inline-suggestion-btn'))"
        ".every(b => b.disabled)")
    check(older_disabled is True,
          "check9: an older reply's buttons are disabled once a later message exists")

    page.evaluate(INJECT_ROW_JS, ["row9", _json(["Neu primär"]), True])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row9")
    newest_state = page.evaluate(GROUP_STATE_JS, "row9")
    check(newest_state["disabledStates"] == [False], "check9: the newest reply's buttons are live")

    sends_before = len(page.evaluate(SEND_CALLS_JS))
    page.evaluate(SET_COMPOSER_JS, "")
    page.evaluate(FOCUS_MSG_JS)
    page.keyboard.press("Control+Enter")
    page.wait_for_timeout(250)
    sends_after = page.evaluate(SEND_CALLS_JS)
    check(sends_after[sends_before:] == ["Neu primär"],
          f"check9: Ctrl+Enter answers the newest reply only (got {sends_after[sends_before:]!r})")

    # ── Check 10: a restored draft is saved again for the chat it belongs to ──
    page.evaluate(INJECT_ROW_JS, ["row10", _json(["Senden"]), True])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row10")
    page.evaluate("""() => {
      window.__draftSaves = [];
      S.session = { session_id: 'sid-A' };
      window._saveComposerDraft = function(sid, text, files){
        window.__draftSaves.push([sid, text]);
      };
      window.__sendCalls.length = 0;
      window.send = function(){
        window.__sendCalls.push(document.getElementById('msg').value);
        return Promise.resolve();
      };
    }""")
    page.evaluate(SET_COMPOSER_JS, "mein Entwurf")
    page.evaluate(CLICK_BUTTON_JS, ["row10", 0])
    page.wait_for_timeout(300)
    draft_saves = page.evaluate("() => window.__draftSaves.slice()")
    restored = page.evaluate(COMPOSER_VALUE_JS)
    check(draft_saves == [["sid-A", "mein Entwurf"]],
          f"check10: the restored draft is persisted for its chat (got {draft_saves!r})")
    check(restored == "mein Entwurf", f"check10: composer still gets the draft back (got {restored!r})")

    # ── Check 11: switching chats mid-send must not write into the other chat ──
    page.evaluate(INJECT_ROW_JS, ["row11", _json(["Senden"]), True])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row11")
    page.evaluate("""() => {
      window.__draftSaves = [];
      S.session = { session_id: 'sid-A' };
      document.getElementById('msg').value = 'Draft für A';
      window.send = function(){
        window.__sendCalls.push(document.getElementById('msg').value);
        // The user opens another chat while the send is still in flight.
        S.session = { session_id: 'sid-B' };
        document.getElementById('msg').value = '';
        return Promise.resolve();
      };
    }""")
    page.evaluate(CLICK_BUTTON_JS, ["row11", 0])
    page.wait_for_timeout(300)
    composer_b = page.evaluate(COMPOSER_VALUE_JS)
    saves_switch = page.evaluate("() => window.__draftSaves.slice()")
    check(composer_b == "" or composer_b == "Senden",
          f"check11: chat B's composer does not receive chat A's draft (got {composer_b!r})")
    check(saves_switch == [["sid-A", "Draft für A"]],
          f"check11: the draft is parked on its own chat (got {saves_switch!r})")

    # ── Check 12: a draft typed while the send is in flight is never overwritten ──
    page.evaluate(INJECT_ROW_JS, ["row12", _json(["Senden"]), True])
    _wait(page, "(rowId) => { const r = document.querySelector('[data-test-row=\"' + rowId + '\"]'); "
                "return r && r.querySelectorAll('.inline-suggestion-btn').length === 1; }", arg="row12")
    page.evaluate("""() => {
      window.__draftSaves = [];
      S.session = { session_id: 'sid-C' };
      window._saveComposerDraft = function(sid, text, files){
        window.__draftSaves.push([sid, text]);
      };
      document.getElementById('msg').value = 'alter Entwurf';
      window.send = function(){
        // The user keeps typing while /api/chat/start is still pending.
        document.getElementById('msg').value = 'neuer Entwurf';
        return Promise.resolve();
      };
    }""")
    page.evaluate(CLICK_BUTTON_JS, ["row12", 0])
    page.wait_for_timeout(300)
    typed = page.evaluate(COMPOSER_VALUE_JS)
    saves_typed = page.evaluate("() => window.__draftSaves.slice()")
    check(typed == "neuer Entwurf", f"check12: the newer draft stays in the composer (got {typed!r})")
    check(saves_typed == [],
          f"check12: the stale captured draft is not saved over the newer one (got {saves_typed!r})")


def _json(value):
    import json
    return json.dumps(value)


def _server_env(state_dir, port):
    """Env for a throwaway server: isolated state, no credentials, no password."""
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HOME": state_dir,
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
        # A HERMES_WEBUI_PASSWORD leaked from the parent shell (e.g. the one
        # protecting a real, already-running WebUI on another port) would
        # otherwise gate this throwaway test server behind /login too and
        # #msg would never attach. Explicitly blank it out (matches
        # tests/conftest.py's subprocess server setup).
        "HERMES_WEBUI_PASSWORD": "",
    })
    return env


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    port = int(os.getenv("INLINE_SUGGESTIONS_PORT") or _free_port())
    base = f"http://127.0.0.1:{port}"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-inline-suggestions-")
    env = _server_env(state_dir, port)

    # A server already listening on the port would answer /health and the checks would
    # silently run against ITS code, not this checkout's. Refuse instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"SETUP FAIL: port {port} is already in use; set INLINE_SUGGESTIONS_PORT", file=sys.stderr)
            return 2

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(base, timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = browser.new_context(base_url=base, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            console_errors = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_selector("#msg", state="attached", timeout=15000)
            page.wait_for_timeout(1000)

            try:
                _run_checks(page, failures)
            except Exception as exc:  # noqa: BLE001 - surface as a failure, not a crash
                failures.append(f"  exception during checks: {exc!r}")

            meaningful = [e for e in console_errors if not _is_benign(e)]
            for e in meaningful:
                failures.append(f"  console/pageerror: {e}")

            # ── Opt-out phase: HERMES_WEBUI_SUGGESTIONS=0 disables the feature on
            # both sides — no buttons in the DOM, raw block left visible, and the
            # server-side prompt stays out of the ephemeral system prompt.
            opt_port = _free_port()
            opt_state = tempfile.mkdtemp(prefix="hermes-inline-suggestions-off-")
            opt_env = _server_env(opt_state, opt_port)
            opt_env["HERMES_WEBUI_SUGGESTIONS"] = "0"
            opt_base = f"http://127.0.0.1:{opt_port}"
            opt_log = open(os.path.join(opt_state, "server.log"), "w")
            opt_proc = subprocess.Popen(
                [sys.executable, server_py], cwd=repo_root, env=opt_env,
                stdout=opt_log, stderr=subprocess.STDOUT,
            )
            try:
                if not _wait_for_health(opt_base, timeout=30):
                    failures.append("  opt-out: server did not become healthy")
                else:
                    ctx_off = browser.new_context(base_url=opt_base, viewport={"width": 1280, "height": 900})
                    page_off = ctx_off.new_page()
                    page_off.goto("/", wait_until="domcontentloaded")
                    page_off.wait_for_selector("#msg", state="attached", timeout=15000)
                    page_off.wait_for_timeout(800)
                    flag = page_off.evaluate("() => (window.__HERMES_CONFIG__ || {}).suggestionsEnabled")
                    if flag is not False:
                        failures.append(f"  opt-out: __HERMES_CONFIG__.suggestionsEnabled is {flag!r}, expected False")
                    page_off.evaluate(SETUP_JS)
                    page_off.evaluate(INJECT_ROW_JS, ["off-1", '["Ja", "Nein"]', False])
                    page_off.wait_for_timeout(700)
                    off_state = page_off.evaluate(GROUP_STATE_JS, "off-1")
                    if off_state and off_state["groupCount"] != 0:
                        failures.append(f"  opt-out: buttons rendered although disabled ({off_state['groupCount']})")
                    if off_state and off_state["preHidden"]:
                        failures.append("  opt-out: raw block was hidden although the feature is off")
                    print("OK  opt-out: HERMES_WEBUI_SUGGESTIONS=0 renders no buttons and keeps the block visible")
                    ctx_off.close()
            finally:
                opt_proc.terminate()
                try:
                    opt_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    opt_proc.kill()

            ctx.close()
            browser.close()

        if failures:
            print("\nINLINE SUGGESTIONS FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nINLINE SUGGESTIONS PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
