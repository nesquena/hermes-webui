"""Regression tests for file-backed large native image session media."""
import base64
import hashlib
import io
import json
import multiprocessing
import os
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


def _cross_process_index_writer(session_dir, index_file, sid, ready, start):
    from pathlib import Path

    from api import models as child_models

    child_models.SESSION_DIR = Path(session_dir)
    child_models.SESSION_INDEX_FILE = Path(index_file)
    child_models.SESSIONS.clear()
    session = child_models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": sid}],
    )
    session.save(skip_index=True)
    ready.put(sid)
    start.wait(60)
    child_models._write_session_index(updates=[session])


def _cross_process_recreate_session(session_dir, index_file, state_dir, sid, result_queue):
    from pathlib import Path

    from api import models as child_models
    from api import session_media as child_media
    import api.upload as child_upload

    child_models.SESSION_DIR = Path(session_dir)
    child_models.SESSION_INDEX_FILE = Path(index_file)
    child_media.STATE_DIR = Path(state_dir)
    child_upload._session_attachment_dir = (
        lambda owner_sid: Path(state_dir) / "attachments" / owner_sid
    )
    child_models.SESSIONS.clear()
    current = child_models.Session.load(sid)
    cleanup = child_models.delete_session_artifacts(
        sid,
        expected_generation=current._publication_generation,
        delete_state_db=False,
    )
    if not cleanup["ok"]:
        result_queue.put({"error": cleanup["residuals"]})
        return
    replacement = child_models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "replacement"}],
    )
    with child_models.reserve_session_destination(sid) as reservation:
        reservation.bind(replacement)
        replacement.save(skip_index=True)
        reservation.commit()
    result_queue.put(
        {
            "token": replacement._publication_generation.token,
            "content": replacement.path.read_text(encoding="utf-8"),
        }
    )


def _cross_process_tombstone_writer(session_dir, sid, ready, start):
    from pathlib import Path

    from api import models as child_models

    child_models.SESSION_DIR = Path(session_dir)
    ready.put(sid)
    start.wait(60)
    child_models._record_webui_deleted_session_tombstone(sid)


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
    if hasattr(models, "_SESSION_PUBLICATION_GENERATIONS"):
        models._SESSION_PUBLICATION_GENERATIONS.clear()
    models._active_destination_reservations().clear()
    with routes.api_config.SESSION_AGENT_CACHE_LOCK:
        routes.api_config.SESSION_AGENT_CACHE.clear()
    with routes.api_config.SESSION_AGENT_LOCKS_LOCK:
        routes.api_config.SESSION_AGENT_LOCKS.clear()
    with routes.api_config.ACTIVE_RUNS_LOCK:
        routes.api_config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    with routes.api_config.STREAM_SESSION_OWNERS_LOCK:
        routes.api_config.STREAM_SESSION_OWNERS.clear()
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
    monkeypatch.setattr("api.turn_journal.delete_turn_journal", lambda _sid, **_kwargs: None)
    monkeypatch.setattr("api.run_journal.delete_run_journal", lambda _sid, **_kwargs: None)
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
    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
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


