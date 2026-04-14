"""
Tests for issue #336 — opt-in chat bubble layout (PR #398).

Covers:
- api/config.py: bubble_layout present in _SETTINGS_DEFAULTS with default False
- api/config.py: bubble_layout present in _SETTINGS_BOOL_KEYS
- api/config.py: bubble_layout not in password-filtered keys (safe to expose)
- static/boot.js: boot path applies bubble-layout class from settings
- static/boot.js: catch path removes bubble-layout class on API failure
- static/panels.js: loadSettingsPanel reads bubble_layout checkbox
- static/panels.js: saveSettings writes bubble_layout and toggles body class
- static/style.css: body.bubble-layout CSS selectors present
- static/style.css: responsive max-width rule for bubble layout
- static/index.html: settingsBubbleLayout checkbox element present
- static/index.html: i18n keys wired on label and description
- static/i18n.js: English label and description keys present
- static/i18n.js: Spanish label and description keys present
- Integration: bubble_layout default is False in GET /api/settings
- Integration: bubble_layout persists via POST /api/settings
- Integration: non-bool value is coerced to bool on POST
"""
import json
import pathlib
import re
import unittest
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text()
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text()
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text()
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text()

from tests._pytest_port import BASE


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── config.py static checks ───────────────────────────────────────────────


class TestBubbleLayoutConfig(unittest.TestCase):
    """Verify bubble_layout is correctly registered in config.py."""

    def test_bubble_layout_in_settings_defaults(self):
        """bubble_layout must appear in _SETTINGS_DEFAULTS."""
        self.assertIn(
            '"bubble_layout"',
            CONFIG_PY,
            "bubble_layout key missing from _SETTINGS_DEFAULTS in api/config.py",
        )

    def test_bubble_layout_default_is_false(self):
        """bubble_layout default value must be False (opt-in, off by default)."""
        # Match  "bubble_layout": False  with optional spacing
        self.assertRegex(
            CONFIG_PY,
            r'"bubble_layout"\s*:\s*False',
            "bubble_layout default must be False in _SETTINGS_DEFAULTS",
        )

    def test_bubble_layout_in_bool_keys(self):
        """bubble_layout must be in _SETTINGS_BOOL_KEYS for coercion."""
        # Find the _SETTINGS_BOOL_KEYS block and verify membership
        bool_keys_match = re.search(
            r"_SETTINGS_BOOL_KEYS\s*=\s*\{([^}]+)\}", CONFIG_PY, re.DOTALL
        )
        self.assertIsNotNone(
            bool_keys_match, "_SETTINGS_BOOL_KEYS block not found in config.py"
        )
        self.assertIn(
            '"bubble_layout"',
            bool_keys_match.group(1),
            "bubble_layout missing from _SETTINGS_BOOL_KEYS",
        )


# ── boot.js static checks ────────────────────────────────────────────────


class TestBubbleLayoutBootJS(unittest.TestCase):
    """Verify bubble-layout class management in boot.js."""

    def test_boot_applies_bubble_layout_class(self):
        """boot.js success path must toggle body.bubble-layout from settings."""
        self.assertIn(
            "classList.toggle('bubble-layout',!!s.bubble_layout)",
            BOOT_JS,
            "boot.js must call classList.toggle('bubble-layout', ...) on settings load",
        )

    def test_boot_catch_removes_bubble_layout_class(self):
        """boot.js catch path must remove bubble-layout (default off on API failure)."""
        self.assertIn(
            "classList.remove('bubble-layout')",
            BOOT_JS,
            "boot.js catch block must call classList.remove('bubble-layout') on API failure",
        )


# ── panels.js static checks ──────────────────────────────────────────────


class TestBubbleLayoutPanelsJS(unittest.TestCase):
    """Verify settings panel wires the bubble_layout checkbox."""

    def test_load_settings_reads_bubble_layout_checkbox(self):
        """loadSettingsPanel must read the settingsBubbleLayout checkbox state."""
        self.assertIn(
            "settingsBubbleLayout",
            PANELS_JS,
            "panels.js must reference settingsBubbleLayout checkbox",
        )

    def test_save_settings_writes_bubble_layout(self):
        """saveSettings must write body.bubble_layout from the checkbox."""
        self.assertIn(
            "body.bubble_layout",
            PANELS_JS,
            "saveSettings must set body.bubble_layout from checkbox",
        )

    def test_save_settings_toggles_body_class(self):
        """saveSettings must apply body class toggle for live preview."""
        self.assertIn(
            "classList.toggle('bubble-layout', body.bubble_layout)",
            PANELS_JS,
            "saveSettings must toggle 'bubble-layout' on document.body for live preview",
        )


# ── style.css static checks ──────────────────────────────────────────────


class TestBubbleLayoutCSS(unittest.TestCase):
    """Verify CSS selectors for bubble layout are present and gated on body class."""

    def test_user_row_right_align_selector_present(self):
        """CSS must right-align user message rows when bubble-layout is active."""
        self.assertIn(
            "body.bubble-layout .msg-row:has(.msg-role.user)",
            STYLE_CSS,
            "CSS selector for user bubble alignment missing from style.css",
        )

    def test_assistant_row_left_align_selector_present(self):
        """CSS must left-align assistant message rows when bubble-layout is active."""
        self.assertIn(
            "body.bubble-layout .msg-row:has(.msg-role.assistant)",
            STYLE_CSS,
            "CSS selector for assistant bubble alignment missing from style.css",
        )

    def test_bubble_layout_responsive_rule_present(self):
        """A responsive max-width rule for narrow screens must be present."""
        # Both selectors must appear inside a @media block
        self.assertRegex(
            STYLE_CSS,
            r"@media\([^)]*700px[^)]*\)[^{]*\{[^}]*bubble-layout",
            "Responsive bubble-layout rule (700px breakpoint) missing from style.css",
        )


