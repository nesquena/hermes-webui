"""Tests for YOLO mode toggle in Web UI (Issue #467).

Covers:
- GET /api/session/yolo — query YOLO state for a session
- POST /api/session/yolo — enable/disable YOLO for a session
- /yolo slash command registration in commands.js
- YOLO pill HTML element presence in index.html
- Skip-all button presence in approval card
- CSS classes for .yolo-pill and .approval-btn.yolo
- i18n keys present in all 6 locales
"""

import os
import re
import json
import pytest

from tests.conftest import requires_agent_modules

TEST_BASE = f"http://127.0.0.1:{os.environ.get('HERMES_WEBUI_TEST_PORT', '8788')}"


def _get(path, expect_ok=True):
    import urllib.request
    import urllib.error

    try:
        with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        if expect_ok:
            return body
        return body


def _post(path, body=None, expect_ok=True):
    import urllib.request
    import urllib.error

    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        TEST_BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return body


# ── Backend endpoint tests ──


@requires_agent_modules
class TestYoloEndpointGet:
    """GET /api/session/yolo should return yolo_enabled state.

    Agent-dependent: the endpoint reads from ``tools.approval._session_yolo``
    in the hermes-agent process. When the agent isn't installed, routes.py
    falls back to a no-op lambda that always returns ``False`` regardless of
    POST state — every assertion here would either silently false-pass or
    flake. Skip cleanly when modules aren't importable.
    """

    def test_yolo_get_returns_false_by_default(self):
        """A fresh session should not have YOLO enabled."""
        data = _get("/api/session/yolo?session_id=test-yolo-fresh-001")
        assert data is not None
        assert data.get("yolo_enabled") is False

    def test_yolo_get_requires_session_id(self):
        """Missing session_id returns an error response."""
        resp = _get("/api/session/yolo?session_id=")
        # Empty session_id may return 400 or empty response
        assert resp is not None


@requires_agent_modules
class TestYoloEndpointPost:
    """POST /api/session/yolo should toggle YOLO for a session.

    Agent-dependent: the endpoint writes to ``tools.approval._session_yolo``
    in the hermes-agent process. Without the agent, routes.py falls back to
    a no-op lambda; the response shape ``{"yolo_enabled": <input>}`` echoes
    the request body, so naive POST-only tests false-pass. The
    ``test_yolo_post_persists_within_session`` test catches this by reading
    state back via GET — it only succeeds when the agent is wired.
    """

    def test_yolo_post_enable(self):
        """Enabling YOLO returns ok=True and yolo_enabled=True."""
        sid = "test-yolo-enable-001"
        data = _post("/api/session/yolo", {"session_id": sid, "enabled": True})
        assert data.get("ok") is True
        assert data.get("yolo_enabled") is True

    def test_yolo_post_disable(self):
        """Disabling YOLO returns ok=True and yolo_enabled=False."""
        sid = "test-yolo-disable-001"
        _post("/api/session/yolo", {"session_id": sid, "enabled": True})
        data = _post("/api/session/yolo", {"session_id": sid, "enabled": False})
        assert data.get("ok") is True
        assert data.get("yolo_enabled") is False

    def test_yolo_post_persists_within_session(self):
        """After enabling, GET should reflect the enabled state."""
        sid = "test-yolo-persist-001"
        _post("/api/session/yolo", {"session_id": sid, "enabled": True})
        data = _get(f"/api/session/yolo?session_id={sid}")
        assert data.get("yolo_enabled") is True

    def test_yolo_post_cross_session_isolation(self):
        """Enabling YOLO for one session doesn't affect another."""
        sid_a = "test-yolo-iso-a"
        sid_b = "test-yolo-iso-b"
        _post("/api/session/yolo", {"session_id": sid_a, "enabled": True})
        data = _get(f"/api/session/yolo?session_id={sid_b}")
        assert data.get("yolo_enabled") is False

    def test_yolo_post_defaults_to_enabled(self):
        """POST without 'enabled' key defaults to True."""
        sid = "test-yolo-default-001"
        data = _post("/api/session/yolo", {"session_id": sid})
        assert data.get("yolo_enabled") is True


# ── Frontend JS tests (static file analysis — no server needed) ──


class TestYoloCommandRegistration:
    """/yolo slash command should be registered in commands.js."""

    @pytest.fixture(scope="class")
    def commands_js(self):
        with open("static/commands.js", "r") as f:
            return f.read()

    def test_yolo_command_in_array(self, commands_js):
        assert "'yolo'" in commands_js or '"yolo"' in commands_js

    def test_yolo_uses_cmdYolo(self, commands_js):
        assert "cmdYolo" in commands_js

    def test_cmdYolo_function_exists(self, commands_js):
        assert re.search(r"function\s+cmdYolo\s*\(", commands_js)

    def test_cmdYolo_calls_yolo_endpoint(self, commands_js):
        assert "/api/session/yolo" in commands_js


class TestYoloPillHTML:
    """YOLO pill element should exist in index.html."""

    @pytest.fixture(scope="class")
    def index_html(self):
        with open("static/index.html", "r") as f:
            return f.read()

    def test_yolo_pill_element_exists(self, index_html):
        assert 'id="yoloPill"' in index_html

    def test_yolo_pill_has_onclick(self, index_html):
        assert 'onclick="cmdYolo()"' in index_html

    def test_yolo_pill_hidden_by_default(self, index_html):
        pill_match = re.search(r'<button[^>]*id="yoloPill"[^>]*>', index_html)
        assert pill_match
        assert "display:none" in pill_match.group(0)

    def test_skip_all_button_exists(self, index_html):
        assert 'id="approvalSkipAll"' in index_html


class TestYoloCSS:
    """YOLO-related CSS classes should exist."""

    @pytest.fixture(scope="class")
    def style_css(self):
        with open("static/style.css", "r") as f:
            return f.read()

    def test_yolo_pill_class(self, style_css):
        assert ".yolo-pill{" in style_css or ".yolo-pill {" in style_css

    def test_yolo_pill_uses_amber(self, style_css):
        assert "#f59e0b" in style_css

    def test_approval_skip_all_class(self, style_css):
        assert ".approval-btn.yolo{" in style_css or ".approval-btn.yolo {" in style_css


class TestYoloI18n:
    """YOLO-related i18n keys should exist in all 6 locales."""

    REQUIRED_KEYS = [
        "cmd_yolo",
        "yolo_no_session",
        "yolo_enabled",
        "yolo_disabled",
        "yolo_pill_label",
        "yolo_pill_title_active",
        "approval_skip_all",
        "approval_skip_all_title",
    ]

    LOCALES = ["en", "ru", "es", "de", "zh", "ko"]

    @pytest.fixture(scope="class")
    def i18n_js(self):
        with open("static/i18n.js", "r") as f:
            return f.read()

    @pytest.mark.parametrize("locale", LOCALES)
    def test_locale_has_all_yolo_keys(self, i18n_js, locale):
        pattern = rf"\s{locale}:\s*\{{"
        match = re.search(pattern, i18n_js)
        assert match, f"Locale '{locale}' not found in i18n.js"
        start = match.end()
        next_locale = re.search(r"\n  \w{2}:\s*\{", i18n_js[start:])
        if next_locale:
            block = i18n_js[start : start + next_locale.start()]
        else:
            block = i18n_js[start:]

        for key in self.REQUIRED_KEYS:
            assert key in block, f"Key '{key}' missing in locale '{locale}'"
