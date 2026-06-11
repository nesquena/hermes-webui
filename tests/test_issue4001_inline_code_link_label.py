from tests.test_renderer_js_behaviour import _render, driver_path  # noqa: F401


def test_inline_code_inside_link_label_renders_as_code(driver_path):
    out = _render(driver_path, "[`8c64957`](https://github.com/x/y)")
    assert '<a href="https://github.com/x/y"' in out
    assert "<code>8c64957</code>" in out
    assert "&lt;code&gt;" not in out


def test_list_item_link_label_keeps_inline_code(driver_path):
    out = _render(driver_path, "- [`8c64957`](https://github.com/x/y)")
    assert "<li>" in out
    assert "<code>8c64957</code>" in out
    assert "&lt;code&gt;" not in out


def test_unknown_raw_html_inside_link_label_is_escaped_once(driver_path):
    out = _render(driver_path, "[<script>alert(1)</script>](https://github.com/x/y)")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&amp;lt;script" not in out
