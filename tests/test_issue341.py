"""Tests for GitHub issue #341: .msg-body table CSS styles."""
import os

CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "style.css")


def _read_css():
    with open(CSS_PATH, "r") as f:
        return f.read()


def test_msg_body_table_css_present():
    css = _read_css()
    assert ".msg-body table" in css, ".msg-body table rule missing from style.css"
    assert "border-collapse:collapse" in css, "border-collapse:collapse missing from style.css"


def test_msg_body_table_th_td_present():
    css = _read_css()
    assert ".msg-body th" in css, ".msg-body th rule missing from style.css"
    assert ".msg-body td" in css, ".msg-body td rule missing from style.css"


def test_msg_body_table_tr_stripe_present():
    css = _read_css()
    assert ".msg-body tr:nth-child(even)" in css, ".msg-body tr:nth-child(even) rule missing from style.css"


def test_msg_body_light_theme_overrides():
    css = _read_css()
    assert ':root:not(.dark) .msg-body th' in css, \
        'Light-mode override for .msg-body th missing from style.css'
    assert ':root:not(.dark) .msg-body td' in css, \
        'Light-mode override for .msg-body td missing from style.css'
