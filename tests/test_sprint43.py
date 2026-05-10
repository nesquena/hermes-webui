"""
Sprint 43 Tests: Bandit security fixes — B310, B324, B110 + QuietHTTPServer (PR #354).

Covers:
- gateway_watcher.py: MD5 uses usedforsecurity=False (B324)
- config.py: URL scheme validation before urlopen (B310)
- bootstrap.py: URL scheme validation in wait_for_health (B310)
- server.py: QuietHTTPServer class exists and extends ThreadingHTTPServer
- server.py: QuietHTTPServer.handle_error suppresses client disconnect errors
- server.py: QuietHTTPServer uses sys.exc_info() not traceback.sys.exc_info()
- Logging: at least 5 modules add a module-level logger (B110 remediation)
- routes.py: session titles redacted in /api/sessions list response
"""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
GATEWAY_WATCHER_PY = (REPO_ROOT / "api" / "gateway_watcher.py").read_text()
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text()
BOOTSTRAP_PY = (REPO_ROOT / "bootstrap.py").read_text()
SERVER_PY = (REPO_ROOT / "server.py").read_text()
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text()
AUTH_PY = (REPO_ROOT / "api" / "auth.py").read_text()
PROFILES_PY = (REPO_ROOT / "api" / "profiles.py").read_text()
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text()
WORKSPACE_PY = (REPO_ROOT / "api" / "workspace.py").read_text()
STATE_SYNC_PY = (REPO_ROOT / "api" / "state_sync.py").read_text()


# ── B324: MD5 usedforsecurity=False ─────────────────────────────────────────


class TestMD5SecurityFix(unittest.TestCase):
    """B324: hashlib.md5 must use usedforsecurity=False for non-crypto hashes."""

    def test_gateway_watcher_md5_usedforsecurity_false(self):
        """_snapshot_hash must pass usedforsecurity=False to hashlib.md5 (PR #354)."""
        self.assertIn(
            "usedforsecurity=False",
            GATEWAY_WATCHER_PY,
            "gateway_watcher.py: MD5 must use usedforsecurity=False (B324)",
        )

    def test_gateway_watcher_md5_pattern(self):
        """Exact pattern: hashlib.md5(..., usedforsecurity=False)."""
        # Use re.search with DOTALL since the arg may span parens internally
        self.assertIsNotNone(
            re.search(
                r"hashlib\.md5\(.*?usedforsecurity=False\)",
                GATEWAY_WATCHER_PY,
                re.DOTALL,
            ),
            "MD5 call must include usedforsecurity=False kwarg",
        )


# ── B310: URL scheme validation ──────────────────────────────────────────────


class TestUrlSchemeValidation(unittest.TestCase):
    """B310: urllib.request.urlopen must not be called with arbitrary schemes."""

    def test_config_scheme_validation_present(self):
        """config.py must validate URL scheme before urlopen (B310 fix)."""
        self.assertIn(
            "parsed_url.scheme",
            CONFIG_PY,
            "config.py: URL scheme validation missing (B310)",
        )
        # Must check against allowed schemes
        self.assertRegex(
            CONFIG_PY,
            r"parsed_url\.scheme\s+not\s+in\s+\(",
            "config.py: scheme check must use 'not in (...)' pattern",
        )

    def test_config_urlopen_has_nosec(self):
        """The urlopen call in config.py must have a # nosec B310 comment."""
        self.assertIn(
            "nosec B310",
            CONFIG_PY,
            "config.py: urlopen must have # nosec B310 after scheme validation",
        )

    def test_bootstrap_scheme_validation_present(self):
        """bootstrap.py wait_for_health must validate URL scheme before urlopen."""
        self.assertIn(
            "Invalid health check URL",
            BOOTSTRAP_PY,
            "bootstrap.py: URL scheme validation missing in wait_for_health (B310)",
        )
        self.assertRegex(
            BOOTSTRAP_PY,
            r"url\.startswith\([^)]+http",
            "bootstrap.py: must check url starts with http:// or https://",
        )

    def test_bootstrap_urlopen_has_nosec(self):
        """The urlopen call in bootstrap.py must have a # nosec B310 comment."""
        self.assertIn(
            "nosec B310",
            BOOTSTRAP_PY,
            "bootstrap.py: urlopen must have # nosec B310 after scheme validation",
        )

    def test_config_allows_http_and_https(self):
        """config.py scheme check must permit both http and https."""
        self.assertIn('"http"', CONFIG_PY, "config.py: http must be in allowed schemes")
        self.assertIn(
            '"https"', CONFIG_PY, "config.py: https must be in allowed schemes"
        )


# ── B110: Bare except/pass → logger.debug() ─────────────────────────────────


