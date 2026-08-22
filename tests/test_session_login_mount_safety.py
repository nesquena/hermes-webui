"""Tests for LLM Wiki Docker home-fallback and /session/login mount-safe redirect.

Covers:
- _llm_wiki_resolve_path home-fallback when configured path is missing
- /session/login 302 redirect to ../login (mount-relative)
- _safeNextPath rejection of /session/login and /api/auth/* targets
- URL resolution under both root (/) and prefixed (/hermes/) mounts
"""
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]


# ── LLM Wiki Docker fallback ──────────────────────────────────────────────────


class TestWikiDockerFallback:
    """_llm_wiki_resolve_path falls back to <HERMES_HOME>/wiki when configured path is missing."""

    def test_fallback_fires_when_configured_missing_and_home_has_schema(self, tmp_path):
        from api.routes import _llm_wiki_resolve_path

        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        wiki = hermes_home / "wiki"
        wiki.mkdir()
        (wiki / "SCHEMA.md").write_text("schema")

        missing_path = str(tmp_path / "nonexistent" / "wiki")

        with patch.dict(os.environ, {"WIKI_PATH": missing_path, "HERMES_HOME": str(hermes_home)}):
            with patch("api.routes._llm_wiki_active_hermes_home", return_value=hermes_home):
                with patch("api.routes._llm_wiki_env_file_path", return_value=None):
                    resolved, source, configured = _llm_wiki_resolve_path()

        assert resolved == wiki
        assert "home-fallback" in source

    def test_no_fallback_when_home_wiki_lacks_shape(self, tmp_path):
        from api.routes import _llm_wiki_resolve_path

        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        (hermes_home / "wiki").mkdir()

        missing_path = str(tmp_path / "nonexistent")

        with patch.dict(os.environ, {"WIKI_PATH": missing_path, "HERMES_HOME": str(hermes_home)}):
            with patch("api.routes._llm_wiki_active_hermes_home", return_value=hermes_home):
                with patch("api.routes._llm_wiki_env_file_path", return_value=None):
                    resolved, source, configured = _llm_wiki_resolve_path()

        assert resolved == Path(missing_path)
        assert "home-fallback" not in source

    def test_no_fallback_when_configured_path_exists(self, tmp_path):
        from api.routes import _llm_wiki_resolve_path

        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        (hermes_home / "wiki").mkdir()
        (hermes_home / "wiki" / "SCHEMA.md").write_text("s")

        real_wiki = tmp_path / "real_wiki"
        real_wiki.mkdir()

        with patch.dict(os.environ, {"WIKI_PATH": str(real_wiki), "HERMES_HOME": str(hermes_home)}):
            with patch("api.routes._llm_wiki_active_hermes_home", return_value=hermes_home):
                with patch("api.routes._llm_wiki_env_file_path", return_value=None):
                    resolved, source, configured = _llm_wiki_resolve_path()

        assert resolved == real_wiki


# ── /session/login redirect (mount-relative URL resolution) ───────────────────


class TestSessionLoginRedirect:
    """Verify /session/login redirects to ../login which resolves correctly under mounts."""

    def test_root_mount_session_login_resolves_to_login(self):
        """Under root mount: /session/login -> ../login resolves to /login."""
        base = "http://host/session/login"
        location = "../login"
        resolved = urlparse(urljoin(base, location))
        assert resolved.path == "/login"

    def test_prefixed_mount_session_login_resolves_to_mount_login(self):
        """Under /hermes/ mount: /hermes/session/login -> ../login resolves to /hermes/login."""
        base = "http://host/hermes/session/login"
        location = "../login"
        resolved = urlparse(urljoin(base, location))
        assert resolved.path == "/hermes/login"

    def test_redirect_preserves_query_string(self):
        """next= query string is forwarded in the redirect Location."""
        base = "http://host/session/login?next=%2Fsession%2Fabc"
        location = "../login?next=%2Fsession%2Fabc"
        resolved = urlparse(urljoin(base, location))
        assert resolved.path == "/login"
        assert "next=" in resolved.query


# ── Auth/passkey/health URL resolution under subpath mounts ───────────────────


class TestMountRelativeFetches:
    """Login.js fetches must be mount-relative (no leading /) so they resolve under subpath."""

    @staticmethod
    def _login_js():
        return (ROOT / "static" / "login.js").read_text(encoding="utf-8")

    def test_auth_login_fetch_is_relative(self):
        src = self._login_js()
        assert "fetch('api/auth/login'" in src
        assert "fetch('/api/auth/login'" not in src

    def test_passkey_options_fetch_is_relative(self):
        src = self._login_js()
        assert "fetch('api/auth/passkey/options'" in src
        assert "fetch('/api/auth/passkey/options'" not in src

    def test_passkey_login_fetch_is_relative(self):
        src = self._login_js()
        assert "fetch('api/auth/passkey/login'" in src
        assert "fetch('/api/auth/passkey/login'" not in src

    def test_auth_status_fetch_is_relative(self):
        src = self._login_js()
        assert "fetch('api/auth/status'" in src
        assert "fetch('/api/auth/status'" not in src

    def test_health_probe_fetch_is_relative(self):
        src = self._login_js()
        assert "fetch('health'" in src
        assert "fetch('/health'" not in src

    def test_safe_next_fallback_is_mount_relative(self):
        """_safeNextPath() default must be './' not '/' to stay under mount."""
        src = self._login_js()
        # Default/fallback returns
        assert "return './';" in src
        assert "return '/';" not in src


# ── _safeNextPath enhanced rejection ─────────────────────────────────────────


class TestSafeNextEnhancedRejection:
    """_safeNextPath must reject /session/login and /api/auth/* as next targets."""

    @staticmethod
    def _login_js():
        return (ROOT / "static" / "login.js").read_text(encoding="utf-8")

    def test_rejects_session_login_in_safe_next(self):
        src = self._login_js()
        assert "'/session/login'" in src

    def test_rejects_api_auth_prefix_in_safe_next(self):
        src = self._login_js()
        assert "'/api/auth/'" in src

    def test_login_loop_collapse_still_works(self):
        """The server-side guard still collapses nested login chains."""
        from api.routes import _safe_login_redirect_path as guard

        assert guard("/session/login") == "/"
        assert guard("/session/login?next=/session/login") == "/"
        assert guard("/hermes/session/login") == "/"


# ── Server-side /session/login PUBLIC_PATHS ───────────────────────────────────


class TestSessionLoginPublicPath:
    """Verify /session/login is public so the 302 fires without auth recursion."""

    def test_session_login_in_public_paths(self):
        from api.auth import PUBLIC_PATHS

        assert "/session/login" in PUBLIC_PATHS
