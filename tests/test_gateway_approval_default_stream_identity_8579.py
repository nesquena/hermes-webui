"""Default chat-completions stream: the approval identity decision (#8579).

On the DEFAULT Gateway path (``HERMES_WEBUI_GATEWAY_USE_RUNS_API`` unset) the
browser turn is a plain ``POST /v1/chat/completions`` stream. When the gateway
parks a prompt for approval it emits an ``approval.request`` frame on that
stream, and ``_run_gateway_chat_streaming`` mirrors it into WebUI polling
state so a card renders and ``/api/approval/respond`` can relay the click.

The Runs API relay (``_relay_gateway_run_approval``) decides, once, at
card-creation time, whether the card carries a canonical per-prompt identity
(``_gateway_agent_identity_v1``). The default-stream branch skipped that
decision, so every card it produced was relayed with the legacy
``{choice, approval_id: ""}`` body. A generation-fenced gateway answers only
``{request_id, execution_generation, choice}`` and rejects the legacy body
with ``400 invalid_params``: the card never settles, the run stays parked
until ``approvals.timeout`` (300 s) and then fails closed as deny.

These tests drive the REAL production functions end to end — the real
``_run_gateway_chat_streaming`` approval branch, the real
``submit_gateway_pending_mirror`` and the real ``/api/approval/respond``
handler — with only the gateway wire faked, exactly as a browser click would.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import api.gateway_chat as gateway_chat
import api.route_approvals as approvals
from api import routes
from api.config import STREAMS, STREAMS_LOCK
from api.gateway_chat import (
    _STREAM_RUN_IDS,
    _gateway_runs_approval_event,
    _relay_gateway_run_approval,
    _run_gateway_chat_streaming,
)

REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

RUN_ID = "run-8579-default-stream"
PROMPT_ID = "prompt-8579-default-stream"
BASE_URL = "http://gw:8642"


def _canonical_payload(**overrides):
    """The exact shape a generation-fenced gateway emits on the stream."""
    payload = {
        "event": "approval.request",
        "run_id": RUN_ID,
        "prompt_id": PROMPT_ID,
        "execution_generation": 7,
        "tool": "shell",
        "command": "rm -rf /tmp/x",
        "description": "delete temp dir",
        "choices": ["once", "deny"],
    }
    payload.update(overrides)
    return payload


def _legacy_payload(**overrides):
    """A legacy gateway: no execution_generation, older approval_id name."""
    payload = {
        "event": "approval.request",
        "run_id": RUN_ID,
        "approval_id": "appr-8579-legacy",
        "tool": "shell",
        "command": "rm -rf /tmp/x",
        "description": "delete temp dir",
        "choices": ["once", "deny"],
    }
    payload.update(overrides)
    return payload


def _mock_session(stream_id, sid):
    session = MagicMock()
    session.active_stream_id = stream_id
    session.workspace = "/tmp"
    session.model = "test"
    session.model_provider = None
    session.profile = None
    session.context_messages = []
    session.messages = []
    session.pending_user_message = None
    session.pending_attachments = None
    session.pending_started_at = None
    session.pending_user_source = None
    return session


class _SseResponse:
    """Minimal SSE response object for the faked /v1/chat/completions stream."""

    def __init__(self, body: bytes):
        self._lines = iter(body.split(b"\n"))

    def __iter__(self):
        return self._lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _sse_body(payload: dict) -> bytes:
    dumped = json.dumps(payload)
    return (
        f"event: approval.request\ndata: {dumped}\n\n"
        'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode("utf-8")


def _drive_default_stream(sid, stream_id, payload, *, caps=None):
    """Run the REAL default-stream approval branch over a faked gateway.

    Returns ``(events, click_state)``:

    * ``events`` — the ``(event_name, event_payload)`` tuples the stream put on
      the browser channel.
    * ``click_state`` — the production state observed at the instant the card
      was published: the emitted card, the gateway run_id registered for the
      still-open connection, and the mirrored pending entry. In production the
      stream stays open (blocked on the gateway) while the run is parked, so
      this is the state the user's click actually lands on. The stream here
      runs to completion, whose teardown retires the mirror, so
      ``_restore_click_state`` rebuilds exactly that state before a click.

    ``caps`` patches ``gateway_supports_approval_identity_v1`` for the whole
    drive; leave it None to leave the real probe in place.
    """
    events: list[tuple[str, dict]] = []
    click_state: dict = {}
    queue = MagicMock()

    def _put(item):
        events.append(item)
        if isinstance(item, tuple) and item[0] == "approval":
            with approvals._lock:
                pending = approvals._pending.get(sid)
                mirrored = (
                    dict(pending[0]) if isinstance(pending, list) and pending else None
                )
            click_state["card"] = item[1]
            click_state["run_id"] = _STREAM_RUN_IDS.get(stream_id)
            click_state["mirror_entry"] = mirrored

    queue.put_nowait = _put

    with STREAMS_LOCK:
        STREAMS[stream_id] = queue

    patches = [
        patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}),
        patch("urllib.request.urlopen", side_effect=_fake_chat_completions_urlopen(payload)),
        patch("api.gateway_chat.get_session", return_value=_mock_session(stream_id, sid)),
        patch("api.gateway_chat._stream_writeback_is_current", return_value=True),
        patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]),
    ]
    if caps is not None:
        patches.append(
            patch("api.config.gateway_supports_approval_identity_v1", return_value=caps)
        )
    try:
        for ctx in patches:
            ctx.start()
        try:
            _run_gateway_chat_streaming(
                session_id=sid,
                msg_text="do something risky",
                model="test",
                workspace="/tmp",
                stream_id=stream_id,
            )
        finally:
            for ctx in reversed(patches):
                ctx.stop()
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)
    return events, click_state


def _fake_chat_completions_urlopen(payload):
    def fake_urlopen(req, *, timeout=None):
        del req, timeout
        return _SseResponse(_sse_body(payload))

    return fake_urlopen


def _restore_click_state(sid, stream_id, click_state):
    """Rebuild the parked-run state a real click would land on.

    Mirrors the production window: the connection is still open (so the
    stream->run registration is live) and the card's mirror is the head of the
    session's pending queue. The mirrored entry is re-submitted verbatim
    through the real ``submit_gateway_pending_mirror``.
    """
    run_id = str(click_state.get("run_id") or "").strip()
    if run_id:
        _STREAM_RUN_IDS[stream_id] = run_id
    with approvals._lock:
        approvals._pending.pop(sid, None)
    entry = click_state.get("mirror_entry")
    if entry is None:
        return None
    head, _total = approvals.submit_gateway_pending_mirror(sid, dict(entry))
    return head


def _card_and_mirror(sid, events, click_state, approval_id, run_id=RUN_ID):
    """Assert the card and its mirror agree, and return the live mirror."""
    approval_events = [e for e in events if isinstance(e, tuple) and e[0] == "approval"]
    assert approval_events, "the default stream must surface the approval card"
    card = approval_events[0][1]
    mirror = approvals.gateway_pending_mirror(sid, approval_id=approval_id, run_id=run_id)
    assert mirror is not None, "the card must be mirrored for polling/respond"
    return card, mirror


def _answer(sid, body, captured, *, gateway_base=BASE_URL):
    """Drive the REAL /api/approval/respond handler with the gateway wire faked."""
    posts = []

    def fake_request_json(_self, req, **_kwargs):
        if req.method == "GET":
            captured.setdefault("gets", []).append(req.full_url)
            return {}
        answer = json.loads(req.data)
        posts.append(answer)
        captured["url"] = req.full_url
        captured["body"] = answer
        return {"ok": True}

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json), \
         patch("api.gateway_chat._gateway_base_url", return_value=gateway_base), \
         patch("api.gateway_chat._gateway_api_key", return_value=""):
        routes._handle_approval_respond(handler, body)
    captured["posts"] = posts
    captured["status"] = handler.send_response.call_args[0][0]
    captured["payload"] = json.loads(handler.wfile.getvalue().decode("utf-8"))
    return handler


def _click(sid, approval_id, run_id=RUN_ID, choice="once", mirror_token=None):
    body = {
        "session_id": sid,
        "choice": choice,
        "approval_id": approval_id,
        "run_id": run_id,
    }
    if mirror_token is not None:
        body["mirror_token"] = mirror_token
    return body


@pytest.fixture(autouse=True)
def _clean_approval_state():
    keys = list(approvals._pending.keys())
    for key in keys:
        approvals._pending.pop(key, None)
    keys = list(approvals._gateway_queues.keys())
    for key in keys:
        approvals._gateway_queues.pop(key, None)
    yield
    for key in list(approvals._pending.keys()):
        approvals._pending.pop(key, None)
    for key in list(approvals._gateway_queues.keys()):
        approvals._gateway_queues.pop(key, None)


# ---------------------------------------------------------------------------
# The shared creation-time decision
# ---------------------------------------------------------------------------

class TestSharedIdentityDecision:
    """One decision, one definition, used by every card origin."""

    def test_generation_is_the_authority(self):
        """A gateway that published a generation fences its prompts: v1."""
        approval = _gateway_runs_approval_event(_canonical_payload())
        assert approval is not None
        assert approval["_gateway_execution_generation"] == 7
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=False):
            assert gateway_chat._gateway_approval_identity_v1(approval, BASE_URL, "") is True

    def test_capability_is_the_fallback(self):
        """No generation, but the gateway advertises the capability: v1."""
        approval = _gateway_runs_approval_event(_legacy_payload())
        assert approval is not None
        assert approval["_gateway_execution_generation"] is None
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=True):
            assert gateway_chat._gateway_approval_identity_v1(approval, BASE_URL, "") is True

    def test_legacy_gateway_stays_legacy(self):
        """No generation and no advertised capability: legacy body."""
        approval = _gateway_runs_approval_event(_legacy_payload())
        assert approval is not None
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=False):
            assert gateway_chat._gateway_approval_identity_v1(approval, BASE_URL, "") is False

    def test_never_promotes_a_local_fallback_id(self):
        """A locally generated fallback id is not canonical identity.

        ``_gateway_runs_approval_event`` mints a uuid when the gateway
        published no id at all. That id was never sent by the gateway, so
        answering with it is a guaranteed 400; it must not be promoted into
        canonical identity even when the capability is advertised.
        """
        approval = _gateway_runs_approval_event({
            "event": "approval.request",
            "run_id": RUN_ID,
            "command": "rm -rf /tmp/x",
            "description": "no id at all",
        })
        assert approval is not None
        assert approval["_gateway_raw_approval_id_present"] is False
        assert approval["approval_id"], "mapper still mints a browser-visible id"
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=True):
            assert gateway_chat._gateway_approval_identity_v1(approval, BASE_URL, "") is False

    def test_no_generation_and_no_raw_id_stays_legacy(self):
        approval = _gateway_runs_approval_event({
            "command": "rm -rf /tmp/x",
            "description": "no id, no generation",
        })
        assert approval is not None
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=False):
            assert gateway_chat._gateway_approval_identity_v1(approval, BASE_URL, "") is False

    def test_helper_is_the_single_definition_used_by_both_origins(self):
        """The relay must not carry a second copy of the decision.

        The maintainer's requirement is one shared creation-time decision; a
        duplicated expression in the relay would let the two origins drift.
        """
        src = (REPO_ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
        relay_start = src.index("def _relay_gateway_run_approval(")
        relay_end = src.index("\ndef ", relay_start + 10)
        relay_body = src[relay_start:relay_end]
        assert "_gateway_approval_identity_v1(" in relay_body, (
            "the Runs API relay must delegate to the shared identity helper"
        )
        assert "_gateway_raw_approval_id_present" not in relay_body, (
            "the relay must not re-derive the identity predicate inline"
        )
        stream_marker = 'if _payload_event in {"hermes.approval.request", "approval.request"}:'
        assert stream_marker in src
        stream_body = src[src.index(stream_marker):]
        assert "_gateway_approval_identity_v1(" in stream_body, (
            "the default chat-completions stream must delegate to the shared identity helper"
        )


# ---------------------------------------------------------------------------
# End to end: default stream -> card mirror -> /api/approval/respond
# ---------------------------------------------------------------------------

class TestDefaultStreamAllowAndDeny:
    """The reported defect: Allow/Deny on a default-stream card."""

    @pytest.mark.parametrize("choice", ["once", "deny"])
    def test_generation_fenced_gateway_settles_the_click(self, choice):
        """A generation-fenced gateway must accept the relayed click.

        This is the reported failure: the card never settles, the run stays
        parked until approvals.timeout (300 s) and then fails closed as deny.
        """
        sid = f"sess-8579-fenced-{choice}"
        stream_id = f"stream-8579-fenced-{choice}"

        events, click_state = _drive_default_stream(sid, stream_id, _canonical_payload())
        card = click_state["card"]
        assert card["approval_id"] == PROMPT_ID
        assert card["run_id"] == RUN_ID
        assert card["_gateway_agent_identity_v1"] is True, (
            "a generation-fenced card must carry the creation-time identity marker; "
            "without it /api/approval/respond posts the legacy body and the gateway "
            "answers 400 invalid_params"
        )

        _restore_click_state(sid, stream_id, click_state)
        mirror = approvals.gateway_pending_mirror(sid, approval_id=PROMPT_ID, run_id=RUN_ID)
        assert mirror is not None, "the card must be mirrored for polling/respond"
        assert mirror["_gateway_agent_identity_v1"] is True

        captured: dict = {}
        handler = _answer(
            sid,
            _click(sid, PROMPT_ID, choice=choice, mirror_token=mirror[approvals._GATEWAY_MIRROR_TOKEN]),
            captured,
        )

        assert captured["url"] == f"{BASE_URL}/v1/runs/{RUN_ID}/approval"
        assert captured["body"] == {
            "request_id": PROMPT_ID,
            "execution_generation": 7,
            "choice": choice,
        }, (
            "the relayed click must use the generation-fenced body the gateway "
            "validates against; the legacy body is a guaranteed 400 invalid_params"
        )
        assert captured["status"] == 200
        assert captured["payload"] == {"ok": True, "choice": choice, "relayed": True}
        assert handler.send_response.call_args[0][0] == 200

    @pytest.mark.parametrize("choice", ["once", "deny"])
    def test_generation_fenced_gateway_settles_without_a_projection_read(self, choice):
        """The card carries its own identity: no click-time status read."""
        sid = f"sess-8579-noread-{choice}"
        stream_id = f"stream-8579-noread-{choice}"
        _events, click_state = _drive_default_stream(sid, stream_id, _canonical_payload())
        _restore_click_state(sid, stream_id, click_state)
        mirror = approvals.gateway_pending_mirror(sid, approval_id=PROMPT_ID, run_id=RUN_ID)
        assert mirror is not None

        captured: dict = {}
        _answer(
            sid,
            _click(sid, PROMPT_ID, choice=choice, mirror_token=mirror[approvals._GATEWAY_MIRROR_TOKEN]),
            captured,
        )
        assert captured["body"] == {
            "request_id": PROMPT_ID,
            "execution_generation": 7,
            "choice": choice,
        }, (
            "the creation-time identity must survive into the relayed body"
        )
        assert "gets" not in captured, (
            f"a canonical card must be answered without a run-projection read: {captured!r}"
        )

    def test_legacy_gateway_keeps_the_legacy_body(self):
        """Backwards compatibility: a no-generation gateway is unchanged."""
        sid = "sess-8579-legacy"
        stream_id = "stream-8579-legacy"
        events, click_state = _drive_default_stream(
            sid, stream_id, _legacy_payload(), caps=False
        )
        card = click_state["card"]
        assert card["approval_id"] == "appr-8579-legacy"
        assert card["_gateway_agent_identity_v1"] is False

        _restore_click_state(sid, stream_id, click_state)
        mirror = approvals.gateway_pending_mirror(
            sid, approval_id="appr-8579-legacy", run_id=RUN_ID
        )
        assert mirror is not None
        assert mirror["_gateway_agent_identity_v1"] is False

        captured: dict = {}
        handler = _answer(
            sid,
            _click(sid, "appr-8579-legacy", mirror_token=mirror[approvals._GATEWAY_MIRROR_TOKEN]),
            captured,
        )
        assert "gets" not in captured
        assert captured["body"] == {"choice": "once", "approval_id": ""}
        assert captured["status"] == 200
        assert captured["payload"] == {"ok": True, "choice": "once", "relayed": True}
        assert handler.send_response.call_args[0][0] == 200

    def test_capability_advertised_gateway_relays_the_published_id(self):
        """A gateway that advertises the capability answers with its own id."""
        sid = "sess-8579-caps"
        stream_id = "stream-8579-caps"
        events, click_state = _drive_default_stream(
            sid, stream_id, _legacy_payload(), caps=True
        )
        assert click_state["card"]["_gateway_agent_identity_v1"] is True

        _restore_click_state(sid, stream_id, click_state)
        mirror = approvals.gateway_pending_mirror(
            sid, approval_id="appr-8579-legacy", run_id=RUN_ID
        )
        assert mirror is not None
        assert mirror["_gateway_agent_identity_v1"] is True

        captured: dict = {}
        handler = _answer(
            sid,
            _click(sid, "appr-8579-legacy", mirror_token=mirror[approvals._GATEWAY_MIRROR_TOKEN]),
            captured,
        )
        # No generation was published, so the body stays the legacy shape but
        # carries the id the gateway itself sent — never a local fallback.
        assert captured["body"] == {"choice": "once", "approval_id": "appr-8579-legacy"}
        assert captured["status"] == 200
        assert handler.send_response.call_args[0][0] == 200

    def test_local_fallback_id_is_relayed_as_legacy_not_as_canonical(self):
        """A card whose id WebUI minted itself is never answered as v1."""
        sid = "sess-8579-fallback"
        stream_id = "stream-8579-fallback"
        payload = _canonical_payload(prompt_id=None, execution_generation=None)
        events, click_state = _drive_default_stream(sid, stream_id, payload, caps=True)

        minted = click_state["card"]["approval_id"]
        assert minted
        assert click_state["card"]["_gateway_agent_identity_v1"] is False, (
            "a locally generated fallback id must not be promoted to canonical identity"
        )

        _restore_click_state(sid, stream_id, click_state)
        mirror = approvals.gateway_pending_mirror(sid, approval_id=minted, run_id=RUN_ID)
        assert mirror is not None

        captured: dict = {}
        handler = _answer(
            sid,
            _click(sid, minted, mirror_token=mirror[approvals._GATEWAY_MIRROR_TOKEN]),
            captured,
        )
        assert captured["body"] == {"choice": "once", "approval_id": ""}
        assert captured["status"] == 200
        assert handler.send_response.call_args[0][0] == 200


# ---------------------------------------------------------------------------
# Preserved invariants named by the maintainer
# ---------------------------------------------------------------------------

class TestDefaultStreamPreservesExistingBehaviour:
    """Stream/run registration, pending-queue ownership, YOLO handling."""

    def test_stream_run_registration_is_preserved(self):
        """#4549 follow-up: the run_id must still land in _STREAM_RUN_IDS."""
        sid = "sess-8579-registration"
        stream_id = "stream-8579-registration"
        _events, click_state = _drive_default_stream(sid, stream_id, _canonical_payload())
        assert click_state.get("run_id") == RUN_ID, (
            "the default stream must keep recording the gateway run_id so the "
            "response can be relayed back and the parked run resumed"
        )

    def test_pending_queue_ownership_is_preserved(self):
        """The card stays a run-backed mirror with its own token and count."""
        sid = "sess-8579-ownership"
        stream_id = "stream-8579-ownership"
        events, click_state = _drive_default_stream(sid, stream_id, _canonical_payload())
        assert click_state["card"]["pending_count"] == 1

        entry = click_state["mirror_entry"]
        assert entry is not None
        assert entry.get(approvals._GATEWAY_MIRROR_FLAG) is True
        assert str(entry.get("run_id") or "").strip() == RUN_ID
        assert str(entry.get(approvals._GATEWAY_MIRROR_TOKEN) or "").strip(), (
            "a run-backed mirror must keep its own ownership token"
        )
        assert entry.get("_gateway_agent_identity_v1") is True

        _restore_click_state(sid, stream_id, click_state)
        with approvals._lock:
            queue = approvals._pending.get(sid)
        assert isinstance(queue, list) and len(queue) == 1, (
            "the parked card must stay the single head of the session queue"
        )
        live = queue[0]
        assert live.get(approvals._GATEWAY_MIRROR_FLAG) is True
        assert str(live.get("run_id") or "").strip() == RUN_ID
        assert str(live.get(approvals._GATEWAY_MIRROR_TOKEN) or "").strip()
        assert live.get("_gateway_agent_identity_v1") is True

        # The exact (approval_id, run_id, mirror_token) triple is what the
        # browser posts; it must resolve to this mirror and no other.
        mirror = approvals.gateway_pending_mirror(
            sid,
            approval_id=PROMPT_ID,
            run_id=RUN_ID,
            mirror_token=live[approvals._GATEWAY_MIRROR_TOKEN],
        )
        assert mirror is not None
        assert mirror["approval_id"] == PROMPT_ID
        assert mirror["run_id"] == RUN_ID
        assert mirror["_gateway_agent_identity_v1"] is True

        # A different token must not resolve it: ownership is per-mirror.
        assert approvals.gateway_pending_mirror(
            sid, approval_id=PROMPT_ID, run_id=RUN_ID, mirror_token="not-the-token"
        ) is None

    def test_no_run_payload_still_produces_a_local_card(self):
        """A stream approval without run_id keeps the local, no-run shape."""
        sid = "sess-8579-norun"
        stream_id = "stream-8579-norun"
        payload = _legacy_payload(run_id=None)
        events, click_state = _drive_default_stream(sid, stream_id, payload, caps=False)

        card = click_state["card"]
        assert card["approval_id"] == "appr-8579-legacy"
        assert card["_gateway_agent_identity_v1"] is False
        assert not str(card.get("run_id") or "").strip()
        assert click_state.get("run_id") is None

    def test_yolo_click_on_a_default_stream_card_relays_the_generation(self):
        """Session YOLO on a default-stream card keeps the same contract."""
        sid = "sess-8579-yolo"
        stream_id = "stream-8579-yolo"
        _events, click_state = _drive_default_stream(sid, stream_id, _canonical_payload())
        _restore_click_state(sid, stream_id, click_state)
        mirror = approvals.gateway_pending_mirror(sid, approval_id=PROMPT_ID, run_id=RUN_ID)
        assert mirror is not None

        posts = []
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        def fake_request_json(_self, req, **_kwargs):
            posts.append(json.loads(req.data))
            return {"ok": True}

        relay_session = SimpleNamespace(active_stream_id=stream_id)
        with patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json), \
             patch("api.gateway_chat._gateway_base_url", return_value=BASE_URL), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.routes.get_session", return_value=relay_session):
            routes._handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "once",
                "approval_id": PROMPT_ID,
                "run_id": RUN_ID,
                "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
                "yolo": True,
            })

        assert posts, "the YOLO click must still be relayed to the gateway run"
        assert posts[0] == {
            "request_id": PROMPT_ID,
            "execution_generation": 7,
            "choice": "once",
        }