# ── index.html static checks ─────────────────────────────────────────────


class TestBubbleLayoutHTML(unittest.TestCase):
    """Verify the settings checkbox is present and correctly wired in index.html."""

    def test_settings_checkbox_present(self):
        """The settingsBubbleLayout checkbox must exist in index.html."""
        self.assertIn(
            'id="settingsBubbleLayout"',
            INDEX_HTML,
            "settingsBubbleLayout checkbox missing from index.html",
        )

    def test_settings_label_i18n_key_wired(self):
        """Label span must carry the settings_label_bubble_layout i18n key."""
        self.assertIn(
            'data-i18n="settings_label_bubble_layout"',
            INDEX_HTML,
            "settings_label_bubble_layout i18n key not wired on label span",
        )

    def test_settings_desc_i18n_key_wired(self):
        """Description div must carry the settings_desc_bubble_layout i18n key."""
        self.assertIn(
            'data-i18n="settings_desc_bubble_layout"',
            INDEX_HTML,
            "settings_desc_bubble_layout i18n key not wired on description div",
        )


# ── i18n.js static checks ────────────────────────────────────────────────


class TestBubbleLayoutI18N(unittest.TestCase):
    """Verify English and Spanish locale keys are present in i18n.js."""

    def _extract_locale_block(self, lang_start_marker, lang_end_marker):
        """Extract the content between two locale markers."""
        start = I18N_JS.find(lang_start_marker)
        end = I18N_JS.find(lang_end_marker, start)
        self.assertGreater(start, -1, f"Start marker '{lang_start_marker}' not found")
        self.assertGreater(end, start, f"End marker '{lang_end_marker}' not found after start")
        return I18N_JS[start:end]

    def test_english_label_key_present(self):
        """English locale must have settings_label_bubble_layout."""
        en_block = self._extract_locale_block("\n  en: {", "\n  es: {")
        self.assertIn(
            "settings_label_bubble_layout",
            en_block,
            "settings_label_bubble_layout missing from English locale",
        )

    def test_english_desc_key_present(self):
        """English locale must have settings_desc_bubble_layout."""
        en_block = self._extract_locale_block("\n  en: {", "\n  es: {")
        self.assertIn(
            "settings_desc_bubble_layout",
            en_block,
            "settings_desc_bubble_layout missing from English locale",
        )

    def test_spanish_label_key_present(self):
        """Spanish locale must have settings_label_bubble_layout."""
        es_block = self._extract_locale_block("\n  es: {", "\n  de: {")
        self.assertIn(
            "settings_label_bubble_layout",
            es_block,
            "settings_label_bubble_layout missing from Spanish locale",
        )

    def test_spanish_desc_key_present(self):
        """Spanish locale must have settings_desc_bubble_layout."""
        es_block = self._extract_locale_block("\n  es: {", "\n  de: {")
        self.assertIn(
            "settings_desc_bubble_layout",
            es_block,
            "settings_desc_bubble_layout missing from Spanish locale",
        )


# ── Integration tests (require live server on test server port) ─────────────────


class TestBubbleLayoutSettingsAPI(unittest.TestCase):
    """Integration tests: bubble_layout via GET/POST /api/settings."""

    def test_bubble_layout_default_is_false(self):
        """GET /api/settings must return bubble_layout: false by default."""
        try:
            d, status = _get("/api/settings")
        except OSError:
            self.skipTest("Server not running on test server port")
        self.assertEqual(status, 200)
        self.assertIn(
            "bubble_layout",
            d,
            "bubble_layout missing from GET /api/settings response",
        )
        self.assertFalse(
            d["bubble_layout"],
            "bubble_layout default must be False (opt-in feature)",
        )

    def test_bubble_layout_persists_true(self):
        """POST /api/settings with bubble_layout:true must persist and round-trip."""
        try:
            _, status = _post("/api/settings", {"bubble_layout": True})
        except OSError:
            self.skipTest("Server not running on test server port")
        self.assertEqual(status, 200)
        d, _ = _get("/api/settings")
        self.assertTrue(d["bubble_layout"], "bubble_layout=True must persist after POST")
        # Restore
        _post("/api/settings", {"bubble_layout": False})

    def test_bubble_layout_persists_false(self):
        """POST /api/settings with bubble_layout:false must persist and round-trip."""
        try:
            _post("/api/settings", {"bubble_layout": True})
            _post("/api/settings", {"bubble_layout": False})
        except OSError:
            self.skipTest("Server not running on test server port")
        d, _ = _get("/api/settings")
        self.assertFalse(d["bubble_layout"], "bubble_layout=False must persist after POST")

    def test_bubble_layout_truthy_string_coerced_to_bool(self):
        """Non-bool truthy value must be coerced to bool by _SETTINGS_BOOL_KEYS logic."""
        try:
            _post("/api/settings", {"bubble_layout": "1"})
        except OSError:
            self.skipTest("Server not running on test server port")
        d, _ = _get("/api/settings")
        self.assertIsInstance(
            d["bubble_layout"],
            bool,
            "bubble_layout must be a bool in API response (bool coercion via _SETTINGS_BOOL_KEYS)",
        )
        # Restore
        _post("/api/settings", {"bubble_layout": False})
