"""#6327 (review 16, blocker 1) — browser runner-local starts carry the complete owner fence.

Ordinary browser ``/api/chat/start`` configured with ``runner-local`` used to
build ``start_run_kwargs`` WITHOUT an ``owner_token``; ``_start_run()`` then
claimed a fence only when a token existed, so the real ``HttpRunnerClient``
deterministically raised ``RunnerFenceRefused`` (retryable 409) before ever
contacting the runner — invisible to the existing browser-start test, which
uses a permissive fake runner client.

This is a PRODUCTION-PATH test: ``_handle_chat_start`` is driven end-to-end
through the REAL ``HttpRunnerClient.start_run()`` (fence schema validation,
request/fence lane cross-bind, POST construction, receiver compare-and-accept).
Only the HTTP transport is faked (same ``_opener`` seam as
``tests/test_runner_client.py``); the runner-side echo accepts the COMPLETE
claimed fence, proving the canonical per-session owner authority is now
established for browser starts and the run is accepted (200), not refused.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from api.runner_client import HttpRunnerClient, _runner_owner_fence_schema_error


@pytest.fixture
def isolated_session_env():
    """Isolate the SESSIONS cache + session dir (mirrors the models tests)."""
    import collections
    import shutil
    import tempfile

    from api import config as _cfg
    from api import models

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    old = {
        "cfg_SESSION_DIR": _cfg.SESSION_DIR,
        "models_SESSION_DIR": getattr(models, "SESSION_DIR", None),
        "cfg_SESSION_INDEX_FILE": _cfg.SESSION_INDEX_FILE,
        "models_SESSION_INDEX_FILE": getattr(models, "SESSION_INDEX_FILE", None),
        "SESSIONS": _cfg.SESSIONS,
        "LOCK": _cfg.LOCK,
        "SESSIONS_MAX": _cfg.SESSIONS_MAX,
        "cfg": getattr(_cfg, "cfg", None),
    }

    index_file = sessions_dir / "_index.json"
    _cfg.SESSION_DIR = sessions_dir
    models.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = index_file
    models.SESSION_INDEX_FILE = index_file
    _cfg.LOCK = threading.Lock()
    models.LOCK = _cfg.LOCK
    _cfg.SESSIONS = collections.OrderedDict()
    models.SESSIONS = _cfg.SESSIONS

    try:
        yield sessions_dir
    finally:
        _cfg.SESSION_DIR = old["cfg_SESSION_DIR"]
        if old["models_SESSION_DIR"] is not None:
            models.SESSION_DIR = old["models_SESSION_DIR"]
        _cfg.SESSION_INDEX_FILE = old["cfg_SESSION_INDEX_FILE"]
        if old["models_SESSION_INDEX_FILE"] is not None:
            models.SESSION_INDEX_FILE = old["models_SESSION_INDEX_FILE"]
        _cfg.SESSIONS = old["SESSIONS"]
        models.SESSIONS = old["SESSIONS"]
        _cfg.LOCK = old["LOCK"]
        models.LOCK = old["LOCK"]
        _cfg.SESSIONS_MAX = old["SESSIONS_MAX"]
        if old["cfg"] is not None:
            _cfg.cfg = old["cfg"]
        shutil.rmtree(tmpdir, ignore_errors=True)


class _BrowserSession:
    """Minimal Session stand-in carrying the attrs the browser start path reads."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.title = "Browser runner-local fence"
        self.workspace = "/workspace"
        self.model = "gpt-5.5"
        self.model_provider = "openai-codex"
        self.profile = None
        self.personality = None
        self.messages = []
        self.context_messages = []
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.llm_title_generated = True
        self.composer_draft = None
        self._loaded_metadata_only = False
        self.created_at = time.time()


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _FakeOpener:
    def __init__(self, fake_urlopen):
        self._fake = fake_urlopen

    def open(self, req, timeout=0):
        return self._fake(req, timeout=timeout)


def test_browser_chat_start_runner_local_accepts_complete_fence(
    isolated_session_env, monkeypatch
):
    """A browser ``_handle_chat_start`` through the REAL ``HttpRunnerClient``
    must mint the canonical per-session owner fence and be ACCEPTED (200 with
    a stream id) — never the deterministic retryable 409 the missing-fence
    path produced before review 16."""
    import api.routes as routes
    from api import models as _models

    sid = "sess-browser-runner-local"
    session = _BrowserSession(sid)
    with _models.LOCK:
        _models.SESSIONS[sid] = session

    # Runner-local adapter selection + real HttpRunnerClient.from_env().
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setenv("HERMES_WEBUI_RUNNER_BASE_URL", "http://127.0.0.1:1")

    # Deterministic route plumbing (model resolution, workspace, profiles).
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda s_id, **kw: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **kw: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_normalize_chat_attachments", lambda raw: [])
    monkeypatch.setattr(routes, "compression_recovery_payload_for_session", lambda s: None)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda s, w: "/workspace")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, None))
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: False)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **kw: (model, provider, False),
    )
    monkeypatch.setattr(
        routes, "_repair_foreign_session_model_provider", lambda s, **kw: kw.get("resolved_provider")
    )
    monkeypatch.setattr(routes, "process_wakeup_credential_state_fingerprint", lambda s: "fp-browser-test")
    monkeypatch.setattr(routes, "_process_wakeup_profile_home", lambda s: "/home/test/.hermes")

    # Capture the JSON responses instead of writing to a socket handler.
    responses = {}

    def _fake_j(handler, payload, status=200, **kw):
        responses["payload"] = payload
        responses["status"] = status
        return payload

    monkeypatch.setattr(routes, "j", _fake_j)
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: _fake_j(handler, {"error": msg}, status=status))

    # Transport seam: the REAL HttpRunnerClient builds the POST; the fake
    # urlopen plays the runner and echoes the COMPLETE fence with accepted:true.
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        fence = dict(captured["body"]["owner_fence"])
        # The real client already validated the schema + lane and cross-bound
        # the request; the receiver now compare-and-accepts the complete claim.
        return _FakeResponse({
            "run_id": "run-browser-1",
            "stream_id": "run-browser-1",
            "status": "running",
            "session_id": fence["session_id"],
            "owner_fence": {**fence, "accepted": True},
        })

    monkeypatch.setattr(
        HttpRunnerClient, "_opener", lambda self: _FakeOpener(fake_urlopen)
    )

    result = routes._handle_chat_start(None, {"session_id": sid, "message": "hello browser runner"})

    # The run was ACCEPTED: status 200, a stream id, no error, and the POST
    # reached the runner with a COMPLETE owner fence.
    assert responses.get("status") == 200, responses
    assert responses.get("payload", {}).get("stream_id") == "run-browser-1", responses
    assert "error" not in (responses.get("payload") or {}), responses
    assert result == responses.get("payload")

    assert captured.get("url", "").endswith("/v1/runs"), captured
    body = captured["body"]
    assert body["session_id"] == sid
    fence = body["owner_fence"]
    # The fence is the COMPLETE receiver-authoritative claim — the real client
    # would have raised RunnerFenceRefused (mapped to a retryable 409) had any
    # field been missing or mismatched.
    assert _runner_owner_fence_schema_error(fence) is None, fence
    assert fence["session_id"] == sid
    assert fence["generation"] == "fp-browser-test"
    assert fence["route"]["model"] == "gpt-5.5"
    assert fence["route"]["provider"] == "openai-codex"
    assert fence["route"]["workspace"] == "/workspace"
    assert fence["route"]["normalized_model"] is False
    assert fence["version"] and fence["lease"], fence
    assert fence["profile"] == "default"  # root/empty profile canonicalized