# ---------------------------------------------------------------------------
# The Runs API origin (the already-correct implementation) must not regress
# ---------------------------------------------------------------------------

class TestRunsApiOriginUnchanged:
    """The shared helper refactor must not move the Runs API path."""

    def _relay(self, payload, *, caps=False, sid="sess-8579-runs"):
        events = []
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=caps):
            assert _relay_gateway_run_approval(
                sid, RUN_ID, payload, BASE_URL, "",
                put_gateway_event=lambda name, data: events.append((name, data)),
            )
        return events

    @pytest.mark.parametrize("choice", ["once", "deny"])
    def test_generation_fenced_card_still_relays_exactly(self, choice):
        sid = f"sess-8579-runs-fenced-{choice}"
        events = self._relay(_canonical_payload(), sid=sid)
        assert events and events[0][0] == "approval"
        assert events[0][1]["_gateway_agent_identity_v1"] is True

        head, _total = approvals.submit_gateway_pending_mirror(sid, dict(events[0][1]))
        assert head is not None
        mirror = approvals.gateway_pending_mirror(sid, approval_id=PROMPT_ID, run_id=RUN_ID)
        assert mirror is not None
        assert mirror["_gateway_agent_identity_v1"] is True

        captured: dict = {}
        handler = _answer(
            sid,
            _click(sid, PROMPT_ID, choice=choice, mirror_token=mirror[approvals._GATEWAY_MIRROR_TOKEN]),
            captured,
        )
        assert captured["body"] == {
            "request_id": PROMPT_ID,
            "execution_generation": 7,
            "choice": choice,
        }
        assert captured["status"] == 200
        assert captured["payload"] == {"ok": True, "choice": choice, "relayed": True}
        assert handler.send_response.call_args[0][0] == 200

    def test_legacy_card_still_relays_the_legacy_body(self):
        events = self._relay(_legacy_payload())
        assert events[0][1]["_gateway_agent_identity_v1"] is False

    def test_capability_advertised_card_still_relays_the_published_id(self):
        events = self._relay(_legacy_payload(), caps=True)
        assert events[0][1]["_gateway_agent_identity_v1"] is True

    def test_local_fallback_id_is_not_canonical(self):
        payload = {
            "event": "approval.request",
            "run_id": RUN_ID,
            "command": "echo hi",
            "description": "no id at all",
        }
        events = self._relay(payload, caps=True)
        assert events[0][1]["_gateway_raw_approval_id_present"] is False
        assert events[0][1]["_gateway_agent_identity_v1"] is False

    def test_translator_rejection_still_returns_false(self):
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=False):
            assert _relay_gateway_run_approval(
                "sess-8579-runs-reject", RUN_ID, {"risk_level": "high"}, BASE_URL, "",
                put_gateway_event=lambda _n, _p: None,
            ) is False


