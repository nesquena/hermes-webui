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

    def test_rejects_encoded_login_chain_at_any_depth(self):
        # A nested LOGIN chain must collapse even when the `?` separating path
        # from query is percent-encoded at any depth (that is the exponential
        # explosion signature). Detection is by the decoded leading PATH.
        assert guard("/session/login%3Fnext%3D/y") == "/"
        assert guard("/session/login%253Fnext%253D/y") == "/"
        assert guard("/hermes/session/login%3Fnext%3D/y") == "/"

    def test_rejects_deeply_encoded_login_chain(self):
        # #5579 gate: a 6+-level percent-encoded login chain must still collapse —
        # the decode loop checks the leading path at EVERY level (incl. the final
        # decoded form), so a login route hidden behind deep encoding is caught.
        # /session/login with the '?' encoded 6 times: %25252525253F
        assert guard("/session/login%25252525253Fnext%25252525253D/y") == "/"

    def test_preserves_non_login_path_carrying_its_own_next_key(self):
        # A legitimate NON-login destination that merely carries its own `next=`
        # query key must still round-trip — only login-route chains collapse.
        # (Regression guarded by test_v050258_opus_followups.py; #5579 gate.)
        assert guard("/x?next=/y") == "/x?next=/y"
        assert guard("/admin?action=foo&next=/real/path") == "/admin?action=foo&next=/real/path"

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
        # _safeNextPath must carry the login-route + length guards and detect the
        # login route through nested percent-encoding via bounded decodeURIComponent.
        assert "/login$/.test(pathOnly)" in LOGIN_JS or "/login$/" in LOGIN_JS
        assert "decodeURIComponent" in LOGIN_JS
        assert "2048" in LOGIN_JS

    def test_workspace_js_skips_next_on_login_page(self):
        # The 401 handler must NOT append the whole login URL as next when it's
        # already on the login page (the recursion source).
        assert "login$/.test(_p)" in WORKSPACE_JS
        assert "window.location.href='login';" in WORKSPACE_JS
        # And the non-login path still captures the real destination.
        assert "'login?next='+encodeURIComponent" in WORKSPACE_JS

    def test_all_client_401_helpers_guard_login_page(self):
        # #5578 Codex round-2: workspace.js was fixed but two more client 401
        # redirect helpers (ui.js _redirectIfUnauth, boot.js redirectToLogin)
        # also nested the login URL. All three must carry the on-login guard.
        UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
        BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
        assert "login$/.test(_p)" in UI_JS, "ui.js _redirectIfUnauth must guard the login page"
        assert "login$/.test(_p)" in BOOT_JS, "boot.js redirectToLogin must guard the login page"


class TestServerCheckAuthGuard:
    def test_check_auth_does_not_nest_login_redirect(self):
        # #5578 Codex round-2 (BRICK): check_auth() runs BEFORE route handling,
        # so the server-side page-redirect loop never reached
        # _safe_login_redirect_path(). check_auth() must itself refuse to wrap a
        # login-shaped path into a fresh next=.
        AUTH_PY = (ROOT / "api" / "auth.py").read_text(encoding="utf-8")
        assert "endswith('/login')" in AUTH_PY or "_login_path.endswith('/login')" in AUTH_PY, (
            "check_auth() must detect a login-shaped path and skip the next= wrap"
        )

    def test_check_auth_resolves_to_real_public_login_route(self):
        # #5578 Codex round-3 (BRICK): a bare relative 'login' from /session/login
        # resolves BACK to /session/login (not public, not the /login route) and
        # loops forever. Must use '../login' so it lands on the real public
        # /login route (and <mount>/login under a subpath mount).
        AUTH_PY = (ROOT / "api" / "auth.py").read_text(encoding="utf-8")
        assert "'../login'" in AUTH_PY, (
            "check_auth() must redirect a session-scoped login path to ../login "
            "(the real public /login), not a bare relative login that loops"
        )

    def test_inner_next_helper_drops_login_keeps_safe_nonlogin(self):
        # The preserved inner next must be safe + non-login. A nested LOGIN chain
        # (even with an encoded `?`) collapses; a legit non-login path that
        # carries its own `next=` key round-trips (#5579 gate — no over-collapse).
        import api.auth as auth
        assert auth._safe_login_inner_next("next=/session/login") == ""
        assert auth._safe_login_inner_next("next=/session/login%3Fnext%3D/y") == ""
        assert auth._safe_login_inner_next("next=//evil") == ""
        assert auth._safe_login_inner_next("next=/session/abc123") == "/session/abc123"
        # parse_qs decodes the value: /x%3Fnext%3D/y -> /x?next=/y, a legit
        # non-login path with its own next key -> preserved (round-trips).
        assert auth._safe_login_inner_next("next=/x%3Fnext%3D/y") == "/x?next=/y"
        assert auth._safe_login_inner_next("") == ""
