"""Generation-fenced approval relay: the canonical Runs API prompt shape.

A gateway with a session authority publishes an approval as
``{prompt_id|request_id, execution_generation, ...}`` and accepts exactly
``{request_id, execution_generation, choice}`` on
``POST /v1/runs/{id}/approval``. The WebUI used to read only ``approval_id``/
``id``, substitute a random uuid, and answer with ``{choice, approval_id}``,
so every click was rejected with ``400 invalid_params``.

These tests pin the wire shapes on both sides of the relay: the canonical
(generation) shape and the legacy (approval_id) shape must both survive.
"""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

import api.route_approvals as approvals
from api import routes
from api.gateway_chat import (
    _gateway_runs_approval_event,
    _relay_gateway_run_approval,
)
from api.runner_client import HttpRunnerClient

SID = "sess-generation-approval"
RUN_ID = "run-generation-approval"
PROMPT_ID = "prompt-generation-approval"


def _canonical_payload(**overrides):
    """The exact shape a generation-fenced gateway emits (#51871 contract)."""
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


def _relay(payload, *, put_event=None):
    with patch(
        "api.config.gateway_supports_approval_identity_v1", return_value=False
    ):
        return _relay_gateway_run_approval(
            SID,
            RUN_ID,
            payload,
            "http://gw:8642",
            "",
            put_gateway_event=put_event or (lambda _name, _payload: None),
        )


def _answer(
    body,
    captured,
    *,
    run_projection=None,
    run_status=None,
    respond_reject=None,
):
    """Drive the real answer handler with the gateway wire faked.

    ``run_status`` non-None makes the run projection GET fail: an int is an HTTP
    status, ``"timeout"`` is a transport failure (no status at all).
    ``respond_reject`` is ``(http_status, code)`` for a rejected POST, and the
    code travels structurally, exactly as ``_request_json`` parses it.
    """

    def fake_request_json(_self, req, **_kwargs):
        if req.method == "GET":
            captured.setdefault("gets", []).append(req.full_url)
            if run_status == "timeout":
                from api.runner_client import RunnerClientError

                raise RunnerClientError("Runner request failed: timed out")
            if run_status:
                from api.runner_client import RunnerClientError

                raise RunnerClientError(
                    f"Runner returned HTTP {run_status}: {{}}", status=run_status
                )
            return run_projection or {}
        answer = json.loads(req.data)
        captured.setdefault("posts", []).append(answer)
        captured["url"] = req.full_url
        captured["body"] = answer
        if respond_reject:
            from api.runner_client import RunnerClientError

            status, code = respond_reject
            raise RunnerClientError(
                f'Runner returned HTTP {status}: {{"error": {{"code": "{code}"}}}}',
                status=status,
                code=code,
            )
        return {"ok": True}

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json):
        routes._handle_approval_respond(handler, body)
    return handler


def setup_function():
    with approvals._lock:
        approvals._pending.pop(SID, None)
        approvals._gateway_queues.pop(SID, None)


def teardown_function():
    with approvals._lock:
        approvals._pending.pop(SID, None)
        approvals._gateway_queues.pop(SID, None)


# ---------------------------------------------------------------------------
# Mapper: keep the prompt identity and its generation
# ---------------------------------------------------------------------------

def test_mapper_keeps_prompt_id_and_execution_generation():
    event = _gateway_runs_approval_event(_canonical_payload())

    assert event is not None
    assert event["approval_id"] == PROMPT_ID
    assert event["_gateway_raw_approval_id_present"] is True
    assert event["_gateway_execution_generation"] == 7


def test_mapper_accepts_request_id_alias_used_by_the_session_bridge():
    event = _gateway_runs_approval_event(
        _canonical_payload(prompt_id=None, request_id="req-bridge-1")
    )

    assert event is not None
    assert event["approval_id"] == "req-bridge-1"
    assert event["_gateway_execution_generation"] == 7


def test_mapper_legacy_payload_is_unchanged():
    event = _gateway_runs_approval_event({
        "approval_id": "appr-legacy",
        "command": "echo hi",
        "description": "legacy approval",
    })

    assert event is not None
    assert event["approval_id"] == "appr-legacy"
    assert event["_gateway_raw_approval_id_present"] is True
    assert event["_gateway_execution_generation"] is None


