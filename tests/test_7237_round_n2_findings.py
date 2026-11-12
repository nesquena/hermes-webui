"""Regression tests for the round-N+2 review findings on PR #7237
(fix(context): drop orphan tool_calls before they poison a session).

Reviewer: nesquena-hermes, CHANGES_REQUESTED 2026-10-05 21:48.

The fail-closed authority gate from the previous head refused GENUINE
production behaviour when it gained ``compression_authorized`` authority.
These tests prove the four core fixes:

1. ``_current_turn_compression_rotation`` accepts real compressor output:
   with producer authority, a compartment card of ANY role at ANY position
   (the installed compressor emits a *user*-role summary when a fresh
   rotation replaces the whole head, and an assistant summary after a
   protected head) must be accepted for wholesale replacement — no positional
   or role filter may refuse it.

2. The sync ``/api/chat`` path (``_handle_chat_sync``) derives the producer
   signal (Agent session-id rotation) and threads ``compression_authorized``
   into the settle, so a genuine compression persists the compacted list
   rather than the old history.

3. A follow-up after an unanswered user tail is a VERIFIED TAIL-MERGE: the
   installed Agent merges consecutive user rows, so a non-exact projection
   whose preceding rows match exactly (and whose merged boundary is validated
   against the submitted prompt) settles the cleaned current exchange instead
   of failing closed and losing the reply.

4. ``_sanitize_messages_for_api`` keeps the merge-visible discriminators on
   the internal Agent replay projection (``preserve_api_content=True``) and
   strips them only from direct provider projections, so the Agent's own
   repair cannot merge historical assistant rows differently than the
   projection this process sent.
"""
import copy
import sys
from types import SimpleNamespace

from api.streaming import (
    _current_turn_compression_rotation,
    _dedupe_replayed_context_messages,
    _sanitize_messages_for_agent,
    _sanitize_messages_for_api,
    _settle_result_messages,
)


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def _no_id(rows):
    return [{k: v for k, v in m.items() if k != "id"} for m in rows]


def _role_content(rows):
    return [(m.get("role"), m.get("content")) for m in rows]


def _marker(content="summary card"):
    return {
        "role": "assistant",
        "content": f"[CONTEXT COMPACTION — REFERENCE ONLY] {content}",
    }


