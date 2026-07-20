"""Regression tests for file-backed large native image session media."""
import base64
import hashlib
import io
import json
import os
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from api import models, routes, session_media
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


@pytest.fixture(autouse=True)
def _isolate_attachment_root(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)


class _FakeHandler:
    def __init__(self, path):
        self.status = None
        self.headers = {"Content-Type": "application/json", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _capture_route(monkeypatch):
    captured = {}

    def fake_json(_handler, payload, *_, **kwargs):
        captured["ok"] = payload
        captured["status"] = kwargs.get("status", 200)
        return True

    def fake_bad(_handler, message, code=400, **kwargs):
        captured["bad"] = (message, kwargs.get("status", code))
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    monkeypatch.setattr(routes, "bad", fake_bad)
    return captured


def _configure_session_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    return session_dir


def _assert_destination_media_is_independent(destination, source_id, raw, data_url):
    destination_id = destination.session_id
    destination_files = list(session_media._session_media_dir(destination_id).iterdir())
    assert len(destination_files) == 1
    assert destination_files[0].read_bytes() == raw
    shutil.rmtree(session_media._session_media_dir(source_id).parent)
    hydrated_messages = session_media.hydrate_session_media_urls(destination.messages, destination_id)
    hydrated_context = session_media.hydrate_session_media_urls(destination.context_messages, destination_id)
    assert hydrated_messages[0]["content"][1]["image_url"]["url"] == data_url
    assert hydrated_context[0]["content"][1]["image_url"]["url"] == data_url


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

    def _unexpected_decode(*_args, **_kwargs):
        raise AssertionError("small data URL should not be decoded")

    monkeypatch.setattr(session_media.base64, "b64decode", _unexpected_decode)
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


def test_stale_tmp_file_is_rewritten_before_returning_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    raw, data_url = _large_png_data_url()
    session_id = "media-stale-tmp"
    media_dir = session_media._session_media_dir(session_id)
    media_dir.mkdir(parents=True)
    digest = hashlib.sha256(raw).hexdigest()
    filename = f"{digest}.png"
    stale_tmp = media_dir / f".{filename}.{os.getpid()}.{threading.get_ident()}.tmp"
    stale_tmp.write_bytes(b"interrupted prior write")
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, session_id) == 1
    assert (media_dir / filename).read_bytes() == raw
    assert not stale_tmp.exists()


def test_clone_references_verifies_hash_before_writing_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    session_media.externalize_large_session_media(messages, "source-hash")
    source_file = next(session_media._session_media_dir("source-hash").iterdir())
    source_file.write_bytes(b"\x89PNG\r\n\x1a\ncorrupt")

    with pytest.raises(ValueError, match="digest"):
        session_media.clone_session_media_references(messages, "source-hash", "dest-hash")

    assert not session_media._session_media_dir("dest-hash").exists()


@pytest.mark.parametrize("path", ["/api/session/duplicate", "/api/session/branch"])
def test_id_copy_failure_does_not_commit_dangling_session(path, tmp_path, monkeypatch):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    source = Session(
        session_id="media-failed-source",
        title="Media source",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source
    next(session_media._session_media_dir(source.session_id).iterdir()).write_bytes(
        b"\x89PNG\r\n\x1a\ncorrupt"
    )
    destination_id = "mediafail001"
    monkeypatch.setattr(models.uuid, "uuid4", lambda: SimpleNamespace(hex=destination_id))
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: source)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_, **__: None)
    captured = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler(path), urlparse(path))

    assert captured["bad"] == ("Could not copy session media", 500)
    assert not (session_dir / f"{destination_id}.json").exists()
    assert destination_id not in models.SESSIONS


def test_duplicate_clones_externalized_media_before_session_commit(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    raw, data_url = _large_png_data_url()
    source = Session(
        session_id="media-duplicate-source",
        title="Media source",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: source)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_, **__: None)
    captured = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler("/api/session/duplicate"), urlparse("/api/session/duplicate"))

    assert "bad" not in captured
    destination_id = captured["ok"]["session"]["session_id"]
    destination = Session.load(destination_id)
    _assert_destination_media_is_independent(destination, source.session_id, raw, data_url)


def test_branch_clones_externalized_media_before_session_commit(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    raw, data_url = _large_png_data_url()
    source = Session(
        session_id="media-branch-source",
        title="Media source",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: source)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_, **__: None)
    captured = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler("/api/session/branch"), urlparse("/api/session/branch"))

    assert "bad" not in captured
    destination_id = captured["ok"]["session_id"]
    destination = Session.load(destination_id)
    _assert_destination_media_is_independent(destination, source.session_id, raw, data_url)


def test_gateway_runs_api_hydrates_compact_history_without_mutation(tmp_path, monkeypatch):
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    stored_history = [_image_message(data_url)]
    session_media.externalize_large_session_media(stored_history, "gateway-media")
    stored_ref = stored_history[0]["content"][1]["image_url"]["url"]
    requests = []
    stream_id = "stream-gateway-media"
    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""

    class _JsonResponse:
        def read(self, _limit=None):
            return json.dumps({"run_id": "run-media"}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __iter__(self):
            return iter([b'data: {"event":"run.completed","output":"done"}\n', b"\n"])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        requests.append(req)
        return _JsonResponse() if req.full_url.endswith("/v1/runs") else _SseResponse()

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run_gateway_runs_api_streaming(
                session_id="gateway-media",
                msg_text="continue",
                model="test-model",
                workspace=str(tmp_path),
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda *_args, **_kwargs: None,
                cancel_event=threading.Event(),
                session=SimpleNamespace(context_messages=stored_history),
            )
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)

    run_body = json.loads(requests[0].data.decode("utf-8"))
    outbound = json.dumps(run_body["conversation_history"])
    assert data_url in outbound
    assert "webui-media://" not in outbound
    assert stored_history[0]["content"][1]["image_url"]["url"] == stored_ref
