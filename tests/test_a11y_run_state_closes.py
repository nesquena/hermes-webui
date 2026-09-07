"""Turn end must close IN THE VIEW, without refreshing the page.

User report (screen reader): "the model finishes writing after a longer time and
I get for example 5. Hermes, working / HHermes / Hermes is working / Idle, and
after refresh I have the turn; it should not work like that".

Measured causes — three separate ones, all from our earlier accessibility changes:

1. `hideLiveRunStatus(sid)` returned on the FIRST line when the session id did
   not match the remembered one, and then did NOT call a11yRunFinished(). The
   run state stayed open forever.
2. `_a11yTurnIsLive()` treated "run is active" as evidence that the LAST
   assistant turn was live. Combined with (1), every finished turn looked live:
   the heading read "working", and `_a11yReorderTurn` intentionally skips live
   turns, so the answer content stayed BEHIND the activity log. Refresh rebuilt
   the view from scratch and therefore "fixed" the symptom.
3. The role icon is a circle with the FIRST LETTER of the name ("H") rendered
   next to the label. Without aria-hidden the screen reader read the letter as a
   separate text node: "H Hermes".

Evidence from the running application after the fix (full turn cycle, Edge +
CDP): while running the heading is "11. Hermes, working"; after completion
WITHOUT refresh it is "26. Hermes 19:09", content present (102 characters), run
closed, no duplicated letter; the pre-refresh view is identical to the
post-refresh view. Accessibility tree: 0 standalone capital letters, 7/7 icons
with aria-hidden, with 7/7 visual visibility preserved.
"""

from pathlib import Path

import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class TestRunStateAlwaysCloses:

    def test_hide_status_closes_run_state_before_the_session_guard(self):
        """The sid guard must not skip closing the run state."""
        idx = UI_JS.find("function hideLiveRunStatus(sid){")
        assert idx > 0, "hideLiveRunStatus nie znaleziona"
        blok = UI_JS[idx:idx + 1400]
        poz_finish = blok.find("a11yRunFinished()")
        poz_guard = blok.find("if(sid&&_liveRunStatusSessionId&&sid!==_liveRunStatusSessionId) return;")
        assert poz_finish > 0, "brak calls a11yRunFinished"
        assert poz_guard > 0, "brak bramki na identyfikator sesji"
        assert poz_finish < poz_guard, (
            "a11yRunFinished() must be BEFORE the mismatched sid return - otherwise "
            "the 'Hermes is working' state stays open and the view never closes the turn."
        )

    def test_stream_done_also_closes_run_state(self):
        """Second, independent close path - one of them can be skipped."""
        messages = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
        assert "a11yRunFinished();" in messages, (
            "The stream-finished handler must also close the run state."
        )


class TestLivenessComesFromTheTurnItself:
    """Turn liveness must come from its OWN markers, not from global state."""

    def test_global_run_state_is_not_used_as_liveness_evidence(self):
        idx = A11Y_JS.find("function _a11yTurnIsLive(row){")
        assert idx > 0
        koniec = A11Y_JS.find("\n}", idx)
        body = A11Y_JS[idx:koniec]
        # What matters is CODE, not comments: the function body intentionally
        # explains why we do not use this clue, and the word a11yRunIsActive
        # appears there. The first version of this test failed on the comment
        # itself - it would check literal text instead of behavior.
        bez_komentarzy = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith("//")
        )
        assert "a11yRunIsActive" not in bez_komentarzy, (
            "The clue 'run active => last turn live' turned ONE missed end signal "
            "into a permanently stuck view (heading 'working', content behind the "
            "log). We determine liveness only from turn markers."
        )

    def test_liveness_markers_belong_to_the_row(self):
        idx = A11Y_JS.find("function _a11yTurnIsLive(row){")
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        for marker in ("liveAssistantTurn", "dataset.live", ".stream-cursor"):
            assert marker in body, f"missing liveness marker: {marker}"


class TestRoleIconIsDecorative:

    def test_role_icon_is_hidden_from_screen_readers(self):
        idx = UI_JS.find("function _assistantRoleHtml(")
        assert idx > 0
        blok = UI_JS[idx:idx + 1400]
        assert 'class="role-icon assistant" aria-hidden="true"' in blok, (
            "The circle with the first letter of the name repeats the adjacent label. "
            "Without aria-hidden the screen reader reads 'H Hermes' - reported as 'HHermes'."
        )

    def test_role_name_is_still_exposed(self):
        """We hide DECORATION, not information - the name must remain."""
        idx = UI_JS.find("function _assistantRoleHtml(")
        blok = UI_JS[idx:idx + 1400]
        assert '<span class="msg-role-name">' in blok
        assert 'aria-hidden="true">${esc(_bn)}' not in blok, (
            "The assistant name must not be hidden from the screen reader."
        )

    def test_snippet_filter_drops_role_parts_independently(self):
        """The snippet filter must not assume the label parts live under .msg-role."""
        assert "'.msg-role', '.role-icon', '.msg-role-name'," in A11Y_JS, (
            "The icon and role name must be filtered SEPARATELY - filtering only "
            "the parent was an assumption about the markup structure."
        )