@pytest.mark.parametrize(
    "path,destination_id",
    [
        ("/api/session/duplicate", "duprollback1"),
        ("/api/session/branch", "branchrollbk"),
    ],
)
@pytest.mark.parametrize("failure", ["save", "index"])
def test_new_id_routes_roll_back_after_media_clone_failure(
    path,
    destination_id,
    failure,
    tmp_path,
    monkeypatch,
):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    source = Session(
        session_id=f"source-{destination_id}",
        title="Media source",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    source.save(skip_index=True)
    models.SESSIONS[source.session_id] = source
    monkeypatch.setattr(models.uuid, "uuid4", lambda: SimpleNamespace(hex=destination_id))
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": source.session_id})
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: source)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_, **__: None)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    monkeypatch.setattr("api.upload._session_attachment_dir", lambda sid: tmp_path / "uploads" / sid)
    monkeypatch.setattr("api.turn_journal.delete_turn_journal", lambda _sid, **_kwargs: 0)
    monkeypatch.setattr("api.run_journal.delete_run_journal", lambda _sid, **_kwargs: False)
    original_save = Session.save

    if failure == "save":
        def fail_destination_save(self, *args, **kwargs):
            if self.session_id == destination_id:
                raise OSError("injected destination save failure")
            return original_save(self, *args, **kwargs)

        monkeypatch.setattr(Session, "save", fail_destination_save)
    else:
        monkeypatch.setattr(
            models,
            "_write_session_index",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected index failure")),
        )

    captured = _capture_route(monkeypatch)
    routes.handle_post(_FakeHandler(path), urlparse(path))

    assert captured["bad"][1] == 500
    assert destination_id not in models.SESSIONS
    assert not (session_dir / f"{destination_id}.json").exists()
    assert not (session_dir / f"{destination_id}.json.bak").exists()
    destination_media = session_media._session_media_dir(destination_id)
    assert not destination_media.exists() or not list(destination_media.iterdir())


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

    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
        session_media.remove_session_media("delete-first")

    assert not session_media._session_media_dir("delete-first").exists()
    quarantines = list((tmp_path / "session-media").glob(".delete-*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_dir()
    assert session_media._session_media_dir("keep-second").exists()
    assert session_media.hydrate_session_media_urls(second, "keep-second")

    # A retry retains the original quarantine as the one durable authority;
    # it must not recursively quarantine the already-detached contents.
    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
        session_media.remove_session_media("delete-first")
    assert list((tmp_path / "session-media").glob(".delete-*")) == quarantines


def test_media_cleanup_retry_prefix_cannot_match_sibling_sid(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    media_root = tmp_path / "session-media"
    media_root.mkdir()
    sibling_quarantine = media_root / (
        session_media._deletion_quarantine_prefix("a-b") + "stale"
    )
    sibling_quarantine.mkdir()

    session_media.remove_session_media("a")

    assert sibling_quarantine.is_dir()


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
    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
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

    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
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
    _stub_delete_route_dependencies(monkeypatch, source, tmp_path)
    destination_id = "btw-failure-destination"
    stream_id = "btw-failure-stream"
    monkeypatch.setattr(routes, "Session", lambda **kwargs: Session(session_id=destination_id, **kwargs))
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: SimpleNamespace(hex=stream_id))
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: SimpleNamespace())

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
    from api.background import find_btw_owner

    owner = find_btw_owner(destination_id, stream_id)
    assert owner is not None
    assert owner["cleanup_pending"] is True
    assert owner["cleanup_residuals"] == [
        {"artifact": "session_media", "error": "SessionMediaIntegrityError"}
    ]


def test_missing_anchored_capabilities_fail_closed_without_persisting_private_refs(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    monkeypatch.setattr(session_media, "_DIR_FD_OK", False, raising=False)

    def anchored_backend_must_not_run(*_args, **_kwargs):
        raise AssertionError("dir-fd backend used when capability is unavailable")

    monkeypatch.setattr(session_media, "_open_private_session", anchored_backend_must_not_run)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]

    assert session_media.externalize_large_session_media(messages, "unsupported-source") == 0
    assert messages[0]["content"][1]["image_url"]["url"] == data_url
    assert not (tmp_path / "session-media").exists()

    compact = [_image_message("webui-media://" + "a" * 64 + ".png")]
    with pytest.raises(session_media.SessionMediaIntegrityError, match="unsupported"):
        session_media.hydrate_session_media_urls(compact, "unsupported-source")
    with pytest.raises(session_media.SessionMediaIntegrityError, match="unsupported"):
        session_media.clone_session_media_references(
            compact,
            "unsupported-source",
            "unsupported-destination",
        )
    (tmp_path / "session-media" / "unsupported-source").mkdir(parents=True)
    with pytest.raises(session_media.SessionMediaIntegrityError, match="unsupported"):
        session_media.remove_session_media("unsupported-source")


def test_ephemeral_cleanup_retires_json_cache_stream_tracking_and_media(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
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
    from api.background import find_btw_owner, track_btw

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
    owner = find_btw_owner(session.session_id, "btw-cleanup-stream")
    assert owner is not None
    assert owner["cleanup_pending"] is True
    assert owner["cleanup_residuals"] == [
        {"artifact": "session_media", "error": "SessionMediaIntegrityError"}
    ]


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

    assert captured["status"] == 500
    assert captured["ok"]["error"] == "Session cleanup incomplete"
    assert {item["artifact"] for item in captured["ok"]["residuals"]} == {
        "session_media"
    }
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

    assert captured["status"] == 500
    assert captured["ok"]["error"] == "Session cleanup incomplete"
    assert {item["artifact"] for item in captured["ok"]["residuals"]} >= {
        "session_media"
    }
    assert session.session_id not in models.SESSIONS
    with pytest.raises(RuntimeError, match="deleted session"):
        session.save(skip_index=True)


@pytest.mark.parametrize(
    "artifact",
    [
        "deleted_session_tombstone",
        "session_json",
        "session_backup",
        "session_temporary_files",
        "attachments",
        "turn_journal",
        "run_journal",
        "session_index",
        "state_db",
    ],
)
def test_delete_reports_and_persists_every_private_artifact_residual(
    artifact,
    tmp_path,
    monkeypatch,
):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    session = Session(
        session_id=f"residual-{artifact.replace('_', '-')}",
        messages=[{"role": "user", "content": "private transcript"}],
    )
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    _stub_delete_route_dependencies(monkeypatch, session, tmp_path)
    captured = _capture_route(monkeypatch)

    if artifact == "deleted_session_tombstone":
        monkeypatch.setattr(models, "_record_webui_deleted_session_tombstone", lambda _sid: None)
    elif artifact in {"session_json", "session_backup", "session_temporary_files"}:
        if artifact == "session_temporary_files":
            target = session_dir / f"{session.session_id}.tmp.crashed"
            target.write_text('{"private":"temporary"}', encoding="utf-8")
        else:
            target = session.path if artifact == "session_json" else session.path.with_suffix(".json.bak")
        if artifact == "session_backup":
            target.write_text('{"private":"backup"}', encoding="utf-8")
        original_unlink = type(target).unlink

        def fail_target_unlink(self, *args, **kwargs):
            if self == target:
                raise OSError("injected unlink failure")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(type(target), "unlink", fail_target_unlink)
    elif artifact == "attachments":
        attachment_dir = tmp_path / "uploads" / session.session_id
        attachment_dir.mkdir(parents=True)
        (attachment_dir / "private.txt").write_text("private", encoding="utf-8")
        monkeypatch.setattr(
            "shutil.rmtree",
            lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("injected rmtree failure"))
            if path == attachment_dir
            else None,
        )
    elif artifact == "turn_journal":
        turn_root = session_dir / "_turn_journal"
        turn_root.mkdir()
        (turn_root / f"{session.session_id}.jsonl").write_text("private\n", encoding="utf-8")
    elif artifact == "run_journal":
        run_root = session_dir / "_run_journal" / session.session_id
        run_root.mkdir(parents=True)
        (run_root / "run.jsonl").write_text("private\n", encoding="utf-8")
    elif artifact == "session_index":
        models.SESSION_INDEX_FILE.write_text(
            json.dumps([{"session_id": session.session_id}]),
            encoding="utf-8",
        )
        monkeypatch.setattr(models, "prune_session_from_index", lambda _sid: None)
    elif artifact == "state_db":
        monkeypatch.setattr(models, "delete_cli_session", lambda _sid: False)

    _call_delete_route(session.session_id)

    assert captured["status"] == 500
    residual_names = {item["artifact"] for item in captured["ok"]["residuals"]}
    assert artifact in residual_names
    persisted = models._load_session_cleanup_residuals()
    assert artifact in {
        item["artifact"] for item in persisted[session.session_id]["residuals"]
    }
    replacement = Session(session_id=session.session_id)
    with pytest.raises(RuntimeError, match="cleanup residuals remain"):
        models._activate_session_publication_generation(replacement)


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        b"{not-json",
        json.dumps(
            {
                "version": models.SESSION_CLEANUP_RESIDUAL_VERSION + 1,
                "session_id": "blocked-residual",
                "residuals": [{"artifact": "session_json"}],
                "delete_state_db": True,
                "durable_tombstone": True,
            }
        ).encode("utf-8"),
    ],
)
def test_corrupt_cleanup_record_blocks_only_its_own_session(
    corrupt_payload,
    tmp_path,
    monkeypatch,
):
    _configure_session_state(tmp_path, monkeypatch)
    blocked_sid = "blocked-residual"
    models._persist_session_cleanup_residuals(
        blocked_sid,
        [{"artifact": "session_json"}],
        durable_tombstone=True,
        delete_state_db=True,
    )
    models._session_cleanup_residual_file(blocked_sid).write_bytes(corrupt_payload)

    unrelated = Session(session_id="unrelated-new-session")
    models._activate_session_publication_generation(unrelated)

    with pytest.raises(RuntimeError, match="cleanup residuals remain"):
        models._activate_session_publication_generation(Session(session_id=blocked_sid))
    with pytest.raises((json.JSONDecodeError, ValueError)):
        models._load_session_cleanup_residuals()


