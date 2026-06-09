from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _css_rule(selector: str) -> str:
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    start = css.index(selector + "{")
    return css[start : css.index("}", start) + 1]


def test_offline_banner_uses_theme_surface_and_active_palette_accent_tokens():
    rule = _css_rule(".offline-banner")

    assert "var(--bg-1" not in rule
    assert "var(--warning" not in rule
    assert "background:color-mix(in srgb,var(--surface) 88%,var(--accent))" in rule
    assert "border-bottom:1px solid color-mix(in srgb,var(--accent) 50%,var(--surface))" in rule


def test_offline_banner_title_and_action_follow_active_palette_accent():
    title_rule = _css_rule(".offline-copy strong")
    action_rule = _css_rule(".offline-action")
    hover_rule = _css_rule(".offline-action:hover")

    assert "color:var(--accent-text)" in title_rule
    assert "color:var(--accent-text)" in action_rule
    assert "background:var(--accent-bg)" in action_rule
    assert "border:1px solid var(--accent-bg-strong)" in action_rule
    assert "background:var(--accent-bg-strong)" in hover_rule
    assert "var(--warning" not in title_rule
    assert "var(--warning" not in action_rule