class TestEmptyMeansDoneIdleMeansPause:
    """Distinction agreed with the user (screen reader).

    "If it is truly doing nothing, let it be empty. If for example we are waiting
    and something more is about to appear, but the model is momentarily doing
    nothing, then Idle."

    So: EMPTY = end of turn, "Idle" = temporary silence DURING a run. Previously
    a11yRunFinished() wrote "Idle" there, so after every completed answer the
    user encountered a message suggesting that something was still happening.
    """

    def test_finished_run_clears_the_status_field(self):
        idx = A11Y_JS.find("function a11yRunFinished(){")
        assert idx > 0
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        assert "el.textContent = '';" in body, (
            "After completion the status field must be EMPTY."
        )
        kod = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))
        assert "a11y_run_idle" not in kod, (
            "Turn end must not write 'Idle' - that word is reserved "
            "for a pause during a run."
        )

    def test_idle_pause_helper_exists_and_requires_an_active_run(self):
        idx = A11Y_JS.find("function a11yRunIdlePause(){")
        assert idx > 0, "missing a11yRunIdlePause - 'Idle' would have nowhere to originate"
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        assert "if (!_a11yRunActive) return;" in body, (
            "'Idle' only makes sense during an ACTIVE run; after completion the field is empty."
        )
        assert "a11y_run_idle" in body

    def test_refresh_switches_to_idle_when_nothing_is_happening(self):
        """Without this call the helper would be code that nobody calls."""
        idx = A11Y_JS.find("function _a11yRefreshRunStatus(){")
        assert idx > 0
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        assert "a11yRunIdlePause()" in body, (
            "The quiet status must switch itself to 'Idle' when a running turn has "
            "neither current activity nor content growth."
        )

    def test_output_probe_looks_only_at_the_live_turn(self):
        """The progress fingerprint must inspect the LIVE TURN, not the whole log.

                The earlier version (`_a11yRunProducedOutput`) had a fallback lookup into
                #messages that found prose from the PREVIOUS, completed turn - the
                function then always returned true and a pause would never be detected.
                The mechanism has been replaced by a progress fingerprint, but the same
                condition still applies.
        """
        idx = A11Y_JS.find("function _a11yRunProgressFingerprint(){")
        assert idx > 0, "missing progress fingerprint"
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        assert "getElementById('messages')" not in body, (
            "Checking for content growth must target the LIVE TURN; looking at the "
            "entire conversation log will always find old prose and a pause would "
            "never be detected."
        )
        assert "liveAssistantTurn" in body

    def test_status_stays_quiet(self):
        """Quiet status: the screen reader does not read it automatically, the user moves there."""
        assert "el.setAttribute('aria-live', 'off');" in A11Y_JS

    def test_idle_threshold_measures_lack_of_change_not_lack_of_element(self):
        """The activity card REMAINS on screen even when the model is silent.

                The first version asked "is there an activity card" and therefore never
                entered 'Idle' - measured: the status stubbornly showed
        "Hermes is working — Processed 0s", even though nothing was growing.
        """
        assert "A11Y_RUN_IDLE_AFTER_MS" in A11Y_JS
        idx = A11Y_JS.find("function _a11yRunProgressFingerprint(){")
        assert idx > 0, "missing progress fingerprint - cannot measure the ABSENCE of change"
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        assert "assistant-segment" in body, "the fingerprint must include prose length"
        assert "_a11yCurrentActivity()" in body
        odsw = A11Y_JS[A11Y_JS.find("function _a11yRefreshRunStatus(){"):]
        assert "_a11yRunSilenceMs() >= A11Y_RUN_IDLE_AFTER_MS" in odsw, (
            "'Idle' must come from a SILENCE threshold, not from the absence of an element on screen."
        )


