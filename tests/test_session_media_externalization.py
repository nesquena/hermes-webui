"""Regression tests for file-backed large native image session media."""
import base64
import json

from api import models, session_media
from api.models import Session
from api.streaming import _sanitize_messages_for_api


def _large_png_data_url():
    # This is intentionally synthetic: the signature is sufficient for the
    # storage boundary, and keeps the regression test free of user media.
    raw = b"\x89PNG\r\n\x1a\n" + (b"\0" * (70 * 1024))
    return raw, "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _image_message(url):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def test_externalize_and_hydrate_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, "media-test") == 1
    ref = messages[0]["content"][1]["image_url"]["url"]
    assert ref.startswith("webui-media://")
    assert data_url not in json.dumps(messages)

    files = list((tmp_path / "attachments" / "media-test" / "session-media").iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == raw
    hydrated = session_media.hydrate_session_media_urls(messages, "media-test")
    assert hydrated[0]["content"][1]["image_url"]["url"] == data_url
    # The persisted representation remains compact after model-call hydration.
    assert messages[0]["content"][1]["image_url"]["url"] == ref


def test_save_compacts_both_visible_and_model_context(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "sessions")
    models.SESSION_DIR.mkdir()
    raw, data_url = _large_png_data_url()
    session = Session(
        session_id="media-save",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )

    session.save(skip_index=True)

    serialized = session.path.read_text(encoding="utf-8")
    assert data_url not in serialized
    assert serialized.count("webui-media://") == 2
    # Deduplication keeps the one image once even when visible/context copies
    # both contained it before save.
    files = list((tmp_path / "attachments" / "media-save" / "session-media").iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == raw

    provider_history = _sanitize_messages_for_api(
        session.context_messages,
        cfg={"agent": {"image_input_mode": "native"}},
        session_id=session.session_id,
    )
    assert provider_history[0]["content"][1]["image_url"]["url"] == data_url


def test_small_or_noncanonical_data_urls_stay_in_json(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    small = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsmall").decode("ascii")
    messages = [
        _image_message(small),
        {"role": "assistant", "content": "literal data:image/png;base64,not-a-content-part"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/svg+xml;base64,PHN2Zy8+"}}]},
    ]

    assert session_media.externalize_large_session_media(messages, "media-small") == 0
    assert messages[0]["content"][1]["image_url"]["url"] == small
    assert "literal data:image" in messages[1]["content"]
    assert messages[2]["content"][0]["image_url"]["url"].startswith("data:image/svg+xml")


def test_uses_the_configured_attachment_root(tmp_path, monkeypatch):
    custom_root = tmp_path / "custom-inbox"
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(custom_root))
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, "media-custom") == 1
    assert list((custom_root / "media-custom" / "session-media").iterdir())
