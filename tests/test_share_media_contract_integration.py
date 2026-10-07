"""Keep #7949's inline-media validator ahead of #7868's private-ref filter."""

import base64
import copy

import pytest

from api import shares
from api.models import Session


@pytest.mark.parametrize("wrapper", ["{}", "`{}`", '"{}"', "**{}**"])
@pytest.mark.parametrize("kind", ["png", "text", "svg", "wrong-magic", "oversized"])
def test_snapshot_validates_inline_media_and_omits_private_neighbors(wrapper, kind):
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 9000
    mime, payload = {
        "png": ("image/png", png),
        "text": ("text/plain", b"PRIVATE_INLINE_BYTES"),
        "svg": ("image/svg+xml", b"<svg>PRIVATE_INLINE_BYTES</svg>"),
        "wrong-magic": ("image/png", b"PRIVATE_INLINE_BYTES"),
        "oversized": ("image/png", png + b"\0" * shares._SHARE_EMBED_MAX_BYTES),
    }[kind]
    encoded = base64.b64encode(payload).decode("ascii")
    token = wrapper.format(f"MEDIA:data:{mime};base64,{encoded}")
    source = (
        f"MEDIA:https://cdn.example/public.png {token} "
        "MEDIA:https://webui.example/api/media?path=private.png "
        "![private](https://webui.example/api/media?path=other.png) "
        "file:///private.png"
    )
    session = Session(session_id="share-contract-integration", messages=[
        {"role": "assistant", "content": source},
    ])
    original = copy.deepcopy(session.messages)
    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]
    assert "MEDIA:https://cdn.example/public.png" in content
    assert "/api/media" not in content
    assert "file://" not in content
    if kind == "png":
        assert f'<img src="data:image/png;base64,{encoded}"' in content
    else:
        assert encoded not in content
        assert "data:" not in content
    assert session.messages == original
    assert shares.build_share_snapshot(session) == snapshot
