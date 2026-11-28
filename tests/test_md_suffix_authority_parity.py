"""One Markdown suffix authority across every layer.

Code-review blocker 1: ``api/config.py:MD_EXTS`` / ``static/workspace.js:MD_EXTS``
used to define ``.md/.markdown/.mdown`` while the MIME map and the chat preview
path defined ``.md/.mkd/.mkdn``.  The canonical set is the union, preserved
everywhere:

    {.md, .markdown, .mdown, .mkd, .mkdn}

These tests assert parity across server MIME mapping, workspace preview routing,
chat preview routing and session-grant authorization, and they *execute* the
real literals (the parsed set / the compiled regex) instead of doing
source-string comparisons.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANONICAL = {".md", ".markdown", ".mdown", ".mkd", ".mkdn"}


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _js_set_literal(source: str, name: str) -> set[str]:
    """Parse ``const NAME = new Set(['.a','.b']);`` out of a JS source file."""
    match = re.search(
        r"const\s+" + re.escape(name) + r"\s*=\s*new\s+Set\(\s*\[(.*?)\]\s*\)",
        source,
        re.S,
    )
    assert match, f"{name} set literal not found"
    return {token.lower() for token in re.findall(r"'([^']*)'|\"([^\"]*)\"", match.group(1)) for token in [token[0] or token[1]]}


def _js_regex(source: str, name: str) -> re.Pattern:
    """Parse ``const NAME=/\.(md|...)$/i;`` and compile it as a Python regex."""
    match = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*/(.+?)/([a-z]*)\s*;", source, re.S)
    assert match, f"{name} regex literal not found"
    flags = re.IGNORECASE if "i" in match.group(2) else 0
    return re.compile(match.group(1), flags)


def _server_suffix_set() -> set[str]:
    from api.config import MD_EXTS

    return {ext.lower() for ext in MD_EXTS}


def test_server_and_workspace_and_chat_authorities_define_the_same_suffixes():
    authorities = {
        "api/config.py:MD_EXTS": _server_suffix_set(),
        "api/config.py:MIME_MAP(markdown)": {
            ext for ext, mime in __import__("api.config", fromlist=["MIME_MAP"]).MIME_MAP.items()
            if str(mime).startswith("text/markdown")
        },
        "static/workspace.js:MD_EXTS": _js_set_literal(_read("static", "workspace.js"), "MD_EXTS"),
        "static/ui.js:_MD_EXTS": set(),
    }
    chat_regex = _js_regex(_read("static", "ui.js"), "_MD_EXTS")
    for suffix in CANONICAL:
        if chat_regex.search("notes" + suffix):
            authorities["static/ui.js:_MD_EXTS"].add(suffix)
    for name, suffixes in authorities.items():
        assert suffixes == CANONICAL, f"{name} diverges: {sorted(suffixes)} != {sorted(CANONICAL)}"


def test_every_js_preview_router_defines_md_exts_with_the_canonical_suffixes():
    definitions = 0
    for path in sorted((ROOT / "static").glob("*.js")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"const\s+MD_EXTS\s*=\s*new\s+Set", source):
            definitions += 1
            assert _js_set_literal(source, "MD_EXTS") == CANONICAL, (
                f"static/{path.name}:MD_EXTS diverges from the canonical set"
            )
    assert definitions >= 1, "no MD_EXTS definition found in static/*.js"


@pytest.mark.parametrize("suffix", sorted(CANONICAL))
def test_chat_preview_router_matches_every_canonical_alias(suffix):
    """The real chat-side regex must route each alias to the inline preview."""
    chat_regex = _js_regex(_read("static", "ui.js"), "_MD_EXTS")
    assert chat_regex.search("notes" + suffix), f"{suffix} is not routed to the markdown preview"
    assert not chat_regex.search("notes" + suffix + ".txt"), f"{suffix} regex is not anchored"


@pytest.mark.parametrize("suffix", sorted(CANONICAL))
def test_mime_map_serves_every_canonical_alias_as_text_markdown(suffix):
    from api.config import MIME_MAP

    assert MIME_MAP.get(suffix) == "text/markdown", f"{suffix} must map to text/markdown"


@pytest.mark.parametrize("suffix", sorted(CANONICAL))
def test_session_grant_authorizes_every_canonical_alias(suffix, tmp_path):
    """Behavioral: the session-grant authorization path accepts each alias."""
    from api import routes

    artifact = tmp_path / ("notes" + suffix)
    artifact.write_text("# Notes", encoding="utf-8")
    session = SimpleNamespace(messages=[{"role": "assistant", "content": f"MEDIA:{artifact}"}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_path(
            "s-media", artifact, {"text/markdown"}
        ), f"{suffix} is not authorized through the session-grant preview path"


def test_workspace_preview_routing_category_is_markdown_for_every_alias():
    """Executes the workspace routing decision (``MD_EXTS.has(ext)``)."""
    source = _read("static", "workspace.js")
    suffixes = _js_set_literal(source, "MD_EXTS")
    assert re.search(r"MD_EXTS\.has\(\s*ext\s*\)", source), "workspace routing no longer consults MD_EXTS"
    for suffix in CANONICAL:
        assert suffix in suffixes, f"workspace routing misses {suffix}"
