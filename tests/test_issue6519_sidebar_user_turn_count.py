"""Regression tests for #6519: sidebar detailed mode should surface
the user-turn count alongside the total message count, so a
one-question/one-answer session, a cron run, and a long interactive
conversation triage differently during session-history cleanup.

The backend already exposes ``user_message_count`` on the list payload
(api/models.py:1884 for the per-session dict and line 7903 for the
list row). This test pins the frontend wiring that consumes it.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_sessions_js() -> str:
    return (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _read_i18n_js() -> str:
    return (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_detailed_mode_renders_user_message_count_when_present():
    """#6519: when the list row carries a numeric ``user_message_count``,
    the detailed-mode meta row includes a user-turns label alongside
    the existing ``X msgs`` label. Source guard pins the wiring."""
    src = _read_sessions_js()
    assert "s.user_message_count" in src, (
        "sessions.js must consume the user_message_count field that the "
        "backend already exposes (api/models.py:7903) — see #6519"
    )
    assert "session_meta_user_turns" in src, (
        "sessions.js must route the new label through the "
        "session_meta_user_turns i18n key for the new badge"
    )


def test_detailed_mode_skips_user_message_count_when_absent():
    """#6519 regression guard: legacy rows that pre-date the field must
    not render a stale 0 — the new label only appears when the
    payload actually carries a number, so it never collides with
    sessions that have not yet been backfilled."""
    src = _read_sessions_js()
    assert "typeof s.user_message_count==='number'" in src, (
        "the user-turn label must only render when user_message_count "
        "is a finite non-negative number — absent payload must not "
        "render a 0 user-turns row"
    )


def test_i18n_user_turns_key_exists_in_all_locales():
    """#6519: the new i18n key must exist in every locale block."""
    src = _read_i18n_js()
    count = src.count("session_meta_user_turns")
    assert count >= 15, (
        f"session_meta_user_turns found in only {count} places (expected "
        f">=15: en, it, ja, ru, es, de, zh, zh-Hant, pt, ko, fr, cs, tr, pl, vi)"
    )


def test_user_turn_label_pluralization_via_t_helper():
    """#6519: the i18n function routes through t() with a count
    argument so locale-specific plural forms work. Pin that the
    en entry is a function (not a static string)."""
    src = _read_i18n_js()
    en_block_start = src.index("  en: {")
    en_block_end = src.index("\n  it: {", en_block_start)
    en_block = src[en_block_start:en_block_end]
    import re
    m = re.search(
        r"session_meta_user_turns\s*:\s*\(\s*([a-zA-Z_$][\w$]*)\s*\)\s*=>",
        en_block,
    )
    assert m is not None, (
        "en session_meta_user_turns must be a function (n) => ... so "
        "t('session_meta_user_turns', count) routes through the count "
        "argument for plural forms. Block start:\n" + en_block[:200]
    )


def test_node_dom_renders_user_turns_badge_in_detailed_meta():
    """End-to-end: a Node DOM script mirrors the production metaBits
    join with a stub t(), and the rendered text must contain both
    the message-count label and the user-turns label. This pins the
    i18n key wiring without spinning up the full sidebar renderer.
    """
    import subprocess
    js_lines = [
        "const t = (key, n) => ({",
        "  session_meta_messages: (n) => n + ' msg' + (n === 1 ? '' : 's'),",
        "  session_meta_user_turns: (n) => n + ' user turn' + (n === 1 ? '' : 's'),",
        "})[key](n);",
        "const s = { message_count: 18, user_message_count: 5 };",
        "let metaBits = [];",
        "const msgCount = typeof s.message_count === 'number' ? s.message_count : 0;",
        "metaBits.push(t('session_meta_messages', msgCount));",
        "if (typeof s.user_message_count === 'number' && Number.isFinite(s.user_message_count) && s.user_message_count >= 0) {",
        "  metaBits.push(t('session_meta_user_turns', s.user_message_count));",
        "}",
        "const out = metaBits.join(' \\u00b7 ');",
        "if (out.indexOf('18 msgs') === -1) throw new Error('missing message label: ' + out);",
        "if (out.indexOf('5 user turns') === -1) throw new Error('missing user-turns label: ' + out);",
    ]
    js = "\n".join(js_lines)
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(
            "node DOM check failed: " + r.stderr + " stdout: " + r.stdout
        )


def test_node_dom_skips_user_turns_when_field_absent():
    """Regression guard: when the payload has no ``user_message_count``,
    the new label is not appended. Pinned to avoid future reordering
    pushing it into the meta row even on legacy rows."""
    import subprocess
    js_lines = [
        "const t = (key, n) => ({",
        "  session_meta_messages: (n) => n + ' msg' + (n === 1 ? '' : 's'),",
        "  session_meta_user_turns: (n) => n + ' user turn' + (n === 1 ? '' : 's'),",
        "})[key](n);",
        # Legacy row: no user_message_count key at all.",
        "const s = { message_count: 18 };",
        "let metaBits = [];",
        "const msgCount = typeof s.message_count === 'number' ? s.message_count : 0;",
        "metaBits.push(t('session_meta_messages', msgCount));",
        "if (typeof s.user_message_count === 'number' && Number.isFinite(s.user_message_count) && s.user_message_count >= 0) {",
        "  metaBits.push(t('session_meta_user_turns', s.user_message_count));",
        "}",
        "const out = metaBits.join(' \\u00b7 ');",
        "if (out.indexOf('user turn') !== -1) throw new Error('legacy row should not show user-turns label: ' + out);",
        "if (out.indexOf('18 msgs') === -1) throw new Error('missing message label: ' + out);",
    ]
    js = "\n".join(js_lines)
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(
            "node DOM check failed: " + r.stderr + " stdout: " + r.stdout
        )