def test_cleanup_retries_valid_records_while_reporting_corrupt_peer(
    tmp_path,
    monkeypatch,
):
    _configure_session_state(tmp_path, monkeypatch)
    valid_sid = "valid-retry-record"
    corrupt_sid = "corrupt-retry-record"
    _stub_delete_route_dependencies(
        monkeypatch,
        Session(session_id=valid_sid),
        tmp_path,
    )
    for sid in (valid_sid, corrupt_sid):
        models._persist_session_cleanup_residuals(
            sid,
            [{"artifact": "session_json"}],
            durable_tombstone=True,
            delete_state_db=True,
        )
    corrupt_path = models._session_cleanup_residual_file(corrupt_sid)
    corrupt_path.write_bytes(b"{partial")
    captured = _capture_route(monkeypatch)

    routes._handle_sessions_cleanup(_FakeHandler("/api/sessions/cleanup"), {})

    assert captured["status"] == 500
    assert captured["ok"]["cleaned"] == 1
    assert not models._session_cleanup_residual_file(valid_sid).exists()
    assert corrupt_path.read_bytes() == b"{partial"
    assert {
        item.get("session_id") for item in captured["ok"]["residuals"]
    } == {corrupt_sid}
    assert captured["ok"]["residuals"][0]["artifacts"][0]["artifact"] == (
        "cleanup_residual_record"
    )
    valid, invalid = models._scan_session_cleanup_residuals()
    assert valid == {}
    assert [item.get("session_id") for item in invalid] == [corrupt_sid]


