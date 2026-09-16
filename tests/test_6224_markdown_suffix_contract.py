"""#6224: one deliberate Markdown suffix set across every layer.

An inline `.md` preview is recognized by four layers that have to agree:

* `api/config.py` — `MD_EXTS` (the declared compatibility set) and `MIME_MAP`;
* `/api/media` authorization — the session-token grant is MIME-typed
  (`_session_media_token_allows_path` maps the suffix through `MIME_MAP` and
  checks it against the session-token whitelist);
* `static/workspace.js` — `MD_EXTS`, the workspace preview routing;
* `static/ui.js` — `_MD_EXTS`, the chat-render detector.

They used to disagree: the server/workspace layers accepted
`.md/.markdown/.mdown` while the chat/MIME layers accepted `.md/.mkd/.mkdn`, so
a `.markdown` artifact that the workspace route previewed was refused by the
authorization path (and `.mkd`/`.mkdn` chat previews worked but were not
previewable from the file browser). These tests parameterize the *real*
`/api/media` handler and the *real* authorization over every supported suffix
plus a non-Markdown control, so the set cannot drift apart again.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd", ".mkdn")
NON_MARKDOWN_SUFFIXES = (".txt", ".rst")

BODY = "# notes\n\nhello markdown\n"


class _FakeHandler:
    def __init__(self, headers=None):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = dict(headers or {})

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, key):
        return next((v for k, v in self.sent_headers if k.lower() == key.lower()), "") or ""


@pytest.fixture
def routes():
    from api import routes

    return routes


@pytest.fixture(autouse=True)
def media_allowed_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)


def _media_get(routes, target, query_extra=""):
    handler = _FakeHandler()
    parsed = SimpleNamespace(path="/api/media", query=f"path={target}{query_extra}")
    routes._handle_media(handler, parsed)
    return handler


def _write_fixture(tmp_path, suffix):
    fixture = tmp_path / f"notes{suffix}"
    fixture.write_text(BODY, encoding="utf-8")
    return fixture


def _assert_served_markdown(handler, fixture):
    assert handler.status == 200, (handler.status, bytes(handler.body))
    assert "text/markdown" in handler.header("Content-Type"), handler.sent_headers
    assert BODY.encode("utf-8") in bytes(handler.body), bytes(handler.body)


# ── the declared set ──────────────────────────────────────────────────────


def test_config_md_exts_declares_the_canonical_set():
    from api.config import MD_EXTS

    assert MD_EXTS == set(MARKDOWN_SUFFIXES), (
        "api/config.MD_EXTS must be the one compatibility set the frontend "
        "detectors mirror (static/workspace.js and static/ui.js)"
    )


@pytest.mark.parametrize("suffix", MARKDOWN_SUFFIXES)
def test_mime_map_maps_every_markdown_suffix(suffix):
    from api.config import MIME_MAP

    assert MIME_MAP.get(suffix) == "text/markdown", (
        f"{suffix} must map to text/markdown — the session-token authorization "
        "tests the MIME type, so an unmapped suffix is silently denied"
    )


@pytest.mark.parametrize("suffix", NON_MARKDOWN_SUFFIXES)
def test_mime_map_does_not_claim_non_markdown_suffixes(suffix):
    from api.config import MIME_MAP

    assert MIME_MAP.get(suffix) != "text/markdown"


@pytest.mark.parametrize("suffix", MARKDOWN_SUFFIXES)
def test_media_handler_serves_every_markdown_suffix(routes, tmp_path, suffix):
    fixture = _write_fixture(tmp_path, suffix)

    handler = _media_get(routes, fixture)

    _assert_served_markdown(handler, fixture)


@pytest.mark.parametrize("suffix", NON_MARKDOWN_SUFFIXES)
def test_media_handler_does_not_serve_non_markdown_as_markdown(routes, tmp_path, suffix):
    fixture = _write_fixture(tmp_path, suffix)

    handler = _media_get(routes, fixture)

    assert handler.status == 200, handler.status
    assert "text/markdown" not in handler.header("Content-Type"), handler.sent_headers


# ── authorization: the session-token grant is MIME-typed ──────────────────


def _grant_session(routes, fixture):
    session = SimpleNamespace(
        messages=[{"role": "assistant", "content": f"MEDIA:{fixture}"}]
    )
    return mock.patch.object(routes, "get_session", return_value=session)


@pytest.mark.parametrize("suffix", MARKDOWN_SUFFIXES)
def test_session_token_grant_authorizes_every_markdown_suffix(
    routes, tmp_path, monkeypatch, suffix
):
    """A MEDIA: token the session emitted must authorize its own artifact.

    The path is deliberately out of every allowed root (the root check is
    neutralized) so the session-token branch is the only way this request can
    be served — exactly the historical-preview case that has to work for each
    supported suffix.
    """
    fixture = _write_fixture(tmp_path, suffix)
    monkeypatch.setattr(routes, "_path_is_within_root", lambda *a, **k: False)

    with _grant_session(routes, fixture):
        handler = _media_get(routes, fixture, "&session_id=s-media")

    _assert_served_markdown(handler, fixture)


@pytest.mark.parametrize("suffix", NON_MARKDOWN_SUFFIXES)
def test_session_token_grant_does_not_widen_to_other_types(
    routes, tmp_path, monkeypatch, suffix
):
    fixture = _write_fixture(tmp_path, suffix)
    monkeypatch.setattr(routes, "_path_is_within_root", lambda *a, **k: False)

    with _grant_session(routes, fixture):
        handler = _media_get(routes, fixture, "&session_id=s-media")

    assert handler.status == 403, (
        "the Markdown grant is MIME-typed: a non-Markdown MEDIA: token must "
        f"still be refused (got {handler.status})"
    )


def test_session_token_grant_is_required_for_out_of_root_markdown(routes, tmp_path, monkeypatch):
    fixture = _write_fixture(tmp_path, ".markdown")
    monkeypatch.setattr(routes, "_path_is_within_root", lambda *a, **k: False)

    # No grant and no session-authored MEDIA: token -> refused.
    with mock.patch.object(routes, "get_session", return_value=SimpleNamespace(messages=[])):
        handler = _media_get(routes, fixture)

    assert handler.status == 403, handler.status


@pytest.mark.parametrize("suffix", MARKDOWN_SUFFIXES)
def test_markdown_is_never_served_inline(routes, tmp_path, suffix):
    """Unifying the suffix set must not add Markdown to the inline preview set."""
    fixture = _write_fixture(tmp_path, suffix)

    handler = _media_get(routes, fixture, "&inline=1")

    assert handler.status == 200, handler.status
    disposition = handler.header("Content-Disposition")
    assert "attachment" in disposition, (
        "text/markdown must stay out of the inline preview whitelist "
        f"(session-token only) — got {disposition!r}"
    )
