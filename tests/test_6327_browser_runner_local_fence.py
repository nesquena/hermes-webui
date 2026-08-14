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
    """Minimal Session stand-in carrying the attrs the browser start path reads.

    PRODUCTION-SHAPED (review 17, blocker): a real ``Session`` always
    initializes ``process_wakeup_pause={}`` (``api/models.py``).  Omitting the
    attribute made ``getattr(..., None)`` match the token's previously
    hard-coded ``pause_state=None`` and false-green the route; with the
    production shape the immediate pre-POST mismatch check compares the live
    ``{}`` against the token snapshot exactly.
    """

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
        self.process_wakeup_pause = {}  # production shape: Session() default
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


# ─────────────────────────────────────────────────────────────────────────────
# Review 17, blocker 1: explicit browser lane changes are fenced VERBATIM
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("normalized_model", [False, True])
def test_browser_explicit_lane_change_is_fenced_verbatim(
    isolated_session_env, monkeypatch, normalized_model
):
    """An EXPLICIT model/provider/workspace change resolved by the browser
    request — NOT yet persisted to the owner fields — must be bound to the
    fence and the ``StartRunRequest`` verbatim and ACCEPTED (200).

    Before review 17 the fence claim recomputed the lane from the still-old
    persisted owner (``_process_wakeup_owner_token_mismatch``) and rejected
    the legitimate explicit change with ``model_changed`` /
    ``provider_changed`` / ``workspace_changed`` (retryable 409) BEFORE
    ``adapter.start_run()`` ever contacted the runner.  The browser-request
    lane is the authority; owner identity, SID, canonical lock, profile/home,
    and credential generation are still re-checked exactly.
    """
    import api.routes as routes
    from api import models as _models

    sid = "sess-browser-explicit-lane"
    # The PERSISTED owner lane is the OLD one (gpt-5.5/openai-codex at
    # /workspace) — the browser resolves an explicit change away from it.
    session = _BrowserSession(sid)
    session.model = "gpt-5.5"
    session.model_provider = "openai-codex"
    session.workspace = "/workspace"
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
    # An explicit WORKSPACE change resolves to the requested workspace.
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: (w or "/workspace")
    )
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, None))
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: False)
    # An explicit model/provider change resolves to the REQUESTED lane, which
    # differs from the persisted owner fields.
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **kw: (model, provider, normalized_model),
    )
    monkeypatch.setattr(
        routes, "_repair_foreign_session_model_provider", lambda s, **kw: kw.get("resolved_provider")
    )
    monkeypatch.setattr(routes, "process_wakeup_credential_state_fingerprint", lambda s: "fp-browser-lane")
    monkeypatch.setattr(routes, "_process_wakeup_profile_home", lambda s: "/home/test/.hermes")

    responses = {}

    def _fake_j(handler, payload, status=200, **kw):
        responses["payload"] = payload
        responses["status"] = status
        return payload

    monkeypatch.setattr(routes, "j", _fake_j)
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: _fake_j(handler, {"error": msg}, status=status))

    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        fence = dict(captured["body"]["owner_fence"])
        return _FakeResponse({
            "run_id": "run-browser-lane-1",
            "stream_id": "run-browser-lane-1",
            "status": "running",
            "session_id": fence["session_id"],
            "owner_fence": {**fence, "accepted": True},
        })

    monkeypatch.setattr(
        HttpRunnerClient, "_opener", lambda self: _FakeOpener(fake_urlopen)
    )

    result = routes._handle_chat_start(
        None,
        {
            "session_id": sid,
            "message": "switch lanes explicitly",
            "model": "claude-sonnet-4",
            "model_provider": "anthropic",
            "workspace": "/other-ws",
            "explicit_model_pick": True,
        },
    )

    # The explicit lane change is ACCEPTED — never the pre-POST 409 the
    # persisted-lane recompute produced before review 17.
    assert responses.get("status") == 200, responses
    assert responses.get("payload", {}).get("stream_id") == "run-browser-lane-1", responses
    assert "error" not in (responses.get("payload") or {}), responses
    assert result == responses.get("payload")

    assert captured.get("url", "").endswith("/v1/runs"), captured
    body = captured["body"]
    fence = body["owner_fence"]
    assert _runner_owner_fence_schema_error(fence) is None, fence
    # The browser-request lane is bound VERBATIM to the fence...
    assert fence["route"]["model"] == "claude-sonnet-4", fence
    assert fence["route"]["provider"] == "anthropic", fence
    assert fence["route"]["workspace"] == "/other-ws", fence
    assert fence["route"]["normalized_model"] is normalized_model, fence
    # ...and the real client cross-bound the StartRunRequest to the same lane.
    assert body["session_id"] == sid
    assert body["model"] == "claude-sonnet-4", body
    assert body["provider"] == "anthropic", body
    assert body["workspace"] == "/other-ws", body
    # Root/default profile canonicalized on the wire, generation carried.
    assert fence["profile"] == "default", fence
    assert fence["generation"] == "fp-browser-lane", fence