def test_cleanup_route_uses_generation_aware_deletion(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    stale = Session(session_id="cleanup-stale-save", title="Untitled")
    stale.save(skip_index=True)
    models.SESSIONS[stale.session_id] = stale
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    monkeypatch.setattr("api.upload._session_attachment_dir", lambda sid: tmp_path / "uploads" / sid)
    monkeypatch.setattr("api.turn_journal.delete_turn_journal", lambda _sid, **_kwargs: 0)
    monkeypatch.setattr("api.run_journal.delete_run_journal", lambda _sid, **_kwargs: False)
    captured = _capture_route(monkeypatch)

    routes._handle_sessions_cleanup(_FakeHandler("/api/sessions/cleanup"), {})

    assert captured["ok"]["cleaned"] == 1
    assert not stale.path.exists()
    with pytest.raises(RuntimeError, match="stale|deleted session"):
        stale.save(skip_index=True)


def test_same_sid_recreation_does_not_authorize_old_session_object(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    old = Session(session_id="same-sid-generation", messages=[{"role": "user", "content": "old"}])
    old.save(skip_index=True)
    old_token = json.loads(old.path.read_text(encoding="utf-8"))[
        "publication_incarnation"
    ]
    models.SESSIONS[old.session_id] = old
    _stub_delete_route_dependencies(monkeypatch, old, tmp_path)
    captured = _capture_route(monkeypatch)
    _call_delete_route(old.session_id)
    assert captured["ok"]["ok"] is True

    replacement = Session(
        session_id=old.session_id,
        messages=[{"role": "user", "content": "replacement"}],
    )
    with models.reserve_session_destination(replacement.session_id) as reservation:
        reservation.bind(replacement)
        replacement.save(skip_index=True)
        reservation.commit()
    replacement_token = json.loads(replacement.path.read_text(encoding="utf-8"))[
        "publication_incarnation"
    ]
    assert replacement_token != old_token

    old.messages.append({"role": "assistant", "content": "stale overwrite"})
    with pytest.raises(RuntimeError, match="stale"):
        old.save(skip_index=True)
    assert "replacement" in replacement.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("cold_registry", [False, True], ids=["same-process", "cold-registry"])
def test_bare_constructor_cannot_adopt_existing_publication_authority(
    cold_registry, tmp_path, monkeypatch
):
    """Only validated loaders may receive a live sidecar's incarnation token."""
    _configure_session_state(tmp_path, monkeypatch)
    sid = "bare-constructor-authority"
    owner = Session(
        session_id=sid,
        title="owner",
        messages=[{"role": "user", "content": "keep"}],
    )
    owner.save()
    sidecar_before = owner.path.read_bytes()
    index_before = models.SESSION_INDEX_FILE.read_bytes()

    if cold_registry:
        models.SESSIONS.clear()
        models._SESSION_PUBLICATION_GENERATIONS.clear()

    unbound = Session(
        session_id=sid,
        title="rogue",
        messages=[{"role": "user", "content": "overwrite"}],
    )
    with pytest.raises(RuntimeError, match="stale session generation"):
        unbound.save()

    assert owner.path.read_bytes() == sidecar_before
    assert models.SESSION_INDEX_FILE.read_bytes() == index_before


@pytest.mark.skipif(models._fcntl is None, reason="requires POSIX process locks")
def test_persisted_incarnation_rejects_stale_object_after_other_process_recreates_sid(
    tmp_path, monkeypatch
):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    sid = "cross-process-incarnation"
    old = Session(
        session_id=sid,
        messages=[{"role": "user", "content": "old"}],
    )
    old.save(skip_index=True)
    old_token = old._publication_generation.token
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    worker = context.Process(
        target=_cross_process_recreate_session,
        args=(
            str(session_dir),
            str(models.SESSION_INDEX_FILE),
            str(tmp_path),
            sid,
            result_queue,
        ),
    )
    worker.start()
    worker.join(20)
    assert worker.exitcode == 0
    result = result_queue.get(timeout=5)
    assert "error" not in result
    assert result["token"] != old_token

    old.messages.append({"role": "assistant", "content": "stale overwrite"})
    with pytest.raises(RuntimeError, match="stale session generation"):
        old.save(skip_index=True)
    assert "replacement" in old.path.read_text(encoding="utf-8")


def test_delete_defers_while_run_is_active_then_retries_idempotently(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    session = Session(
        session_id="delete-active-run",
        messages=[{"role": "user", "content": "still running"}],
    )
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    _stub_delete_route_dependencies(monkeypatch, session, tmp_path)
    captured = _capture_route(monkeypatch)
    stream_id = "delete-active-stream"
    with routes.api_config.ACTIVE_RUNS_LOCK:
        routes.api_config.ACTIVE_RUNS[stream_id] = {"session_id": session.session_id}
    try:
        _call_delete_route(session.session_id)
        assert captured["status"] == 500
        assert {item["artifact"] for item in captured["ok"]["residuals"]} >= {
            "active_run"
        }
        assert session.path.exists()
        with pytest.raises(RuntimeError, match="deleted session"):
            session.save(skip_index=True)
    finally:
        with routes.api_config.ACTIVE_RUNS_LOCK:
            routes.api_config.ACTIVE_RUNS.pop(stream_id, None)

    _call_delete_route(session.session_id)
    assert captured["status"] == 200
    assert captured["ok"]["ok"] is True
    assert not session.path.exists()
    assert session.session_id not in models._load_session_cleanup_residuals()


def test_cleanup_retry_preserves_messaging_state_db_ownership_policy(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    session = Session(
        session_id="messaging-cleanup-policy",
        messages=[{"role": "user", "content": "external owner"}],
    )
    session.save(skip_index=True)
    backup = session.path.with_suffix(".json.bak")
    backup.write_text("private backup", encoding="utf-8")
    models.SESSIONS[session.session_id] = session
    _stub_delete_route_dependencies(monkeypatch, session, tmp_path)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: True)
    state_db_calls = []
    monkeypatch.setattr(
        models,
        "delete_cli_session",
        lambda sid: state_db_calls.append(sid) or True,
    )
    captured = _capture_route(monkeypatch)
    original_unlink = type(backup).unlink

    def fail_backup(self, *args, **kwargs):
        if self == backup:
            raise OSError("backup busy")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(backup), "unlink", fail_backup)
    _call_delete_route(session.session_id)
    assert captured["status"] == 500
    policy = models._load_session_cleanup_residuals()[session.session_id]
    assert policy["delete_state_db"] is False
    assert policy["durable_tombstone"] is False
    assert state_db_calls == []

    monkeypatch.setattr(type(backup), "unlink", original_unlink)
    routes._handle_sessions_cleanup(_FakeHandler("/api/sessions/cleanup"), {})

    assert captured["status"] == 200
    assert state_db_calls == []
    assert not backup.exists()
    assert session.session_id not in models._load_session_cleanup_residuals()


def test_ephemeral_cancel_before_worker_retires_exact_owned_lifecycle(tmp_path, monkeypatch):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    from api.background import cleanup_btw, track_btw
    from api.config import SESSION_AGENT_LOCKS

    ephemeral = Session(
        session_id="btw-preworker-cancel",
        parent_session_id="btw-preworker-parent",
        messages=[{"role": "user", "content": "private question"}],
    )
    ephemeral.active_stream_id = "btw-preworker-stream"
    ephemeral.save(skip_index=True)
    models.SESSIONS[ephemeral.session_id] = ephemeral
    routes.register_stream_owner(ephemeral.active_stream_id, ephemeral.session_id)
    track_btw(
        ephemeral.parent_session_id,
        ephemeral.session_id,
        ephemeral.active_stream_id,
        "private question",
    )
    SESSION_AGENT_LOCKS[ephemeral.session_id] = threading.Lock()
    run_dir = session_dir / "_run_journal" / ephemeral.session_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.jsonl").write_text('{"private":true}\n', encoding="utf-8")
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)

    streaming._run_agent_streaming(
        ephemeral.session_id,
        "private question",
        "model",
        str(tmp_path),
        ephemeral.active_stream_id,
        ephemeral=True,
    )

    assert not ephemeral.path.exists()
    assert not run_dir.exists()
    assert ephemeral.session_id not in models.SESSIONS
    assert ephemeral.session_id not in SESSION_AGENT_LOCKS
    assert cleanup_btw(
        ephemeral.parent_session_id,
        ephemeral.session_id,
        ephemeral.active_stream_id,
    ) is None


def test_ephemeral_cleanup_does_not_erase_newer_parent_owner(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    from api.background import cleanup_btw, track_btw

    older = Session(session_id="btw-older", parent_session_id="shared-parent")
    older.active_stream_id = "older-stream"
    newer = Session(session_id="btw-newer", parent_session_id="shared-parent")
    newer.active_stream_id = "newer-stream"
    track_btw("shared-parent", older.session_id, older.active_stream_id, "older")
    track_btw("shared-parent", newer.session_id, newer.active_stream_id, "newer")
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)

    streaming._cleanup_ephemeral_session(older, stream_id=older.active_stream_id)

    owned = cleanup_btw("shared-parent", newer.session_id, newer.active_stream_id)
    assert owned is not None
    assert owned["ephemeral_session_id"] == newer.session_id


def test_ephemeral_cleanup_failure_keeps_exact_retryable_owner(tmp_path, monkeypatch):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    from api.background import find_btw_owner, track_btw

    session = Session(session_id="btw-cleanup-residual", parent_session_id="btw-residual-parent")
    session.active_stream_id = "btw-residual-stream"
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    track_btw(
        session.parent_session_id,
        session.session_id,
        session.active_stream_id,
        "private question",
    )
    run_dir = session_dir / "_run_journal" / session.session_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.jsonl").write_text("private\n", encoding="utf-8")
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    monkeypatch.setattr(
        "api.run_journal.delete_run_journal",
        lambda _sid, **_kwargs: False,
    )

    result = streaming._cleanup_ephemeral_session(session)

    assert result["ok"] is False
    assert {item["artifact"] for item in result["residuals"]} >= {"run_journal"}
    owner = find_btw_owner(session.session_id, "btw-residual-stream")
    assert owner is not None
    assert owner["cleanup_pending"] is True
    assert session.session_id in models._load_session_cleanup_residuals()


def test_sidecar_outer_identity_cannot_redirect_session_namespace(tmp_path, monkeypatch):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    payload = {
        "session_id": "identity-b",
        "title": "claimed",
        "created_at": 1,
        "updated_at": 1,
        "messages": [{"role": "user", "content": "secret"}],
    }
    (session_dir / "identity-a.json").write_text(json.dumps(payload), encoding="utf-8")

    assert Session.load("identity-a") is None
    assert Session.load_metadata_only("identity-a") is None
    assert models._load_session_from_path(session_dir / "identity-a.json") is None
    assert "identity-b" not in models.SESSIONS


def test_destination_collision_preserves_existing_owner_bytes(tmp_path, monkeypatch):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    path = session_dir / "reserved-owner.json"
    original = b'{"session_id":"reserved-owner","messages":[{"role":"user","content":"keep"}]}'
    path.write_bytes(original)

    with pytest.raises(models.SessionDestinationCollisionError):
        with models.reserve_session_destination("reserved-owner"):
            raise AssertionError("collision reservation entered")

    assert path.read_bytes() == original


def test_destination_reservation_rebuilds_corrupt_index_from_sidecars(tmp_path, monkeypatch):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    models.SESSION_INDEX_FILE.write_text("{not-json", encoding="utf-8")

    with models.reserve_session_destination("index-rebuild-owner"):
        pass

    assert json.loads(models.SESSION_INDEX_FILE.read_bytes()) == []
    assert not (session_dir / "index-rebuild-owner.json").exists()


def test_destination_reservation_token_rejects_same_thread_piggyback(
    tmp_path, monkeypatch
):
    _configure_session_state(tmp_path, monkeypatch)
    owner = Session(
        session_id="reserved-token-owner",
        messages=[{"role": "user", "content": "owner"}],
    )
    rogue = Session(
        session_id=owner.session_id,
        messages=[{"role": "user", "content": "rogue"}],
    )

    with models.reserve_session_destination(owner.session_id) as reservation:
        reservation.bind(owner)
        with pytest.raises(RuntimeError, match="publication transaction"):
            rogue.save(skip_index=True)
        owner.save(skip_index=True)
        reservation.commit()

    persisted = json.loads(owner.path.read_text(encoding="utf-8"))
    assert persisted["messages"][0]["content"] == "owner"


@pytest.mark.skipif(models._fcntl is None, reason="requires POSIX process locks")
def test_index_read_modify_write_is_serialized_across_processes(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    models.SESSION_INDEX_FILE.write_text("[]", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()

    workers = [
        context.Process(
            target=_cross_process_index_writer,
            args=(
                str(models.SESSION_DIR),
                str(models.SESSION_INDEX_FILE),
                sid,
                ready,
                start,
            ),
        )
        for sid in ("process-index-a", "process-index-b")
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=60), ready.get(timeout=60)} == {
        "process-index-a",
        "process-index-b",
    }
    start.set()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0

    rows = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    assert {row["session_id"] for row in rows} == {
        "process-index-a",
        "process-index-b",
    }


@pytest.mark.skipif(models._fcntl is None, reason="requires POSIX process locks")
def test_tombstone_read_modify_write_is_serialized_across_processes(
    tmp_path, monkeypatch
):
    _configure_session_state(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    workers = [
        context.Process(
            target=_cross_process_tombstone_writer,
            args=(str(models.SESSION_DIR), sid, ready, start),
        )
        for sid in ("process-tombstone-a", "process-tombstone-b")
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=60), ready.get(timeout=60)} == {
        "process-tombstone-a",
        "process-tombstone-b",
    }
    start.set()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0

    assert models._load_webui_deleted_session_tombstone() == {
        "process-tombstone-a",
        "process-tombstone-b",
    }


@pytest.mark.parametrize(
    "corrupt_bytes",
    [
        b"{partial",
        json.dumps({"version": 999, "ids": ["some-deleted-session"]}).encode(),
    ],
    ids=["invalid-json", "future-version"],
)
def test_corrupt_deleted_tombstone_isolated_by_destination_reservation(
    tmp_path, monkeypatch, corrupt_bytes
):
    _configure_session_state(tmp_path, monkeypatch)
    tombstone_path = models._webui_deleted_session_tombstone_file()
    tombstone_path.write_bytes(corrupt_bytes)
    sid = "corrupt-tombstone-publication"

    with pytest.raises(RuntimeError, match="unreadable deleted-session authority"):
        Session(session_id=sid).save(skip_index=True)

    reserved = Session(session_id=sid)
    with models.reserve_session_destination(sid) as reservation:
        reservation.bind(reserved)
        reserved.save(skip_index=True)
        reservation.commit()

    assert (models.SESSION_DIR / f"{sid}.json").exists()
    assert not models._session_incarnation_claim_file(sid).exists()
    assert tombstone_path.read_bytes() == corrupt_bytes


@pytest.mark.parametrize("path", ["/api/share/create", "/api/share/revoke"])
def test_external_share_sidecar_save_failure_releases_destination_claim(
    path, tmp_path, monkeypatch
):
    session_dir = _configure_session_state(tmp_path, monkeypatch)
    sid = "external-share-sidecar"
    snapshot = Session(
        session_id=sid,
        title="External share",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "share me"}],
    )
    snapshot.share_token = "existing-share-token" if path.endswith("revoke") else None
    snapshot.share_created_at = 123.0
    share_meta = {
        "share_token": "created-share-token",
        "share_title": "External share",
        "share_message_count": 1,
        "share_created_at": 123.0,
        "share_updated_at": 124.0,
    }
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
    monkeypatch.setattr(
        routes,
        "_resolve_share_session_pair",
        lambda _sid, _handler: (snapshot, None, {}),
    )
    monkeypatch.setattr(routes, "create_or_refresh_share", lambda _session: share_meta)
    monkeypatch.setattr(routes, "revoke_share", lambda _session: True)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)
    captured = _capture_route(monkeypatch)
    original_save = Session.save

    def fail_save(_session, *args, **kwargs):
        raise OSError("injected share sidecar save failure")

    monkeypatch.setattr(Session, "save", fail_save)
    with pytest.raises(OSError, match="injected share sidecar save failure"):
        routes.handle_post(_FakeHandler(path), urlparse(path))

    assert not models._session_incarnation_claim_file(sid).exists()
    assert not (session_dir / f"{sid}.json").exists()
    assert sid not in models._active_destination_reservations()

    monkeypatch.setattr(Session, "save", original_save)
    routes.handle_post(_FakeHandler(path), urlparse(path))

    assert captured["status"] == 200
    assert (session_dir / f"{sid}.json").exists()
    assert not models._session_incarnation_claim_file(sid).exists()


