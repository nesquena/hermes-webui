"""
Tests for issue #673 — sidebar density mode for the session list.

Covers:
- api/config.py: sidebar_density registered in defaults + enum validation
- static/index.html: settingsSidebarDensity field and i18n wiring present
- static/boot.js: boot path applies window._sidebarDensity with compact default
- static/panels.js: load/save settings wire sidebar_density correctly
- static/sessions.js: detailed mode renders message count + model, and profile
  only when the "show all profiles" toggle is active
- static/i18n.js: locale keys exist for all shipped locales
- Integration: GET/POST /api/settings round-trip sidebar_density
"""

import json
import pathlib
import unittest
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

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


class TestSidebarDensityConfig(unittest.TestCase):
    def test_sidebar_density_in_defaults(self):
        self.assertIn('"sidebar_density"', CONFIG_PY)

    def test_sidebar_density_default_is_compact(self):
        self.assertRegex(CONFIG_PY, r'"sidebar_density"\s*:\s*"compact"')

    def test_sidebar_density_in_enum_values(self):
        self.assertIn('"sidebar_density": {"compact", "detailed"}', CONFIG_PY)


class TestSidebarDensityHTML(unittest.TestCase):
    def test_settings_select_present(self):
        self.assertIn('id="settingsSidebarDensity"', INDEX_HTML)

    def test_i18n_wiring_present(self):
        for key in (
            'data-i18n="settings_label_sidebar_density"',
            'data-i18n="settings_desc_sidebar_density"',
            'data-i18n="settings_sidebar_density_compact"',
            'data-i18n="settings_sidebar_density_detailed"',
        ):
            self.assertIn(key, INDEX_HTML)


class TestSidebarDensityBootAndPanels(unittest.TestCase):
    def test_boot_applies_sidebar_density(self):
        self.assertIn(
            "window._sidebarDensity=(s.sidebar_density==='detailed'?'detailed':'compact');",
            BOOT_JS,
        )

    def test_boot_fallback_is_compact(self):
        self.assertIn("window._sidebarDensity='compact';", BOOT_JS)

    def test_settings_panel_reads_sidebar_density(self):
        self.assertIn("settingsSidebarDensity", PANELS_JS)
        self.assertIn(
            "settings.sidebar_density==='detailed'?'detailed':'compact'",
            PANELS_JS,
        )

    def test_settings_save_writes_sidebar_density(self):
        self.assertIn("body.sidebar_density=sidebarDensity;", PANELS_JS)
        self.assertIn(
            "window._sidebarDensity=sidebarDensity==='detailed'?'detailed':'compact';",
            PANELS_JS,
        )


class TestSidebarDensitySessionRendering(unittest.TestCase):
    def test_detailed_mode_branch_present(self):
        self.assertIn(
            "const density=(window._sidebarDensity==='detailed'?'detailed':'compact');",
            SESSIONS_JS,
        )
        self.assertIn("if(density==='detailed')", SESSIONS_JS)

    def test_detailed_mode_uses_message_count_and_model(self):
        self.assertIn(
            "typeof s.message_count==='number'?s.message_count:0", SESSIONS_JS
        )
        self.assertIn("const modelMeta=_formatSessionModelWithGateway(s);", SESSIONS_JS)
        self.assertIn("if(modelMeta) metaBits.push(modelMeta);", SESSIONS_JS)
        self.assertIn("t('session_meta_messages', msgCount)", SESSIONS_JS)

    def test_profile_only_when_show_all_profiles(self):
        self.assertIn(
            "if(_showAllProfiles&&s.profile) metaBits.push(s.profile);", SESSIONS_JS
        )

    def test_session_meta_css_hook_present(self):
        self.assertIn(".session-meta", STYLE_CSS)


class TestSidebarDensityI18N(unittest.TestCase):
    def _extract_locale_block(self, start_marker, end_marker):
        start = I18N_JS.find(start_marker)
        end = I18N_JS.find(end_marker, start)
        self.assertGreater(start, -1)
        self.assertGreater(end, start)
        return I18N_JS[start:end]

    def test_all_locale_blocks_have_sidebar_density_keys(self):
        locale_ranges = [
            ("\n  en: {", "\n  ru: {"),
            ("\n  ru: {", "\n  es: {"),
            ("\n  es: {", "\n  de: {"),
            ("\n  de: {", "\n  zh: {"),
            ("\n  zh: {", "\n  // Traditional Chinese (zh-Hant)"),
            ("\n  // Traditional Chinese (zh-Hant)\n  'zh-Hant': {", "\n};"),
        ]
        required = (
            "settings_label_sidebar_density",
            "settings_desc_sidebar_density",
            "settings_sidebar_density_compact",
            "settings_sidebar_density_detailed",
            "session_meta_messages",
        )
        for start, end in locale_ranges:
            block = self._extract_locale_block(start, end)
            for key in required:
                self.assertIn(key, block, f"{key} missing from locale block {start}")


class TestSidebarDensitySettingsAPI(unittest.TestCase):
    def test_sidebar_density_default_is_compact(self):
        try:
            data, status = _get("/api/settings")
        except OSError:
            self.skipTest("Server not running on test server port")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("sidebar_density"), "compact")

    def test_sidebar_density_round_trips_detailed(self):
        try:
            _, status = _post("/api/settings", {"sidebar_density": "detailed"})
        except OSError:
            self.skipTest("Server not running on test server port")
        self.assertEqual(status, 200)
        data, _ = _get("/api/settings")
        self.assertEqual(data.get("sidebar_density"), "detailed")
        _post("/api/settings", {"sidebar_density": "compact"})

    def test_invalid_sidebar_density_is_ignored(self):
        try:
            _post("/api/settings", {"sidebar_density": "compact"})
            data, status = _post("/api/settings", {"sidebar_density": "nope"})
        except OSError:
            self.skipTest("Server not running on test server port")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("sidebar_density"), "compact")
        current, _ = _get("/api/settings")
        self.assertEqual(current.get("sidebar_density"), "compact")