def test_mapper_ignores_non_integer_generation():
    event = _gateway_runs_approval_event(
        _canonical_payload(execution_generation="7")
    )

    assert event is not None
    assert event["_gateway_execution_generation"] is None


# ---------------------------------------------------------------------------
# Client: pick the body shape from what the gateway published
# ---------------------------------------------------------------------------

def test_respond_approval_sends_exactly_the_generation_fenced_body():
    client = HttpRunnerClient(base_url="http://gw:8642")
    captured = {}

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body
        return {}

    client._post = fake_post
    client.respond_approval(RUN_ID, PROMPT_ID, "once", execution_generation=7)

    assert captured["url"] == f"/v1/runs/{RUN_ID}/approval"
    # Exactly the three keys the gateway validates against; an extra key such
    # as ``approval_id`` is a hard 400 on the canonical endpoint.
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }


def test_respond_approval_without_generation_keeps_the_legacy_body():
    client = HttpRunnerClient(base_url="http://gw:8642")
    captured = {}

    def fake_post(url, body):
        captured["body"] = body
        return {}

    client._post = fake_post
    client.respond_approval(RUN_ID, "appr-legacy", "deny")

    assert captured["body"] == {"choice": "deny", "approval_id": "appr-legacy"}


# ---------------------------------------------------------------------------
# End to end: event payload -> card mirror -> HTTP answer
# ---------------------------------------------------------------------------

def test_canonical_event_is_answered_with_generation_fenced_body():
    """The reported defect: a real gateway payload must not produce a 400."""
    cards = []
    assert _relay(_canonical_payload(), put_event=lambda n, p: cards.append((n, p)))

    mirror = approvals.gateway_pending_mirror(
        SID, approval_id=PROMPT_ID, run_id=RUN_ID
    )
    assert mirror is not None, "the canonical approval must surface as one card"
    assert mirror["approval_id"] == PROMPT_ID

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert captured["url"].endswith(f"/v1/runs/{RUN_ID}/approval")
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }
    assert handler.send_response.call_args[0][0] == 200


def test_legacy_click_posts_at_once_without_a_status_read():
    """Backwards compatibility, and no click-time status read (review row 5).

    A gateway that publishes no generation is answered with the legacy body
    immediately. Resolving identity here instead would park the POST behind a
    run-projection GET whose 60 s timeout outlives the browser's own 30 s.
    """
    payload = {
        "event": "approval.request",
        "run_id": RUN_ID,
        "approval_id": "appr-legacy-1",
        "command": "echo hi",
        "description": "legacy approval",
        "choices": ["once", "deny"],
    }
    assert _relay(payload)
    mirror = approvals.gateway_pending_mirror(
        SID, approval_id="appr-legacy-1", run_id=RUN_ID
    )
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": "appr-legacy-1",
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert "gets" not in captured, (
        f"a legacy click must be answered without a run-projection read: {captured!r}")
    assert captured["body"] == {"choice": "once", "approval_id": ""}
    assert handler.send_response.call_args[0][0] == 200


def test_yolo_auto_approve_also_carries_the_generation():
    """The sibling path: session YOLO answers without a card, same contract."""
    captured = {}

    def fake_request_json(_self, req, **_kwargs):
        captured["body"] = json.loads(req.data)
        return {"ok": True}

    with patch(
        "api.config.gateway_supports_approval_identity_v1", return_value=False
    ), patch(
        "api.gateway_chat._gateway_session_yolo_enabled", return_value=True
    ), patch(
        "api.runner_client.HttpRunnerClient._request_json", new=fake_request_json
    ):
        assert _relay_gateway_run_approval(
            SID,
            RUN_ID,
            _canonical_payload(),
            "http://gw:8642",
            "",
            put_gateway_event=lambda _n, _p: None,
        )

    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }


# ---------------------------------------------------------------------------
# Answering a card the UI rebuilt itself, and the stale/ambiguous rejections
# ---------------------------------------------------------------------------

