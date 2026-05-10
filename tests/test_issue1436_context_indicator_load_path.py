"""Regression test for #1436 — context-window indicator broken on load path.

#1356 (closed Apr 30 2026) fixed the indicator on the **live SSE path** by adding
a model-metadata fallback when `agent.context_compressor` didn't provide
`context_length`.  But the **GET /api/session load path** (used when clicking
older sessions from the sidebar) was missed — it returned `context_length=0`
verbatim from the persisted Session object for any session that pre-dates
#1318 (when the field was added).

Combined with two cascading frontend fallbacks (`promptTok = last_prompt_tokens
|| input_tokens`, `ctxWindow = context_length || 128*1024`), older sessions
loaded their indicator showing nonsense like "100 / 890% used (context exceeded)"
because:
  - `context_length=0` → fallback to 131,072 (128K JS default)
  - `last_prompt_tokens=0` → fallback to **cumulative** `input_tokens`
    (often 1M+ on long sessions)
  - 1.2M / 131K = ~890% → ring caps at 100, tooltip shows "890% used"

Two-layer fix:
  1. Backend (api/routes.py:1295-1305) — add model_metadata fallback so
     loaded sessions get a sane `context_length` even when persisted as 0.
  2. Frontend (static/ui.js:1269) — drop the `input_tokens` fallback for
     `promptTok`.  Cumulative input is fundamentally wrong for "context window
     % used"; better to render "·" + "tokens used" (honest no-data) than a
     misleading >100% percentage.

Reported by @AvidFuturist in Discord (May 1 2026, "the 100 comes up way too
often").  Confirmed live on the dev server: 23 of 75 sessions had
`context_length=0` + `input_tokens > 128K`, all rendering >100%.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

ROUTES = Path(__file__).resolve().parent.parent / "api" / "routes.py"
UI_JS = Path(__file__).resolve().parent.parent / "static" / "ui.js"


# ─────────────────────────────────────────────────────────────────────────────
# Backend: GET /api/session load path
# ─────────────────────────────────────────────────────────────────────────────


class TestIssue1436BackendFallback:
    """The /api/session GET handler must resolve context_length via
    agent.model_metadata.get_model_context_length when the persisted value is 0."""

    def _stub_session(
        self, *, context_length, model, last_prompt_tokens=0, input_tokens=0
    ):
        """Build a mock Session that mimics the persisted-session shape."""
        s = MagicMock()
        s.context_length = context_length
        s.threshold_tokens = 0
        s.last_prompt_tokens = last_prompt_tokens
        s.input_tokens = input_tokens
        s.output_tokens = 0
        s.model = model
        s.title = "test-session"
        s.session_id = "test-1436"
        s.messages = []
        s.tool_calls = []
        s.active_stream_id = None
        s.pending_user_message = None
        s.pending_attachments = []
        s.pending_started_at = None
        # compact() returns a dict that gets merged with the response.
        s.compact.return_value = {
            "session_id": "test-1436",
            "title": "test-session",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "context_length": context_length,
            "threshold_tokens": 0,
            "last_prompt_tokens": last_prompt_tokens,
        }
        return s

    def _invoke_get_session(self, session_obj, *, fallback_returns=0):
        """Hit handle_get(/api/session?session_id=...) and capture the JSON response."""
        import api.routes as routes

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data
            captured["status"] = status

        # Patch get_model_context_length so the test doesn't depend on the
        # live agent.model_metadata bundle.
        fake_module = MagicMock()
        fake_module.get_model_context_length = MagicMock(return_value=fallback_returns)

        handler = MagicMock()
        parsed = urlparse("/api/session?session_id=test-1436&messages=0")

        # Patch import so `from agent.model_metadata import ...` resolves to our fake.
        with (
            patch("api.routes.get_session", return_value=session_obj),
            patch("api.routes.j", side_effect=fake_j),
            patch.dict("sys.modules", {"agent.model_metadata": fake_module}),
        ):
            routes.handle_get(handler, parsed)
        return captured

    def test_persisted_context_length_passed_through_unchanged(self):
        """When Session.context_length is non-zero, return it as-is (no fallback)."""
        s = self._stub_session(context_length=200_000, model="claude-sonnet-4.6")
        result = self._invoke_get_session(s, fallback_returns=999_999)
        body = result["data"]["session"]
        assert body["context_length"] == 200_000, (
            f"persisted context_length must pass through unchanged, "
            f"got {body['context_length']}"
        )

    def test_zero_context_length_falls_back_to_model_metadata(self):
        """Pre-#1318 sessions with context_length=0 must resolve via model_metadata."""
        s = self._stub_session(
            context_length=0, model="claude-opus-4-7", input_tokens=1_200_000
        )
        result = self._invoke_get_session(s, fallback_returns=1_000_000)
        body = result["data"]["session"]
        assert body["context_length"] == 1_000_000, (
            f"context_length=0 must resolve to model's 1M window via fallback, "
            f"got {body['context_length']}"
        )

    def test_fallback_called_with_persisted_model(self):
        """Fallback must be called with Session.model (not empty string)."""
        import api.routes as routes

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data

        fake_module = MagicMock()
        fake_module.get_model_context_length = MagicMock(return_value=400_000)

        s = self._stub_session(context_length=0, model="gpt-5-mini")
        handler = MagicMock()
        parsed = urlparse("/api/session?session_id=test-1436&messages=0")

        with (
            patch("api.routes.get_session", return_value=s),
            patch("api.routes.j", side_effect=fake_j),
            patch.dict("sys.modules", {"agent.model_metadata": fake_module}),
        ):
            routes.handle_get(handler, parsed)

        # First positional arg should be the model name; second is base_url ("")
        called_args = fake_module.get_model_context_length.call_args
        assert called_args is not None, "get_model_context_length was never called"
        assert called_args.args[0] == "gpt-5-mini", (
            f"fallback called with wrong model: {called_args}"
        )

    def test_empty_model_skips_fallback(self):
        """If Session.model is empty AND no effective_model is available, skip
        the fallback rather than calling get_model_context_length('')."""
        s = self._stub_session(context_length=0, model="")
        # resolve_model=0 to skip _resolve_effective_session_model_for_display
        import api.routes as routes

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data

        fake_module = MagicMock()
        fake_module.get_model_context_length = MagicMock(return_value=256_000)

        handler = MagicMock()
        parsed = urlparse(
            "/api/session?session_id=test-1436&messages=0&resolve_model=0"
        )

        with (
            patch("api.routes.get_session", return_value=s),
            patch("api.routes.j", side_effect=fake_j),
            patch.dict("sys.modules", {"agent.model_metadata": fake_module}),
        ):
            routes.handle_get(handler, parsed)

        # When model is empty, fallback either isn't called OR returns no value
        # we use.  Either way context_length stays 0.
        body = captured["data"]["session"]
        assert body["context_length"] == 0, (
            f"empty model must NOT trigger fallback (avoids the 256K default-for-unknown trap), "
            f"got context_length={body['context_length']}"
        )

    def test_fallback_exception_does_not_break_response(self):
        """If the fallback raises (older agent build, missing module), the route
        must still return a response — context_length just stays 0."""
        import api.routes as routes

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data

        # Fallback raises on import
        fake_module = MagicMock()
        fake_module.get_model_context_length = MagicMock(
            side_effect=RuntimeError("simulated agent error")
        )

        s = self._stub_session(context_length=0, model="claude-opus-4-7")
        handler = MagicMock()
        parsed = urlparse("/api/session?session_id=test-1436&messages=0")

        with (
            patch("api.routes.get_session", return_value=s),
            patch("api.routes.j", side_effect=fake_j),
            patch.dict("sys.modules", {"agent.model_metadata": fake_module}),
        ):
            routes.handle_get(handler, parsed)  # must not raise

        body = captured["data"]["session"]
        assert body["context_length"] == 0, (
            "fallback exception must be swallowed; field stays 0"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Frontend: static/ui.js _syncCtxIndicator
# ─────────────────────────────────────────────────────────────────────────────


class TestIssue1436FrontendDefense:
    """The frontend must NOT fall back to cumulative input_tokens when
    last_prompt_tokens is missing — that produces nonsense percentages."""

    def test_promptTok_does_not_fall_back_to_input_tokens(self):
        """Verify the line `promptTok = usage.last_prompt_tokens||usage.input_tokens||0`
        has been removed.  The old fallback divides cumulative input by the
        context window, producing the >100% bug."""
        src = UI_JS.read_text(encoding="utf-8")
        # The bug shape: the `||usage.input_tokens` fragment must NOT appear
        # on any line that defines `promptTok`.
        for line_num, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if (
                "promptTok" in line
                and "=" in line
                and "usage.last_prompt_tokens" in line
            ):
                assert "usage.input_tokens" not in line, (
                    f"static/ui.js:{line_num} still falls back to cumulative "
                    f"input_tokens for promptTok — this produces the >100% indicator "
                    f"bug from #1436.  Line: {line.rstrip()!r}"
                )

    def test_promptTok_assignment_uses_last_prompt_tokens_only(self):
        """Verify the new assignment: `promptTok = usage.last_prompt_tokens || 0`."""
        src = UI_JS.read_text(encoding="utf-8")
        # Allow whitespace variations.
        normalized = "".join(src.split())
        assert "constpromptTok=usage.last_prompt_tokens||0" in normalized, (
            "static/ui.js _syncCtxIndicator must assign "
            "`promptTok = usage.last_prompt_tokens || 0` (no input_tokens fallback)"
        )

    def test_no_data_branch_renders_dot(self):
        """When promptTok is 0 (no last-prompt data), the `!hasPromptTok` branch
        must render '·' (U+00B7) on the ring instead of computing a percentage.
        This is the existing behavior; the test pins it so a future refactor
        doesn't accidentally re-introduce a numeric fallback."""
        src = UI_JS.read_text(encoding="utf-8")
        assert "hasPromptTok=!!promptTok" in src.replace(" ", ""), (
            "hasPromptTok must be a boolean of promptTok"
        )
        # The ring center text uses '·' when !hasPromptTok
        assert "hasPromptTok?String(pct):'\\u00b7'" in src.replace(" ", ""), (
            "ring center must show '·' (\\u00b7) when no last-prompt data"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Static-source assertions (defense in depth — pin the comment markers in place)
# ─────────────────────────────────────────────────────────────────────────────


class TestIssue1436SourceMarkers:
    """Pin the fix comments + import shape so a future refactor can't silently
    drop the fallback."""

    def test_routes_load_path_imports_get_model_context_length(self):
        src = ROUTES.read_text(encoding="utf-8")
        # The import must appear inside the GET /api/session load-path block.
        start = src.find('if parsed.path == "/api/session":')
        end = src.find('if parsed.path == "/api/projects":', start)
        block = src[start:end]
        assert "from agent.model_metadata import get_model_context_length" in block, (
            "GET /api/session load-path block must lazy-import "
            "get_model_context_length for the context_length=0 fallback (#1436)"
        )

    def test_routes_load_path_marks_fix_with_issue_number(self):
        """Comment must reference #1436 so future maintainers find this trail."""
        src = ROUTES.read_text(encoding="utf-8")
        # Find the load-path block (between "if parsed.path == \"/api/session\":"
        # and the next `if parsed.path` after it).
        start = src.find('if parsed.path == "/api/session":')
        end = src.find('if parsed.path == "/api/projects":', start)
        block = src[start:end]
        assert "#1436" in block, (
            "GET /api/session load-path block must reference #1436 in a comment"
        )
