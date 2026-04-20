"""Tests for the batch of fixes from PRs #506-#521 (v0.50.47).

Covers:
  - /root workspace unblocking (#510/#521)
  - Attached-files split guard (#521)
  - custom_providers model visibility (#515/#519)
  - Cron skill cache invalidation (#507/#508)
  - System (auto) theme (#504/#506/#509/#514)
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text()


# ── Group A: /root workspace ──────────────────────────────────────────────────

class TestRootWorkspaceUnblocked:

    def test_root_not_in_blocked_system_roots(self):
        src = read("api/workspace.py")
        assert "Path('/root')" not in src, (
            "/root must not be in _BLOCKED_SYSTEM_ROOTS — "
            "breaks deployments where Hermes runs as root"
        )

    def test_etc_still_blocked(self):
        """Sanity: other dangerous paths remain blocked."""
        src = read("api/workspace.py")
        assert "Path('/etc')" in src
        assert "Path('/proc')" in src

    def test_split_guard_present(self):
        src = read("api/streaming.py")
        assert "'\\n\\n[Attached files:' in msg_text" in src, (
            "base_text split must guard against missing '[Attached files:' "
            "to avoid empty-string on plain messages"
        )


# ── Group B: custom_providers visibility ─────────────────────────────────────

class TestCustomProvidersVisibility:

    def test_has_custom_providers_variable_present(self):
        src = read("api/config.py")
        assert "_has_custom_providers" in src, (
            "_has_custom_providers variable must exist in get_available_models()"
        )

    def test_discard_custom_conditional_on_no_custom_providers(self):
        src = read("api/config.py")
        assert "not _has_custom_providers" in src, (
            "detected_providers.discard('custom') must be gated on "
            "'not _has_custom_providers'"
        )

    def test_custom_providers_isinstance_check(self):
        src = read("api/config.py")
        assert "isinstance(_custom_providers_cfg, list)" in src, (
            "_has_custom_providers must check isinstance(..., list)"
        )


# ── Group C: cron skill cache ─────────────────────────────────────────────────

class TestCronSkillCacheInvalidation:

    def _panels_src(self):
        return read("static/panels.js")

    def test_cache_busted_on_form_open(self):
        src = self._panels_src()
        # toggleCronForm should set cache to null unconditionally
        m = re.search(
            r'function toggleCronForm\(\)\{.*?_cronSkillsCache=null',
            src, re.DOTALL
        )
        assert m, (
            "toggleCronForm must unconditionally null _cronSkillsCache "
            "before fetching skills"
        )

    def test_cache_not_guarded_by_if_on_open(self):
        src = self._panels_src()
        # The old guard should be gone
        assert "if(!_cronSkillsCache)" not in src, (
            "toggleCronForm should not use 'if(!_cronSkillsCache)' guard — "
            "cache must always be busted on open"
        )

    def test_cache_busted_on_skill_save(self):
        src = self._panels_src()
        # After submitSkillSave's api() call, _cronSkillsCache must be nulled
        m = re.search(
            r'async function submitSkillSave\(\).*?_skillsData\s*=\s*null.*?_cronSkillsCache\s*=\s*null',
            src, re.DOTALL
        )
        assert m, (
            "_cronSkillsCache must be set to null in submitSkillSave() "
            "right after _skillsData = null"
        )


# ── Group D: System (auto) theme ──────────────────────────────────────────────

class TestSystemTheme:

    def test_apply_theme_helper_in_boot_js(self):
        src = read("static/boot.js")
        assert "function _applyTheme(" in src, (
            "_applyTheme helper function must be defined in boot.js"
        )

    def test_apply_theme_resolves_system(self):
        src = read("static/boot.js")
        assert "normalized.theme==='system'" in src or "=== 'system'" in src, (
            "_applyTheme must branch on 'system' to resolve via matchMedia"
        )

    def test_apply_theme_uses_matchmedia(self):
        src = read("static/boot.js")
        assert "prefers-color-scheme" in src, (
            "_applyTheme must use matchMedia('(prefers-color-scheme:dark)')"
        )

    def test_load_settings_calls_apply_theme(self):
        src = read("static/boot.js")
        assert "_applyTheme(appearance.theme)" in src, (
            "loadSettings must call _applyTheme() instead of direct data-theme assignment"
        )

    def test_system_option_in_theme_picker(self):
        html = read("static/index.html")
        assert "_pickTheme('system')" in html, (
            "Theme picker must include a system theme button"
        )
        assert ">System<" in html, (
            "Theme picker must show 'System' label"
        )

    def test_theme_picker_uses_pick_theme(self):
        html = read("static/index.html")
        assert "_pickTheme(" in html, (
            "Theme buttons must call _pickTheme()"
        )

    def test_flicker_script_resolves_system(self):
        html = read("static/index.html")
        # The head flicker-prevention IIFE must handle 'system'
        assert "==='system'" in html or "=== 'system'" in html, (
            "Flicker-prevention head script must resolve 'system' before setting data-theme"
        )
        assert "legacy={slate:['dark','slate']" in html, (
            "Flicker-prevention head script must normalize legacy theme names on first paint"
        )

    def test_system_in_commands_themes_list(self):
        src = read("static/commands.js")
        assert "'system'" in src, (
            "/theme command must include 'system' in the valid themes array"
        )

    def test_commands_uses_apply_theme(self):
        src = read("static/commands.js")
        assert "_applyTheme(appearance.theme)" in src, (
            "cmdTheme must call _applyTheme() with the normalized canonical theme"
        )

    def test_commands_accept_legacy_theme_aliases(self):
        src = read("static/commands.js")
        assert "const legacyThemes=Object.keys(_LEGACY_THEME_MAP||{});" in src, (
            "cmdTheme must accept legacy theme aliases and map them onto canonical appearance values"
        )

    def test_panels_reverts_via_apply_theme(self):
        src = read("static/panels.js")
        assert "_applyTheme(_settingsThemeOnOpen)" in src or \
               "_applyTheme(" in src, (
            "_revertSettingsPreview must call _applyTheme() so 'system' "
            "is correctly re-activated on settings discard"
        )

    def test_panels_saves_system_string_not_resolved(self):
        src = read("static/panels.js")
        assert "localStorage.getItem('hermes-theme')" in src, (
            "_settingsThemeOnOpen must read from localStorage to preserve "
            "the 'system' string, not the resolved 'dark'/'light'"
        )

    def test_i18n_cmd_theme_includes_system_english(self):
        src = read("static/i18n.js")
        assert "system/dark/light" in src, (
            "English cmd_theme i18n key must include 'system' in the theme list"
        )

    def test_i18n_cmd_theme_all_locales(self):
        src = read("static/i18n.js")
        count = src.count("system/dark/light")
        assert count >= 5, (
            f"cmd_theme description should mention 'system' in all 5 locales; "
            f"found {count}"
        )

    def test_theme_listener_cleanup_uses_stable_handler(self):
        src = read("static/boot.js")
        assert "_systemThemeMq&&_onSystemThemeChange" in src, (
            "_applyTheme must track the active OS-theme listener so it can be removed cleanly"
        )
        assert "removeEventListener('change',_onSystemThemeChange)" in src, (
            "_applyTheme must remove the previous OS-theme listener before adding a new one"
        )

    def test_panels_hydrates_appearance_before_models_fetch(self):
        src = read("static/panels.js")
        skin_idx = src.index("const skinVal=(settings.skin||'default').toLowerCase();")
        # models is now declared as let models=null before the try block
        models_idx = src.index("models=await api('/api/models');")
        assert skin_idx < models_idx, (
            "loadSettingsPanel must hydrate theme/skin before awaiting /api/models, "
            "otherwise a slow model fetch can clobber an in-progress skin selection"
        )