def test_import_index_failure_rolls_back_json_media_and_cache(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    destination_id = "importrollback"
    monkeypatch.setattr(models.uuid, "uuid4", lambda: SimpleNamespace(hex=destination_id))
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda path: path)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        models,
        "_write_session_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("index failed")),
    )
    captured = _capture_route(monkeypatch)

    routes._handle_session_import(
        _FakeHandler("/api/session/import"),
        {"messages": [_image_message(data_url)], "workspace": str(tmp_path)},
    )

    assert captured["bad"][1] == 500
    assert destination_id not in models.SESSIONS
    assert not (models.SESSION_DIR / f"{destination_id}.json").exists()
    assert not session_media._session_media_dir(destination_id).exists()


def test_focused_recovery_index_failure_rolls_back_destination(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    source = Session(
        session_id="focused-recovery-source",
        title="Exhausted",
        workspace=str(tmp_path),
    )
    source.save(skip_index=True)
    destination_id = "recoveryrollback"
    monkeypatch.setattr(models.uuid, "uuid4", lambda: SimpleNamespace(hex=destination_id))
    monkeypatch.setattr(routes, "get_session", lambda _sid: source)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(
        routes,
        "compression_recovery_payload_for_session",
        lambda _session: {"recommended_action": routes.COMPRESSION_RECOVERY_ACTION_START_FOCUSED},
    )
    monkeypatch.setattr(routes, "find_compression_recovery_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        models,
        "_write_session_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("index failed")),
    )
    captured = _capture_route(monkeypatch)

    routes._handle_session_compression_recovery_start(
        _FakeHandler("/api/session/compression-recovery/start"),
        {"session_id": source.session_id},
    )

    assert captured["bad"][1] == 500
    assert destination_id not in models.SESSIONS
    assert not (models.SESSION_DIR / f"{destination_id}.json").exists()


def test_delete_requires_durable_tombstone_directory_barrier(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    session = Session(
        session_id="delete-dir-barrier",
        messages=[{"role": "user", "content": "retain until intent is durable"}],
    )
    session.save(skip_index=True)
    original_fsync_parent = models._fsync_parent_directory

    def fail_tombstone_barrier(path):
        if path == models._webui_deleted_session_tombstone_file():
            raise OSError("directory fsync failed")
        return original_fsync_parent(path)

    monkeypatch.setattr(models, "_fsync_parent_directory", fail_tombstone_barrier)
    result = models.delete_session_artifacts(
        session.session_id,
        delete_state_db=False,
    )

    assert result["ok"] is False
    assert {item["artifact"] for item in result["residuals"]} >= {
        "deleted_session_tombstone"
    }
    assert session.path.exists()


def test_session_save_does_not_report_success_when_directory_barrier_fails(
    tmp_path, monkeypatch
):
    _configure_session_state(tmp_path, monkeypatch)
    session = Session(
        session_id="save-dir-barrier",
        messages=[{"role": "user", "content": "durable publication"}],
    )
    original_fsync_parent = models._fsync_parent_directory

    def fail_sidecar_barrier(path):
        if path == session.path:
            raise OSError("directory fsync failed")
        return original_fsync_parent(path)

    monkeypatch.setattr(models, "_fsync_parent_directory", fail_sidecar_barrier)

    with pytest.raises(OSError, match="directory fsync failed"):
        session.save(skip_index=True)


def test_media_final_replacement_is_not_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    session_media.externalize_large_session_media(messages, "replace-race")
    original_assert = session_media._assert_entry_still_names_fd
    calls = 0
    replacement_names = []

    def replace_before_final_check(parent_fd, name, child_fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            moved = name + ".moved"
            os.rename(name, moved, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, dir_fd=parent_fd)
            replacement_names.append(name)
        return original_assert(parent_fd, name, child_fd)

    monkeypatch.setattr(
        session_media,
        "_assert_entry_still_names_fd",
        replace_before_final_check,
    )
    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
        session_media.remove_session_media("replace-race")

    # Retirement stops at the first held child before a pathname deletion is
    # attempted, so this outer directory check never becomes a delete race.
    assert calls == 1
    assert replacement_names == []
    assert not session_media._session_media_dir("replace-race").exists()


def test_media_directory_replacement_after_final_check_is_not_removed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    sid = "replace-after-directory-check"
    session_media.externalize_large_session_media(messages, sid)
    next(session_media._session_media_dir(sid).iterdir()).unlink()
    original_assert = session_media._assert_entry_still_names_fd
    quarantine_prefix = session_media._deletion_quarantine_prefix(sid)
    checked_quarantine = 0
    replacement = []

    def replace_after_final_check(parent_fd, name, child_fd):
        nonlocal checked_quarantine
        result = original_assert(parent_fd, name, child_fd)
        if name.startswith(quarantine_prefix):
            checked_quarantine += 1
            if checked_quarantine == 2:
                moved = name + ".moved"
                os.rename(name, moved, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(name, dir_fd=parent_fd)
                replacement.append(name)
        return result

    monkeypatch.setattr(
        session_media,
        "_assert_entry_still_names_fd",
        replace_after_final_check,
    )

    with pytest.raises(session_media.SessionMediaIntegrityError, match="changed"):
        session_media.remove_session_media(sid)

    assert replacement
    assert (tmp_path / "session-media" / replacement[0]).is_dir()
    assert (tmp_path / "session-media" / f"{replacement[0]}.moved").is_dir()


def test_prune_file_replacement_after_final_check_is_not_unlinked(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    sid = "replace-after-file-check"
    session_media.externalize_large_session_media(messages, sid)
    original_assert = session_media._assert_regular_entry_still_names_fd
    replacement = []

    def replace_after_final_check(parent_fd, name, entry_fd):
        result = original_assert(parent_fd, name, entry_fd)
        if name.startswith(".prune-") and not replacement:
            moved = name + ".moved"
            os.rename(name, moved, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"replacement")
            replacement.append(name)
        return result

    monkeypatch.setattr(
        session_media,
        "_assert_regular_entry_still_names_fd",
        replace_after_final_check,
    )

    with pytest.raises(session_media.SessionMediaIntegrityError, match="changed"):
        session_media.prune_session_media(sid, [])

    assert replacement
    media_dir = session_media._session_media_dir(sid)
    assert (media_dir / replacement[0]).read_bytes() == b"replacement"
    assert (media_dir / f"{replacement[0]}.moved").is_file()


def test_prune_replacement_after_innermost_check_survives_retirement(
    tmp_path, monkeypatch
):
    """The final held-FD check must not be followed by pathname unlink."""
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    sid = "replace-after-innermost-file-check"
    session_media.externalize_large_session_media(messages, sid)
    original_assert = session_media._assert_regular_entry_still_names_fd
    checks = 0
    replacement = []

    def replace_after_innermost_check(parent_fd, name, entry_fd):
        nonlocal checks
        result = original_assert(parent_fd, name, entry_fd)
        if name.startswith(".prune-"):
            checks += 1
            if checks == 2:
                moved = name + ".moved"
                os.rename(name, moved, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(fd, "wb") as handle:
                    handle.write(b"replacement")
                replacement.append(name)
        return result

    monkeypatch.setattr(
        session_media,
        "_assert_regular_entry_still_names_fd",
        replace_after_innermost_check,
    )

    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
        session_media.prune_session_media(sid, [])

    assert checks == 2
    assert replacement
    media_dir = session_media._session_media_dir(sid)
    assert (media_dir / replacement[0]).read_bytes() == b"replacement"
    assert (media_dir / f"{replacement[0]}.moved").is_file()


def test_directory_replacement_after_innermost_check_survives_retirement(
    tmp_path, monkeypatch
):
    """The final held-FD check must not be followed by pathname rmdir."""
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    sid = "replace-after-innermost-directory-check"
    session_media.externalize_large_session_media(messages, sid)
    next(session_media._session_media_dir(sid).iterdir()).unlink()
    original_assert = session_media._assert_entry_still_names_fd
    quarantine_prefix = session_media._deletion_quarantine_prefix(sid)
    checks = 0
    replacement = []

    def replace_after_innermost_check(parent_fd, name, entry_fd):
        nonlocal checks
        result = original_assert(parent_fd, name, entry_fd)
        if name.startswith(quarantine_prefix):
            checks += 1
            if checks == 3:
                moved = name + ".moved"
                os.rename(name, moved, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(name, dir_fd=parent_fd)
                replacement.append(name)
        return result

    monkeypatch.setattr(
        session_media,
        "_assert_entry_still_names_fd",
        replace_after_innermost_check,
    )

    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
        session_media.remove_session_media(sid)

    assert checks == 3
    assert replacement
    media_root = tmp_path / "session-media"
    assert (media_root / replacement[0]).is_dir()
    assert (media_root / f"{replacement[0]}.moved").is_dir()


def test_prune_exact_removal_allows_additional_hardlink(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    _raw, data_url = _large_png_data_url()
    messages = [_image_message(data_url)]
    sid = "prune-hardlink"
    session_media.externalize_large_session_media(messages, sid)
    media_dir = session_media._session_media_dir(sid)
    stored = next(media_dir.iterdir())
    sibling = media_dir / "additional-hardlink.png"
    os.link(stored, sibling)

    with pytest.raises(session_media.SessionMediaIntegrityError, match="retaining quarantine"):
        session_media.prune_session_media(sid, [])
    assert len(list(media_dir.glob(".prune-*"))) == 1
    assert len([path for path in media_dir.iterdir() if not path.name.startswith(".prune-")]) == 1


def test_unsupported_backend_detects_broken_private_root_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    (tmp_path / "missing-target").mkdir()
    os.symlink(tmp_path / "gone", tmp_path / "session-media")
    monkeypatch.setattr(session_media, "_DIR_FD_OK", False)

    with pytest.raises(session_media.SessionMediaIntegrityError, match="unsupported"):
        session_media.remove_session_media("broken-root")


def test_clear_durably_removes_backup_and_private_media(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="clear-private-media",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    models.SESSIONS[session.session_id] = session
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes.api_config, "_evict_session_agent", lambda _sid: None)
    captured = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler("/api/session/clear"), urlparse("/api/session/clear"))

    assert captured["status"] == 500
    assert captured["ok"]["error"] == "Session clear incomplete"
    assert {item["artifact"] for item in captured["ok"]["residuals"]} == {
        "session_media"
    }
    assert not session.path.with_suffix(".json.bak").exists()
    assert not session_media._session_media_dir(session.session_id).exists()
    assert len(list((tmp_path / "session-media").glob(".delete-*"))) == 1


def test_clear_retires_backup_and_media_even_when_live_transcript_is_already_empty(
    tmp_path, monkeypatch
):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="clear-already-empty",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    session.messages = []
    session.context_messages = []
    session.save(skip_index=True)
    backup = session.path.with_suffix(".json.bak")
    assert backup.exists()
    assert session_media._session_media_dir(session.session_id).exists()
    models.SESSIONS[session.session_id] = session
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes.api_config, "_evict_session_agent", lambda _sid: None)
    captured = _capture_route(monkeypatch)

    routes.handle_post(_FakeHandler("/api/session/clear"), urlparse("/api/session/clear"))

    assert captured["status"] == 500
    assert {item["artifact"] for item in captured["ok"]["residuals"]} == {
        "session_media"
    }
    assert not backup.exists()
    assert not session_media._session_media_dir(session.session_id).exists()


def test_truncate_prunes_only_media_unreachable_from_live_or_backup(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw_a, data_url_a = _large_png_data_url(b"a")
    _raw_b, data_url_b = _large_png_data_url(b"b")
    _raw_stray, data_url_stray = _large_png_data_url(b"z")
    session = Session(
        session_id="truncate-reachability",
        messages=[_image_message(data_url_a), _image_message(data_url_b)],
        context_messages=[_image_message(data_url_a), _image_message(data_url_b)],
    )
    session.save(skip_index=True)
    stray = [_image_message(data_url_stray)]
    session_media.externalize_large_session_media(stray, session.session_id)
    stray_name = stray[0]["content"][1]["image_url"]["url"].split("//", 1)[1]

    from api.session_ops import truncate_session_at_keep

    truncate_session_at_keep(session, 1)
    session.save(skip_index=True, prune_media=True)

    files = {path.name for path in session_media._session_media_dir(session.session_id).iterdir()}
    assert stray_name not in files
    # The removed second message remains reachable from the recovery backup.
    assert len([name for name in files if not name.startswith(".prune-")]) == 2
    assert len([name for name in files if name.startswith(".prune-")]) == 1


def test_truncate_does_not_report_committed_write_as_failed_when_prune_fails(
    tmp_path,
    monkeypatch,
    caplog,
):
    _configure_session_state(tmp_path, monkeypatch)
    session = Session(
        session_id="truncate-prune-failure",
        messages=[
            {"role": "user", "content": "keep"},
            {"role": "assistant", "content": "remove"},
        ],
        context_messages=[
            {"role": "user", "content": "keep"},
            {"role": "assistant", "content": "remove"},
        ],
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": session.session_id, "keep_count": 1},
    )
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes.api_config, "_evict_session_agent", lambda _sid: None)
    original_prune = session_media.prune_session_media
    monkeypatch.setattr(
        session_media,
        "prune_session_media",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected prune failure")),
    )
    captured = _capture_route(monkeypatch)

    routes.handle_post(
        _FakeHandler("/api/session/truncate"),
        urlparse("/api/session/truncate"),
    )

    assert captured["ok"]["ok"] is True
    assert captured["status"] == 200
    assert json.loads(session.path.read_text(encoding="utf-8"))["messages"] == [
        {"role": "user", "content": "keep"}
    ]
    assert "Could not prune session media after transcript truncation" in caplog.text
    residual = models._load_session_cleanup_residuals()[session.session_id]
    assert residual["operation"] == "prune"
    assert residual["residuals"][0]["artifact"] == "session_media"

    monkeypatch.setattr(session_media, "prune_session_media", original_prune)
    routes._handle_sessions_cleanup(_FakeHandler("/api/sessions/cleanup"), {})
    assert captured["status"] == 200
    assert session.path.exists()
    assert session.session_id not in models._load_session_cleanup_residuals()
    index_rows = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    assert any(row.get("session_id") == session.session_id for row in index_rows)


@pytest.mark.parametrize(
    "field,value_factory",
    [
        ("tool_calls", lambda ref: [{"result": ref}]),
        ("composer_draft", lambda ref: {"text": ref}),
        ("anchor_activity_scenes", lambda ref: {"scene": {"body": ref}}),
        ("process_wakeup_pause", lambda ref: {"detail": ref}),
        ("custom_extra", lambda ref: {"nested": [ref]}),
    ],
)
def test_save_rejects_private_refs_outside_sanctioned_transcript_fields(
    field,
    value_factory,
    tmp_path,
    monkeypatch,
):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id=f"private-field-{field.replace('_', '-')}",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    before = session.path.read_bytes()
    ref = session.messages[0]["content"][1]["image_url"]["url"]
    setattr(session, field, value_factory(ref))

    with pytest.raises(
        session_media.SessionMediaIntegrityError,
        match="outside messages/context_messages",
    ):
        session.save(skip_index=True)

    assert session.path.read_bytes() == before


def test_save_rejects_private_ref_in_non_image_transcript_path(tmp_path, monkeypatch):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="private-non-image-part",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    before = session.path.read_bytes()
    ref = session.messages[0]["content"][1]["image_url"]["url"]
    session.messages = [{"role": "user", "content": ref}]

    with pytest.raises(
        session_media.SessionMediaIntegrityError,
        match="outside an image part",
    ):
        session.save(skip_index=True)

    assert session.path.read_bytes() == before


def test_save_rejects_image_shaped_private_ref_in_message_metadata(
    tmp_path, monkeypatch
):
    _configure_session_state(tmp_path, monkeypatch)
    _raw, data_url = _large_png_data_url()
    session = Session(
        session_id="private-image-shaped-metadata",
        messages=[_image_message(data_url)],
        context_messages=[_image_message(data_url)],
    )
    session.save(skip_index=True)
    before = session.path.read_bytes()
    ref = session.messages[0]["content"][1]["image_url"]["url"]
    session.messages[0]["metadata"] = {
        "type": "image_url",
        "image_url": {"url": ref},
    }

    with pytest.raises(
        session_media.SessionMediaIntegrityError,
        match="outside an image part",
    ):
        session.save(skip_index=True)

    assert session.path.read_bytes() == before
