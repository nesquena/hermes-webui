"""Regression coverage for the three #3401 deep-review fixes (release stage-3401).

#3401 (live-to-final assistant reply redesign) is a large refactor of the highest-traffic
render/stream surface. Its full test suite was green and Opus passed it, but a Codex
diff-vs-master regression read + a live-browser drive surfaced three regressions the
rewritten tests no longer guarded:

  FIX 1 — inline ``<think>…</think>answer`` reasoning vanished. ``_assistantReasoningPayloadText``
          used ``$``-anchored regexes, so a leading think block followed by a visible answer
          extracted NO reasoning and the Thinking card never rendered (master used the
          non-anchored form). The matching display-stripper is non-anchored, so the extractor
          must be too.

  FIX 2 — reconnect/reload duplicated the live reply. ``_rememberRunJournalCursor`` advanced a
          closure-local seq but never wrote ``INFLIGHT[activeSid].lastRunJournalSeq`` — the value
          ``persistInflightState`` saves and a reload reads back as the ``after_seq`` replay
          floor. So a hard reload restored ``lastAssistantText`` then replayed the journal from
          ``after_seq=0`` on top of it.

  FIX 3 — the shipped Neon skin silently stopped working. The PR deleted the
          ``:root[data-skin="neon"]`` CSS while leaving Neon registered in the picker, so users
          could select it and get default styling.

These are static source-structure / registration assertions plus a general
"every registered skin has CSS" guard so a dropped-skin regression fails fast.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name}() not found"
    brace = src.find("{", match.end())
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1:i - 1]


# ── FIX 1: inline <think> reasoning extraction must be non-anchored ──

def test_reasoning_payload_extractor_is_not_dollar_anchored():
    """A leading <think> block followed by visible answer text must still yield the
    reasoning. A trailing `$` anchor dropped it whenever the turn also had an answer."""
    body = _function_body(UI_JS, "_assistantReasoningPayloadText")
    # The think/thought/turn extraction regexes must NOT end with `$` after the close tag.
    assert "<\\/think>\\s*$/" not in body and "</think>\\s*$/" not in body, (
        "the <think> extraction regex must not be $-anchored (drops reasoning when a "
        "visible answer follows the think block) — #3401 inline-think regression"
    )
    assert "<channel\\|>\\s*$/" not in body, "channel-thought extraction must not be $-anchored"
    assert "<turn\\|>\\s*$/" not in body, "turn-thinking extraction must not be $-anchored"
    # And the non-anchored leading-block form must be present.
    assert "<think>([\\s\\S]*?)<\\/think>\\s*/" in body, (
        "extractor must match a LEADING <think> block (non-anchored), mirroring "
        "_stripLeadingAssistantThinkingMarkup"
    )


def test_extractor_and_stripper_anchoring_agree():
    """The reasoning extractor and the display-content stripper must use the same
    (non-anchored, leading-block) matching, or one shows a card the other can't strip."""
    strip = _function_body(UI_JS, "_stripLeadingAssistantThinkingMarkup")
    extract = _function_body(UI_JS, "_assistantReasoningPayloadText")
    # Neither should be $-anchored on the think close tag.
    assert "</think>\\s*$/" not in strip and "<\\/think>\\s*$/" not in strip
    assert "</think>\\s*$/" not in extract and "<\\/think>\\s*$/" not in extract


# ── FIX 2: reconnect cursor must persist into INFLIGHT ──

def test_run_journal_cursor_persisted_into_inflight():
    """_rememberRunJournalCursor must write the advanced seq onto INFLIGHT so a reload
    replays from the correct after_seq floor (not 0 over restored live text)."""
    body = _function_body(MESSAGES_JS, "_rememberRunJournalCursor")
    assert "INFLIGHT[activeSid]" in body, (
        "the cursor must be mirrored onto the persisted INFLIGHT entry (#3401 reconnect dup)"
    )
    assert "lastRunJournalSeq=seq" in body.replace(" ", ""), (
        "INFLIGHT[activeSid].lastRunJournalSeq must be set to the advanced seq"
    )
    # And a persist must be scheduled so the value survives a reload.
    assert "_throttledPersist" in body or "persistInflightState" in body, (
        "advancing the cursor must schedule an INFLIGHT persist"
    )


def test_persist_inflight_saves_run_journal_seq():
    """persistInflightState must still save lastRunJournalSeq (the value reload reads back)."""
    body = _function_body(MESSAGES_JS, "persistInflightState")
    assert "lastRunJournalSeq" in body


# ── FIX 3: every registered skin must have CSS (general guard) ──

def test_neon_skin_css_restored():
    assert ':root[data-skin="neon"]' in CSS, "Neon skin CSS block must be present"
    assert ':root.dark[data-skin="neon"]' in CSS, "Neon dark variant CSS must be present"


def test_every_registered_skin_has_css():
    """Any skin offered in the picker (_SKINS in boot.js) must have a CSS block, so a
    refactor cannot silently drop a shipped skin's styling while leaving it selectable."""
    # Extract skin values registered in boot.js _SKINS = [ {name:'X', value:'y'}, ... ]
    skins_block = re.search(r"_SKINS\s*=\s*\[(.*?)\]", BOOT_JS, re.DOTALL)
    assert skins_block, "_SKINS registration not found in boot.js"
    values = set(re.findall(r"value\s*:\s*'([a-z0-9-]+)'", skins_block.group(1)))
    # 'default'/'system' style entries have no data-skin CSS; only check non-default skins.
    css_skins = set(re.findall(r'data-skin="([a-z0-9-]+)"', CSS))
    missing = sorted(s for s in values if s and s not in css_skins and s not in {"default", "system", ""})
    assert not missing, f"registered skins with no CSS block (silent breakage): {missing}"


# ── FIX 4: settled tool-worklog rebuild must run while busy too (switch-back) ──

def test_settled_worklog_rebuild_not_gated_on_idle_only():
    """The settled tool/worklog/thinking rebuild must also run when busy if there are
    tool calls. Gating purely on `!S.busy` dropped every prior settled turn's worklog
    when renderMessages re-ran during an active stream (switch-back to in-progress
    session) — the same content-loss-on-switch class as #3668. (#3401 regression)"""
    body = UI_JS
    # The rebuild guard must include the `|| (S.toolCalls && S.toolCalls.length)` arm.
    assert re.search(
        r"if\(!S\.busy\s*\|\|\s*\(S\.toolCalls\s*&&\s*S\.toolCalls\.length\)\)\{",
        body,
    ), (
        "the settled worklog rebuild must run while busy when tool calls exist "
        "(if(!S.busy || (S.toolCalls && S.toolCalls.length))), not gate purely on !S.busy"
    )
    # And the bare `if(!S.busy){` immediately before that rebuild's worklog-wipe must be gone.
    assert "if(!S.busy){\n    inner.querySelectorAll('.tool-worklog-group" not in body, (
        "the worklog-rebuild block must not be gated on the bare !S.busy form"
    )

