"""Regression test: WebUI streaming must merge platform_toolsets.webui.

The CLI resolves its toolsets with ``_get_platform_tools(config, "cli")``.
The WebUI streaming path used only ``_resolve_cli_toolsets(_cfg)`` (the cli
composite), so any toolset configured under ``platform_toolsets.webui`` was
silently ignored — the only way to add a WebUI-only tool was to duplicate it
under the ``cli`` key, which also leaks it into terminal sessions.
"""

from pathlib import Path

import ast


ROOT = Path(__file__).resolve().parents[1]


def _streaming_source() -> str:
    return (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


def _toolset_resolution_region(src: str) -> str:
    """Return the source region from the import of _resolve_cli_toolsets to
    the per-session override that follows it."""
    start = src.index("from api.config import _resolve_cli_toolsets")
    end = src.index("Per-session toolset override", start)
    return src[start:end]


def test_webui_toolsets_are_merged_into_the_streaming_resolution():
    src = _streaming_source()
    region = _toolset_resolution_region(src)

    assert '_get_platform_tools(_cfg, "webui")' in region, (
        "streaming must merge the platform_toolsets.webui composite on top of "
        "the cli toolsets"
    )


def test_merge_failure_never_breaks_streaming():
    """The webui merge is best-effort: a missing/raising helper must not
    abort the turn (older hermes-agent versions may not ship the helper)."""
    src = _streaming_source()
    region = _toolset_resolution_region(src)

    assert "try:" in region
    assert region.index("try:") < region.index('_get_platform_tools(_cfg, "webui")')


def test_merge_is_a_union_not_a_replacement():
    src = _streaming_source()
    region = _toolset_resolution_region(src)

    # Union semantics: webui-only additions extend the cli set; the cli set is
    # never dropped or filtered by the webui key.
    assert "set(_toolsets) | set(_webui_toolsets)" in region


def test_syntax_valid():
    ast.parse(_streaming_source())
