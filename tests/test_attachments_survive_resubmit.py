"""Attachments captured on a persisted user row must survive an edit/resubmit.

A user uploads images, submits, cancels or edits the text, then sends again --
possibly after switching model provider. The row on disk records the uploads as
FILENAME STRINGS in ``attachments``. Re-sending rebuilds the provider parts from
those entries via ``_build_native_multimodal_message``.

Measured pre-fix: a string entry was skipped by the ``isinstance(att, dict)``
guard, so the rebuilt turn carried no image part at all and the user's picture
silently vanished from the request.
"""
from __future__ import annotations

import base64


import pytest

from api.streaming import _build_native_multimodal_message

# A 1x1 PNG -- a real, decodable image so validation passes for the right reason.
_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def attachment(tmp_path):
    """A stored upload plus the filename string a persisted row actually holds."""
    from api.upload import _attachment_root

    root = _attachment_root()
    directory = root / "resubmit-session"
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "photo.png"
    image.write_bytes(_PNG)
    return image, image.name


def _image_parts(out):
    if not isinstance(out, list):
        return []
    return [p for p in out if isinstance(p, dict) and p.get("type") == "image_url"]


def test_filename_string_attachment_rebuilds_image_part(attachment):
    """The stored shape (a bare filename) must rebuild the image part.

    This is the exact shape `messages[].attachments` holds on disk, and the exact
    input the resubmit path passes back in.
    """
    image, name = attachment
    out = _build_native_multimodal_message("", "look at this", [name], str(image.parent.parent))
    assert _image_parts(out), "filename-string attachment was dropped on resubmit"


def test_absolute_path_string_attachment_rebuilds_image_part(attachment):
    """An absolute path string must rebuild the image part too."""
    image, _ = attachment
    out = _build_native_multimodal_message("", "look", [str(image)], str(image.parent.parent))
    assert _image_parts(out)


def test_dict_attachment_still_works(attachment):
    """The dict shape already worked and must not regress."""
    image, _ = attachment
    out = _build_native_multimodal_message(
        "", "look", [{"path": str(image), "mime": "image/png"}], str(image.parent.parent)
    )
    assert _image_parts(out)


def test_unknown_filename_is_ignored_not_crashing(attachment):
    """A string that resolves to no real file is skipped, not raised."""
    from api.upload import _attachment_root

    out = _build_native_multimodal_message(
        "", "look", ["definitely-not-a-real-upload.png"], str(_attachment_root().parent)
    )
    assert not _image_parts(out)


def test_path_outside_allowed_roots_is_still_rejected(tmp_path):
    """Path containment must not be weakened: an arbitrary path stays refused."""
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG)
    out = _build_native_multimodal_message("", "look", [str(outside)], str(tmp_path / "ws"))
    assert not _image_parts(out), "arbitrary client-supplied path was read"


def test_text_mode_still_returns_plain_string(attachment):
    """image_input_mode=text keeps the non-embedding contract for string entries."""
    image, name = attachment
    out = _build_native_multimodal_message(
        "", "look", [name], str(image.parent.parent),
        cfg={"agent": {"image_input_mode": "text"}},
    )
    assert isinstance(out, str)
    assert "look" in out


def test_name_only_dict_attachment_rebuilds_image_part(attachment):
    """`_normalize_chat_attachments` turns an older client's filename into
    {"name": <filename>, "path": ""}. That empty path is the shape the resend
    path actually receives, and it must resolve too.
    """
    image, name = attachment
    out = _build_native_multimodal_message(
        "", "look", [{"name": name, "path": "", "mime": ""}], str(image.parent.parent)
    )
    assert _image_parts(out), "name-only attachment was dropped on resubmit"


def test_resend_never_picks_another_sessions_picture(attachment):
    """A filename-only attachment must resolve inside ITS OWN session directory.

    Two sessions can upload the same name (`photo.png`). Searching every session
    directory and taking the first match could hand the model another chat's
    picture even when the correct file still exists -- a cross-session leak.
    """
    from api.upload import _attachment_root

    inbox = _attachment_root()
    mine = inbox / "session-mine"
    theirs = inbox / "session-theirs"
    for directory in (mine, theirs):
        directory.mkdir(parents=True, exist_ok=True)

    # Their copy sorts FIRST, so any "first match anywhere" would win.
    theirs_image = theirs / "photo.png"
    theirs_image.write_bytes(_PNG)
    mine_image = mine / "photo.png"
    mine_image.write_bytes(_PNG + b"\x00mine")

    out = _build_native_multimodal_message(
        "", "look", ["photo.png"], str(inbox), session_id="session-mine"
    )
    parts = _image_parts(out)
    assert parts, "own-session attachment was not resolved"
    import base64 as _b64

    url = parts[0]["image_url"]["url"]
    decoded = _b64.b64decode(url.split("base64,", 1)[1])
    assert decoded.endswith(b"\x00mine"), "resolved ANOTHER session's picture"


def test_resend_without_session_id_still_resolves(attachment):
    """Omitting session_id keeps the previous behaviour (inbox-wide search)."""
    image, name = attachment
    out = _build_native_multimodal_message("", "look", [name], str(image.parent.parent))
    assert _image_parts(out)