def test_stale_card_never_answers_the_prompt_that_replaced_it():
    """Review row 2, the must-fix: a stale card must not answer prompt B.

    Card A is on screen, and the run has since parked prompt B. A's click
    reaches the gateway as A's own id and is rejected 409; the projection then
    names B. Adopting that single pending prompt — the pre-fix behaviour —
    approved B with a click the user made on A. Here A settles as stale and B is
    never posted with the user's choice.
    """
    assert _relay(_canonical_payload(prompt_id="prompt-A", execution_generation=7))
    mirror = approvals.gateway_pending_mirror(SID, approval_id="prompt-A", run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": "prompt-A",
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={
            "status": "waiting_for_approval",
            "pending_controls": [
                {
                    "kind": "approval",
                    "prompt_id": "prompt-B",
                    "execution_generation": 8,
                    "choices": ["once", "deny"],
                }
            ],
        },
        respond_reject=(409, "approval_not_pending"),
    )

    answered = [answer.get("request_id") for answer in captured.get("posts", [])]
    assert answered == ["prompt-A"], (
        f"only the card's own prompt may be answered, got {captured.get('posts')!r}")
    assert handler.send_response.call_args[0][0] == 200
    assert b'"already_resolved": true' in handler.wfile.getvalue()


@pytest.mark.parametrize("run_status", ["timeout", 404, 409])
def test_unverifiable_projection_keeps_the_card_and_reports_the_error(run_status):
    """Review row 3: a failed refresh is a failure, never ``already_resolved``.

    The projection read used to answer ``None`` for both "read failed" and "this
    prompt is gone", so a timeout, a 404 or an unrelated 409 became
    ``200 already_resolved`` and the mirror was retired without an accepted
    answer.
    """
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_status=run_status,
        respond_reject=(409, "approval_not_pending"),
    )

    assert handler.send_response.call_args[0][0] == 502, handler.wfile.getvalue()
    assert b"gateway_approval_relay_failed" in handler.wfile.getvalue()
    assert approvals.gateway_pending_mirror(
        SID, approval_id=PROMPT_ID, run_id=RUN_ID
    ) is not None, "the card must survive an unverified rejection"
    assert len(captured.get("posts", [])) == 1, captured


def test_ambiguous_projection_is_not_reported_as_success():
    """Review must-fix 2, ambiguity arm: several prompts parked, none is this card's.

    The projection cannot say whether this card was answered, so the mirror
    stays and the rejection is reported instead of settling the card.
    """
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None
    pending = [
        {
            "kind": "approval",
            "prompt_id": f"prompt-other-{generation}",
            "execution_generation": generation,
            "choices": ["once", "deny"],
        }
        for generation in (8, 9)
    ]

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={"status": "waiting_for_approval", "pending_controls": pending},
        respond_reject=(409, "approval_not_pending"),
    )

    assert handler.send_response.call_args[0][0] == 502, handler.wfile.getvalue()
    assert approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID) is not None


def test_unreadable_pending_prompt_is_not_reported_as_success():
    """A parked prompt with no readable generation must not settle the card.

    A mixed projection lists an approval but omits ``execution_generation``.
    Filtering that entry out before the "is it gone?" decision left an empty
    remainder, so the read succeeded into ``superseded`` and the click returned
    ``200 already_resolved`` for a prompt the run was still holding.
    """
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={
            "status": "waiting_for_approval",
            "pending_controls": [
                {"kind": "approval", "prompt_id": PROMPT_ID, "choices": ["once", "deny"]}
            ],
        },
        respond_reject=(409, "approval_not_pending"),
    )

    assert handler.send_response.call_args[0][0] == 502, handler.wfile.getvalue()
    assert approvals.gateway_pending_mirror(
        SID, approval_id=PROMPT_ID, run_id=RUN_ID
    ) is not None, "an unreadable projection must keep the card"


