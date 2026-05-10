"""
Tests for the dynamic version badge (issue: stale hardcoded version strings).

Covers:
  1. api/updates.py: _detect_webui_version() resolution chain
  2. api/updates.py: _detect_agent_version() detection fallback
  3. api/updates.py: WEBUI_VERSION module constant is set and non-empty
  4. api/routes.py: GET /api/settings includes webui_version and agent_version keys
  5. static/index.html: two version badges are present
  6. static/panels.js: loadSettingsPanel() populates both version badges from settings
  7. server.py: server_version is not the old hardcoded string
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# 1. _detect_webui_version — resolution chain
# ---------------------------------------------------------------------------


class TestDetectWebUIVersion:
    def _fresh_detect(
        self, mock_run_git=None, version_file_content=None, tmp_path=None
    ):
        """Call _detect_webui_version() with controlled dependencies."""
        import api.updates as upd

        fake_root = tmp_path or Path("/nonexistent-path")

        if version_file_content is not None:
            vf = tmp_path / "api" / "_version.py"
            vf.parent.mkdir(parents=True, exist_ok=True)
            vf.write_text(version_file_content, encoding="utf-8")

        def _run_git_side_effect(args, cwd, timeout=10):
            if mock_run_git is not None:
                return mock_run_git(args, cwd, timeout)
            return ("", False)

        with (
            patch.object(upd, "_run_git", side_effect=_run_git_side_effect),
            patch.object(upd, "REPO_ROOT", fake_root),
        ):
            return upd._detect_webui_version()

    def test_git_success_returns_tag(self, tmp_path):
        """When git describe succeeds, returns the tag string directly."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("v0.50.123", True),
            tmp_path=tmp_path,
        )
        assert result == "v0.50.123"

    def test_git_between_tags_returns_descriptor(self, tmp_path):
        """Between releases, git describe returns a post-tag descriptor — pass it through."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("v0.50.123-3-ge91325d", True),
            tmp_path=tmp_path,
        )
        assert result == "v0.50.123-3-ge91325d"

    def test_git_failure_falls_back_to_version_file(self, tmp_path):
        """When git fails (Docker image), falls back to api/_version.py."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("", False),
            version_file_content="__version__ = 'v0.50.100'\n",
            tmp_path=tmp_path,
        )
        assert result == "v0.50.100"

    def test_git_failure_no_version_file_returns_unknown(self, tmp_path):
        """When git fails and no _version.py exists, returns 'unknown'."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("", False),
            tmp_path=tmp_path,
        )
        assert result == "unknown"

    def test_version_file_malformed_returns_unknown(self, tmp_path):
        """Malformed _version.py (no recognisable __version__ assignment) returns 'unknown'."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("", False),
            version_file_content="this is not valid python !!! ~~~\n",
            tmp_path=tmp_path,
        )
        assert result == "unknown"

    def test_git_uses_correct_describe_flags(self, tmp_path):
        """git describe is called with --tags --always --dirty."""
        called_args = []

        def capture(args, cwd, timeout=10):
            called_args.append(args)
            return ("v0.50.123", True)

        self._fresh_detect(mock_run_git=capture, tmp_path=tmp_path)
        assert called_args, "git was never called"
        assert "--tags" in called_args[0]
        assert "--always" in called_args[0]
        assert "--dirty" in called_args[0]


# ---------------------------------------------------------------------------
# 2. _detect_agent_version — resolution chain
# ---------------------------------------------------------------------------


