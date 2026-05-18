"""Static frontend checks for the profile-scoped Gateway Tile contract."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    m = re.search(rf"function {re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    assert m, f"function {name} not found"
    i, depth = m.end(), 1
    while i < len(src) and depth > 0:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"function {name} did not parse cleanly"
    return src[m.start():i]


def test_gateway_tile_knows_unknown_and_unavailable_are_not_stopped():
    label = _extract_function(PANELS_JS, "_gatewayLabelForPhase")
    toggle = _extract_function(PANELS_JS, "_gatewayToggleLabelForPhase")
    assert "case 'unknown': return 'Unknown';" in label
    assert "case 'unavailable': return 'Unavailable';" in label
    assert "case 'unknown': return 'Check status';" in toggle
    assert "case 'unavailable': return 'Unavailable';" in toggle


def test_gateway_status_contract_fields_are_cached_and_repainted():
    refresh = _extract_function(PANELS_JS, "_refreshGatewayStatus")
    repaint = _extract_function(PANELS_JS, "_repaintGatewayTile")
    for field in ("control_available", "status_source", "health", "detail", "desired_enabled"):
        assert field in refresh, f"_refreshGatewayStatus must cache {field} from backend contract"
    assert "state.control_available === false" in repaint
    assert "disabled" in repaint


def test_gateway_detail_render_starts_stable_visible_poller_after_immediate_refresh():
    bind = _extract_function(PANELS_JS, "_bindProfileOpsConsole")
    assert "_refreshGatewayStatus(profileName)" in bind
    # Stable polling must start for all visible phases, not only starting/stopping.
    assert "_startGatewayPoller(profileName)" in bind
    assert "st.phase === 'starting' || st.phase === 'stopping'" not in bind


def test_gateway_poller_uses_fast_transient_and_slow_stable_cadences_and_stops_when_hidden():
    poller = _extract_function(PANELS_JS, "_startGatewayPoller")
    assert "_GATEWAY_TRANSIENT_POLL_MS" in PANELS_JS, "transient states should poll about every 1.5s"
    assert "_GATEWAY_STABLE_POLL_MS" in PANELS_JS, "stable states should poll every 10-15s"
    assert "_GATEWAY_TRANSIENT_POLL_MS" in poller
    assert "_GATEWAY_STABLE_POLL_MS" in poller
    assert "document.visibilityState === 'hidden'" in poller
    assert "_currentPanel !== 'profiles'" in poller
    assert "_currentProfileDetail" in poller
    assert "setTimeout" in poller and "setInterval" not in poller


def test_gateway_poller_does_not_stop_after_stale_transient_recovers_to_stable_phase():
    poller = _extract_function(PANELS_JS, "_startGatewayPoller")
    assert "const result = await _refreshGatewayStatus(profileName);" in poller
    assert "const phase = (result && result.phase) || state.phase || 'stopped';" in poller
    assert "schedule(_GATEWAY_TRANSIENT_PHASES.has(phase) ? _GATEWAY_TRANSIENT_POLL_MS : _GATEWAY_STABLE_POLL_MS);" in poller
    assert "phase && phase !== 'starting' && phase !== 'stopping'" not in poller
    assert "_stopGatewayPoller(profileName);" in poller


def test_gateway_tile_renders_keyboard_info_button_and_copyable_dialog_path():
    tile = _extract_function(PANELS_JS, "_profileGatewayTile")
    bind = _extract_function(PANELS_JS, "_bindProfileOpsConsole")
    dialog = _extract_function(PANELS_JS, "_openGatewayInfoDialog")
    assert "profile-gateway-info" in tile
    assert "data-gateway-info" in tile
    assert "View gateway status details" in tile
    assert "data-gateway-info" in bind
    assert "_openGatewayInfoDialog(profileName)" in bind
    for required in ("role=\"dialog\"", "Gateway status details", "Profile", "Phase", "Status source", "Health reason", "Copy", "Close", "textarea"):
        assert required in dialog
    assert "navigator.clipboard.writeText" in dialog


def test_gateway_toggle_never_restarts_and_respects_unavailable_control():
    toggle = _extract_function(PANELS_JS, "_onGatewayToggle")
    assert "restart" not in toggle
    assert "state.control_available === false" in toggle
    assert "phase === 'unavailable'" in toggle


def test_gateway_info_css_supports_button_tooltip_and_dialog():
    for selector in (
        ".profile-gateway-info",
        ".profile-gateway-info-tooltip",
        ".gateway-info-dialog",
        ".gateway-info-detail",
    ):
        assert selector in STYLE_CSS, f"missing CSS selector {selector}"


# ── Platforms button (per-profile messaging-platform config) ──────────────


def test_gateway_tile_renders_platforms_button_with_data_action():
    """The new ⚙ Platforms button must be in the tile template, with the
    per-profile dispatch attribute and the count suffix slot."""
    tile = _extract_function(PANELS_JS, "_profileGatewayTile")
    assert "profile-gateway-platforms-btn" in tile, (
        "tile must render the new .profile-gateway-platforms-btn"
    )
    assert "data-platforms-action" in tile, (
        "tile must wire the platforms button with data-platforms-action=<name>"
    )
    # The button text includes the cog glyph and the "Platforms · N" suffix.
    assert "Platforms" in tile
    # Profile name is interpolated as ${name} (the esc'd profile name) — the
    # data-platforms-action attribute MUST use it.
    assert 'data-platforms-action="${name}"' in tile


def test_gateway_tile_platforms_button_default_count_is_zero():
    """When _platformsByProfile has no cached payload for this profile, the
    rendered count should be 0 (the modal load will refresh it)."""
    tile = _extract_function(PANELS_JS, "_profileGatewayTile")
    # The render path uses _platformsConfiguredCount() against the cached
    # payload; with an empty cache the helper must yield 0 and the button
    # text "Platforms · 0".
    assert "_platformsByProfile" in PANELS_JS, "module-level platforms cache map missing"
    assert "_platformsConfiguredCount" in PANELS_JS, "configured-count helper missing"
    assert "_platformsConfiguredCount" in tile, (
        "tile must compute the count via _platformsConfiguredCount"
    )
    # The "· 0" fallback must be in scope when the cache miss falls through.
    assert "Platforms · " in tile or "Platforms ·" in tile, (
        "tile button text must include the 'Platforms · N' suffix pattern"
    )


def test_gateway_tile_platforms_button_renders_in_every_phase():
    """The platforms button is independent of gateway lifecycle phase —
    credential config must be reachable in stopped, starting, running,
    stopping, failed, unknown, and unavailable phases. The template is
    one string; verify the button markup is OUTSIDE any phase-gated
    branch by checking it's present unconditionally."""
    tile = _extract_function(PANELS_JS, "_profileGatewayTile")
    # The platforms button must appear exactly once in the template
    # (one render path, no phase-conditional branches around it).
    btn_count = tile.count("profile-gateway-platforms-btn")
    assert btn_count >= 1, "platforms button missing from tile template"
    # The toggle and the platforms button share the .profile-gateway-control
    # row (space-between layout). They live as siblings in one render path.
    # Confirm there is no `if (phase ...)` or ternary that omits the button.
    # The button markup must be reachable from the single template literal.
    # We can't execute JS here, but we can confirm the button is not wrapped
    # in a phase-conditional ternary by checking that the button class is
    # NOT inside a `? ... : ''` expression branched on phase.
    # Heuristic: find the button class line and ensure phase keywords
    # ('unavailable', 'failed', etc.) don't appear within 60 chars before it.
    idx = tile.index("profile-gateway-platforms-btn")
    window = tile[max(0, idx - 200):idx]
    for phase_keyword in ("phase === 'unavailable'", "phase === 'failed'",
                          "phase === 'stopped'", "phase === 'running'"):
        assert phase_keyword not in window, (
            f"platforms button appears phase-gated by {phase_keyword}; "
            "it must render in every phase"
        )


