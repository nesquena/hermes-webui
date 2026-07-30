"""Regression coverage for #2998: gold panel save buttons need visible icons."""

from pathlib import Path


CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")


def test_panel_primary_buttons_use_foreground_token_for_icon_contrast():
    assert "--panel-head-primary-fg" in CSS
    assert ".panel-head-btn.primary{background:var(--accent,var(--link));color:var(--panel-head-primary-fg,#fff);border:none;}" in CSS
    assert ".panel-head-btn.primary:hover{background:var(--accent-hover,var(--accent,var(--link)));color:var(--panel-head-primary-fg,#fff);}" in CSS
    assert ".panel-head-btn.primary svg{color:var(--panel-head-primary-fg,#fff);}" in CSS


def test_dark_theme_panel_primary_buttons_use_dark_foreground_on_bright_accents():
    assert ":root.dark{--panel-head-primary-fg:var(--bg);}" in CSS
