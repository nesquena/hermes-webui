import importlib.util
from pathlib import Path

import pytest


_HELPERS_PATH = Path(__file__).with_name("test_renderer_js_behaviour.py")
_SPEC = importlib.util.spec_from_file_location("issue5641_renderer_helpers", _HELPERS_PATH)
_HELPERS = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_HELPERS)

NODE = _HELPERS.NODE
_render = _HELPERS._render
driver_path = _HELPERS.driver_path


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.mark.parametrize(
    "src",
    [
        "| a | b | \n|---|---|\n| 1 | 2 |",
        "| a | b |\n|---|---| \n| 1 | 2 |",
        " | a | b |\n |---|---|\n | 1 | 2 |",
        "   | a | b |\n   |---|---|\n   | 1 | 2 |",
    ],
)
def test_table_rows_with_edge_whitespace_still_render_as_table(driver_path, src):
    out = _render(driver_path, src)
    assert "<table><thead>" in out
    assert "<td>1</td>" in out
    assert "<p>" not in out


def test_four_space_indented_table_like_block_stays_outside_table(driver_path):
    src = (
        "    | a | b |\n"
        "    |---|---|\n"
        "    | 1 | 2 |"
    )
    out = _render(driver_path, src)
    assert "<table" not in out