class TestForeignSessionStillRunningIsAnnounced:
    """A session driven externally (TUI/Telegram) must also emit a signal.

    Report: "if I open in webui a running session that I started elsewhere, then
    if something is happening there I also want to know about it and I want the
    timer to run - the session is alive and I have no information that it is alive".

    Measured BEFORE: for a running TUI session the server returned active_stream_id=null
    and is_streaming=false (webui sets them only for ITS OWN turns), although in
    state.db the last message had a timestamp from 4 s earlier. All four
    channels were silent: indicator hidden, quiet status not created, run state
    off, heading without "working".

    Measured AFTER (session 20260817_175957_970e47, live during measurement):
    heading "2. Hermes, working", counter 5->10->15->20->25->30 s (TICKING),
    "Idle — 20 s" during a pause, one-time announcement "Hermes is working",
    status still quiet (aria-live=off).
    """

    def test_watchdog_exists_and_is_started(self):
        assert "function a11yWatchForeignRun(){" in A11Y_JS
        assert "window.a11yWatchForeignRun = a11yWatchForeignRun;" in A11Y_JS
        assert "DOMContentLoaded" in A11Y_JS, (
            "The watchdog must start by itself - otherwise the signal depends on whether someone calls it."
        )

    def test_liveness_is_growth_of_last_message_at_not_a_flag(self):
        idx = A11Y_JS.find("async function _a11yForeignPoll(){")
        assert idx > 0
        body = A11Y_JS[idx:A11Y_JS.find("\nfunction a11yWatchForeignRun", idx)]
        assert "last_message_at" in body, (
            "The only field that grows independently of stream ownership."
        )
        assert "active_stream_id" not in body, (
            "active_stream_id is empty for TUI/Telegram sessions - relying on it was exactly the cause of silence."
            ""
        )
        assert "stamp > _a11yForeignLastStamp" in body, "growth must be measured"

    def test_watchdog_defers_to_this_tab_when_it_owns_the_turn(self):
        idx = A11Y_JS.find("async function _a11yForeignPoll(){")
        body = A11Y_JS[idx:A11Y_JS.find("\nfunction a11yWatchForeignRun", idx)]
        assert "_a11yThisTabOwnsTurn()" in body, (
            "When this tab owns the turn, the regular stream path owns the state - two owners are a race."
            ""
        )

    def test_watchdog_extinguishes_itself_so_state_cannot_hang(self):
        """This clue must not repeat the 'working forever' defect."""
        assert "A11Y_FOREIGN_DONE_AFTER_MS" in A11Y_JS
        idx = A11Y_JS.find("async function _a11yForeignPoll(){")
        body = A11Y_JS[idx:A11Y_JS.find("\nfunction a11yWatchForeignRun", idx)]
        assert "a11yRunFinished()" in body, (
            "The watchdog MUST clear the state itself after measured lack of growth."
        )

    def test_network_failure_is_not_treated_as_finished(self):
        """Fail closed: lack of a response is not evidence that the work is finished."""
        idx = A11Y_JS.find("async function _a11yForeignPoll(){")
        body = A11Y_JS[idx:A11Y_JS.find("\nfunction a11yWatchForeignRun", idx)]
        assert "catch (_e) { return; }" in body

    def test_heading_reads_working_for_a_foreign_run(self):
        """In a foreign session there is NO live turn in the DOM, and the heading must say 'working'."""
        idx = A11Y_JS.find("function _a11yTurnIsLive(row){")
        body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
        assert "_a11yForeignOwnsRun" in body
        assert "typeof _a11yForeignOwnsRun !== 'undefined'" in body, (
            "The variable is declared with let LATER in the file - a bare reference "
            "can throw ReferenceError and break the whole render."
        )


class TestSilenceBaselineBelongsToTheRun:
    """A new run must not inherit silence from before itself.

    Measured symptom (18.08.2026, CDP on the running app + node vm):
    immediately after the state lit up, the quiet status showed "Idle — 0 s". That message
    is INTERNALLY CONTRADICTORY: the run counter says 0 s, while the word "Idle" requires
    A11Y_RUN_IDLE_AFTER_MS = 12 s WITHOUT a change in the progress fingerprint.

    Cause: `_a11yRunLastFingerprint` / `_a11yRunLastChangeAt` are module-level
    and were reset only on a fingerprint CHANGE, so when the user simply watched
    an open conversation, the marker aged forever and the first measurement of a
    new run immediately exceeded the threshold.

    Effect for a screen reader user: they hear "Idle" (nothing is happening)
    exactly at the moment when the work is STARTING - so the status lies in the
    opposite direction from the previously fixed "working after completion". WCAG 4.1.3.

    The tests execute REAL code in node (vm), they do not inspect source text -
    the fix depends on assignment order over time, which grep cannot measure.
    """

    HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(SRC, 'utf8');
