"""Graphite skin registration and neutral workbench palette."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_graphite_skin_is_registered_end_to_end():
    assert "{name:'Graphite'" in BOOT_JS
    assert "graphite:1" in INDEX_HTML
    assert '"graphite"' in CONFIG_PY
    assert "/graphite/" in I18N_JS


def test_graphite_skin_defines_light_and_dark_palettes():
    assert ':root[data-skin="graphite"]{' in CSS
    assert ':root.dark[data-skin="graphite"]{' in CSS
    for token in ("--bg:#FFFFFF", "--sidebar:#F3F3F3", "--accent:#303030", "--gold:#303030"):
        assert token in CSS, f"Graphite light token missing: {token}"
    for token in ("--bg:#151614", "--sidebar:#242624", "--accent:#D7D6CE", "--gold:#D7D6CE"):
        assert token in CSS, f"Graphite dark token missing: {token}"


def test_graphite_skin_uses_neutral_accent_not_green_brand_chrome():
    assert "{name:'Graphite', colors:['#FFFFFF','#D6D6D6','#242424']}" in BOOT_JS
    assert "--accent:#10A37F" not in CSS
    assert "--accent:#0F8F70" not in CSS


def test_graphite_light_skin_avoids_olive_tinted_surfaces():
    for token in ("#F5F5F2", "#E9E9E4", "#ECECE6", "#E7E7DF", "#D6D6CC", "#B8B8AE"):
        assert token not in CSS
        assert token not in BOOT_JS


def test_graphite_skin_tunes_workbench_chrome():
    assert ':root[data-skin="graphite"] .composer-box' in CSS
    assert "box-shadow:0 1px 2px rgba(0,0,0,0.07)" in CSS
    assert ':root[data-skin="graphite"] .workspace-panel-edge-toggle' in CSS
    assert ':root[data-skin="graphite"] .scroll-to-bottom-btn' in CSS
    assert ':root[data-skin="graphite"] .session-item{border-radius:8px;font-size:12.5px;' in CSS
    assert ':root[data-skin="graphite"] .session-meta{font-size:10.5px;' in CSS
    assert ':root[data-skin="graphite"] .session-item.active' in CSS
    assert ':root[data-skin="graphite"] .session-item.active{position:relative;padding:10px 12px 10px 18px;' in CSS
    assert ':root[data-skin="graphite"] .session-item.active.streaming' in CSS
    assert ':root[data-skin="graphite"] .session-item.active.menu-open{padding-right:40px;}' in CSS
    assert ':root[data-skin="graphite"] .session-item.active:hover .session-time:not(.is-hidden)' in CSS
    assert ':root[data-skin="graphite"] .session-item.active .session-lineage-count' in CSS
    assert ':root[data-skin="graphite"] .app-titlebar-icon rect' in CSS
    assert ':root[data-skin="graphite"] #mainSettings .theme-pick-btn.active' in CSS
    assert ':root[data-skin="graphite"] .msg-row[data-role="user"] .msg-body' in CSS


def test_graphite_skin_keeps_code_block_frame_continuous():
    assert ':root[data-skin="graphite"] .pre-header{background:var(--code-bg);border-color:var(--border-muted);border-bottom:0;}' in CSS
    assert ':root[data-skin="graphite"] .pre-header+pre{border-color:var(--border-muted);border-top:1px solid var(--border-muted);margin-top:0;}' in CSS


def test_graphite_skin_uses_native_ui_and_mono_font_stacks():
    assert '--font-ui:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif' in CSS
    assert '--font-mono:ui-monospace,"SFMono-Regular","SF Mono",Menlo,Consolas,"Liberation Mono",monospace' in CSS
    assert "font-weight:430" in CSS
    assert "-webkit-font-smoothing:antialiased" in CSS
    assert ':root[data-skin="graphite"] textarea#msg' in CSS
    assert ':root[data-skin="graphite"] textarea#msg{font-family:var(--font-ui)!important;font-size:14px;' in CSS
    assert ':root[data-skin="graphite"] .msg-body{font-family:var(--font-ui);font-size:13px;font-weight:430;' in CSS
    assert ':root[data-skin="graphite"][data-font-size="large"] .msg-body{font-size:15px;' in CSS
    assert ':root[data-skin="graphite"] .tool-card-name' in CSS