def test_browser_explicit_lane_change_root_default_profile_is_accepted(
    isolated_session_env, monkeypatch
):
    """The explicit-lane browser start also succeeds for a root (profile
    None/empty) session — the fence canonicalizes the root profile wire
    identity to 'default' and binds the request lane verbatim (review 17)."""
    import api.routes as routes
    from api import models as _models

    sid = "sess-browser-explicit-root"
    session = _BrowserSession(sid)  # profile stays None (root)
    session.model = "old-model"
    session.model_provider = "old-provider"
    with _models.LOCK:
        _models.SESSIONS[sid] = session

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setenv("HERMES_WEBUI_RUNNER_BASE_URL", "http://127.0.0.1:1")

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda s_id, **kw: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **kw: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_normalize_chat_attachments", lambda raw: [])
    monkeypatch.setattr(routes, "compression_recovery_payload_for_session", lambda s: None)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: (w or "/workspace")
    )
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, None))
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: False)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **kw: (model, provider, True),
    )
    monkeypatch.setattr(
        routes, "_repair_foreign_session_model_provider", lambda s, **kw: kw.get("resolved_provider")
    )
    monkeypatch.setattr(routes, "process_wakeup_credential_state_fingerprint", lambda s: "fp-root-lane")
    monkeypatch.setattr(routes, "_process_wakeup_profile_home", lambda s: "/home/test/.hermes")

    responses = {}

    def _fake_j(handler, payload, status=200, **kw):
        responses["payload"] = payload
        responses["status"] = status
        return payload

    monkeypatch.setattr(routes, "j", _fake_j)
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: _fake_j(handler, {"error": msg}, status=status))

    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        fence = dict(captured["body"]["owner_fence"])
        return _FakeResponse({
            "run_id": "run-browser-root-1",
            "stream_id": "run-browser-root-1",
            "status": "running",
            "session_id": fence["session_id"],
            "owner_fence": {**fence, "accepted": True},
        })

    monkeypatch.setattr(
        HttpRunnerClient, "_opener", lambda self: _FakeOpener(fake_urlopen)
    )

    result = routes._handle_chat_start(
        None,
        {
            "session_id": sid,
            "message": "root explicit lane",
            "model": "new-model",
            "model_provider": "new-provider",
            "workspace": "/root-ws",
            "explicit_model_pick": True,
        },
    )

    assert responses.get("status") == 200, responses
    assert responses.get("payload", {}).get("stream_id") == "run-browser-root-1", responses
    assert "error" not in (responses.get("payload") or {}), responses

    fence = captured["body"]["owner_fence"]
    assert _runner_owner_fence_schema_error(fence) is None, fence
    assert fence["profile"] == "default"  # root/empty profile canonicalized
    assert fence["route"]["model"] == "new-model", fence
    assert fence["route"]["provider"] == "new-provider", fence
    assert fence["route"]["workspace"] == "/root-ws", fence
    assert fence["route"]["normalized_model"] is True, fence
    assert captured["body"]["model"] == "new-model", captured["body"]
    assert captured["body"]["provider"] == "new-provider", captured["body"]
    assert captured["body"]["workspace"] == "/root-ws", captured["body"]


