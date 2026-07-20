"""Regression tests for file-backed large native image session media."""
import base64
import hashlib
import io
import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from api import models, routes, session_media, streaming
from api.models import Session
from api.streaming import _sanitize_messages_for_api


def _large_png_data_url(fill=b"\0"):
    # This is intentionally synthetic: the signature is sufficient for the
    # storage boundary, and keeps the regression test free of user media.
    raw = b"\x89PNG\r\n\x1a\n" + (fill * (70 * 1024))
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
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    models._SESSION_PUBLICATION_DELETED.clear()
    models._SESSION_PUBLICATION_LOCKS.clear()
    return session_dir


def _stub_delete_route_dependencies(monkeypatch, session, tmp_path):
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "prune_session_from_index", lambda _sid: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes.api_config, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    monkeypatch.setattr("api.upload._session_attachment_dir", lambda _sid: tmp_path / "uploads" / _sid)
    monkeypatch.setattr("api.turn_journal.delete_turn_journal", lambda _sid: None)
    monkeypatch.setattr("api.run_journal.delete_run_journal", lambda _sid: None)
    monkeypatch.setattr("api.background_process.forget_bg_task_completion_dedup", lambda _sid: None)
    monkeypatch.setattr("api.terminal.close_terminal", lambda _sid: None)


def _call_delete_route(session_id: str):
    body = json.dumps({"session_id": session_id}).encode("utf-8")
    handler = _FakeHandler("/api/session/delete")
    handler.rfile = io.BytesIO(body)
    handler.headers["Content-Length"] = str(len(body))
    routes.handle_post(handler, urlparse("/api/session/delete"))


def _assert_destination_media_is_independent(destination, source_id, raw, data_url):
    destination_id = destination.session_id
    destination_files = list(session_media._session_media_dir(destination_id).iterdir())
    assert len(destination_files) == 1
    assert destination_files[0].read_bytes() == raw
    session_media.remove_session_media(source_id)
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

    files = list((tmp_path / "session-media" / "media-test").iterdir())
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
    files = list((tmp_path / "session-media" / "media-save").iterdir())
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


def test_private_store_ignores_attachment_root_moves(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    custom_root = tmp_path / "custom-inbox"
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(custom_root))
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, "media-custom") == 1
    assert list((tmp_path / "session-media" / "media-custom").iterdir())
    assert not custom_root.exists()

    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path / "moved-inbox"))
    hydrated = session_media.hydrate_session_media_urls(messages, "media-custom")
    assert hydrated[0]["content"][1]["image_url"]["url"] == data_url


def test_stale_tmp_file_is_rewritten_before_returning_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    raw, data_url = _large_png_data_url()
    session_id = "media-stale-tmp"
    media_dir = session_media._session_media_dir(session_id)
    media_dir.mkdir(parents=True)
    digest = hashlib.sha256(raw).hexdigest()
    filename = f"{digest}.png"
    stale_tmp = media_dir / f".{filename}.stale.tmp"
    stale_tmp.write_bytes(b"interrupted prior write")
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, session_id) == 1
    assert (media_dir / filename).read_bytes() == raw
    # Random exclusive temp names ensure an unrelated stale file cannot block
    # or be mistaken for the in-flight write.
    assert stale_tmp.read_bytes() == b"interrupted prior write"


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


def test_hydration_and_provider_sanitizer_fail_closed_on_digest_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    session_media.externalize_large_session_media(messages, "media-corrupt")
    media_file = next(session_media._session_media_dir("media-corrupt").iterdir())
    media_file.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"x" * (70 * 1024)))

    with pytest.raises(session_media.SessionMediaIntegrityError, match="digest"):
        session_media.hydrate_session_media_urls(messages, "media-corrupt")
    with pytest.raises(session_media.SessionMediaIntegrityError, match="digest"):
        _sanitize_messages_for_api(messages, session_id="media-corrupt")


