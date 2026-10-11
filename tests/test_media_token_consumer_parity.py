"""Cross-consumer regression coverage for MEDIA token boundary parity (#6890)."""

from __future__ import annotations

import re
import time
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.test_renderer_js_behaviour import NODE, _DRIVER_SRC, _render

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def media_parity_driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("media_parity_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _write_png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


class _MediaHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.headers = {}
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, key):
        return next((value for name, value in self.sent_headers if name == key), "")


def test_wrapped_inner_punctuation_selects_exact_file_across_consumers(
    media_parity_driver, tmp_path, monkeypatch
):
    from api import routes, shares
    from api.helpers import split_media_token_ref
    from api.media_snapshots import annotate_media_snapshots

    base = tmp_path / "chart.png"
    punctuated = tmp_path / "chart.png!"
    base.write_bytes(b"blue-base-file")
    punctuated.write_bytes(b"red-punctuated-file")
    text = f"**MEDIA:{punctuated}**"
    encoded = urllib.parse.quote(str(punctuated), safe="").replace("%21", "!")

    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert match is not None
    assert split_media_token_ref(text, match) == (str(punctuated), "**")

    rendered = _render(media_parity_driver, text)
    assert f"path={encoded}" in rendered
    assert f"path={urllib.parse.quote(str(base), safe='')}\"" not in rendered

    shared = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    assert "data:image/png;base64," not in shared
    assert shares._PLACEHOLDER in shared
    assert shared.endswith("**")

    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_path(
            "s-media-parity", punctuated, {"application/octet-stream"}
        )
        assert not routes._session_media_token_allows_image_path(
            "s-media-parity", base, {"image/png"}
        )

    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = _MediaHandler()
    parsed = SimpleNamespace(
        path="/api/media", query=f"path={urllib.parse.quote(str(punctuated), safe='')}"
    )
    routes._handle_media(handler, parsed)
    assert handler.status == 200
    assert bytes(handler.body) == punctuated.read_bytes()
    assert bytes(handler.body) != base.read_bytes()

    monkeypatch.setenv(
        "HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "media_snapshots")
    )
    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    snapshots = messages[0]["_media_snapshots"]
    assert str(punctuated.resolve()) in snapshots
    assert str(base.resolve()) not in snapshots


def test_malformed_interior_punctuation_has_bounded_renderer_and_route_growth(
    media_parity_driver, tmp_path
):
    from api import routes

    target = tmp_path / "chart.png"
    _write_png(target)

    def render_elapsed(size):
        started = time.perf_counter()
        _render(media_parity_driver, f"MEDIA:/tmp/chart.png{'!' * size}z")
        return time.perf_counter() - started

    def route_elapsed(size):
        text = f"MEDIA:{target}{'!' * size}z"
        session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
        started = time.perf_counter()
        with mock.patch.object(routes, "get_session", return_value=session):
            assert not routes._session_media_token_allows_image_path(
                "s-media-linear", target, {"image/png"}
            )
        return time.perf_counter() - started

    # An eightfold input increase must stay far below quadratic (64x) growth.
    # The additive allowance covers process startup and timer jitter on CI.
    small_render = render_elapsed(4_000)
    large_render = render_elapsed(32_000)
    assert large_render <= small_render * 20 + 0.15

    small_route = route_elapsed(2_000)
    large_route = route_elapsed(16_000)
    assert large_route <= small_route * 20 + 0.15


@pytest.mark.parametrize(
    ("entity_quote", "literal_quote"),
    [("&quot;", '"'), ("&#39;", "'")],
)
def test_entity_balanced_local_media_matches_renderer_stream_server_consumers(
    media_parity_driver, tmp_path, monkeypatch, entity_quote, literal_quote
):
    from api import routes, shares
    from api.helpers import split_media_token_ref
    from api.media_snapshots import annotate_media_snapshots

    image = tmp_path / "ok.png"
    _write_png(image)
    text = f"{entity_quote}MEDIA:{image}{entity_quote}."
    encoded = urllib.parse.quote(str(image), safe="")

    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert match is not None
    assert split_media_token_ref(text, match) == (str(image), f"{literal_quote}.")

    rendered = _render(media_parity_driver, text)
    assert f"path={encoded}" in rendered
    assert f"path={encoded}%26" not in rendered

    shared = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in shared
    assert shares._PLACEHOLDER not in shared

    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_image_path(
            "s-media-entity-parity", image, {"image/png"}
        )

    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "media_snapshots")
    )
    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    snapshots = messages[0]["_media_snapshots"]
    assert str(image.resolve()) in snapshots
    assert len(snapshots[str(image.resolve())]) == 64


@pytest.mark.parametrize("punctuation", [".", ",", "?"])
def test_bare_remote_path_preserves_ambiguous_trailing_bytes_while_server_consumers_bypass_remote_refs(
    media_parity_driver, tmp_path, punctuation
):
    from api import routes, shares
    from api.media_snapshots import annotate_media_snapshots

    clean_ref = "https://example.com/a.png"
    text = f"MEDIA:{clean_ref}{punctuation}"

    rendered = _render(media_parity_driver, text)
    assert f'src="{clean_ref}{punctuation}"' in rendered

    # Public-share embedding intentionally handles only local refs; a remote
    # token must pass through byte-for-byte instead of being normalized as a
    # local path.
    assert shares._embed_share_media(text, allowed_roots=(tmp_path,)) == text

    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 0
    assert "_media_snapshots" not in messages[0]

    # Session-token authorization is likewise local-path-only. Feeding the same
    # remote transcript token must never authorize an unrelated local file.
    local_image = tmp_path / "a.png"
    _write_png(local_image)
    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert not routes._session_media_token_allows_image_path(
            "s-media-parity", local_image, {"image/png"}
        )