class TestFinding1AuthorizedGenuineRotationAccepted:
    """Finding 1: authorized compression must be accepted in BOTH real shapes.

    The installed ``ContextCompressor`` emits the summary *after* a protected
    head (index 3, assistant role) for a first compression, and at index 0 as
    a USER-role row for a later compression whose head protection decayed
    (``compress_start == 0`` forces ``force_user_leading``). The previous head
    dropped user-role markers and demanded ``markers[0][0] == 0``, refusing
    both.
    """

    PROMPT = "third question"

    @staticmethod
    def _projection_raw():
        # A protected head (user, assistant, user) that survives sanitizing.
        return [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]

    @staticmethod
    def _projection():
        return _sanitize_messages_for_agent(copy.deepcopy(TestFinding1AuthorizedGenuineRotationAccepted._projection_raw()))

    def test_user_role_marker_at_index_zero_authorized_is_rotation(self):
        # Decayed-head shape: compress_start == 0 -> user-leading summary.
        result = [
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] whole arc"},
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "the answer"},
        ]
        assert _current_turn_compression_rotation(
            result,
            self._projection(),
            self.PROMPT,
            compression_authorized=True,
        ), (
            "authorized decayed-head compression (user-role marker at index 0) "
            "must be accepted for wholesale replacement — the old role filter "
            "dropped every user-role marker and refused it"
        )

    def test_protected_head_assistant_marker_at_index_three_is_rotation(self):
        # Protected-head shape: head kept verbatim (3 rows), summary lands at
        # index 3 as an assistant row.
        projected = self._projection()
        result = list(projected) + [
            _marker("after protected head"),
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "the answer"},
        ]
        assert _current_turn_compression_rotation(
            result, projected, self.PROMPT, compression_authorized=True,
        ), (
            "authorized protected-head compression (assistant marker at index 3) "
            "must be wholesale — the old markers[0][0] == 0 position gate "
            "refused it and re-sent oversized history next turn"
        )

    def test_unauthorized_identical_user_marker_fails_closed(self):
        result = [
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] user arc"},
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "the answer"},
        ]
        assert not _current_turn_compression_rotation(
            result, self._projection(), self.PROMPT, compression_authorized=False,
        ), "without producer authority even a genuine-looking marker must fail closed"

    def test_authorized_no_marker_is_still_a_rotation(self):
        """#7237 round 3 finding 4, REVERSED: marker presence is not authority.

        This test previously asserted the opposite — that an authorized return
        with no ``[CONTEXT COMPACTION`` card must fail closed. The reviewer
        disproved that against the real producer: the installed
        ``ContextCompressor.compress()`` assembles its summary as an assistant
        ``[PRIOR CONTEXT ...]`` carrier, which is not spelled that way, so the
        spelling check refused a compression that had genuinely run (7 returned
        rows became 25 persisted old rows, answer absent).

        Authority comes from the producer, never from marker presence.
        """
        result = [
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "the answer"},
        ]
        assert _current_turn_compression_rotation(
            result, self._projection(), self.PROMPT, compression_authorized=True,
        ), (
            "producer authority must be enough on its own; requiring a "
            "[CONTEXT COMPACTION card refuses real [PRIOR CONTEXT] summaries"
        )

    def test_unauthorized_no_marker_still_fails_closed(self):
        """The negative control: no producer authority, no rotation — ever."""
        result = [
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "the answer"},
        ]
        assert not _current_turn_compression_rotation(
            result, self._projection(), self.PROMPT, compression_authorized=False,
        ), "without producer authority nothing may be treated as a rotation"

    def test_authorized_protected_head_full_settle_wholesales(self):
        projected = self._projection()
        result = list(projected) + [
            _marker("after protected head"),
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "the answer"},
        ]
        session = SimpleNamespace(
            session_id="sid-auth-protected",
            messages=[copy.deepcopy(m) for m in projected],
            context_messages=list(projected),
            title="t",
            profile=None,
        )
        _settle_result_messages(
            session, list(projected), list(projected), list(result),
            self.PROMPT, "cli", None,
            projected_history=list(projected),
            compression_authorized=True,
        )
        assert _role_content(_no_id(session.context_messages)) == _role_content(
            _no_id(result)
        ), (
            "authorized protected-head compression must settle the compacted "
            "list wholesale, not keep the raw history + lose the answer"
        )