def test_gateway_corrupt_media_never_reaches_urlopen(tmp_path, monkeypatch):
    from api.gateway_chat import _run_gateway_runs_api_streaming

    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    history = [_image_message(data_url)]
    session_media.externalize_large_session_media(history, "gateway-corrupt")
    next(session_media._session_media_dir("gateway-corrupt").iterdir()).write_bytes(
        b"\x89PNG\r\n\x1a\ncorrupt"
    )
    called = False

    def unexpected_urlopen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("corrupt private media crossed the Gateway boundary")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    with pytest.raises(session_media.SessionMediaIntegrityError, match="digest"):
        _run_gateway_runs_api_streaming(
            session_id="gateway-corrupt",
            msg_text="continue",
            model="test-model",
            workspace=str(tmp_path),
            stream_id="gateway-corrupt-stream",
            base_url="http://gw:8642",
            api_key="secret",
            prefill_messages=[],
            body_extras={},
            put_gateway_event=lambda *_args, **_kwargs: None,
            cancel_event=threading.Event(),
            session=SimpleNamespace(context_messages=history),
        )
    assert not called


def test_clone_preflights_all_references_before_destination_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw_a, data_a = _large_png_data_url(b"a")
    _raw_b, data_b = _large_png_data_url(b"b")
    messages = [_image_message(data_a), _image_message(data_b)]
    session_media.externalize_large_session_media(messages, "multi-source")
    files = sorted(session_media._session_media_dir("multi-source").iterdir())
    files[-1].write_bytes(b"\x89PNG\r\n\x1a\ncorrupt")

    with pytest.raises(session_media.SessionMediaIntegrityError):
        session_media.clone_session_media_references(
            messages,
            "multi-source",
            "multi-destination",
        )
    assert not session_media._session_media_dir("multi-destination").exists()


def test_clone_rolls_back_all_published_blobs_on_directory_fsync_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw_a, data_a = _large_png_data_url(b"a")
    _raw_b, data_b = _large_png_data_url(b"b")
    messages = [_image_message(data_a), _image_message(data_b)]
    session_media.externalize_large_session_media(messages, "rollback-source")
    destination_dir = session_media._session_media_dir("rollback-destination")
    destination_dir.mkdir(parents=True)
    monkeypatch.setattr(
        session_media,
        "_fsync_dir",
        lambda _fd: (_ for _ in ()).throw(OSError("directory fsync failed")),
    )

    with pytest.raises(OSError, match="directory fsync failed"):
        session_media.clone_session_media_references(
            messages,
            "rollback-source",
            "rollback-destination",
        )
    assert list(destination_dir.iterdir()) == []


