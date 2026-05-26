"""Regression coverage for #2697 — per-session reasoning effort override.

Three layers under test (matching the issue's spec):

1. **Session storage** — `Session.reasoning_effort` round-trips through the
   sidecar JSON. None means "inherit profile default" (the pre-PR behaviour).

2. **Resolve-at-stream-start precedence** — `api/streaming.py` honours
   `session.reasoning_effort` over the profile's
   `agent.reasoning_effort` from `config.yaml`. Static assertion on the
   resolve block in `streaming.py` so the precedence cannot silently regress.

3. **Slash command + UI** — `cmdReasoning()` accepts an optional `session`
   qualifier and posts to `/api/session/reasoning`; the chip dropdown has
   two scope options and a clear-override row; the chip carries a
   `session-override` class for the visual indicator (italic + dot).

Run only the model-level test cleanly without the live test server; the
text-based tests just grep the source for the invariants we want locked in
(same pattern as `test_reasoning_show_hide.py` and `test_reasoning_chip_btw_fixes.py`).
"""
from __future__ import annotations

import pathlib

from api.models import SESSION_DIR, Session


REPO = pathlib.Path(__file__).resolve().parent.parent

# Ensure the sessions directory exists for the model round-trip tests. The
# webui server normally creates this on startup; running tests as a plain
# pytest invocation may race or skip the creation.
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ── Layer 1: session storage round-trip ─────────────────────────────────────


class TestSessionReasoningEffortRoundTrip:
    """Session.reasoning_effort persists across save/load."""

    def test_default_is_none(self):
        s = Session(session_id="r2697aa")
        assert s.reasoning_effort is None, (
            "default reasoning_effort must be None so profile default applies"
        )

    def test_explicit_value_is_normalised(self):
        s = Session(session_id="r2697ab", reasoning_effort="HIGH")
        assert s.reasoning_effort == "high", (
            "reasoning_effort should be lowercased on construction"
        )

    def test_empty_string_is_treated_as_none(self):
        s = Session(session_id="r2697ac", reasoning_effort="")
        assert s.reasoning_effort is None, (
            "empty/whitespace reasoning_effort must coerce to None (inherit)"
        )

    def test_save_and_reload_preserves_override(self):
        sid = "r2697ad"
        s = Session(session_id=sid, reasoning_effort="medium")
        s.save()
        try:
            reloaded = Session.load(sid)
            assert reloaded is not None
            assert reloaded.reasoning_effort == "medium", (
                "session sidecar must round-trip reasoning_effort"
            )
        finally:
            # Clean up the temp file the test wrote.
            try:
                s.path.unlink()
            except FileNotFoundError:
                pass

    def test_clear_override_persists_as_none(self):
        sid = "r2697ae"
        s = Session(session_id=sid, reasoning_effort="high")
        s.save()
        try:
            reloaded = Session.load(sid)
            assert reloaded.reasoning_effort == "high"
            reloaded.reasoning_effort = None
            reloaded.save()
            reloaded2 = Session.load(sid)
            assert reloaded2.reasoning_effort is None, (
                "clearing the override must persist as None so the next stream "
                "falls back to the profile default"
            )
        finally:
            try:
                s.path.unlink()
            except FileNotFoundError:
                pass

    def test_compact_payload_emits_reasoning_effort(self):
        """The UI reads `session.reasoning_effort` via the compact payload."""
        s = Session(session_id="r2697af", reasoning_effort="low")
        payload = s.compact()
        assert "reasoning_effort" in payload, (
            "compact() must include reasoning_effort so the UI can sync the chip"
        )
        assert payload["reasoning_effort"] == "low"


# ── Layer 2: streaming.py honours session-overrides-profile precedence ──────


class TestStreamingResolvesSessionOverFirst:
    """The reasoning_config build in api/streaming.py must prefer
    session.reasoning_effort when set, falling back to the profile's
    agent.reasoning_effort only when the session has none.
    """

    SRC = _read("api/streaming.py")

    def test_session_override_branch_exists(self):
        assert "_session_effort = getattr(s, 'reasoning_effort', None)" in self.SRC, (
            "streaming.py must read the session-level override before the "
            "profile default — see #2697"
        )

    def test_session_override_takes_precedence_over_profile_default(self):
        # The branch order matters: session-effort first, profile second.
        idx_session = self.SRC.find("_session_effort")
        idx_profile = self.SRC.find(
            "_effort_cfg.get('reasoning_effort')"
        )
        assert 0 < idx_session < idx_profile, (
            "session-override read must appear BEFORE the profile-default read "
            "so the override wins when both are present"
        )

    def test_parse_reasoning_effort_handles_session_value(self):
        # The session value is parsed via the same parse_reasoning_effort
        # helper as the profile value, so 'none' / valid efforts route the
        # same way and unknown values silently become None.
        assert "_reasoning_config = _parse_reff(_session_effort)" in self.SRC