class TestSyncPathPassesCompressionAuthorized:
    """Finding 2: sync /api/chat must thread producer authority into the settle.

    Previously ``_handle_chat_sync`` always called
    ``_dedupe_replayed_context_messages`` without ``compression_authorized``,
    so a genuine compression there preserved only the old history. The handler
    now derives the producer signal (Agent session-id rotation across the
    call) and passes it.
    """

    def test_sync_path_accepts_authorized_compression(self, tmp_path, monkeypatch):
        import api.config as config
        import api.models as models
        import api.routes as routes

        state_dir = tmp_path / "state"
        session_dir = state_dir / "sessions"
        session_dir.mkdir(parents=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
        monkeypatch.setattr(routes, "get_session", models.get_session)
        monkeypatch.setattr(routes, "title_from", models.title_from)
        monkeypatch.setattr(
            config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"}
        )
        monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
        monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
        monkeypatch.setattr(routes, "load_settings", lambda: {})
        monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

        raw = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ]
        session = models.Session(
            session_id="sync_compression_authorized",
            workspace=str(tmp_path),
            messages=list(raw),
            context_messages=list(raw),
            model="test-model",
            model_provider="test-provider",
        )
        session.save(touch_updated_at=False)

        compacted = [
            _marker("genuine sync compression"),
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "compacted answer"},
        ]

        class FakeAgent:
            def __init__(self, session_id=None, **_kwargs):
                self.session_id = session_id or "rotated-session-9"
                self._initial = self.session_id

            def run_conversation(self, **_kwargs):
                # The Agent genuinely compressed: rotation id advanced.
                self.session_id = "rotated-session-9new"
                return {"messages": [copy.deepcopy(m) for m in compacted]}

        monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

        class _Handler:
            status = None
            headers = {}
            body = bytearray()
            wfile = None

            def __init__(self):
                self.wfile = self

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name] = value

            def end_headers(self):
                pass

            def write(self, data):
                self.body.extend(data)

        handler = _Handler()
        routes._handle_chat_sync(
            handler,
            {
                "session_id": session.session_id,
                "message": "follow-up",
                "workspace": str(tmp_path),
            },
        )
        assert handler.status == 200, handler.status

        reloaded = models.Session.load(session.session_id)
        assert reloaded is not None
        persisted = reloaded.context_messages
        assert _role_content(_no_id(persisted)) == _role_content(_no_id(compacted)), (
            "a genuine sync compression must persist the compacted context "
            "wholesale; the old handler never passed compression_authorized "
            f"so it kept the old history ({_role_content(persisted)})"
        )

    def test_sync_path_without_rotation_fails_closed(self, tmp_path, monkeypatch):
        import api.config as config
        import api.models as models
        import api.routes as routes

        state_dir = tmp_path / "state"
        session_dir = state_dir / "sessions"
        session_dir.mkdir(parents=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
        monkeypatch.setattr(routes, "get_session", models.get_session)
        monkeypatch.setattr(routes, "title_from", models.title_from)
        monkeypatch.setattr(
            config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"}
        )
        monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
        monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
        monkeypatch.setattr(routes, "load_settings", lambda: {})
        monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

        raw = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ]
        session = models.Session(
            session_id="sync_no_rotation",
            workspace=str(tmp_path),
            messages=list(raw),
            context_messages=list(raw),
            model="test-model",
            model_provider="test-provider",
        )
        session.save(touch_updated_at=False)

        # NO session-id rotation: the Agent did NOT compress, yet it returns the
        # compacted-shaped list. Without producer authority that full-history
        # divergent shape must NOT wholesale-replace the raw context (fail
        # closed) — authority comes only from the rotation signal.
        result = [
            _marker("unproven popped-in card"),
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "compacted answer"},
        ]

        class FakeAgent:
            def __init__(self, session_id=None, **_kwargs):
                self.session_id = session_id or "static-session"

            def run_conversation(self, **_kwargs):
                # No rotation -> compression_authorized stays False.
                return {"messages": [copy.deepcopy(m) for m in result]}

        monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

        class _Handler:
            status = None
            headers = {}
            body = bytearray()

            def __init__(self):
                self.wfile = self

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name] = value

            def end_headers(self):
                pass

            def write(self, data):
                self.body.extend(data)

        handler = _Handler()
        routes._handle_chat_sync(
            handler,
            {
                "session_id": session.session_id,
                "message": "follow-up",
                "workspace": str(tmp_path),
            },
        )
        assert handler.status == 200, handler.status
        reloaded = models.Session.load(session.session_id)
        persisted = reloaded.context_messages
        # Without compression authority, an unproven divergent marker-less
        # rotation may not overwrite the raw pre-turn context (fail closed).
        assert _role_content(_no_id(persisted)) == _role_content(_no_id(raw)), (
            "a non-rotation sync turn must fail closed, preserving the raw "
            "context, not persist the returned full-conversation list"
        )