@pytest.mark.parametrize("failure", ["replace", "directory_fsync"])
def test_externalize_rolls_back_publication_failures(failure, tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    media_dir = session_media._session_media_dir("publication-failure")
    media_dir.mkdir(parents=True)

    if failure == "replace":
        monkeypatch.setattr(
            session_media.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        )
    else:
        monkeypatch.setattr(
            session_media,
            "_fsync_dir",
            lambda _fd: (_ for _ in ()).throw(OSError("directory fsync failed")),
        )

    with pytest.raises(OSError, match="failed"):
        session_media.externalize_large_session_media(messages, "publication-failure")
    assert messages[0]["content"][1]["image_url"]["url"] == data_url
    assert list(media_dir.iterdir()) == []


def test_private_store_rejects_symlink_component(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    private_root = tmp_path / "session-media"
    outside = tmp_path / "outside"
    private_root.mkdir()
    outside.mkdir()
    (private_root / "symlink-session").symlink_to(outside, target_is_directory=True)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    with pytest.raises(OSError):
        session_media.externalize_large_session_media(messages, "symlink-session")
    assert messages[0]["content"][1]["image_url"]["url"] == data_url
    assert list(outside.iterdir()) == []


def test_parent_swap_is_detected_without_writing_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    media_dir = session_media._session_media_dir("swap-session")
    media_dir.mkdir(parents=True)
    moved = media_dir.with_name("swap-session-held")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_replace = session_media.os.replace

    def swap_then_replace(source, destination, **kwargs):
        media_dir.rename(moved)
        media_dir.symlink_to(outside, target_is_directory=True)
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(session_media.os, "replace", swap_then_replace)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    with pytest.raises(session_media.SessionMediaIntegrityError, match="directory changed"):
        session_media.externalize_large_session_media(messages, "swap-session")
    assert messages[0]["content"][1]["image_url"]["url"] == data_url
    assert list(outside.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_concurrent_writers_publish_one_verified_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    raw, data_url = _large_png_data_url()
    values = [[_image_message(data_url)] for _ in range(8)]
    errors = []

    def write(value):
        try:
            session_media.externalize_large_session_media(value, "concurrent-session")
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(value,)) for value in values]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    refs = {value[0]["content"][1]["image_url"]["url"] for value in values}
    assert len(refs) == 1
    files = list(session_media._session_media_dir("concurrent-session").iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == raw


def test_remove_session_media_deletes_only_requested_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    first = [_image_message(data_url)]
    second = [_image_message(data_url)]
    session_media.externalize_large_session_media(first, "delete-first")
    session_media.externalize_large_session_media(second, "keep-second")

    session_media.remove_session_media("delete-first")

    assert not session_media._session_media_dir("delete-first").exists()
    assert session_media._session_media_dir("keep-second").exists()
    assert session_media.hydrate_session_media_urls(second, "keep-second")


def test_archive_named_session_media_cannot_precreate_private_namespace(tmp_path, monkeypatch):
    from api.upload import extract_archive

    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    attachment_session = tmp_path / "attachments" / "archive-session"
    attachment_session.mkdir(parents=True)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("attacker.txt", "not private media")
    extract_archive(archive.getvalue(), "session-media.zip", attachment_session)
    assert (attachment_session / "session-media" / "attacker.txt").exists()

    raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    session_media.externalize_large_session_media(messages, "archive-session")
    private_files = list(session_media._session_media_dir("archive-session").iterdir())
    assert len(private_files) == 1
    assert private_files[0].read_bytes() == raw


def test_legacy_attachment_media_is_verified_and_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    raw, _data_url = _large_png_data_url()
    filename = f"{hashlib.sha256(raw).hexdigest()}.png"
    legacy = tmp_path / "attachments" / "legacy-session" / "session-media"
    legacy.mkdir(parents=True)
    (legacy / filename).write_bytes(raw)
    messages = [_image_message(f"webui-media://{filename}")]

    hydrated = session_media.hydrate_session_media_urls(messages, "legacy-session")
    assert hydrated[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (tmp_path / "session-media" / "legacy-session" / filename).read_bytes() == raw


def test_btw_clones_media_before_new_session_is_published(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    raw, data_url = _large_png_data_url()
    source = Session(
        session_id="btw-source",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: SimpleNamespace())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args: None)
    monkeypatch.setattr("api.background.track_btw", lambda *_args: None)

    class NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(routes.threading, "Thread", NoopThread)
    captured = _capture_route(monkeypatch)

    routes._handle_btw(
        _FakeHandler("/api/btw"),
        {"session_id": source.session_id, "question": "What is in the image?"},
    )

    assert "bad" not in captured
    destination_id = captured["ok"]["session_id"]
    destination = Session.load(destination_id)
    session_media.remove_session_media(source.session_id)
    hydrated = session_media.hydrate_session_media_urls(
        destination.context_messages,
        destination_id,
    )
    assert hydrated[0]["content"][1]["image_url"]["url"] == data_url
    assert next(session_media._session_media_dir(destination_id).iterdir()).read_bytes() == raw


def test_export_import_inlines_verified_media_and_establishes_new_ownership(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    raw, data_url = _large_png_data_url()
    source = Session(
        session_id="export-source",
        workspace=str(tmp_path),
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(routes, "_profiles_match", lambda *_args: True)
    export_handler = _FakeHandler("/api/session/export")

    routes._handle_session_export(
        export_handler,
        urlparse(f"/api/session/export?session_id={source.session_id}"),
    )
    exported = json.loads(export_handler.wfile.getvalue())
    assert data_url in json.dumps(exported)
    assert "webui-media://" not in json.dumps(exported)

    session_media.remove_session_media(source.session_id)
    captured = _capture_route(monkeypatch)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda path: path)
    routes._handle_session_import(_FakeHandler("/api/session/import"), exported)
    assert "bad" not in captured
    imported_id = captured["ok"]["session"]["session_id"]
    imported = Session.load(imported_id)
    hydrated = session_media.hydrate_session_media_urls(imported.context_messages, imported_id)
    assert hydrated[0]["content"][1]["image_url"]["url"] == data_url
    assert next(session_media._session_media_dir(imported_id).iterdir()).read_bytes() == raw


def test_import_rejects_private_references_without_publishing_session(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    session_media.externalize_large_session_media(messages, "foreign-source")
    before = set(models.SESSIONS)
    captured = _capture_route(monkeypatch)

    routes._handle_session_import(
        _FakeHandler("/api/session/import"),
        {"messages": messages, "workspace": str(tmp_path)},
    )
    assert captured["bad"][1] == 400
    assert set(models.SESSIONS) == before


def test_manual_compression_stops_before_provider_on_corrupt_media(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)] + [
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "three"},
    ]
    session = Session(session_id="compress-corrupt", messages=messages)
    session.save(skip_index=True)
    next(session_media._session_media_dir(session.session_id).iterdir()).write_bytes(
        b"\x89PNG\r\n\x1a\ncorrupt"
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    captured = _capture_route(monkeypatch)

    routes._handle_session_compress(
        _FakeHandler("/api/session/compress"),
        {"session_id": session.session_id},
    )
    assert captured["bad"][1] == 400
    assert "digest verification" in captured["bad"][0]


@pytest.mark.parametrize("damage", ["missing", "digest-mismatch"])
def test_save_refuses_broken_retained_reference_without_replacing_json(
    damage,
    tmp_path,
    monkeypatch,
):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id=f"save-refusal-{damage}",
        title="before",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    prior_json = session.path.read_bytes()
    media_file = next(session_media._session_media_dir(session.session_id).iterdir())
    if damage == "missing":
        media_file.unlink()
    else:
        media_file.write_bytes(b"\x89PNG\r\n\x1a\nwrong-content")
    session.title = "must-not-publish"

    with pytest.raises(session_media.SessionMediaIntegrityError):
        session.save(skip_index=True)

    assert session.path.read_bytes() == prior_json


def test_clone_rolls_back_when_post_yield_directory_identity_check_fails(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    session_media.externalize_large_session_media(messages, "identity-source")
    original_assert = session_media._assert_private_handles
    destination_checks = 0

    def fail_only_post_yield(state_fd, media_fd, session_fd, sid):
        nonlocal destination_checks
        if sid == "identity-destination":
            destination_checks += 1
            if destination_checks == 2:
                raise session_media.SessionMediaIntegrityError("simulated parent swap")
        return original_assert(state_fd, media_fd, session_fd, sid)

    monkeypatch.setattr(session_media, "_assert_private_handles", fail_only_post_yield)

    with pytest.raises(session_media.SessionMediaIntegrityError, match="parent swap"):
        session_media.clone_session_media_references(
            messages,
            "identity-source",
            "identity-destination",
        )

    destination = session_media._session_media_dir("identity-destination")
    assert not destination.exists() or not list(destination.iterdir())


def test_btw_thread_start_failure_rolls_back_all_ephemeral_ownership(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    source = Session(
        session_id="btw-failure-source",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source
    destination_id = "btw-failure-destination"
    monkeypatch.setattr(routes, "Session", lambda **kwargs: Session(session_id=destination_id, **kwargs))
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: SimpleNamespace())
    monkeypatch.setattr("api.background.track_btw", lambda *_args: None)

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(routes.threading, "Thread", FailingThread)
    captured = _capture_route(monkeypatch)

    routes._handle_btw(
        _FakeHandler("/api/btw"),
        {"session_id": source.session_id, "question": "What is in the image?"},
    )

    assert captured["bad"] == ("Could not start side-question session", 500)
    assert destination_id not in models.SESSIONS
    assert not (models.SESSION_DIR / f"{destination_id}.json").exists()
    assert not session_media._session_media_dir(destination_id).exists()
    assert destination_id not in routes.api_config.STREAM_SESSION_OWNERS.values()


def test_path_fallback_externalizes_hydrates_clones_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    monkeypatch.setattr(session_media, "_DIR_FD_OK", False, raising=False)

    def anchored_backend_must_not_run(*_args, **_kwargs):
        raise AssertionError("dir-fd backend used when capability is unavailable")

    monkeypatch.setattr(session_media, "_open_private_session", anchored_backend_must_not_run)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, "fallback-source") == 1
    assert session_media.hydrate_session_media_urls(messages, "fallback-source")[0][
        "content"
    ][1]["image_url"]["url"] == data_url
    assert session_media.clone_session_media_references(
        messages,
        "fallback-source",
        "fallback-destination",
    ) == 1
    session_media.remove_session_media("fallback-source")
    assert session_media.hydrate_session_media_urls(messages, "fallback-destination")[0][
        "content"
    ][1]["image_url"]["url"] == data_url
    session_media.remove_session_media("fallback-destination")
    assert not session_media._session_media_dir("fallback-destination").exists()


def test_ephemeral_cleanup_retires_json_cache_stream_tracking_and_media(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="btw-cleanup",
        parent_session_id="btw-parent",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.active_stream_id = "btw-cleanup-stream"
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    routes.STREAMS[session.active_stream_id] = SimpleNamespace()
    routes.register_stream_owner(session.active_stream_id, session.session_id)
    from api.background import cleanup_btw, track_btw

    track_btw(
        session.parent_session_id,
        session.session_id,
        session.active_stream_id,
        "question",
    )

    streaming._cleanup_ephemeral_session(session)

    assert session.session_id not in models.SESSIONS
    assert "btw-cleanup-stream" not in routes.STREAMS
    assert routes.stream_owner_session_id("btw-cleanup-stream") is None
    assert not session.path.exists()
    assert not session_media._session_media_dir(session.session_id).exists()
    assert cleanup_btw(session.parent_session_id) is None


def test_delete_serializes_against_late_save_and_tombstones_publication(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="delete-late-save",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    _stub_delete_route_dependencies(monkeypatch, session, tmp_path)
    captured = _capture_route(monkeypatch)
    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()
    original_remove = session_media.remove_session_media

    def blocking_remove(session_id):
        entered_cleanup.set()
        assert release_cleanup.wait(timeout=5)
        return original_remove(session_id)

    monkeypatch.setattr(session_media, "remove_session_media", blocking_remove)
    delete_thread = threading.Thread(target=_call_delete_route, args=(session.session_id,))
    delete_thread.start()
    assert entered_cleanup.wait(timeout=5)
    save_finished = threading.Event()
    save_error = []

    def late_save():
        try:
            session.save(skip_index=True)
        except Exception as exc:
            save_error.append(exc)
        finally:
            save_finished.set()

    save_thread = threading.Thread(target=late_save)
    save_thread.start()
    assert not save_finished.wait(timeout=0.1)
    release_cleanup.set()
    delete_thread.join(timeout=5)
    save_thread.join(timeout=5)

    assert captured["ok"]["ok"] is True
    assert save_error and "deleted session" in str(save_error[0])
    assert not session.path.exists()
    assert not session_media._session_media_dir(session.session_id).exists()


def test_delete_reports_failure_when_private_media_cleanup_fails(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="delete-media-failure",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    _stub_delete_route_dependencies(monkeypatch, session, tmp_path)
    captured = _capture_route(monkeypatch)
    monkeypatch.setattr(
        session_media,
        "remove_session_media",
        lambda _sid: (_ for _ in ()).throw(OSError("disk failure")),
    )

    _call_delete_route(session.session_id)

    assert captured["bad"][1] == 500
    assert "private media cleanup failed" in captured["bad"][0]
    assert session.session_id not in models.SESSIONS
    with pytest.raises(RuntimeError, match="deleted session"):
        session.save(skip_index=True)