# ---------------------------------------------------------------------------
# The status / replay origin goes through the same relay
# ---------------------------------------------------------------------------

class TestStatusOriginSharesTheDecision:
    """A card rebuilt from durable run status uses the same relay."""

    def test_status_projection_card_carries_the_marker(self):
        sid = "sess-8579-status"
        events = []
        status_approval = {
            # The session bridge answers status["approval"] with the latest
            # pending prompt, in the canonical (prompt_id + generation) shape.
            "prompt_id": PROMPT_ID,
            "execution_generation": 7,
            "tool": "shell",
            "command": "rm -rf /tmp/x",
            "description": "delete temp dir",
            "choices": ["once", "deny"],
        }
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=False):
            assert _relay_gateway_run_approval(
                sid, RUN_ID, status_approval, BASE_URL, "",
                put_gateway_event=lambda name, data: events.append((name, data)),
            )
        assert events and events[0][0] == "approval"
        assert events[0][1]["_gateway_agent_identity_v1"] is True
        assert events[0][1]["approval_id"] == PROMPT_ID

    def test_status_projection_without_identity_stays_legacy(self):
        sid = "sess-8579-status-legacy"
        events = []
        status_approval = {
            "approval_id": "appr-8579-status-legacy",
            "tool": "shell",
            "command": "rm -rf /tmp/x",
            "description": "delete temp dir",
        }
        with patch("api.config.gateway_supports_approval_identity_v1", return_value=False):
            assert _relay_gateway_run_approval(
                sid, RUN_ID, status_approval, BASE_URL, "",
                put_gateway_event=lambda name, data: events.append((name, data)),
            )
        assert events[0][1]["_gateway_agent_identity_v1"] is False
