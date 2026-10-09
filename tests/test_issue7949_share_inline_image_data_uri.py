"""Regression tests for #7949: public-share creation 500s on an inline image.

A conversation containing an inline-image MEDIA token — ``MEDIA:data:image/
...;base64,<blob>`` — whose data URI was longer than ~4 KB made
``build_share_snapshot`` / ``_embed_share_media`` raise
``OSError: [Errno 36] File name too long`` (ENAMETOOLONG) and return HTTP 500.

Root cause: the whole base64 blob became the ``raw`` "path" and
``_resolve_against_roots`` stat()ed it outside its try/except.

The fix routes ``data:`` tokens to an in-memory validator that applies the
same public-share policy as a local file (raster MIME allow-list, strict
base64, byte cap, magic bytes) and never touches the filesystem. A valid
raster image survives as an ``<img>``; every other data URI (text, octet-stream,
SVG, malformed, oversized, mismatched magic) is replaced by the placeholder, so
none of its bytes can reach the public snapshot JSON. ``_resolve_against_roots``
also keeps a defensive length/newline/NUL guard.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from api import shares

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SECRET = "sk-live-THIS-MUST-NOT-APPEAR-1234567890"


def _embed(text: str, roots):
    return shares._embed_share_media(text, allowed_roots=tuple(roots))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _FakeSession:
    def __init__(self, tmp_path: Path, messages):
        self.session_id = "s-7949"
        self.title = "Share test"
        self.messages = messages
        self.workspace = str(tmp_path / "workspace")
        self.created_at = 0
        self.updated_at = 0

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "messages": self.messages,
            "workspace": self.workspace,
        }


def _snapshot_json(tmp_path: Path, content: str) -> str:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    session = _FakeSession(
        tmp_path,
        [
            {"role": "user", "content": "render it"},
            {"role": "assistant", "content": content},
        ],
    )
    return json.dumps(shares.build_share_snapshot(session))


def test_valid_png_data_uri_survives_as_image(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    out = _embed(f"Chart: MEDIA:data:image/png;base64,{_b64(PNG)} done", [ws])
    assert f'<img src="data:image/png;base64,{_b64(PNG)}"' in out
    assert shares._PLACEHOLDER not in out
    assert out.endswith(" done")


@pytest.mark.parametrize("blob_len", [5_000, 60_000, 400_000])
def test_large_valid_png_does_not_crash_and_survives(tmp_path: Path, blob_len):
    """The exact #7949 crash shape: a >4 KB inline image must not raise."""
    big = PNG + b"\x00" * blob_len
    snap = _snapshot_json(tmp_path, f"Here: MEDIA:data:image/png;base64,{_b64(big)}")
    assert "data:image/png;base64," in snap


def test_mixed_case_scheme_and_jpg_alias(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    out = _embed(f"MEDIA:DATA:Image/JPG;base64,{_b64(jpeg)}", [ws])
    assert '<img src="data:image/jpeg;base64,' in out


@pytest.mark.parametrize(
    "uri",
    [
        f"data:text/plain;base64,{_b64(SECRET.encode())}",
        f"data:application/octet-stream;base64,{_b64(SECRET.encode())}",
        "data:image/svg+xml;base64,"
        + _b64(f'<svg xmlns="http://www.w3.org/2000/svg"><text>{SECRET}</text></svg>'.encode()),
        # declared PNG, but the bytes are text: magic-byte mismatch
        f"data:image/png;base64,{_b64(SECRET.encode())}",
        # not base64 at all (URL-encoded form)
        "data:image/png,%89PNG" + SECRET,
        # malformed base64 alphabet
        "data:image/png;base64,@@@" + SECRET,
    ],
)
def test_unsafe_data_uris_never_reach_snapshot(tmp_path: Path, uri):
    snap = _snapshot_json(tmp_path, f"leak: MEDIA:{uri} end")
    assert SECRET not in snap
    assert _b64(SECRET.encode()) not in snap
    assert shares._PLACEHOLDER in snap


def test_oversized_png_is_placeholdered_without_decoding(tmp_path: Path):
    huge = PNG + b"\x00" * (shares._SHARE_EMBED_MAX_BYTES + 1)
    snap = _snapshot_json(tmp_path, f"MEDIA:data:image/png;base64,{_b64(huge)}")
    assert "data:image/png;base64," not in snap
    assert shares._PLACEHOLDER in snap


def test_data_uri_never_reaches_filesystem(tmp_path: Path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    calls = []
    real = shares.Path.resolve

    def _spy(self, *a, **k):
        if "base64" in str(self):
            calls.append(str(self)[:40])
        return real(self, *a, **k)

    monkeypatch.setattr(shares.Path, "resolve", _spy)
    for uri in (
        f"data:image/png;base64,{_b64(PNG + b'0' * 9000)}",
        f"data:text/plain;base64,{_b64(b'x' * 9000)}",
        "DATA:image/png;base64,!!!" + "A" * 9000,
    ):
        _embed(f"MEDIA:{uri}", [ws])
    assert calls == []


def test_real_local_image_still_embeds(tmp_path: Path):
    """Regression guard: the fix must not break normal local-file embedding."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "ok.png").write_bytes(PNG)
    out = _embed("See MEDIA:ok.png here", [ws])
    assert '<img src="data:image/png;base64,' in out
    assert shares._PLACEHOLDER not in out


def test_http_url_still_passes_through(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    out = _embed("x MEDIA:https://example.com/a.png y", [ws])
    assert "MEDIA:https://example.com/a.png" in out


def test_resolver_rejects_overlong_token_without_raising(tmp_path: Path):
    """Belt-and-suspenders: _resolve_against_roots must fail closed, never
    raise, on an over-length / newline / NUL relative token."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    # "a"*300 + ".png" is under the 4096 guard but over NAME_MAX for one path
    # component, so it exercises the is_file() probe moved inside try/except.
    for raw in ("x" * 5000, "a" * 300 + ".png", "a\nb.png", "a\x00b.png"):
        out = _embed(f"MEDIA:{raw}", [ws])
        assert '<img src="data:' not in out