function makeEl(id) {
  return {
    id, textContent: '', className: '', _attrs: {},
    setAttribute(k, v){ this._attrs[k] = v; },
    getAttribute(k){ return this._attrs[k] ?? null; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    appendChild(){}, insertBefore(){}, parentElement: null,
  };
}
const status = makeEl('a11yRunStatus');
const context = {
  console,
  setInterval(){ return 1; }, clearInterval(){}, setTimeout(){ return 1; }, clearTimeout(){}, Date,
  document: {
    getElementById(id){ return id === 'a11yRunStatus' ? status : null; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    createElement(){ return makeEl('nowy'); }, addEventListener(){},
    body: { appendChild(){} }, readyState: 'complete',
  },
  window: { addEventListener(){} },
  location: { pathname: '/session/abc' },
  t(k){ return {a11y_run_working: 'Hermes is working', a11y_run_idle: 'Idle',
                a11y_run_started: 'Hermes is working'}[k] || k; },
  fetch(){ return Promise.reject(new Error('no network in test')); },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(src, context);
const out = vm.runInContext(SCENARIO, context);
console.log(JSON.stringify(out));
"""

    def _run(self, scenario):
        if not NODE:
            pytest.skip("node niedostepny")
        script = (
            self.HARNESS
            .replace("SRC", json.dumps(str(REPO / "static" / "a11y-helpers.js")))
            .replace("SCENARIO", json.dumps(scenario))
        )
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
        assert r.returncode == 0, f"node failed: {r.stderr[-2000:]}"
        return json.loads(r.stdout)

    def test_new_run_does_not_inherit_silence_from_before_it(self):
        """A run that just started says 'working' - even after long inactivity."""
        out = self._run("""(() => {
          _a11yRunSilenceMs();                        // establish the fingerprint baseline
          _a11yRunLastChangeAt = Date.now() - 20000;  // 20 s of PAGE inactivity
          a11yRunStarted();
          return {text: document.getElementById('a11yRunStatus').textContent,
                  counter: _a11yRunElapsedText()};
        })()""")
        assert out["counter"] == "0 s"
        assert out["text"].startswith("Hermes is working"), (
            f"A new run reports {out['text']!r} with counter {out['counter']!r} — "
            "'Idle' requires 12 s without change, so this message is self-contradictory."
        )

    def test_real_silence_during_a_run_still_reports_idle(self):
        """Negative control: the fix must not kill the function it protects."""
        out = self._run("""(() => {
          a11yRunStarted();
          _a11yRunLastChangeAt = Date.now() - 20000;  // 20 s of silence ALREADY DURING the run
          _a11yRefreshRunStatus();
          return {text: document.getElementById('a11yRunStatus').textContent};
        })()""")
        assert out["text"].startswith("Idle"), (
            "A pause during work must still produce 'Idle' - otherwise instead of a wrong "
            "message we have no information."
        )

    def test_second_run_after_a_finished_one_is_clean(self):
        """A run releases its baseline at the end, so the next one starts from zero."""
        out = self._run("""(() => {
          a11yRunStarted();
          _a11yRunLastChangeAt = Date.now() - 20000;
          a11yRunFinished();
          const poKoncu = document.getElementById('a11yRunStatus').textContent;
          a11yRunStarted();
          return {poKoncu, drugi: document.getElementById('a11yRunStatus').textContent};
        })()""")
        assert out["poKoncu"] == "", "Turn end leaves an EMPTY field."
        assert out["drugi"].startswith("Hermes is working"), (
            f"The second run reports {out['drugi']!r} — it inherited the marker from the first one."
        )

    def test_idle_never_appears_without_an_active_run(self):
        """Empty = end. Without a run, no silence may trigger 'Idle'."""
        out = self._run("""(() => {
          a11yRunStarted();
          a11yRunFinished();
          _a11yRunLastChangeAt = Date.now() - 60000;
          _a11yRefreshRunStatus();
          a11yRunIdlePause();
          return {text: document.getElementById('a11yRunStatus').textContent,
                  isActive: a11yRunIsActive()};
        })()""")
        assert out["isActive"] is False
        assert out["text"] == ""

    def test_baseline_reset_is_one_shared_helper_not_copies(self):
        """Chokepoint, not N parallel assignments (guideline 8 from GUIDELINES)."""
        assert "function _a11yResetSilenceBaseline(){" in A11Y_JS
        for fn in ("function a11yRunStarted(){", "function a11yRunFinished(){"):
            idx = A11Y_JS.find(fn)
            assert idx > 0, f"missing {fn}"
            body = A11Y_JS[idx:A11Y_JS.find("\n}", idx)]
            assert "_a11yResetSilenceBaseline()" in body, (
                f"{fn} must release the silence baseline through a SHARED helper - the run boundary "
                "is the only place where this measurement may be reset."
            )
            assert "_a11yRunLastChangeAt =" not in body, (
                "A copied assignment instead of a helper will drift on the next change."
            )