class TestVerifiedTailMergeAfterUnansweredTail:
    """Finding 3: a follow-up after an unanswered user tail must keep the reply.

    The installed Agent merges consecutive user rows, so when the user sends a
    follow-up after an unanswered tail, the returned conversation folds the
    tail into the current user row. The projection-authority gate used to fail
    closed there (a non-exact prefix), discarding the turn's reply on the
    streaming path. This tests the verified tail-merge path.
    """

    def test_repeated_tail_tested_direct(self):
        proj = _sanitize_messages_for_agent(
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "the stale tail"},
            ]
        )
        # The returned list: projection rows (tail included) but the final user
        # row is replaced by a merged one carrying the tail + the submitted
        # prompt, then the reply.
        result = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "the stale tail\n\nfollow-up prompt"},
            {"role": "assistant", "content": "THE FOLLOW-UP REPLY"},
        ]
        from api.streaming import _verified_tail_merge_result

        current, protected = _verified_tail_merge_result(
            proj,
            result,
            "follow-up prompt",
            "the stale tail",
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "the stale tail"},
            ],
        )
        assert current is not None, "verified tail-merge must be recognised"
        assert any(
            m.get("role") == "assistant" and m.get("content") == "THE FOLLOW-UP REPLY"
            for m in current
        ), "the follow-up reply must be kept"

    def test_dedupe_settle_keeps_follow_up_reply(self):
        proj = _sanitize_messages_for_agent(
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "unanswered tail"},
            ]
        )
        raw = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "unanswered tail"},
        ]
        result = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "unanswered tail\n\nfollow-up"},
            {"role": "assistant", "content": "THE FOLLOW-UP REPLY"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result), "follow-up",
            projected_history=list(proj),
        )
        assert any(
            m.get("content") == "THE FOLLOW-UP REPLY"
            for m in settled
        ), (
            "the verified tail-merge must persist the follow-up reply, not "
            "fail closed to raw history and drop it (finding 3)"
        )

    def test_unrelated_non_exact_still_fails_closed(self):
        # A fully divergent (NOT trailing-merged) list must still fail closed.
        proj = _sanitize_messages_for_agent(
            [{"role": "user", "content": "first question"}]
        )
        raw = [{"role": "user", "content": "first question"}]
        result = [
            {"role": "user", "content": "someone else"},
            {"role": "assistant", "content": "ROGUE"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result), "follow-up",
            projected_history=list(proj),
        )
        assert _role_content(_no_id(settled)) == _role_content(_no_id(raw)), (
            "a fully divergent non-exact projection must still fail closed to "
            "the raw pre-turn context"
        )


class TestSanitizeKeepsDiscriminatorsOnAgentReplay:
    """Finding 4: merge-visible discriminators survive the Agent replay projection.

    ``preserve_api_content=True`` is the internal Agent history path. It must
    keep ``_MERGE_VISIBLE_DISCRIMINATORS`` so the Agent's own pass-0 repair
    sees the same fields, matching the projection this process handed it; only
    the direct provider projection (default) strips them.
    """

    def test_agent_replay_keeps_discriminators(self):
        discriminated = {
            "role": "assistant",
            "content": "partial answer",
            "finish_reason": "verification_required",
            "api_content": "...",
        }
        agent_projection = _sanitize_messages_for_agent([copy.deepcopy(discriminated)])
        assert agent_projection, "agent projection must keep the row"
        assert agent_projection[0].get("finish_reason") == "verification_required", (
            "internal Agent history must keep the merge-visible discriminator "
            "(finding 4) so the Agent's own pass-0 repair matches that "
            "projection instead of merging historical rows into a drifted prefix"
        )

    def test_direct_provider_projection_strips_discriminators(self):
        discriminated = {
            "role": "assistant",
            "content": "partial answer",
            "finish_reason": "verification_required",
        }
        provider_projection = _sanitize_messages_for_api([copy.deepcopy(discriminated)])
        assert provider_projection
        assert provider_projection[0].get("finish_reason") is None, (
            "direct provider projection must strip the merge-visible "
            "discriminators to honour the _API_SAFE_MSG_KEYS wire contract"
        )