# ── Layer 3a: routes.py exposes /api/session/reasoning ──────────────────────


class TestSessionReasoningRoute:
    SRC = _read("api/routes.py")

    def test_endpoint_registered(self):
        assert 'parsed.path == "/api/session/reasoning"' in self.SRC, (
            "routes.py must expose /api/session/reasoning for the UI to write"
        )

    def test_endpoint_validates_effort(self):
        # Unknown effort levels must 400 rather than silently writing garbage
        # to the sidecar (matches the profile-default route's behaviour).
        assert "VALID_REASONING_EFFORTS" in self.SRC, (
            "/api/session/reasoning must validate effort against the canonical "
            "set, same as /api/reasoning"
        )

    def test_endpoint_accepts_null_to_clear_override(self):
        # The endpoint must accept a null/empty effort and write None so the
        # session falls back to the profile default on the next stream.
        block_start = self.SRC.find('parsed.path == "/api/session/reasoning"')
        assert block_start != -1
        block = self.SRC[block_start:block_start + 2500]
        assert "effort = None" in block, (
            "/api/session/reasoning must accept null/empty effort to clear "
            "the override and inherit the profile default"
        )

    def test_duplicate_session_carries_reasoning_effort_forward(self):
        # Per maintainer Q2 — duplicate session should carry the override.
        block_start = self.SRC.find('parsed.path == "/api/session/duplicate"')
        assert block_start != -1
        block = self.SRC[block_start:block_start + 4000]
        assert 'reasoning_effort=getattr(session, "reasoning_effort", None)' in block, (
            "/api/session/duplicate must carry the per-session override "
            "forward (maintainer's Q2 on #2697)"
        )


# ── Layer 3b: slash command + dropdown UI ───────────────────────────────────


class TestSlashCommandSessionVariant:
    COMMANDS_JS = _read("static/commands.js")

    def test_session_token_parsed(self):
        assert "const wantSession=(scopeToken==='session');" in self.COMMANDS_JS, (
            "cmdReasoning must parse a 'session' second token to scope the write"
        )

    def test_session_branch_posts_to_session_endpoint(self):
        assert "'/api/session/reasoning'" in self.COMMANDS_JS, (
            "the session-scoped branch must post to /api/session/reasoning"
        )

    def test_unknown_scope_token_is_rejected(self):
        assert "Unknown scope: " in self.COMMANDS_JS, (
            "an unrecognised second token (e.g. /reasoning high foo) must surface "
            "an error rather than silently treating it as a profile write"
        )

    def test_command_arg_string_mentions_session(self):
        # Autocomplete should hint at the new qualifier without breaking the
        # existing subArgs list (which still surfaces the effort levels).
        assert "[session]" in self.COMMANDS_JS, (
            "COMMANDS entry for /reasoning must hint at the optional [session] qualifier"
        )


class TestDropdownScopePicker:
    INDEX = _read("static/index.html")
    UI_JS = _read("static/ui.js")
    STYLE_CSS = _read("static/style.css")

    def test_dropdown_has_two_scope_options(self):
        assert 'data-scope="session"' in self.INDEX
        assert 'data-scope="profile"' in self.INDEX
        assert "Set for this session only" in self.INDEX
        assert "Set as profile default" in self.INDEX

    def test_dropdown_has_clear_override_row(self):
        assert 'id="reasoningClearOverride"' in self.INDEX
        assert "Clear session override" in self.INDEX

    def test_ui_js_tracks_write_scope_state(self):
        assert "_reasoningWriteScope='session'" in self.UI_JS, (
            "ui.js must default the write scope to 'session' so a click on an "
            "effort writes the override (not the profile default) when a session "
            "is active"
        )

    def test_chip_carries_session_override_class(self):
        assert "session-override" in self.UI_JS, (
            "the chip must toggle a session-override CSS class so the indicator "
            "(italic + dot) reflects an active override"
        )

    def test_css_indicator_styles_present(self):
        assert ".composer-reasoning-chip.session-override" in self.STYLE_CSS, (
            "CSS must define a session-override style (italic + leading dot) "
            "for the indicator"
        )