class TestBareExceptLogging(unittest.TestCase):
    """B110: bare except/pass blocks must be replaced with logger.debug()."""

    MODULES_REQUIRING_LOGGER = [
        ("api/auth.py", AUTH_PY),
        ("api/config.py", CONFIG_PY),
        ("api/gateway_watcher.py", GATEWAY_WATCHER_PY),
        ("api/profiles.py", PROFILES_PY),
        ("api/streaming.py", STREAMING_PY),
        ("api/workspace.py", WORKSPACE_PY),
        ("api/state_sync.py", STATE_SYNC_PY),
        ("api/routes.py", ROUTES_PY),
    ]

    def test_module_level_loggers_present(self):
        """All fixed modules must have a module-level logger = logging.getLogger(__name__)."""
        for name, src in self.MODULES_REQUIRING_LOGGER:
            with self.subTest(module=name):
                self.assertIn(
                    "logger = logging.getLogger(__name__)",
                    src,
                    f"{name}: module-level logger missing (B110 fix requires logger)",
                )

    def test_gateway_watcher_no_bare_pass_in_except(self):
        """gateway_watcher.py critical except blocks must not use bare pass."""
        # The poll loop except block that previously had 'pass' must now use logger
        self.assertIn(
            "logger.debug",
            GATEWAY_WATCHER_PY,
            "gateway_watcher.py: must use logger.debug not bare pass (B110)",
        )

    def test_profiles_reload_dotenv_logs_on_error(self):
        """profiles.py _reload_dotenv except must log + reset _loaded_profile_env_keys."""
        # Both the reset and the debug log should be present in the except block
        self.assertIn(
            "_loaded_profile_env_keys = set()",
            PROFILES_PY,
            "profiles.py: _reload_dotenv except must reset _loaded_profile_env_keys",
        )
        self.assertIn(
            "Failed to reload dotenv",
            PROFILES_PY,
            "profiles.py: _reload_dotenv except must log a warning",
        )


# ── QuietHTTPServer ──────────────────────────────────────────────────────────


class TestQuietHTTPServer(unittest.TestCase):
    """server.py: QuietHTTPServer suppresses client disconnect noise."""

    def test_quiet_http_server_class_exists(self):
        """QuietHTTPServer must be defined in server.py."""
        self.assertIn(
            "class QuietHTTPServer",
            SERVER_PY,
            "server.py: QuietHTTPServer class missing (PR #354)",
        )

    def test_quiet_http_server_extends_threading_http_server(self):
        """QuietHTTPServer must extend ThreadingHTTPServer."""
        self.assertRegex(
            SERVER_PY,
            r"class QuietHTTPServer\(ThreadingHTTPServer\)",
            "QuietHTTPServer must extend ThreadingHTTPServer",
        )

    def test_quiet_http_server_used_as_server(self):
        """main() must instantiate QuietHTTPServer not raw ThreadingHTTPServer."""
        # After the class is defined, the server creation should use QuietHTTPServer
        after_class = SERVER_PY[SERVER_PY.find("class QuietHTTPServer") :]
        self.assertIn(
            "QuietHTTPServer(",
            after_class,
            "main() must use QuietHTTPServer, not ThreadingHTTPServer directly",
        )

    def test_handle_error_suppresses_connection_reset(self):
        """handle_error must suppress ConnectionResetError and BrokenPipeError."""
        self.assertIn(
            "ConnectionResetError",
            SERVER_PY,
            "QuietHTTPServer.handle_error must handle ConnectionResetError",
        )
        self.assertIn(
            "BrokenPipeError",
            SERVER_PY,
            "QuietHTTPServer.handle_error must handle BrokenPipeError",
        )

    def test_uses_sys_exc_info_not_traceback_sys(self):
        """handle_error must use sys.exc_info() not traceback.sys.exc_info() (implementation detail)."""
        self.assertNotIn(
            "traceback.sys.exc_info()",
            SERVER_PY,
            "server.py: must use sys.exc_info() not traceback.sys.exc_info()",
        )
        self.assertIn(
            "sys.exc_info()",
            SERVER_PY,
            "server.py: handle_error must call sys.exc_info()",
        )

    def test_sys_imported_in_server(self):
        """server.py must import sys (needed for sys.exc_info)."""
        self.assertIsNotNone(
            re.search(r"^import sys", SERVER_PY, re.MULTILINE),
            "server.py: sys must be imported",
        )

    def test_handle_error_calls_super(self):
        """handle_error must call super().handle_error for non-client-disconnect errors."""
        self.assertIn(
            "super().handle_error(request, client_address)",
            SERVER_PY,
            "QuietHTTPServer.handle_error must delegate to super for real errors",
        )


# ── Session title redaction in /api/sessions ────────────────────────────────


class TestSessionTitleRedaction(unittest.TestCase):
    """routes.py: session titles must be redacted in the sessions list endpoint."""

    def test_redact_text_called_on_session_titles(self):
        """routes.py must call _redact_text on session titles in /api/sessions."""
        self.assertRegex(
            ROUTES_PY,
            r"_redact_text\([^)]*\btitle\b[^)]*\)",
            "routes.py: session titles must be redacted via _redact_text in /api/sessions",
        )

    def test_redact_text_imported_in_routes(self):
        """routes.py must import _redact_text from api.helpers."""
        self.assertIn(
            "_redact_text",
            ROUTES_PY,
            "routes.py: _redact_text must be imported from api.helpers",
        )