@pytest.mark.parametrize(
    ("opener", "suffix"),
    [("", "!"), ("", ";"), ("", ":"), ("__", "_"), ("**", "*")],
)
def test_ambiguous_local_suffix_bytes_select_the_exact_file_across_consumers(
    media_parity_driver, tmp_path, monkeypatch, opener, suffix
):
    from api import routes, shares
    from api.helpers import split_media_token_ref
    from api.media_snapshots import annotate_media_snapshots

    base = tmp_path / "chart.png"
    suffixed = tmp_path / f"chart.png{suffix}"
    base.write_bytes(b"blue-base-file")
    suffixed.write_bytes(f"red-suffixed-file-{suffix}".encode())
    text = f"{opener}MEDIA:{suffixed}"
    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert match is not None
    assert split_media_token_ref(text, match) == (str(suffixed), "")

    rendered = _render(media_parity_driver, text)
    encoded = (
        urllib.parse.quote(str(suffixed), safe="")
        .replace("%21", "!")
        .replace("%2A", "*")
    )
    assert f"api/media?path={encoded}" in rendered

    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "media_snapshots")
    )
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = _MediaHandler()
    parsed = SimpleNamespace(
        path="/api/media", query=f"path={urllib.parse.quote(str(suffixed), safe='')}"
    )
    routes._handle_media(handler, parsed)
    assert handler.status == 200
    assert bytes(handler.body) == suffixed.read_bytes()
    assert bytes(handler.body) != base.read_bytes()
    assert suffixed.name in handler.header("Content-Disposition")

    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    assert str(suffixed.resolve()) in messages[0]["_media_snapshots"]
    assert str(base.resolve()) not in messages[0]["_media_snapshots"]

    # A punctuation-bearing path is not an image by extension. Public shares
    # must therefore redact it, never silently embed the distinct base image.
    assert (
        shares._embed_share_media(text, allowed_roots=(tmp_path,))
        == opener + shares._PLACEHOLDER
    )


@pytest.mark.parametrize("wrapped", [False, True])
def test_backtick_filename_matches_renderer_auth_and_snapshot(
    media_parity_driver, tmp_path, monkeypatch, wrapped
):
    from api import routes
    from api.media_snapshots import annotate_media_snapshots

    image = tmp_path / ("ok.png" if wrapped else "ok`final.png")
    _write_png(image)
    text = f"`MEDIA:{image}`" if wrapped else f"MEDIA:{image}"
    rendered = _render(media_parity_driver, text)
    encoded = urllib.parse.quote(str(image), safe="")
    assert f"path={encoded}" in rendered
    assert f"path={encoded}%60" not in rendered
    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_image_path("backticks", image, {"image/png"})
    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    assert str(image.resolve()) in messages[0]["_media_snapshots"]


@pytest.mark.parametrize("suffix", ["!", ";", ":", "!!", ".!", ";."])
def test_remote_path_keeps_meaningful_trailing_bytes(media_parity_driver, suffix):
    ref = f"https://example.com/a.png{suffix}"
    assert f'src="{ref}"' in _render(media_parity_driver, f"MEDIA:{ref}")


@pytest.mark.parametrize("ref", ["_", "__", "*", "**", "`", "*_", "___"])
def test_unmatched_delimiter_only_filename_is_a_media_ref(media_parity_driver, ref):
    from api.helpers import split_media_token_ref

    text = f"MEDIA:{ref}"
    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert split_media_token_ref(text, match) == (ref, "")
    encoded = urllib.parse.quote(ref, safe="").replace("%2A", "*")
    assert f"api/media?path={encoded}" in _render(media_parity_driver, text)


@pytest.mark.parametrize("delimiter", ["*", "**", "***", "_", "__", "___", "`"])
def test_matching_empty_wrapper_is_not_a_media_ref(media_parity_driver, delimiter):
    from api.helpers import split_media_token_ref

    text = f"{delimiter}MEDIA:{delimiter}"
    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert split_media_token_ref(text, match) is None
    assert "api/media?path=" not in _render(media_parity_driver, text)


@pytest.mark.parametrize("suffix", ["?signature=!", "?signature=?", "#", "#?"])
def test_remote_query_and_fragment_bytes_stay_intact(media_parity_driver, suffix):
    ref = f"https://example.com/a.png{suffix}"
    assert f'src="{ref}"' in _render(media_parity_driver, f"MEDIA:{ref}")


@pytest.mark.parametrize("closer", [")", "]"])
def test_unwrapped_remote_period_requires_sentence_boundary(media_parity_driver, closer):
    ref = "https://example.com/a.png."
    assert f'src="{ref}"' in _render(media_parity_driver, f"MEDIA:{ref}{closer}")
