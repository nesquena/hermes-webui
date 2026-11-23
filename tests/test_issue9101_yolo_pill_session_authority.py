"""#9101: the YOLO pill must show only the open session's server state, and its
"click to disable" must never enable YOLO or approve a pending command.

The behavioural cases run the REAL loadSession() (sessions.js), the YOLO state
section of messages.js, the YOLO toggle section of commands.js and the pill's
real inline onclick from index.html in isolated Node vm contexts ("tabs")
against one shared fake server: tests/fixtures/issue9101_yolo_pill_harness.cjs.
Each case asserts the safe invariant (driver exit 0 = held, 1 = violated,
2 = harness error).

Mechanisms covered (letters follow the issue investigation):
  a/a2  a late read for session A paints A's state onto B, then the pill click
        enables YOLO for B (a2: or approves B's visible approval card)
  b/b2  a read answered before a /yolo toggle lands after it and overwrites it
  c/c2  a same-session refresh, or an idle load / page reload, hides an ON flag
  d     no cross-tab sync: a stale pill in tab 2 must not re-enable on click
  e/e2  any stale ON pill: its click must send a disable, never an enable
        (e2: and never approve the visible approval card)
  f     a pill painted for A, clicked after the open session changed without
        loadSession(): it must not write to the new session at all
  g/h   a failed switch leaves the previous session open with its pill
  i/i2  overlapping /yolo commands post twice (both enable, or enable then
        disable under an "on" pill)
  j/j2  a non-boolean GET /api/session/yolo answer paints or hides the pill
  k1-k4 a blank-page action (workspace prompt/switch, new file/folder) mints
        a session without loadSession() and keeps the old session's pill
  k5    an apperror after a compression rotation installs the continuation
        without dropping the old pill or reading the new session's flag
  #9101 follow-up (server revision; client YOLO paths, /yolo included, are
  master's code):
  r5b   the hide setting never hides an ON pill
  r5c   with YOLO on, the hide setting shrinks the pill to its compact ⚡
        form; the ON pill carries an explicit aria-label
  r21   older OFF reads landing around a /yolo never hide its ON
  r2d   a late `yolo-changed` frame never overrides a newer read
  r8b/r8e/r9/r9c/r11 restart orderings the pre-#9101 rules get right
  r13-r17 late answers from a server run the tab may not have seen (r16/r17:
        a restart it never observed) end as master does, synced
  KNOWN_GAPS_AS_ON_MASTER: orderings that need YOLO answers ordered by the
        server revision (cross-tab frames, older-vs-newer answers, restart
        snapshots), which the client cannot do safely across a restart it
        has not seen, and r7 (a stale /yolo read undoing a later pill
        disable), which needs /yolo to send the revision it read; strict
        xfails with master's end state (follow-up issue)
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "issue9101_yolo_pill_harness.cjs"
STATIC = ROOT / "static"
NODE = shutil.which("node")

SCENARIOS = [
    "a_late_cross_session_read_then_pill_click_enables_B",
    "a2_late_read_then_pill_click_approves_B_visible_card",
    "b_stale_read_overwrites_toggle_off_then_click_enables",
    "b2_stale_read_hides_toggle_on",
    "c_same_session_refresh_hides_enabled",
    "c2_fresh_load_of_idle_session_hides_enabled",
    "d_cross_tab_stale_pill_click_reenables",
    "e_stale_on_pill_click_after_server_restart_enables",
    "e2_stale_on_pill_click_with_visible_card_approves",
    "f_pill_click_after_untracked_session_swap_enables_new_session",
    "g_enable_during_failed_switch_keeps_open_session_pill",
    "h_failed_switch_reads_back_open_session_flag",
    "i_overlapping_yolo_commands_both_enable",
    "i2_yolo_while_first_post_in_flight_desyncs_pill",
    "i3_yolo_for_other_session_while_first_in_flight_is_dropped",
    "j_malformed_read_paints_on_pill",
    "j2_malformed_read_hides_on_pill",
    "k1_prompt_workspace_path_install_keeps_stale_pill",
    "k2_switch_to_workspace_install_keeps_stale_pill",
    "k3_prompt_new_file_install_keeps_stale_pill",
    "k4_prompt_new_folder_install_keeps_stale_pill",
    "k5_apperror_rotation_keeps_stale_pill_and_skips_read",
    "l_malformed_read_never_toggles",
]

REVISION_SCENARIOS = [
    "r2d_older_frame_never_overrides_newer_read",
    "r5b_hide_setting_keeps_on_pill_visible",
    "r5c_hide_setting_shrinks_on_pill_to_icon",
    "r8b_post_restart_answer_is_accepted",
    "r8e_old_epoch_read_from_an_older_load_is_dropped",
    "r9_first_sync_overlap_shows_the_last_answer",
    "r9c_first_sync_overlap_applies_the_last_off",
    "r11_late_old_epoch_answers_cause_no_reads",
    "r13_late_old_post_then_old_newer_read",
    "r14_old_reads_revision_order_mirror",
    "r15_late_old_writes_after_epoch_change",
    "r16_unobserved_restart_old_post_then_old_newer_read",
    "r17_unobserved_restart_old_writes",
    "r21_older_off_reads_around_yolo_never_hide_its_on",
]

# Orderings master (b1935597c040, b8fb4024d3ae) also gets wrong and that only
# client-side revision ordering fixed. The client cannot order answers across
# a server restart it has not seen (r16/r17), so it keeps master's rules and
# master's end state here; a follow-up issue owns them (a server-confirmed
# epoch first). r7 needs /yolo to send the revision it read (the server's 409
# is in place); /yolo is master's code, so it keeps master's end state too.
# Strict: a fix makes these XPASS and fail, so move them up.
KNOWN_GAPS_AS_ON_MASTER = [
    "r1_cross_tab_yolo_reaches_open_tab_via_session_stream",
    "r2_older_write_answer_never_overrides_newer_frame",
    "r2b_newer_read_answer_overrides_older_write_answer",
    "r2c_older_yolo_answer_never_overrides_newer_frame",
    "r2e_older_approval_answer_never_overrides_newer_read",
    "r3_restart_snapshot_clears_stale_on_pill",
    "r5_failed_install_read_keeps_on_pill_via_snapshot",
    "r7_stale_yolo_read_cannot_undo_later_pill_disable",
    "r8_old_epoch_answer_never_overrides_new_epoch",
    "r8c_old_epoch_read_after_newer_read_never_hides_on",
    "r8d_old_epoch_post_answer_never_shows_on_after_restart",
    "r9b_old_epoch_approval_answer_after_new_epoch_is_dropped",
    "r11c_old_process_answers_keep_their_revision_order",
    "r12_late_old_on_then_stale_new_off_never_hides_on",
]


def _run(name):
    proc = subprocess.run(
        [NODE or "node", str(DRIVER), name, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = proc.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1])
    except (IndexError, ValueError):
        out = {"raw_stdout": proc.stdout, "stderr": proc.stderr}
    return proc.returncode, out


@pytest.mark.skipif(NODE is None, reason="node required")
@pytest.mark.parametrize(
    "name",
    [
        # The harness drives the real read path end to end.
        "control_streaming_load_shows_own_state",
        # A disable the server rejects keeps the authoritative error payload.
        "control_failed_pill_disable_keeps_authoritative_state",
        # The /yolo in-flight guard is released after a failure or a stale read.
        "control_yolo_guard_released_after_failure_and_stale_switch",
        # A same-session apperror keeps the view and makes no extra YOLO read.
        "control_apperror_same_session_keeps_view_without_read",
    ],
)
def test_controls(name):
    code, out = _run(name)
    assert code == 0, out


@pytest.mark.skipif(NODE is None, reason="node required")
@pytest.mark.parametrize("name", SCENARIOS)
def test_yolo_pill_tracks_open_session_and_only_disables(name):
    code, out = _run(name)
    assert code != 2, f"harness error: {out}"
    assert code == 0, f"{out.get('message')}\n{json.dumps(out.get('detail'), indent=1)}"


@pytest.mark.skipif(NODE is None, reason="node required")
@pytest.mark.parametrize("name", REVISION_SCENARIOS)
def test_yolo_view_follows_server_revision(name):
    code, out = _run(name)
    assert code != 2, f"harness error: {out}"
    assert code == 0, f"{out.get('message')}\n{json.dumps(out.get('detail'), indent=1)}"


@pytest.mark.skipif(NODE is None, reason="node required")
@pytest.mark.parametrize("name", KNOWN_GAPS_AS_ON_MASTER)
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="needs revision ordering; as on master (follow-up issue)")
def test_known_gaps_match_master(name):
    code, out = _run(name)
    if code == 2:
        raise RuntimeError(f"harness error: {out}")
    assert code == 0, f"{out.get('message')}\n{json.dumps(out.get('detail'), indent=1)}"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _function_body(src, name):
    match = re.search(r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    assert match, f"{name} not found"
    end = src.find("\n}\n", match.start())
    assert end > 0, f"{name} has no column-zero close"
    return src[match.start():end + 2]


def test_pill_click_carries_disable_intent():
    pill = re.search(r'<button[^>]*id="yoloPill"[^>]*>', _read("index.html"))
    assert pill, "yoloPill missing from index.html"
    assert 'onclick="disableYoloFromPill()"' in pill.group(0)
    assert 'data-i18n-title="yolo_pill_title_active"' in pill.group(0)
    body = _function_body(_read("commands.js"), "disableYoloFromPill")
    assert "enabled:false" in body.replace(" ", "")
    assert "data-yolo-session" in body
    # The pill never computes a toggle and never routes into the approval card.
    assert "toggleYoloFromApproval" not in body
    assert "/api/session/yolo?" not in body


def test_yolo_view_has_one_writer():
    """Every write of the client's YOLO view goes through _applyYoloState."""
    sources = {name: _read(name) for name in ("messages.js", "commands.js", "sessions.js")}
    applier = _function_body(sources["messages.js"], "_applyYoloState")
    writes = re.compile(r"\b(_yoloEnabled|_yoloView)\s*=(?!=)")
    for name, src in sources.items():
        for match in writes.finditer(src):
            line = src[src.rfind("\n", 0, match.start()) + 1:src.find("\n", match.start())]
            if line.startswith("let ") or line.lstrip().startswith("//"):
                continue  # module declaration or comment
            start = sources["messages.js"].find(applier)
            inside = name == "messages.js" and start <= match.start() < start + len(applier)
            assert inside, f"{name}: YOLO view written outside _applyYoloState: {line.strip()}"


def test_new_chat_keys_yolo_view_to_the_new_session():
    """New Chat installs S.session without loadSession(); the previous
    session's pill must not stay on screen for the brand-new session."""
    body = _function_body(_read("sessions.js"), "newSession")
    install = body.index("S.session=data.session;")
    bump = body.index("_loadSessionGeneration++;", install)
    reset = body.index("_applyYoloState(S.session.session_id,false,{reset:true})", install)
    assert install < bump < reset


def test_idle_install_reads_yolo_under_current_load_guard():
    body = _function_body(_read("sessions.js"), "loadSession")
    reads = [m.start() for m in re.finditer(r"_fetchYoloState\(sid,\{gen:_loadGeneration\}\)", body)]
    # INFLIGHT reattach branch, idle-with-live-stream branch, idle branch.
    assert len(reads) == 3, reads
    assert "if(_isCurrentLoad()&&typeof _fetchYoloState==='function') _fetchYoloState(sid,{gen:_loadGeneration});" in body
    # The reset no longer hides a same-session view; it is keyed by sid.
    assert "_yoloEnabled=false" not in body
    assert "_applyYoloState(sid,false,{reset:true})" in body
