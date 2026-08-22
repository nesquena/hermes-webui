"""Cross-consumer regression coverage for MEDIA token boundary parity (#6890)."""

from __future__ import annotations

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


def test_wrapped_inner_punctuation_matches_renderer_share_auth_and_snapshot(
    media_parity_driver, tmp_path, monkeypatch
):
    from api import routes, shares
    from api.media_snapshots import annotate_media_snapshots

    image = tmp_path / "ok.png"
    _write_png(image)
    text = f"**MEDIA:{image}.**"
    encoded = urllib.parse.quote(str(image), safe="")

    rendered = _render(media_parity_driver, text)
    assert f"path={encoded}" in rendered
    assert f"path={encoded}." not in rendered
    assert ".</strong>" in rendered

    shared = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in shared
    assert shares._PLACEHOLDER not in shared
    assert shared.endswith(".**")

    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_image_path(
            "s-media-parity", image, {"image/png"}
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


@pytest.mark.parametrize("punctuation", [".", ";", "!"])
def test_punctuated_http_path_is_preserved_while_server_consumers_bypass_remote_refs(
    media_parity_driver, tmp_path, punctuation
):
    from api import routes, shares
    from api.media_snapshots import annotate_media_snapshots

    ref = f"https://example.com/a.png{punctuation}"
    text = f"MEDIA:{ref}"

    rendered = _render(media_parity_driver, text)
    assert f'src="{ref}"' in rendered

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