class TestDetectAgentVersion:
    def _fresh_detect(
        self, mock_run_git=None, version_file_content=None, tmp_path=None
    ):
        """Call _detect_agent_version() with controlled dependencies."""
        import api.updates as upd

        fake_root = tmp_path or Path("/nonexistent-agent-path")

        if version_file_content is not None:
            vf = fake_root / "VERSION"
            vf.write_text(version_file_content, encoding="utf-8")

        def _run_git_side_effect(args, cwd, timeout=10):
            if mock_run_git is not None:
                return mock_run_git(args, cwd, timeout)
            return ("", False)

        with (
            patch.object(upd, "_run_git", side_effect=_run_git_side_effect),
            patch.object(upd, "_AGENT_DIR", fake_root),
        ):
            return upd._detect_agent_version()

    def test_version_file_is_preferred(self, tmp_path):
        """Agent VERSION file should be read before git fallback."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("v0.50.999", True),
            version_file_content="v0.60.1\n",
            tmp_path=tmp_path,
        )
        assert result == "v0.60.1"

    def test_git_fallback_used_when_version_file_missing(self, tmp_path):
        """When VERSION file is absent, we fall back to git describe in agent path."""
        (tmp_path / ".git").mkdir()
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("v0.60.2", True),
            tmp_path=tmp_path,
        )
        assert result == "v0.60.2"

    def test_missing_agent_returns_not_detected(self):
        """When no agent checkout is available, detect function returns 'not detected'."""
        import api.updates as upd

        with patch.object(upd, "_AGENT_DIR", None):
            assert upd._detect_agent_version() == "not detected"

    def test_agent_detect_returns_not_detected_on_fail(self, tmp_path):
        """Git fallback failure should remain user-friendly and not raise."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ("", False),
            tmp_path=tmp_path,
        )
        assert result == "not detected"


# ---------------------------------------------------------------------------
# 3. WEBUI_VERSION module constant
# ---------------------------------------------------------------------------


class TestWebUIVersionConstant:
    def test_webui_version_is_set(self):
        """WEBUI_VERSION is a non-empty string exported from api.updates."""
        import api.updates as upd

        assert hasattr(upd, "WEBUI_VERSION"), (
            "WEBUI_VERSION not exported from api.updates"
        )
        assert isinstance(upd.WEBUI_VERSION, str)
        assert upd.WEBUI_VERSION, "WEBUI_VERSION must not be empty string"

    def test_webui_version_is_not_old_hardcoded(self):
        """WEBUI_VERSION must not be the old stale value from server.py."""
        import api.updates as upd

        # These were the two stale hardcoded strings before this fix
        assert upd.WEBUI_VERSION not in ("0.50.38", "HermesWebUI/0.50.38"), (
            "WEBUI_VERSION still holds the old hardcoded server.py value"
        )


# ---------------------------------------------------------------------------
# 4. GET /api/settings includes webui_version and agent_version
# ---------------------------------------------------------------------------


class TestSettingsEndpointVersion:
    def test_api_settings_includes_webui_version(self):
        """GET /api/settings response dict must include webui_version key."""
        import api.routes as routes
        import api.updates as upd

        # Patch load_settings to return a minimal dict (no disk I/O)
        minimal_settings = {"send_key": "enter", "theme": "dark"}

        handler = MagicMock()
        from urllib.parse import urlparse

        parsed = urlparse("/api/settings")

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data

        with (
            patch("api.routes.load_settings", return_value=dict(minimal_settings)),
            patch("api.routes.j", side_effect=fake_j),
        ):
            routes.handle_get(handler, parsed)

        assert "webui_version" in captured.get("data", {}), (
            "/api/settings response must contain webui_version key"
        )
        assert captured["data"]["webui_version"] == upd.WEBUI_VERSION
        assert "agent_version" in captured.get("data", {}), (
            "/api/settings response must contain agent_version key"
        )
        assert captured["data"]["agent_version"] == upd.AGENT_VERSION

    def test_api_settings_webui_version_not_empty(self):
        """webui_version and agent_version in /api/settings must be non-empty strings."""
        import api.routes as routes

        handler = MagicMock()
        from urllib.parse import urlparse

        parsed = urlparse("/api/settings")

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data

        with (
            patch("api.routes.load_settings", return_value={}),
            patch("api.routes.j", side_effect=fake_j),
        ):
            routes.handle_get(handler, parsed)

        version = captured.get("data", {}).get("webui_version", "")
        assert version, "webui_version in /api/settings must not be empty"
        agent_version = captured.get("data", {}).get("agent_version", "")
        assert agent_version, "agent_version in /api/settings must not be empty"

    def test_api_settings_no_password_hash(self):
        """password_hash must still be stripped even with version injection."""
        import api.routes as routes

        handler = MagicMock()
        from urllib.parse import urlparse

        parsed = urlparse("/api/settings")

        captured = {}

        def fake_j(h, data, status=200):
            captured["data"] = data

        with (
            patch(
                "api.routes.load_settings", return_value={"password_hash": "secret123"}
            ),
            patch("api.routes.j", side_effect=fake_j),
        ):
            routes.handle_get(handler, parsed)

        assert "password_hash" not in captured.get("data", {}), (
            "password_hash must still be stripped from /api/settings"
        )