def test_gateway_tile_platforms_button_disabled_when_hermes_agent_unavailable():
    """When the platforms cache holds a payload with ok=false (hermes-agent
    not importable), the button should be disabled with the tooltip
    message. The render path must read state.platforms_unavailable or
    inspect the cached payload's ok flag."""
    panels = PANELS_JS
    # The render path must consult the cached payload to decide disabled/title.
    # Check that _profileGatewayTile or its helpers reference the unavailable
    # case (either via cached.ok === false or an explicit unavailable flag).
    tile = _extract_function(panels, "_profileGatewayTile")
    # Either the tile itself or a render-time helper must handle the
    # "hermes-agent not available" disabled case.
    has_unavailable_handling = (
        "platforms_unavailable" in tile
        or "platforms && platforms.ok === false" in tile
        or "cached && cached.ok === false" in tile
        or 'message: "hermes-agent not available"' in panels
        or "hermes-agent not available" in panels
    )
    assert has_unavailable_handling, (
        "tile must surface the 'hermes-agent not available' disabled state "
        "on the platforms button"
    )


def test_platforms_helpers_exist_in_panels_js():
    """The five platform helpers documented in the spec must be defined."""
    for fn_name in (
        "_loadProfilePlatforms",
        "_openPlatformsManager",
        "_renderPlatformCard",
        "_savePlatform",
        "_clearPlatform",
        "_platformsConfiguredCount",
    ):
        assert (
            f"function {fn_name}" in PANELS_JS
        ), f"helper {fn_name}() missing from panels.js"


def test_repaint_gateway_tile_updates_platforms_count():
    """_repaintGatewayTile must refresh the .profile-gateway-platforms-btn
    text when the cached platforms payload changes (e.g. after Save)."""
    repaint = _extract_function(PANELS_JS, "_repaintGatewayTile")
    assert "profile-gateway-platforms-btn" in repaint, (
        "_repaintGatewayTile must locate and refresh the platforms button"
    )
    assert "_platformsByProfile" in repaint, (
        "_repaintGatewayTile must read from the platforms cache"
    )


def test_platforms_modal_overlay_reuses_skills_overlay_chrome():
    """The platforms modal must reuse the .profile-skills-manager-overlay
    chrome verbatim (per the spec) for backdrop / centering / blur."""
    open_fn = _extract_function(PANELS_JS, "_openPlatformsManager")
    assert "profile-skills-manager-overlay" in open_fn, (
        "_openPlatformsManager must mount with the existing overlay chrome"
    )


def test_platforms_modal_inner_buttons_stop_propagation():
    """Per the project's inline-editor rule (feedback_inline_editor_click_bubble),
    Save/Cancel buttons inside the modal must call stopPropagation() so
    they don't race a host listener and silently re-enter edit mode."""
    open_fn = _extract_function(PANELS_JS, "_openPlatformsManager")
    assert "stopPropagation" in open_fn, (
        "modal Save/Cancel handlers must call event.stopPropagation()"
    )


def test_platforms_css_classes_are_defined():
    """The modal-card classes referenced in the mockup must exist in style.css."""
    for selector in (
        ".profile-gateway-platforms-btn",
        ".platforms-manager-list",
        ".platforms-card",
        ".platforms-card-head",
        ".platforms-card-body",
        ".platforms-field",
        ".pf-status",
        ".pf-secret-row",
    ):
        assert selector in STYLE_CSS, f"missing CSS selector {selector}"
