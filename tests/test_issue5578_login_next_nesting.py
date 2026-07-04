"""Regression test for #5578 — login `next=` self-nesting URL explosion.

Bug: an expired-auth 401 while already on `/session/login?next=…` fed the login
redirect its own URL. Each of the three guards (workspace.js api() 401 handler,
login.js `_safeNextPath`, routes.py `_safe_login_redirect_path`) validated against
open-redirect but NONE rejected a `next` pointing back at the login page, so the
nested `/session/login?next=/session/login%3Fnext%3D…` chain re-percent-encoded
and grew exponentially on every bounce until the tab broke (~12k chars).

Fix: all three guards now reject a `next` that targets the login page or already
carries a nested `next=`, plus a length cap — while preserving legitimate
`next=/some/real/path` redirects and the existing open-redirect protections.
"""
from pathlib import Path

from api.routes import _safe_login_redirect_path as guard

ROOT = Path(__file__).resolve().parents[1]
LOGIN_JS = (ROOT / "static" / "login.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


# ── server guard: the self-nesting cases the bug exploited ──────────────────

class TestServerGuardRejectsNesting:
    def test_rejects_login_self_reference(self):
        assert guard("/login") == "/"
        assert guard("/session/login") == "/"
        assert guard("/session/login/") == "/"
        assert guard("/hermes/session/login") == "/"  # subpath mount

    def test_rejects_nested_next_chain(self):
        nested = "/session/login?next=/session/login%3Fnext%3D/session/login"
        assert guard(nested) == "/"

    def test_rejects_encoded_next_at_any_depth(self):
        # raw ?next=, single-encoded %3Fnext, double-encoded %253Fnext
        assert guard("/x?next=/y") == "/"
        assert guard("/x%3Fnext%3D/y") == "/"
        assert guard("/x%253Fnext%253D/y") == "/"
        assert guard("/x&next=/y") == "/"

    def test_rejects_overlong_next(self):
        assert guard("/" + "a" * 3000) == "/"

    def test_the_exact_12k_explosion_collapses(self):
        # Reconstruct the reported exponential chain; the guard must collapse it.
        enc = "%3Fnext%3D"
        blown = "/session/login" + (enc + "/session/login") * 40
        assert len(blown) > 500
        assert guard(blown) == "/"


class TestServerGuardPreservesLegitimateRedirects:
    def test_preserves_real_session_path(self):
        assert guard("/session/abc123") == "/session/abc123"

    def test_preserves_root_and_plain_paths(self):
        assert guard("/") == "/"
        assert guard("/workspace") == "/workspace"
        assert guard("/session/xyz?tab=files") == "/session/xyz?tab=files"

    def test_still_rejects_open_redirect_classics(self):
        # The pre-existing protections must remain intact.
        assert guard("//evil.example") == "/"
        assert guard("/\\evil") == "/"
        assert guard("https://evil.example") == "/"
        assert guard("/x\x00y") == "/"
        assert guard("") == "/"
        assert guard(None) == "/"


# ── client guards: static wiring (both JS sites carry the self-ref guard) ───

class TestClientGuardsWired:
    def test_login_js_rejects_login_self_and_nested_next(self):
        # _safeNextPath must carry the login-self + nested-next + length guards.
        assert "/login$/.test(pathOnly)" in LOGIN_JS or "/login$/" in LOGIN_JS
        assert "next=" in LOGIN_JS and "%253f" in LOGIN_JS.lower()
        assert "2048" in LOGIN_JS

    def test_workspace_js_skips_next_on_login_page(self):
        # The 401 handler must NOT append the whole login URL as next when it's
        # already on the login page (the recursion source).
        assert "login$/.test(_p)" in WORKSPACE_JS
        assert "window.location.href='login';" in WORKSPACE_JS
        # And the non-login path still captures the real destination.
        assert "'login?next='+encodeURIComponent" in WORKSPACE_JS