def test_republished_prompt_at_a_new_generation_replaces_its_card():
    """Review row 4: the same prompt id at a new generation is a new prompt.

    The dedupe key carries the generation, so the replay is relayed instead of
    suppressed, and the mirror is replaced (new payload, new token) rather than
    left pointing at the dead generation.
    """
    from api.gateway_chat import _gateway_approval_key

    assert _gateway_approval_key(
        _canonical_payload(execution_generation=7)
    ) != _gateway_approval_key(
        _canonical_payload(execution_generation=8)
    ), "a re-published generation must not dedupe against the old one"

    assert _relay(_canonical_payload(execution_generation=7))
    first = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert first is not None
    assert first[approvals._GATEWAY_EXECUTION_GENERATION] == 7

    cards = []
    assert _relay(
        _canonical_payload(execution_generation=8, command="echo second"),
        put_event=lambda name, payload: cards.append((name, payload)),
    )

    mirrors = [
        entry
        for entry in approvals._pending.get(SID, [])
        if entry.get(approvals._GATEWAY_MIRROR_FLAG)
    ]
    assert len(mirrors) == 1, f"the obsolete mirror must be replaced, got {mirrors!r}"
    assert mirrors[0][approvals._GATEWAY_EXECUTION_GENERATION] == 8
    assert mirrors[0][approvals._GATEWAY_MIRROR_TOKEN] != first[approvals._GATEWAY_MIRROR_TOKEN]
    assert cards and cards[0][1]["approval_id"] == PROMPT_ID


def test_choice_the_gateway_does_not_offer_is_refused_before_it_is_sent():
    """A stale card's "session" button must not become a gateway 400."""
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "session",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert "body" not in captured, "an unofferable choice must not reach the gateway"
    assert handler.send_response.call_args[0][0] == 400
    assert b"approval_choice_not_offered" in handler.wfile.getvalue()


def test_canonical_card_answers_without_a_projection_refresh():
    """The fast path stays a single POST: no extra GET on a well-formed card."""
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "deny",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert "gets" not in captured
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "deny",
    }
    assert handler.send_response.call_args[0][0] == 200


def test_replay_of_an_already_answered_prompt_is_idempotent():
    """A double click must not paint 502 on an approval that already worked.

    The first answer resumes the run and consumes the prompt; the second POST
    is rejected with 409 ``approval_not_pending`` and the run has no control
    left to re-project, so the only correct reading is "already done".
    """
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={"status": "running", "pending_controls": []},
        respond_reject=(409, "approval_not_pending"),
    )

    assert handler.send_response.call_args[0][0] == 200
    assert b'"already_resolved": true' in handler.wfile.getvalue()
    assert approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID) is None


@pytest.mark.parametrize(
    "run_projection",
    [
        # The installed Agent's legacy runs bridge: projects ``approval``, not
        # ``pending_controls``, and does emit 409 approval_not_pending.
        {"status": "waiting_for_approval", "approval": {"request_id": PROMPT_ID, "choices": ["once", "deny"]}},
        {"status": "running"},
        {"status": "waiting_for_approval", "pending_controls": None},
        {"status": "waiting_for_approval", "pending_controls": {"kind": "approval", "prompt_id": PROMPT_ID}},
    ],
    ids=["legacy-approval-shape", "status-only", "controls-null", "controls-not-a-list"],
)
def test_projection_without_a_controls_list_keeps_the_card(run_projection):
    """Release-gate fold: only a readable ``pending_controls`` list that omits
    this prompt proves it was answered. A projection with no list must keep the
    card and report the rejection instead of ``200 already_resolved``."""
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection=run_projection,
        respond_reject=(409, "approval_not_pending"),
    )

    assert handler.send_response.call_args[0][0] == 502, handler.wfile.getvalue()
    assert b"already_resolved" not in handler.wfile.getvalue()
    assert approvals.gateway_pending_mirror(
        SID, approval_id=PROMPT_ID, run_id=RUN_ID
    ) is not None, "a projection without a controls list must keep the card"


def test_empty_controls_list_still_settles_as_already_resolved():
    """Control for the fold above: an authority projection that reads fine and
    lists no pending prompt still proves the answer is in effect."""
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={"status": "running", "pending_controls": []},
        respond_reject=(409, "approval_not_pending"),
    )

    assert handler.send_response.call_args[0][0] == 200, handler.wfile.getvalue()
    assert b'"already_resolved": true' in handler.wfile.getvalue()