# ---------------------------------------------------------------------------
# 5. static/index.html — version badges
# ---------------------------------------------------------------------------


class TestIndexHTMLBadge:
    def _read_html(self):
        return (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_old_stale_version_removed_from_html(self):
        """The old hardcoded v0.50.87 badge must not appear in index.html."""
        html = self._read_html()
        assert "v0.50.87" not in html, (
            "Stale hardcoded version v0.50.87 still present in index.html. "
            "The badge should be a neutral placeholder; JS populates it at runtime."
        )

    def test_badge_element_still_present(self):
        """System version badge spans must still be in the DOM for both WebUI and Agent pills."""
        html = self._read_html()
        assert "settings-webui-version-badge" in html, (
            "WebUI badge element missing from index.html"
        )
        assert "settings-agent-version-badge" in html, (
            "Agent badge element missing from index.html"
        )


# ---------------------------------------------------------------------------
# 6. static/panels.js — badge population from settings
# ---------------------------------------------------------------------------


class TestPanelsJSVersionBadge:
    def _read_js(self):
        return (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    def test_panels_js_reads_webui_version(self):
        """loadSettingsPanel must reference settings.webui_version to populate the badge."""
        src = self._read_js()
        assert "webui_version" in src, (
            "panels.js loadSettingsPanel() must read settings.webui_version "
            "to populate the badge dynamically"
        )

    def test_panels_js_targets_version_badges(self):
        """loadSettingsPanel must target the two version badge elements."""
        src = self._read_js()
        assert "settings-webui-version-badge" in src, (
            "panels.js must query #settings-webui-version-badge to update the WebUI text"
        )
        assert "settings-agent-version-badge" in src, (
            "panels.js must query #settings-agent-version-badge to update the Agent text"
        )
        assert "agent_version" in src, (
            "loadSettingsPanel must read settings.agent_version to populate the agent badge"
        )


# ---------------------------------------------------------------------------
# 7. server.py — server_version not the old hardcoded string
# ---------------------------------------------------------------------------


class TestServerVersionHeader:
    def test_server_version_not_old_hardcoded(self):
        """server.py Handler.server_version must not be the stale hardcoded value."""
        src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        assert "HermesWebUI/0.50.38" not in src, (
            "server.py still contains the old hardcoded server_version string. "
            "It should use WEBUI_VERSION from api.updates."
        )

    def test_server_version_uses_webui_version(self):
        """server.py must reference WEBUI_VERSION when setting server_version."""
        src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        assert "WEBUI_VERSION" in src, (
            "server.py must import and use WEBUI_VERSION from api.updates "
            "to keep the HTTP Server: header in sync with git tags"
        )

    def test_server_py_imports_webui_version(self):
        """server.py must import WEBUI_VERSION from api.updates."""
        src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        assert "from api.updates import WEBUI_VERSION" in src, (
            "server.py must import WEBUI_VERSION from api.updates"
        )

    def test_server_version_no_slash_when_unknown(self):
        """When WEBUI_VERSION is 'unknown', server_version must be bare 'HermesWebUI' with no slash."""
        src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        # The guard must be present so log aggregators don't see 'HermesWebUI/unknown'
        assert "'unknown'" in src or '"unknown"' in src, (
            "server.py must guard against emitting 'HermesWebUI/unknown' as the server header"
        )

    def test_server_version_uses_removeprefix_not_lstrip(self):
        """server.py must use str.removeprefix() to strip 'v', not lstrip() which strips chars."""
        src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        assert "lstrip" not in src, (
            "server.py must use removeprefix('v') not lstrip('v') — lstrip strips characters, "
            "not a prefix, and would incorrectly mangle strings like 'vvv0.50.124'"
        )
        assert "removeprefix" in src, (
            "server.py must use removeprefix('v') to strip the leading 'v' from the version tag"
        )
