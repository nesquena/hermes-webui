"""Focused static contracts for Workstream E client bootstrap behavior."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SW = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
BOOT = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _array_block(source, name):
    match = re.search(
        rf"const {re.escape(name)}\s*=\s*\[(?P<body>.*?)\];",
        source,
        re.DOTALL,
    )
    assert match, f"missing {name} allowlist"
    return match.group("body")


def test_versioned_shell_references_are_pinned_and_heavy_panels_are_inert():
    """The parser must not fetch non-chat bundles before the loader asks for them."""
    for asset in (
        "style.css",
        "pwa-startup.js",
        "i18n.js",
        "ui.js",
        "workspace.js",
        "terminal.js",
        "messages.js",
        "sessions.js",
        "commands.js",
        "boot.js",
        "outline.js",
    ):
        assert f"static/{asset}?v=__WEBUI_VERSION__" in INDEX

    for module, asset in (
        ("extension-settings", "extension_settings.js"),
        ("panels", "panels.js"),
        ("onboarding", "onboarding.js"),
    ):
        assert re.search(
            rf'<script[^>]+data-hermes-lazy="{module}"[^>]+data-src="static/{asset}\?v=__WEBUI_VERSION__"',
            INDEX,
        ), f"{module} must be represented by a versioned inert lazy script"
        assert not re.search(
            rf'<script[^>]+(?<!data-)src="static/{asset}\?v=__WEBUI_VERSION__"[^>]*>',
            INDEX,
        ), f"{asset} must not remain an eager script"

    for asset in ("ui.js", "messages.js", "sessions.js"):
        assert re.search(
            rf'<script[^>]+src="static/{asset}\?v=__WEBUI_VERSION__"[^>]+defer',
            INDEX,
        ), f"chat-critical {asset} must remain eager/deferred"


def test_lazy_module_loader_is_singleton_and_boot_does_not_await_panels():
    assert "window._hermesEnsureClientModule" in BOOT
    assert "window._hermesScheduleClientModule" in BOOT
    assert "const _hermesModulePromises = new Map()" in BOOT
    assert "data-hermes-lazy" in BOOT
    assert "window._hermesEnsureClientModule('onboarding')" in BOOT
    assert "await _workspaceListReady" not in BOOT
    assert "await _onboardingReady" not in BOOT
    for name in ("toggleProfileDropdown", "toggleComposerWsDropdown", "switchSettingsSection"):
        assert name in BOOT, f"cold-boot proxy missing for visible panel control {name}"


def test_shell_cache_is_cache_first_but_navigation_and_auth_stay_network_first():
    shell = _array_block(SW, "SHELL_ASSETS")
    lazy = _array_block(SW, "LAZY_ASSETS")

    assert "style.css' + VQ" in shell
    assert "boot.js' + VQ" in shell
    assert "panels.js' + VQ" not in shell
    assert "onboarding.js' + VQ" not in shell
    assert "extension_settings.js' + VQ" not in shell
    assert "manifest.json" not in shell
    assert "panels.js' + VQ" in lazy
    assert "onboarding.js' + VQ" in lazy
    assert "extension_settings.js' + VQ" in lazy

    shell_marker = "// Shell assets: stale-while-revalidate"
    shell_start = SW.index(shell_marker)
    shell_block = SW[shell_start : shell_start + 1400]
    assert "cache.match(event.request)" in shell_block
    assert shell_block.index("cache.match(event.request)") < shell_block.index("fetch(")
    assert "cache.put(event.request, response.clone())" in shell_block

    navigation_start = SW.index("if (event.request.mode === 'navigate')")
    navigation_block = SW[navigation_start : SW.index("  // Only explicit shell assets", navigation_start)]
    assert navigation_block.index("fetch(") < navigation_block.index("caches.match")
    assert "cache: 'no-store'" in navigation_block
    assert "url.pathname.endsWith('/login')" in SW
    assert "url.pathname.endsWith('/static/login.js')" in SW
    assert "url.pathname.endsWith('/sw.js')" in SW


def test_service_worker_stages_versioned_cache_before_activation():
    assert "const CACHE_PREFIX = 'hermes-shell-'" in SW
    assert "const STAGING_CACHE_NAME = CACHE_NAME + '-staging'" in SW
    assert "async function precacheShell()" in SW
    assert "await stagingCache.addAll(SHELL_ASSETS)" in SW
    assert "self.skipWaiting();" in SW
    install = SW[SW.index("self.addEventListener('install'") : SW.index("self.addEventListener('activate'")]
    assert "precacheShell()" in install
    assert ".then(() => { self.skipWaiting(); })" in install
    assert "catch" in install
    assert "keeping previous cache" in install
    assert "event.waitUntil(deleteOldShellCaches())" in SW
    assert "k.startsWith(CACHE_PREFIX)" in SW
    assert "k !== CACHE_NAME" in SW


def test_mobile_zoom_is_allowed_without_losing_ios_input_floor_or_targets():
    viewport = re.search(r'<meta name="viewport" content="([^"]+)">', INDEX)
    assert viewport, "the shell must declare a mobile viewport"
    content = viewport.group(1)
    assert "width=device-width" in content
    assert "initial-scale=1" in content
    assert "maximum-scale" not in content
    assert "user-scalable" not in content

    assert re.search(
        r"@media\s*\(hover:none\)\s*and\s*\(pointer:coarse\)\s*\{[^}]*"
        r"input\s*,\s*textarea\s*,\s*select\s*\{[^}]*font-size:\s*16px",
        CSS,
        re.DOTALL,
    ), "touch inputs need an explicit 16px iOS zoom floor"
    assert re.search(r"min-(?:width|height):44px", CSS)


def test_client_lifecycle_releases_mobile_viewport_and_shutdown_channel_resources():
    assert "window.visualViewport.removeEventListener('resize'" in BOOT
    assert "window.visualViewport.removeEventListener('scroll'" in BOOT
    assert "clearTimeout(_mobileViewportReflowTimer)" in BOOT
    assert "if(event && event.persisted)return" in BOOT
    assert "_stopChan.close()" in BOOT
    assert "pagehide" in BOOT


@pytest.mark.parametrize("relative_path", ["static/sw.js", "static/boot.js"])
def test_changed_browser_scripts_parse(relative_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "--check", str(ROOT / relative_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