# ─────────────────────────────────────────────────────────────────────────────
# Review 17, blocker: browser owner tokens encode the wrong pause-state shape
# ─────────────────────────────────────────────────────────────────────────────


def test_browser_pause_state_mutation_after_token_capture_refused_before_post(
    isolated_session_env, monkeypatch
):
    """NEGATIVE regression (review 17, blocker): the browser owner token must
    snapshot the canonical owner's pause state (same representation as the
    immutable owner-token/mismatch path: copy a dict, otherwise None) under
    the AGENT lock, and the claim must be refused BEFORE the runner POST when
    the live pause state mutates after token capture.

    A real ``Session`` initializes ``process_wakeup_pause={}``, so a
    hard-coded ``None`` token made the pre-POST mismatch check see live {}
    != token None -> ``pause_state_changed`` -> retryable 409 before
    ``adapter.start_run()`` for EVERY ordinary browser start (the previous
    ``_BrowserSession`` omitted the attribute and false-greened the route via
    ``getattr(..., None)``).  Here the token captures ``{}`` correctly and a
    LATER mutation is detected by the claim fence: 409 retryable, zero POSTs.
    """
    import api.routes as routes
    from api import models as _models

    sid = "sess-browser-pause-mutation"
    session = _BrowserSession(sid)
    with _models.LOCK:
        _models.SESSIONS[sid] = session

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setenv("HERMES_WEBUI_RUNNER_BASE_URL", "http://127.0.0.1:1")

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda s_id, **kw: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **kw: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_normalize_chat_attachments", lambda raw: [])
    monkeypatch.setattr(routes, "compression_recovery_payload_for_session", lambda s: None)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: (w or "/workspace")
    )
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
    monkeypatch.setattr(routes, "process_wakeup_credential_state_fingerprint", lambda s: "fp-pause-mut")
    monkeypatch.setattr(routes, "_process_wakeup_profile_home", lambda s: "/home/test/.hermes")

    responses = {}

    def _fake_j(handler, payload, status=200, **kw):
        responses["payload"] = payload
        responses["status"] = status
        return payload

    monkeypatch.setattr(routes, "j", _fake_j)
    monkeypatch.setattr(
        routes, "bad", lambda handler, msg, status=400: _fake_j(handler, {"error": msg}, status=status)
    )

    # The REAL token builder captures the pause snapshot under the AGENT lock;
    # we then MUTATE the live pause state AFTER capture and before the claim
    # fence runs — the immutable token must refuse the claim pre-POST.
    real_build = routes._build_browser_start_owner_token

    def _build_then_mutate_pause(s, **kwargs):
        token, err = real_build(s, **kwargs)
        assert token is not None, err
        assert token["pause_state"] == {}, token  # production {} shape captured
        s.process_wakeup_pause = {
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "classification": "manual_pause",
            "reason": "paused after token capture",
        }
        return token, err

    monkeypatch.setattr(routes, "_build_browser_start_owner_token", _build_then_mutate_pause)

    post_count = {"n": 0}

    def fake_urlopen(req, timeout=0):
        post_count["n"] += 1
        return _FakeResponse({})

    monkeypatch.setattr(
        HttpRunnerClient, "_opener", lambda self: _FakeOpener(fake_urlopen)
    )

    result = routes._handle_chat_start(
        None, {"session_id": sid, "message": "pause mutated after token capture"}
    )

    # The claim is REFUSED before POST: retryable 409, pause_state_changed,
    # and the runner transport was never contacted.
    assert responses.get("status") == 409, responses
    payload = responses.get("payload") or {}
    assert payload.get("retryable") is True, payload
    assert payload.get("owner_fence") == "pause_state_changed", payload
    assert "error" in payload, payload
    assert result == payload
    assert post_count["n"] == 0, post_count
