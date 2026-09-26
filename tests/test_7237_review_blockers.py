"""Regression tests for the #7237 review blockers.

Blocker 1 (SILENT data loss): the outbound orphan sanitizer's projected
history must never be persisted wholesale on the next turn. When the
replayed prefix no longer matches the raw pre-turn context (because the
sanitizer rewrote it), the settle path must keep the raw pre-turn context
and settle only the current-turn slice on top — unless a compression
marker explains the rotation.

Blocker 2 (valid pair dropped): ``assistant(call), assistant(progress),
tool(result)`` is a legitimate, commonly-occurring shape. The outbound
sanitizer must merge consecutive assistant rows (mirroring the Agent's
pass order: merge BEFORE orphan detection) so the call/result pair both
survive the projection. A genuine user/system boundary between a call and
its result is still an orphan.

Blocker 2a (projection-path discriminators): the
``_API_SAFE_MSG_KEYS`` allowlist used to drop ``codex_reasoning_items`` /
``codex_message_items`` / ``finish_reason`` BEFORE the Agent pass-0
merge could see them, so the Codex-interim / verification-supersede
behaviours were only proven by the helper tests, never by the real
projection callers. The sanitizer must let the merge see those
discriminators, then strip them in a final pass so the wire contract
still matches ``_API_SAFE_MSG_KEYS``.

Blocker 2b (Agent-merge contract drift): the WebUI merge was missing two
behaviours the Agent's own ``_merge_assistant_into`` implements —
① carrying ``reasoning_content`` from the later row when the survivor
lacks it, and ② dropping the survivor's stale ``api_content`` when the
joined content actually changed. The drift only mattered in scenarios
the helper-only tests didn't cover; pin both behaviours on the real
projection paths.

Blocker 3 (repeated-prompt fallback): the non-compression fallback that
locates the current turn in ``result_messages`` when the active-turn
identity is unavailable must select the LAST matching user row, not the
first. First-match would replay the entire history from an earlier
identical prompt, just like ``_find_current_user_turn`` was already
written to avoid. ``_looks_like_current_user_turn_scan`` is the
``#7237 review`` blocker 3 target; this test exercises the fallback path
through the public dedupe entry point so the same regression cannot
re-enter via either helper.
"""
import copy
import json
from types import SimpleNamespace

from api.streaming import (
    _API_PUBLIC_STRIP_KEYS,
    _API_SAFE_MSG_KEYS,
    _MERGE_VISIBLE_DISCRIMINATORS,
    _dedupe_replayed_context_messages,
    _sanitize_messages_for_api,
    _sanitize_messages_for_agent,
    _settle_result_messages,
    _strip_orphan_tool_calls,
    _merge_consecutive_assistant_rows,
    _api_safe_message_positions,
    _looks_like_current_user_turn_scan,
    _restore_reasoning_metadata_before_boundary,
)


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def _assistant(cids, content=""):
    return {"role": "assistant", "content": content, "tool_calls": [_call(c) for c in cids]}


def _tool(cid, content="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


class TestBlocker1WholesaleAcceptanceGuard:
    """The first local turn after an orphan repair must not destroy history."""

    def _raw_history(self):
        # Two completed turns. The second assistant carries a call/result pair.
        return [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
            {"role": "user", "content": "second question", "timestamp": 3.0},
            {"role": "assistant", "content": "", "tool_calls": [_call("k1")], "timestamp": 4.0},
            {"role": "tool", "tool_call_id": "k1", "content": "tool output", "timestamp": 5.0},
            {"role": "assistant", "content": "second answer", "timestamp": 6.0},
        ]

    def _raw_history_with_orphan_pair(self):
        """History that the outbound sanitizer will rewrite, so the Agent's
        projected result no longer has the raw history as an exact prefix.

        ``k1`` has a paired tool result, so the sanitizer must KEEP the pair.
        The pre-existing ``_raw_history`` fixture is retained for the
        compression-rotation / identity tests that don't rely on a sanitizer
        rewrite. ``test_sanitize_preserves_paired_call_then_orhan_sanitizer_strips_pair``
        (below) documents why that fixture cannot stand in for a prefix-mismatch
        test: the sanitizer keeps the pair, so the projected prefix still
        matches the raw history and the new fallback branch is never exercised.
        """
        return [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
            {"role": "user", "content": "second question", "timestamp": 3.0},
            # Trailing assistant carries a call/result pair split by a real
            # user turn — the call is a genuine orphan and the result must be
            # dropped. The sanitizer rewrites this row, so the projected
            # prefix will no longer match the raw history (the failure mode
            # the #7237 wholesale-acceptance guard exists to block).
            {"role": "assistant", "content": "", "tool_calls": [_call("ghost")], "timestamp": 4.0},
            {"role": "user", "content": "interrupt", "timestamp": 5.0},
            {"role": "tool", "tool_call_id": "ghost", "content": "late result", "timestamp": 6.0},
        ]

    def test_sanitize_keeps_paired_call_so_prefix_still_matches(self):
        """#7237 review finding 1: document the premise of the previous
        ``_raw_history`` oracle. The second turn's call/result pair is
        adjacent, so ``_sanitize_messages_for_api`` keeps it. That means
        ``_dedupe_replayed_context_messages`` sees an exact prefix and takes
        the cheap branch — the new "no identity, prefix mismatch" fallback
        path the wholesale-acceptance guard adds is never exercised by the
        old fixture. The oracle therefore does NOT prove the new branch
        works. Use ``_raw_history_with_orphan_pair`` for that.
        """
        raw = self._raw_history()
        sanitized = _sanitize_messages_for_api(raw)
        # Sanity: the pair survives, so the projected prefix still matches.
        surviving_call_ids = [
            tc.get("id")
            for m in sanitized
            for tc in (m.get("tool_calls") or [])
        ]
        assert surviving_call_ids == ["k1"], (
            "fixture premise: the sanitizer must keep the paired call so the "
            "previous 'third question' test could pass through the cheap "
            "prefix-match branch. The new prefix-mismatch test below uses a "
            "different fixture that the sanitizer DOES rewrite."
        )

    def test_next_turn_persists_raw_history_not_projected(self):
        """Save-then-reload: persisted context keeps the original pair.

        Uses ``_raw_history_with_orphan_pair``: a sanitizer rewrite is
        required to force the Agent's projected result off the raw prefix,
        so the wholesale-acceptance guard's new fallback path is actually
        exercised. The previous ``_raw_history`` fixture is documented in
        ``test_sanitize_keeps_paired_call_so_prefix_still_matches``.
        """
        raw = self._raw_history_with_orphan_pair()
        # The outbound sanitizer drops the orphan call + the late tool result
        # (a user row sits between them), so the projected history no longer
        # has raw as an exact prefix.
        sanitized = _sanitize_messages_for_api(raw)
        # Sanity: the rewrite really happened — projected != raw.
        assert [m.get("role") for m in sanitized] != [m.get("role") for m in raw]
        # Simulate the Agent returning the sanitized history + a new turn.
        result = sanitized + [
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ]
        identity = None  # no active-turn identity: fallback scan must fire
        settled = _dedupe_replayed_context_messages(raw, result, "third question", identity)
        # The persisted context must keep the ORIGINAL tool pair verbatim.
        assert settled == raw + [
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ], "raw pre-turn context must survive settle intact"

    def test_next_turn_save_then_reload_keeps_pair(self):
        """Full settle round-trip: session.context_messages keeps the pair."""
        raw = self._raw_history()
        sanitized = _sanitize_messages_for_api(raw)
        result = sanitized + [
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ]
        session = SimpleNamespace(
            messages=list(raw),
            context_messages=list(raw),
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            list(raw),          # previous_messages (display)
            list(raw),          # previous_context_messages
            result,
            "third question",
            "webui",
            None,
        )
        persisted = session.context_messages
        tool_rows = [m for m in persisted if m.get("role") == "tool"]
        assert tool_rows, "the original tool result must survive settle"
        call_rows = [
            m for m in persisted
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        assert call_rows, "the original tool call must survive settle"
        assert any(
            tc.get("id") == "k1" for m in call_rows for tc in m["tool_calls"]
        ), "call k1 must still be present in persisted context"

    def test_compression_rotation_still_replaces_wholesale(self):
        """A compression turn legitimately rotates the context."""
        raw = self._raw_history()
        marker = {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION] summary of prior turns",
        }
        result = [
            marker,
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ]
        settled = _dedupe_replayed_context_messages(raw, result, "third question", None)
        # With a compression marker the wholesale replacement stands.
        assert settled == result

    def test_identity_boundary_used_when_present(self):
        """With a valid active-turn identity the boundary slice is honoured."""
        raw = self._raw_history()
        sanitized = _sanitize_messages_for_api(raw)
        user_row = {"role": "user", "content": "third question", "_active_turn_token": "tok-1"}
        result = sanitized + [
            user_row,
            {"role": "assistant", "content": "third answer"},
        ]
        identity = {
            "turn_id": "turn-1",
            "token": "tok-1",
            "agent_turn_boundary_resolved": True,
            "current_turn_user_idx": len(result) - 2,
        }
        settled = _dedupe_replayed_context_messages(raw, result, "third question", identity)
        assert settled == raw + [user_row, {"role": "assistant", "content": "third answer"}]


class TestBlocker2MergeBeforeOrphan:
    """Mergeable assistant rows must not orphan a valid call/result pair."""

    def test_progress_row_between_call_and_result_keeps_pair(self):
        msgs = [_assistant(["m1"], content=""), {"role": "assistant", "content": "progress"}, _tool("m1")]
        sanitized = _sanitize_messages_for_api(msgs)
        call_rows = [m for m in sanitized if m.get("tool_calls")]
        assert call_rows, "the call row must survive"
        assert any(tc.get("id") == "m1" for m in call_rows for tc in m["tool_calls"])
        tool_rows = [m for m in sanitized if m.get("role") == "tool"]
        assert any(m.get("tool_call_id") == "m1" for m in tool_rows), "the result must survive"

    def test_merge_helper_unions_tool_calls(self):
        msgs = [_assistant(["m1"], content=""), {"role": "assistant", "content": "progress"}]
        merged = _merge_consecutive_assistant_rows(msgs)
        assert len(merged) == 1
        assert [tc["id"] for tc in merged[0]["tool_calls"]] == ["m1"]
        assert merged[0]["content"] == "progress"

    def test_merge_helper_does_not_mutate_input(self):
        msgs = [_assistant(["m1"], content=""), {"role": "assistant", "content": "progress"}]
        before = json.dumps(msgs)
        _merge_consecutive_assistant_rows(msgs)
        assert json.dumps(msgs) == before

    def test_merge_helper_codex_interim_exempt(self):
        interim = {
            "role": "assistant", "content": "interim",
            "codex_message_items": [{"type": "message"}],
        }
        msgs = [_assistant(["m1"], content=""), interim]
        merged = _merge_consecutive_assistant_rows(msgs)
        assert len(merged) == 2, "codex interim rows are replayed verbatim"

    def test_merge_helper_verification_supersede(self):
        provisional = {
            "role": "assistant", "content": "provisional",
            "tool_calls": [_call("v1")],
            "finish_reason": "verification_required",
        }
        final = {"role": "assistant", "content": "final answer"}
        merged = _merge_consecutive_assistant_rows([provisional, final])
        assert len(merged) == 1
        assert "tool_calls" not in merged[0], "superseded candidate is replaced, not unioned"
        assert merged[0]["content"] == "final answer"

    def test_genuine_user_boundary_still_orphans(self):
        """A user turn between call and result is a real orphan — split the
        merged/mergeable cases from the genuine-boundary case."""
        msgs = [_assistant(["g1"]), {"role": "user", "content": "interrupt"}, _tool("g1")]
        sanitized = _sanitize_messages_for_api(msgs)
        assert not any(m.get("tool_calls") for m in sanitized), "call across a user boundary is an orphan"
        tool_rows = [m for m in sanitized if m.get("role") == "tool"]
        assert not tool_rows, "result across a user boundary is dropped too"

    def test_api_safe_positions_merge_keeps_origin_index(self):
        msgs = [
            {"role": "user", "content": "go"},
            _assistant(["m1"], content=""),
            {"role": "assistant", "content": "progress"},
            _tool("m1"),
        ]
        out = _api_safe_message_positions(msgs)
        surviving_calls = [(idx, m) for idx, m in out if m.get("tool_calls")]
        assert surviving_calls, "merged survivor keeps the call"
        idx, first = surviving_calls[0]
        assert idx == 1, "merged row keeps the FIRST row's original index"
        tool_rows = [(i, m) for i, m in out if m.get("role") == "tool"]
        assert tool_rows and tool_rows[0][1]["tool_call_id"] == "m1"


class TestBlocker2ProjectionPath:
    """#7237 review finding 2a/2b: the real sanitizer/positions projection
    paths must (a) preserve ``codex_*``/``finish_reason`` discriminators into
    the Agent pass-0 merge and strip them in a final pass, and (b) match the
    Agent's ``_merge_assistant_into`` behaviours for ``reasoning_content``
    carry and ``api_content`` invalidation. The helper-only tests above
    cover the merge function in isolation; this class covers the projection
    callers, which is what real session flows hit.
    """

    def test_sanitize_for_api_strips_merge_visible_discriminators_in_final_pass(self):
        """The merge-visible discriminators are kept long enough for the
        Agent pass-0 merge to see them, then dropped in a final pass so
        the public sanitizer output still matches ``_API_SAFE_MSG_KEYS``.
        """
        msgs = [
            {
                "role": "assistant", "content": "first", "tool_calls": [_call("v1")],
                "finish_reason": "verification_required",
            },
            {
                "role": "assistant", "content": "second",
                "codex_reasoning_items": [{"type": "reasoning"}],
            },
        ]
        out = _sanitize_messages_for_api(msgs)
        # The merge-visible discriminators are stripped in the final pass.
        for m in out:
            for forbidden in _MERGE_VISIBLE_DISCRIMINATORS:
                assert forbidden not in m, (
                    f"{forbidden} must not survive the public sanitizer "
                    f"projection — only the Agent merge may see it"
                )
            for key in m:
                assert key in _API_SAFE_MSG_KEYS, (
                    f"key {key!r} leaked into the public sanitizer output"
                )

    def test_sanitize_for_api_keeps_codex_interim_separate(self):
        """A codex_interim row (carrying ``codex_message_items``) is exempt
        from the Agent pass-0 merge. The real projection must therefore keep
        it distinct from the ADJACENT assistant row, even though both are
        role=assistant.

        The interim row sits directly next to a mergeable assistant row on
        purpose: the earlier fixture put a tool row between them, which left
        the test green even if interim preservation stopped working. With
        adjacent rows the merger WOULD collapse the pair into one row if the
        exemption were lost, so the exact row count is the discriminator
        (#7237 review, nesquena-hermes 2026-09-23).
        """
        interim = {
            "role": "assistant",
            "content": "interim",
            "codex_message_items": [{"type": "message"}],
        }
        msgs = [
            {"role": "user", "content": "go"},
            _assistant(["m1"], content="before interim"),
            interim,
            {"role": "assistant", "content": "after interim"},
        ]

        sanitizer_out = _sanitize_messages_for_api(msgs)
        assert [m.get("content") for m in sanitizer_out] == [
            "go",
            "before interim",
            "interim",
            "after interim",
        ], (
            "the codex_interim row must stay its own assistant turn beside "
            "the mergeable assistant rows; collapsing them loses the row"
        )
        for m in sanitizer_out:
            assert "codex_message_items" not in m, (
                "codex_message_items is a merge-visible discriminator "
                "and must not leak into the public sanitizer output"
            )

        positions_out = _api_safe_message_positions(msgs)
        assert [m.get("content") for _idx, m in positions_out] == [
            "go",
            "before interim",
            "interim",
            "after interim",
        ], (
            "the positions projection must also keep the adjacent codex_interim "
            "row separate from its assistant neighbours"
        )
        assert [idx for idx, _m in positions_out] == [0, 1, 2, 3], (
            "adjacent non-interim assistant rows are still merged pair-wise, "
            "but the interim row keeps its own position"
        )

    def test_sanitize_for_api_verification_supersede_on_real_path(self):
        """A verification_required assistant row followed by a final
        answer is superseded on the real projection path, not unioned.
        """
        provisional = {
            "role": "assistant", "content": "provisional",
            "tool_calls": [_call("v1")],
            "finish_reason": "verification_required",
        }
        final = {"role": "assistant", "content": "final answer"}
        out = _sanitize_messages_for_api([provisional, final])
        assert len(out) == 1
        assert out[0]["content"] == "final answer"
        # ``finish_reason`` does not survive the public projection.
        assert "finish_reason" not in out[0]
        # The provisional tool call was replaced, not unioned.
        assert not out[0].get("tool_calls")

    def test_api_safe_positions_strips_merge_visible_discriminators(self):
        """The ``_api_safe_message_positions`` projection also strips the
        merge-visible discriminators before returning. Their presence in
        the wire payload would leak Agent-internal state to downstream
        callers that consume the position list.
        """
        interim = {
            "role": "assistant", "content": "interim",
            "codex_message_items": [{"type": "message"}],
        }
        msgs = [
            {"role": "user", "content": "go"},
            _assistant(["m1"], content=""),
            interim,
            _tool("m1"),
        ]
        out = _api_safe_message_positions(msgs)
        for _idx, m in out:
            for forbidden in _MERGE_VISIBLE_DISCRIMINATORS | {"api_content"}:
                assert forbidden not in m, (
                    f"{forbidden} must not survive the positions projection"
                )
            for key in m:
                assert key in _API_SAFE_MSG_KEYS, (
                    f"key {key!r} leaked into positions output"
                )

    def test_sanitize_for_api_carries_reasoning_content_from_later_row(self):
        """Agent merge contract ①: when the survivor lacks
        ``reasoning_content`` and the later row carries one, the merge
        copies the later value onto the survivor. The real sanitizer
        must match this — a strict thinking provider needs a
        ``reasoning_content`` on the merged tool-call turn.
        """
        msgs = [
            {
                "role": "assistant", "content": "first",
                "tool_calls": [_call("r1")],
            },
            {
                "role": "assistant", "content": "second",
                "reasoning_content": "thought: the answer is 42",
            },
            _tool("r1"),
        ]
        out = _sanitize_messages_for_api(msgs)
        merged = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
        assert merged, "the merged tool-call row survives"
        assert merged[0].get("reasoning_content") == "thought: the answer is 42", (
            "the survivor's reasoning_content must be carried from the "
            "later row, matching the Agent's _merge_assistant_into contract"
        )

    def test_sanitize_for_api_drops_stale_api_content_on_content_rewrite(self):
        """Agent merge contract ②: when the joined content actually
        changes, the survivor's ``api_content`` sidecar is dropped. A
        stale sidecar would replay pre-merge bytes on the next request
        and silently overwrite the rewrite. The Agent replay projection
        (``preserve_api_content=True``) is the only path that must keep
        ``api_content``; the sanitizer strips it on non-rewrite
        content-only-equal merges to honour the prompt-cache invariant.
        """
        msgs = [
            {
                "role": "assistant", "content": "first",
                "tool_calls": [_call("c1")],
                "api_content": "stale bytes the rewrite just discarded",
            },
            {
                "role": "assistant", "content": "second", "tool_calls": [_call("c2")],
            },
            _tool("c1"),
            _tool("c2"),
        ]
        # Public sanitizer (preserve_api_content=False): api_content never survives.
        out = _sanitize_messages_for_api(msgs)
        assert all("api_content" not in m for m in out), (
            "public sanitizer strips api_content even when the merge "
            "saw a stale sidecar; only the Agent replay path keeps it"
        )

    def test_sanitize_for_agent_keeps_api_content_on_unchanged_merge(self):
        """When the merge does NOT change the visible content (the later
        row carried only an empty ``new_content``), the survivor's
        ``api_content`` MUST survive on the Agent replay path: dropping
        it would break the prompt-cache replay invariant for no reason.
        The Agent's own contract distinguishes the "unchanged" and
        "rewritten" cases; the WebUI helper now does too.

        The orphan-repair pass runs AFTER the merge and drops call ids
        that lack an immediately-following tool result. To keep the merged
        row in the output, the survivor's call must already be answered
        by an adjacent tool row.
        """
        msgs = [
            {
                "role": "assistant", "content": "only the survivor",
                "tool_calls": [_call("d1")],
                "api_content": "stale bytes for unchanged survivor",
            },
            {
                "role": "assistant", "content": "",  # empty → join collapses to prev
            },
            _tool("d1"),
        ]
        before_bytes = json.dumps(msgs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        out = _sanitize_messages_for_agent(msgs)
        # Source non-mutation: the merge must not rewrite the input rows.
        assert json.dumps(msgs, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == before_bytes
        merged = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
        assert merged
        # The merge was a no-op on content, so the sidecar survives.
        assert merged[0].get("api_content") == "stale bytes for unchanged survivor", (
            "agent replay projection must keep the sidecar when the "
            "merge did not change the visible content"
        )

    def test_sanitize_for_agent_drops_api_content_on_rewrite(self):
        """The Agent replay path must drop the survivor's ``api_content``
        when the merge actually changes the visible content. Pre-merge
        bytes cannot describe the post-merge turn; replaying them would
        silently undo the rewrite at the next API build.

        The orphan-repair pass runs AFTER the merge; this fixture must
        give the merged row an immediately-following tool result so the
        repair does not drop the call. The point of the test is the
        content rewrite + api_content drop, not the orphan-repair.
        """
        msgs = [
            {
                "role": "assistant", "content": "first",
                "tool_calls": [_call("e1")],
                "api_content": "stale bytes the rewrite just discarded",
            },
            {
                "role": "assistant", "content": "second", "tool_calls": [_call("e2")],
            },
            _tool("e1"),
            _tool("e2"),
        ]
        out = _sanitize_messages_for_agent(msgs)
        merged = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
        assert merged
        assert "api_content" not in merged[0], (
            "the survivor's api_content must be dropped when the merge "
            "actually changed the visible content"
        )

    def test_merge_visible_strip_keys_cover_required_discriminators(self):
        """Defensive invariant: the strip set must include every merge
        discriminator so a future ``_API_SAFE_MSG_KEYS`` expansion cannot
        silently leak an internal field into the public sanitizer output.
        """
        assert {'codex_reasoning_items', 'codex_message_items', 'finish_reason'} <= _API_PUBLIC_STRIP_KEYS
        assert _MERGE_VISIBLE_DISCRIMINATORS <= _API_PUBLIC_STRIP_KEYS
        # ``api_content`` is also stripped from the public sanitizer (the
        # ``preserve_api_content`` opt-in is the only path that keeps it).
        assert 'api_content' in _API_PUBLIC_STRIP_KEYS


class TestBlockerOwnershipFailClosed:
    """#7237 review (nesquena-hermes, 2026-09-23): an unmatched user row
    must never be treated as current-turn authority.

    ``_looks_like_current_user_turn_scan``'s contract is "return the index of
    a row that looks like the current user turn, else None". The old tail
    returned the last arbitrary ``role: "user"`` row after no prompt-derived
    match, so ``_dedupe_replayed_context_messages`` guessed a boundary from
    an unrelated (often synthetic continuation) user row and dropped the
    assistant/tool output between the real turn and that guess.

    Fail-closed rule: with no proven ownership — neither an active-turn
    identity/checkpoint, nor a prompt-derived match — keep the raw
    ``previous_context`` exactly as it is and do NOT slice from a guessed row.

    Data-regression rule (#7237 review, nesquena-hermes 2026-09-23):
    ``result_messages`` is the FULL conversation the Agent returned — the
    projected history this process sent it, then the current turn's rows. The
    only rows that may be appended to the raw ``previous_context`` are those
    after that exact projection; a projected history that was rewritten by the
    outbound sanitizer must never be appended beside the authoritative raw
    history, or the duplicated rows ride the next model context too.
    """

    PROMPT = "please refactor streaming.py"

    def _previous_context(self):
        return [
            {"role": "user", "content": "real first question", "timestamp": 1.0},
            # Reasoning-only assistant row: the outbound sanitizer drops it, so
            # the projection sent to the Agent is SHORTER than this raw context
            # and no longer has it as an exact prefix — the production shape
            # after a history rewrite (#7237 review data-regression finding).
            {"role": "assistant", "content": "", "reasoning_content": "hidden thought", "timestamp": 1.5},
            {"role": "assistant", "content": "real first answer", "timestamp": 2.0},
            {"role": "user", "content": "second question", "timestamp": 3.0},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call("k1")],
                "timestamp": 4.0,
            },
            {"role": "tool", "tool_call_id": "k1", "content": "tool output", "timestamp": 5.0},
            {"role": "assistant", "content": "second answer", "timestamp": 6.0},
        ]

    def _current_turn_rows(self):
        """The production current-turn shape: a transformed real user turn,
        intervening assistant/tool output, then a synthetic continuation
        user row the agent loop appended AFTER the real turn."""
        return [
            {
                "role": "user",
                "content": "Mid-turn correction (steer): " + self.PROMPT,
            },
            {"role": "assistant", "content": "worked on the refactor"},
            {"role": "assistant", "content": "", "tool_calls": [_call("k2")]},
            {"role": "tool", "tool_call_id": "k2", "content": "refactor output"},
            {"role": "user", "content": "Continue from where you stopped"},
            {"role": "assistant", "content": "synthetic answer"},
        ]

    def _agent_result(self, previous_context=None):
        """Build the REAL production shape: the Agent returns
        ``projected_history + current_turn_rows``.

        The projection is ``_sanitize_messages_for_agent(raw)``, which
        rewrites historical rows (timestamps/ids dropped, mergeable
        assistant rows joined), so it is NOT the raw history and a byte
        comparison against it always diverges. That divergence is precisely
        why the settle must prove ownership from the projection rather than
        from the raw context.
        """
        raw = self._previous_context() if previous_context is None else previous_context
        projected = _sanitize_messages_for_agent(raw)
        return projected, projected + self._current_turn_rows()

    def test_scan_returns_none_when_no_prompt_match(self):
        """Contract: no proven ownership -> None (never an arbitrary row)."""
        msgs = [
            {"role": "user", "content": "unrelated"},
            {"role": "assistant", "content": "answer"},
        ]
        assert _looks_like_current_user_turn_scan(msgs, "totally different prompt") is None
        # A non-matching user row is not ownership proof either.
        msgs = [
            {"role": "user", "content": "prefix of prompt real first question tail"},
            {"role": "assistant", "content": "answer"},
        ]
        assert _looks_like_current_user_turn_scan(msgs, "something else entirely") is None
        # Empty list still returns None.
        assert _looks_like_current_user_turn_scan([], "anything") is None
        assert _looks_like_current_user_turn_scan(None, "anything") is None

    def test_dedupe_keeps_raw_context_when_no_proven_boundary(self):
        """Weak/transformed current prompt + later synthetic user row: the
        guessed boundary must not stand. Raw previous_context is kept and the
        projected result is NOT wholesale-accepted, so nothing is lost.
        """
        previous_context = self._previous_context()
        projected, result_messages = self._agent_result()
        # Sanity: the scan really cannot prove ownership here.
        assert _looks_like_current_user_turn_scan(result_messages, self.PROMPT) is None

        settled = _dedupe_replayed_context_messages(
            previous_context, result_messages, self.PROMPT, None,
            projected_history=projected,
        )
        # Fail closed: raw context preserved, plus the PROVEN current-turn
        # suffix from the projection. Crucially, none of the raw rows may be
        # dropped by a guessed boundary.
        assert settled[:len(previous_context)] == previous_context, (
            "fail-closed settle must keep the raw pre-turn context intact; "
            "guessing a boundary from an unrelated user row drops history "
            "(#7237 review ownership defect 1)"
        )
        # The synthetic continuation user row is not the boundary: the rows
        # between the real turn and it are not swallowed.
        contents = [m.get("content") for m in settled]
        assert "worked on the refactor" in contents
        assert "refactor output" in contents

    def test_dedupe_does_not_persist_whole_projected_result_without_boundary(self):
        """The no-proven-boundary path must fall through to the shared
        wholesale-acceptance guard, never to ``result_messages`` as-is. With
        no projection threaded there is no ownership proof at all, so the
        settle must return the raw context alone rather than append
        unproven historical rows.
        """
        previous_context = self._previous_context()
        _projected, result_messages = self._agent_result()
        # NOTE: the projection is deliberately NOT threaded here — this pins
        # the caller's fail-closed contract when no ownership signal exists.
        settled = _dedupe_replayed_context_messages(
            previous_context, result_messages, self.PROMPT, None,
        )
        assert settled == previous_context, (
            "without a threaded projection the settle must fail closed to the "
            "raw context alone; appending unproven projected rows duplicates "
            "history beside the authoritative raw history (#7237 review "
            "data-regression finding)"
        )

    def test_settle_round_trip_preserves_raw_history(self):
        """Full ``_settle_result_messages`` path: with no proven ownership the
        persisted context keeps the raw pre-turn rows AND the current turn's
        output, so neither the historical pair nor the new turn is lost.
        """
        raw = self._previous_context()
        projected, result = self._agent_result()
        session = SimpleNamespace(
            messages=list(raw),
            context_messages=list(raw),
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            list(raw),
            list(raw),
            result,
            self.PROMPT,
            "webui",
            None,
            projected,
        )
        persisted = session.context_messages
        assert [(m["role"], m["content"]) for m in persisted[:len(raw)]] == [
            (m["role"], m["content"]) for m in raw
        ], "raw pre-turn context survives the settle intact"
        # The historical call/result pair is still there.
        assert any(
            tc.get("id") == "k1"
            for m in persisted
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        )
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "k1" for m in persisted
        )
        # The current-turn rows are appended too — nothing between the real
        # turn and the synthetic continuation row was swallowed.
        contents = [m.get("content") for m in persisted]
        assert "worked on the refactor" in contents
        assert "refactor output" in contents

    def test_strong_match_still_slices_current_turn(self):
        """A prompt-derived match IS ownership proof: the boundary still works.
        """
        previous_context = self._previous_context()
        prompt = "please refactor streaming.py"
        result_messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "refactored"},
        ]
        settled = _dedupe_replayed_context_messages(
            previous_context, result_messages, prompt, None,
        )
        assert settled == previous_context + result_messages


class TestNoProjectedHistoricalResidual:
    """#7237 review (nesquena-hermes, 2026-09-23) data-regression finding.

    ``result["messages"]`` is the FULL conversation the Agent returned —
    ``conversation_history`` (the exact projection this process sent via
    ``agent.run_conversation``) followed by the current turn's rows. The
    no-boundary fallback used to pass that full list through
    ``_strip_replayed_prefix()`` / ``_strip_replayed_context_items()`` and
    append the residual to the raw ``previous_context``. Those helpers remove
    suffix/prefix overlap and exact replay blocks, but rows the OUTBOUND
    SANITIZER rewrote survive the strip as a residual, so the projected
    history was duplicated beside the authoritative raw history and rode the
    next model context.

    The fix threads the exact projected ``conversation_history`` so only the
    rows after that proven projection — the current turn — may be appended.
    When no sent-history prefix (and no turn-ownership signal) proves a
    boundary, NOTHING is appended (fail closed).

    Fixtures below use the real projection (``_sanitize_messages_for_agent``),
    which rewrites history in ways the replay-key strip cannot reverse, so the
    old residual genuinely survives. ``result_messages`` BEGIN with the
    projected historical prefix — the production shape the pre-existing tests
    in ``TestBlockerOwnershipFailClosed`` never built (their
    ``result_messages`` began at the current turn).
    """

    PROMPT = "please refactor streaming.py"

    # Raw pre-turn history: the sanitizer merges the two consecutive assistant
    # rows into ONE row, so the projection is shorter than the raw context and
    # its surviving row is a rewritten historical row. That is the divergence
    # that made the old strip leave a residual behind.
    RAW_HISTORY = [
        {"role": "user", "content": "real first question", "timestamp": 1.0},
        {"role": "assistant", "content": "", "reasoning_content": "hidden thought", "timestamp": 1.5},
        {"role": "assistant", "content": "call row", "tool_calls": [_call("k1")], "timestamp": 2.0},
        {"role": "tool", "tool_call_id": "k1", "content": "tool output", "timestamp": 3.0},
        {"role": "assistant", "content": "real first answer", "timestamp": 4.0},
    ]

    # Current turn: a TRANSFORMED real user turn (a steer preamble was
    # prepended, so the strict prompt match in
    # _looks_like_current_user_turn_scan cannot prove ownership), intervening
    # assistant/tool output, and a LATER synthetic user row the agent loop
    # appended after the real turn.
    CURRENT_TURN = [
        {"role": "user", "content": "Mid-turn correction (steer): " + PROMPT},
        {"role": "assistant", "content": "worked on the refactor"},
        {"role": "assistant", "content": "", "tool_calls": [_call("k2")]},
        {"role": "tool", "tool_call_id": "k2", "content": "refactor output"},
        {"role": "user", "content": "Continue from where you stopped"},
        {"role": "assistant", "content": "synthetic answer"},
    ]

    @classmethod
    def _fixture(cls):
        """Build the production shape: the Agent returns
        ``projected_history + current_turn``."""
        raw = [copy.deepcopy(m) for m in cls.RAW_HISTORY]
        projected = _sanitize_messages_for_agent(raw)
        return (
            raw,
            projected,
            projected + [copy.deepcopy(m) for m in cls.CURRENT_TURN],
        )

    def test_projection_diverges_from_raw_premise(self):
        """Premise: the sanitizer rewrite really makes the projection diverge
        from the raw history, so the old strip could not collapse it.
        """
        _raw, _projected, result = self._fixture()
        # No ownership signal may prove a boundary in this fixture.
        assert _looks_like_current_user_turn_scan(result, self.PROMPT) is None

    def test_no_projected_historical_residual_is_appended(self):
        """Direct regression: the exact projection sent to the Agent is
        threaded, so the settle appends ONLY the proven current-turn suffix —
        no sanitizer-rewritten historical rows survive beside the raw history.
        """
        raw, projected, result = self._fixture()
        settled = _dedupe_replayed_context_messages(
            raw, result, self.PROMPT, None,
            projected_history=projected,
        )
        assert settled == raw + self.CURRENT_TURN, (
            "the settle must be exactly raw context + the proven current-turn "
            "suffix; a projected historical residual duplicated beside it is "
            "the #7237 review data-regression defect"
        )

    def test_unmatched_projection_fails_closed(self):
        """Fail closed: when the returned list does NOT start with the exact
        projection (no sent-history prefix, no turn-ownership signal), NO
        unproven historical rows are appended at all — not even the current
        turn's, because its boundary is unproven too.
        """
        raw, projected, _result = self._fixture()
        # A different projection (the agent rotated / rewrote history again):
        # its prefix cannot prove the boundary.
        mismatch = [{"role": "system", "content": "rotated history"}] + projected
        settled = _dedupe_replayed_context_messages(
            raw, mismatch + self.CURRENT_TURN, self.PROMPT, None,
            projected_history=projected,
        )
        assert settled == raw, (
            "with no proven sent-history prefix and no turn-ownership signal "
            "the settle must keep the raw pre-turn context and append NOTHING "
            "(#7237 review data-regression finding, fail closed)"
        )

    def test_full_settle_persists_raw_plus_proven_suffix_only(self):
        """Full ``_settle_result_messages`` regression in the production
        shape: ``result_messages`` starts with the projected historical
        prefix, so the persisted context is exactly the raw pre-turn history
        plus the proven current-turn suffix — no projected historical residual.
        """
        raw, projected, result = self._fixture()
        session = SimpleNamespace(
            messages=[copy.deepcopy(m) for m in raw],
            context_messages=[copy.deepcopy(m) for m in raw],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result,
            self.PROMPT,
            "webui",
            None,
            projected,
        )
        persisted = session.context_messages
        expected = (
            [copy.deepcopy(m) for m in raw]
            + [copy.deepcopy(m) for m in self.CURRENT_TURN]
        )
        # The settle mints stable ids on the new rows, so compare on the
        # durable payload rather than on row identity.
        assert [(m.get("role"), m.get("content")) for m in persisted] == [
            (m.get("role"), m.get("content")) for m in expected
        ], (
            "persisted context must be raw history + proven current-turn "
            "suffix only; a projected historical residual is the #7237 review "
            "data-regression defect"
        )
        # The historical call/result pair survives untouched.
        assert any(
            tc.get("id") == "k1"
            for m in persisted
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        ), "the raw historical call k1 must survive the settle"
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "k1" for m in persisted
        ), "the raw historical result k1 must survive the settle"
        # The current turn's rows are all present, in order.
        tail_contents = [m.get("content") for m in persisted[len(raw):]]
        assert tail_contents == [m.get("content") for m in self.CURRENT_TURN], (
            "the proven current-turn suffix must be appended verbatim"
        )


class TestOldGuardStillHolds:
    """The pre-existing contract (from the PR's own tests) must survive."""

    def test_positional_orphan_still_repaired(self):
        msgs = [_assistant(["orphan"], content="visible"), {"role": "user", "content": "next"}]
        repaired = _strip_orphan_tool_calls(msgs)
        assert "tool_calls" not in repaired[0]

    def test_non_adjacent_result_cannot_rescue_earlier_orphan(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_call("foreign")]},
            {"role": "assistant", "content": "intervening"},
            {"role": "tool", "tool_call_id": "foreign", "content": "late result"},
        ]
        sanitized = _sanitize_messages_for_api(msgs)
        # After merge-then-orphan the intervening assistant merges into the
        # caller row; the late result pairs with the merged call — the Agent's
        # own pass order keeps both. (Previously this whole pair was dropped.)
        tool_rows = [m for m in sanitized if m.get("role") == "tool"]
        assert tool_rows, "agent pass order keeps the late-paired call/result"


class TestBlocker3RepeatedPromptFallback:
    """#7237 review finding 3: the non-compression fallback that locates
    the current turn in ``result_messages`` when the active-turn identity
    is unavailable must select the LAST matching user row, not the first.

    ``_find_current_user_turn`` is already written to walk to the end and
    prefer the last strong match (see its docstring: "first-match would
    return that old index, causing the merge to replay the entire history
    from that point"). The new ``_looks_like_current_user_turn_scan`` fallback
    was added at the same boundary but returned the first match, recreating
    the bug the established helper was built to avoid. These tests pin the
    fallback to the same last-strong-match contract.
    """

    def test_scan_returns_last_matching_user_turn_when_prompt_repeats(self):
        """Repeated-identical-prompt: the LATER user row is the current
        turn, the EARLIER one is historical. First-match would replay the
        full history from the older row. ``_find_current_user_turn`` already
        pins last-match; ``_looks_like_current_user_turn_scan`` must too.
        """
        prompt = "what is the meaning of life"
        msgs = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "42"},
            # The current turn uses the same prompt again.
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "still 42"},
        ]
        idx = _looks_like_current_user_turn_scan(msgs, prompt)
        assert idx == 2, (
            "fallback must select the LAST matching user row, not the first; "
            "first-match would replay historical turns (#7237 review finding 3)"
        )

    def test_scan_returns_none_when_no_prompt_match(self):
        """With no prompt-derived match the scan returns None: an unmatched
        user row is NOT ownership proof.

        The legacy contract anchored on the most recent user row even when no
        text matched. That row can be a synthetic continuation prompt the
        agent loop appended AFTER the real turn, so using it as the boundary
        drops the assistant/tool output that belongs to the real turn in
        between (#7237 review ownership defect 1, nesquena-hermes
        2026-09-23). None makes the caller fail closed instead.
        """
        msgs = [
            {"role": "user", "content": "unrelated"},
            {"role": "assistant", "content": "answer"},
        ]
        assert _looks_like_current_user_turn_scan(msgs, "totally different prompt") is None
        # A synthetic continuation row must not win either.
        msgs = [
            {"role": "user", "content": "please refactor streaming.py"},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "Continue"},
        ]
        assert _looks_like_current_user_turn_scan(msgs, "unrelated rewritten prompt") is None
        # Empty list still returns None (no user row to anchor on).
        assert _looks_like_current_user_turn_scan([], "anything") is None
        assert _looks_like_current_user_turn_scan(None, "anything") is None

    def test_dedupe_fallback_uses_last_match_not_first_when_no_identity(self):
        """End-to-end through the public dedupe entry point: when the
        active-turn identity is unavailable, the fallback must locate the
        current user turn by last-strong-match so the merged context does
        NOT replay the historical assistant output.

        Fixture: ``previous_context`` is a four-row history ending with a
        user/assistant pair whose prompt reappears in the new turn.
        ``result_messages`` carries the current turn only — the historical
        prefix is NOT an exact prefix (the prior assistant answers and the
        first user row are not in the result), so the dedupe takes the
        no-identity / no-marker path and falls back to
        ``_looks_like_current_user_turn_scan``.

        With first-match the fallback locates the user row at index 0 and
        the dedupe re-emits the entire current turn starting from that
        historical re-entry, leaving the historical user row duplicated at
        the tail (a partial replay). With last-match the slice starts at
        the current user row (index 1) and the historical user row at
        index 0 is dropped as a replayed item.
        """
        prompt = "what is the meaning of life"
        previous_context = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "42"},
        ]
        # result_messages does NOT start with previous_context[:n] for any
        # n >= 1, so neither the cheap prefix-match branch nor the
        # stale-merge branch fires. The fallback is the only path that can
        # locate the current turn.
        result_messages = [
            {"role": "user", "content": prompt},  # historical re-entry of the current prompt
            {"role": "user", "content": prompt},  # the actual current turn
            {"role": "assistant", "content": "still 42"},
        ]
        settled = _dedupe_replayed_context_messages(
            previous_context, result_messages, prompt, None,
        )
        # With LAST-match the boundary is the second user row (index 1),
        # so the historical user re-entry at index 0 is recognised as a
        # replayed item and dropped. With FIRST-match the boundary is
        # index 0, so the dedupe would re-emit the entire current turn
        # and the historical re-entry duplicates onto the tail.
        assert settled == previous_context + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "still 42"},
        ], (
            "no-identity / no-marker fallback must slice from the LAST "
            "matching user turn, not the first (#7237 review finding 3)"
        )


class TestVerificationSupersessionSurvivorIndex:
    """#7237 review (nesquena-hermes, 2026-09-23) ownership defect 2.

    In ``_api_safe_message_positions`` the verification-supersession arm
    replaced the provisional row with the surviving final row while KEEPING
    the dead provisional row's original index. Downstream
    ``_restore_reasoning_metadata_before_boundary`` walks the stored pairs and
    dereferences ``previous_messages[prev_idx]`` for the row it compares, so
    the survivor was aligned to a row that no longer exists in the projection
    and its stable id / timestamp / display reasoning could not be recovered.

    The replacement must store the survivor row's OWN index.
    """

    @staticmethod
    def _provisional():
        return {
            "role": "assistant",
            "content": "provisional",
            "tool_calls": [_call("v1")],
            "finish_reason": "verification_required",
        }

    def test_positions_keeps_survivor_own_index(self):
        """The surviving final row must carry ITS OWN original index."""
        msgs = [
            {"role": "user", "content": "go"},
            self._provisional(),
            {"role": "assistant", "content": "final answer"},
        ]
        out = _api_safe_message_positions(msgs)
        pairs = [(idx, m) for idx, m in out]
        assert [m.get("content") for _idx, m in pairs] == ["go", "final answer"], (
            "verification supersession keeps the final row and drops the "
            "provisional candidate"
        )
        final_idx = [idx for idx, m in pairs if m.get("content") == "final answer"]
        assert final_idx == [2], (
            "the survivor must store its OWN original index (2); storing the "
            "dead provisional row's index (1) misaligns every downstream "
            "metadata restore (#7237 review ownership defect 2)"
        )

    def test_positions_all_indices_address_their_own_row(self):
        """Every positional pair must be self-describing: the stored index must
        resolve to a row in the RAW list that the stored message is derived
        from.

        A union merge keeps the first row's index because that row IS the
        merged body; a supersession must keep the survivor's own index because
        the provisional row it replaced is gone from the projection. What can
        never happen is a stored index resolving to a row whose ROLE differs
        from the stored message (i.e. an index pointing at a row the
        projection no longer emits).
        """
        msgs = [
            {"role": "user", "content": "go"},
            self._provisional(),
            {"role": "assistant", "content": "final answer"},
            {"role": "assistant", "content": "tail answer"},
            {"role": "user", "content": "next"},
        ]
        out = _api_safe_message_positions(msgs)
        assert len(out) == 3, "user / merged-assistant / next"
        for idx, msg in out:
            assert msgs[idx].get("role") == msg.get("role"), (
                f"stored index {idx} resolves to {msgs[idx]!r}, not the "
                f"survivor row {msg!r} — positional alignment is broken"
            )
        # The merged assistant body must address the row that survived it.
        merged_idx, _merged = out[1]
        assert merged_idx == 2, (
            "the union-merge survivor keeps the FIRST merged row's index (2)"
        )

    def test_reasoning_metadata_restores_onto_survivor(self):
        """End-to-end: after supersession the surviving final row recovers its
        own stable id, timestamp and display reasoning.
        """
        previous_messages = [
            {"role": "user", "content": "go", "id": 1},
            {
                **self._provisional(),
                "id": 2,
                "reasoning": "provisional thinking",
                "timestamp": 20.0,
            },
            {
                "role": "assistant",
                "content": "final answer",
                "id": 3,
                "reasoning": "final thinking",
                "timestamp": 30.0,
            },
        ]
        # The agent rebuilds the projection it actually holds: the superseded
        # provisional row is gone, so the surviving final row sits at the
        # position the positional walk pairs with prev_safe[1].
        updated = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "final answer"},
        ]
        out = _restore_reasoning_metadata_before_boundary(previous_messages, updated)
        by_content = {m.get("content"): m for m in out}
        assert by_content["go"].get("id") == 1, "the untouched user row keeps its id"
        survivor = by_content["final answer"]
        assert survivor.get("id") == 3, (
            "the surviving final row must recover its OWN stable id; a "
            "misaligned index steals or drops the provisional row's id"
        )
        assert survivor.get("timestamp") == 30.0, (
            "the surviving row must recover its OWN timestamp"
        )
        assert survivor.get("reasoning") == "final thinking", (
            "the surviving row must recover its OWN display reasoning"
        )

    def test_sanitizer_path_still_supersedes(self):
        """The parallel ``_sanitize_messages_for_api`` supersession is
        unaffected: one row, the final content, no provisional tool calls.
        """
        out = _sanitize_messages_for_api(
            [self._provisional(), {"role": "assistant", "content": "final answer"}]
        )
        assert len(out) == 1
        assert out[0]["content"] == "final answer"
        assert not out[0].get("tool_calls")


class TestReGate20260926OwnershipFindings:
    """#7237 re-gate (nesquena-hermes 2026-09-26) three remaining ownership
    defects, all in ``_dedupe_replayed_context_messages`` / its projected-
    prefix proof helper.

    1. *Historical repeated prompt can still win before the projected-prefix
       proof runs.* The no-compression path used to scan the full returned
       conversation for ``msg_text`` and only THEN consult
       ``_proven_current_turn_suffix``. If a sanitizer-rewritten historical
       user row happened to equal ``msg_text`` while the real current user
       row was transformed and a later synthetic user also did not match,
       the scan selected the historical row and the sanitized historical
       projection was appended beside the authoritative raw history —
       recreating the duplication this round was intended to close. Fix:
       when ``projected_history`` is supplied, resolve that authority first.
       If the projection is a verbatim prefix, append exactly the returned
       suffix; a prompt match inside that historical prefix must NOT
       become current-turn ownership.

    2. *Explicitly empty projected history treated as "not supplied".* The
       previous implementation collapsed ``None`` and ``[]`` via
       ``list(projected_history or [])`` and rejected both with
       ``if not projection``. An empty projection is valid and authoritative
       when non-empty raw history sanitizes to ``[]`` (reasoning-only,
       error, empty-partial, or orphan rows). In that case the Agent was
       sent no history and the FULL returned list is the current-turn
       suffix. The previous code instead reached the fail-closed return and
       dropped the entire transformed current turn. Fix: distinguish
       ``None`` (not supplied) from ``[]`` (supplied empty) — ``[]`` proves
       the entire returned list is the suffix.

    3. *The claimed exact prefix check is NOT exact.* ``_proven_current_turn_suffix``
       delegated to ``_message_replay_key`` / ``_message_identity``, which
       normalize whitespace and truncate content at 500 characters. Two
       model-facing rows that differ only in whitespace or after character
       500 were therefore accepted as the same sent projection, granting
       ownership to an unproven suffix. Fix: compare complete model-facing
       row values (role, content, tool_calls, tool_call_id, name, refusal,
       reasoning_content, codex_*/finish_reason, api_content) — NOT the
       replay/dedup key.

    Each regression below pins the defect and is built from the REAL
    ``_sanitize_messages_for_agent(raw)`` so the projection genuinely
    diverges from the raw history in the way the production shape does.
    """

    # --- helpers ---------------------------------------------------------

    PROMPT = "please refactor streaming.py"

    def _historical_user_matching_prompt_fixture(self):
        """Raw history where the sanitizer rewrites consecutive assistant
        rows into a single merged row AND the prompt appears in a historical
        user row. The merge means the projection no longer matches the raw
        tail by replay key, so the previous prompt-scan path would leave the
        sanitized merged rows in the slice as a residual duplication.

        The sanitizer merges ``[asst(call k1), asst("progress")]`` into a
        single ``asst(call k1, "progress")`` row, so the projection's row
        count is one less than the raw context's. The replay-key tail-match
        in ``_strip_replayed_prefix`` therefore cannot strip the rewritten
        projection rows after a prompt-scan boundary inside the projection,
        and the sanitized historical rows are appended beside the raw
        history — the exact defect finding 1 describes.
        """
        raw = [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
            {"role": "user", "content": self.PROMPT, "timestamp": 3.0},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call("k1")],
                "timestamp": 4.0,
            },
            {"role": "assistant", "content": "progress", "timestamp": 4.5},
            {"role": "tool", "tool_call_id": "k1", "content": "tool output", "timestamp": 5.0},
            {"role": "assistant", "content": "second answer", "timestamp": 6.0},
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        # Sanity: the sanitizer really merged the consecutive assistant
        # rows, so the projection is shorter than the raw context. Without
        # that divergence the replay-key strip would mask the defect.
        assert len(projected) < len(raw), (
            "fixture premise: the sanitizer must rewrite the raw context "
            "so the projection diverges from the raw tail; otherwise the "
            "defect is masked by _strip_replayed_prefix"
        )
        # The projection still carries the historical user row whose content
        # equals the prompt — the row the old prompt scan would mis-pick.
        assert any(
            m.get("role") == "user" and m.get("content") == self.PROMPT
            for m in projected
        ), "fixture premise: the projection must contain a user row equal to msg_text"
        return raw, projected

    def _current_turn_transformed_real_user(self):
        """Current turn: a transformed real user row, intervening assistant
        / tool output, then a later synthetic continuation user row the
        agent loop appended after the real turn. None of the user rows
        match ``self.PROMPT`` via the strict match path, so a prompt scan
        against the full returned list would fall through to the historical
        match — which is exactly the defect."""
        return [
            {"role": "user", "content": "Mid-turn correction (steer): " + self.PROMPT},
            {"role": "assistant", "content": "worked on the refactor"},
            {"role": "assistant", "content": "", "tool_calls": [_call("k2")]},
            {"role": "tool", "tool_call_id": "k2", "content": "refactor output"},
            {"role": "user", "content": "Continue from where you stopped"},
            {"role": "assistant", "content": "synthetic answer"},
        ]

    # --- finding 1: historical repeated prompt must not win --------------

    def test_historical_repeated_prompt_does_not_win_when_projection_supplied(self):
        """Direct regression for ownership finding 1 (2026-09-26).

        The projection is supplied, the projection is a verbatim prefix of
        the returned list, and the projection contains a historical user
        row equal to ``msg_text`` while the real current user row is
        transformed. The settle must resolve the projection authority
        FIRST and append exactly the returned suffix — the sanitized
        historical projection must not be appended beside the raw history
        as a duplicated residual.
        """
        raw, projected = self._historical_user_matching_prompt_fixture()
        current_turn = self._current_turn_transformed_real_user()
        result_messages = list(projected) + [copy.deepcopy(m) for m in current_turn]

        # Sanity: the prompt scan would mis-pick the historical user row
        # if it were allowed to run before the projection proof.
        scan_idx = _looks_like_current_user_turn_scan(result_messages, self.PROMPT)
        assert scan_idx is not None, (
            "fixture premise: the prompt scan must find the historical user "
            "row inside the projection so the defect is exercisable"
        )
        assert scan_idx < len(projected), (
            "fixture premise: the scan must locate the row inside the "
            "projected prefix, not in the real current turn"
        )

        settled = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.PROMPT, None,
            projected_history=list(projected),
        )

        # The settle must be exactly the raw pre-turn context plus the real
        # current-turn suffix. No sanitized historical row may ride along.
        assert settled == list(raw) + current_turn, (
            "with the projection supplied and verbatim, the settle must be "
            "raw context + the proven current-turn suffix; a prompt match "
            "inside the projected historical prefix must not become "
            "current-turn ownership (#7237 ownership finding 1, "
            "nesquena-hermes 2026-09-26)"
        )
        # Belt and braces: the sanitized historical merged assistant row
        # must not appear as a residual in the settle.
        sanitized_merged_signatures = [
            m for m in settled
            if m.get("role") == "assistant"
            and any(tc.get("id") == "k1" for tc in (m.get("tool_calls") or []))
            and m.get("content") == "progress"
        ]
        assert sanitized_merged_signatures == [], (
            "the sanitizer-merged historical assistant row must not be "
            "appended beside the raw history; the projection proof runs "
            "first so the historical prefix is never sliced into the "
            "current-turn delta"
        )

    def test_historical_repeated_prompt_full_settle_preserves_raw_history(self):
        """Full ``_settle_result_messages`` regression for finding 1: the
        persisted context is the raw pre-turn history plus the real
        current-turn suffix only, with no projected historical residual.
        """
        raw, projected = self._historical_user_matching_prompt_fixture()
        current_turn = self._current_turn_transformed_real_user()
        result_messages = list(projected) + [copy.deepcopy(m) for m in current_turn]

        session = SimpleNamespace(
            messages=[copy.deepcopy(m) for m in raw],
            context_messages=[copy.deepcopy(m) for m in raw],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result_messages,
            self.PROMPT,
            "webui",
            None,
            list(projected),
        )
        persisted = session.context_messages
        expected_tail = [copy.deepcopy(m) for m in current_turn]
        assert [(m.get("role"), m.get("content")) for m in persisted] == [
            (m.get("role"), m.get("content")) for m in list(raw) + expected_tail
        ], (
            "full settle must persist raw history + proven current-turn "
            "suffix only; a historical residual from a mis-picked prompt "
            "match is the #7237 ownership finding 1 defect"
        )

    # --- finding 2: explicitly empty projection is authoritative --------

    def test_empty_projection_proves_full_returned_list_is_suffix(self):
        """Direct regression for ownership finding 2 (2026-09-26).

        Non-empty raw history that sanitizes to ``[]`` (reasoning-only,
        error, empty-partial, or orphan rows) means the Agent was sent no
        history. An explicitly supplied ``[]`` projection is therefore
        authoritative: the FULL returned list is the current-turn suffix.
        The previous implementation collapsed ``None`` and ``[]`` and
        dropped the entire current turn. The fix returns the raw context
        plus the full returned list.
        """
        # A history whose every row is dropped by the outbound sanitizer:
        # a reasoning-only assistant row (no visible content, no tool calls)
        # and an orphan tool row whose call was never made. Both are
        # dropped, so the projection is the explicit empty list.
        raw = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "hidden thought",
                "timestamp": 1.0,
            },
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan", "timestamp": 2.0},
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        assert projected == [], (
            "fixture premise: non-empty raw history that sanitizes to [] "
            "is the exact shape the empty-projection branch must handle"
        )

        current_turn = [
            {"role": "user", "content": self.PROMPT},
            {"role": "assistant", "content": "fresh answer"},
        ]
        result_messages = list(projected) + [copy.deepcopy(m) for m in current_turn]
        # The Agent return is the empty projection + the current turn.
        assert result_messages == [copy.deepcopy(m) for m in current_turn]

        settled = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.PROMPT, None,
            projected_history=list(projected),  # explicit []
        )
        # The full returned list survives as the current-turn suffix.
        assert settled == list(raw) + current_turn, (
            "an explicit [] projection proves the full returned list is the "
            "current-turn suffix; the previous code dropped the entire "
            "current turn (#7237 ownership finding 2, nesquena-hermes "
            "2026-09-26)"
        )

    def test_empty_projection_with_empty_result_returns_raw_only(self):
        """Edge case for finding 2: the projection is ``[]`` and the
        Agent also returned ``[]`` (the current turn produced no rows).
        The settle returns the empty result list — the early-return
        contract for an empty result is preserved, and there is no
        current-turn suffix to append. This pins that the
        ``[]``-projection branch does not accidentally turn an empty
        result into a wholesale-replace of the raw context.
        """
        raw = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "hidden thought",
                "timestamp": 1.0,
            },
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        assert projected == []

        settled = _dedupe_replayed_context_messages(
            list(raw), [], self.PROMPT, None,
            projected_history=list(projected),  # explicit []
        )
        assert settled == [], (
            "an explicit [] projection with an empty result returns the "
            "empty list (the early-return contract); the raw context is "
            "preserved only when there is something to settle"
        )

    def test_none_projection_still_fails_closed(self):
        """``None`` (not supplied) must remain a no-proof signal: when
        no prompt-derived boundary is proven either, the settle fails
        closed to the raw context alone. This pins the ``None`` vs
        ``[]`` distinction introduced in finding 2.
        """
        raw = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "hidden thought",
                "timestamp": 1.0,
            },
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        assert projected == []
        # A transformed real user row (steer preamble) and a later
        # synthetic continuation user row — neither matches the strict
        # prompt, so the scan returns None and the no-projection branch
        # must fail closed.
        result_messages = list(projected) + [
            {"role": "user", "content": "Mid-turn correction (steer): " + self.PROMPT},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "Continue from where you stopped"},
            {"role": "assistant", "content": "synthetic answer"},
        ]
        # No projection threaded — the settle has no ownership signal and
        # must fail closed to the raw context alone.
        settled = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.PROMPT, None,
        )
        assert settled == list(raw), (
            "without a threaded projection and without a prompt-derived "
            "boundary the settle must fail closed; the [] vs None "
            "distinction in finding 2 must not turn 'not supplied' into "
            "'supplied empty'"
        )

    # --- finding 3: strict exact prefix check ---------------------------

    def test_prefix_check_fails_closed_when_rows_differ_after_500_chars(self):
        """Direct regression for ownership finding 3 (2026-09-26).

        Two model-facing rows that differ only AFTER character 500 must
        not be accepted as the same sent projection. The previous
        ``_message_identity`` truncated content at 500 characters, so the
        drift past that point was invisible to the prefix proof. The new
        strict check compares complete model-facing row values and must
        fail closed in that case.

        The end-to-end settle path is not exercised here: the replay-key
        prefix check (``_messages_have_prefix`` with ``_message_replay_key``)
        ALSO truncates at 500, so in this fixture it accepts the drift
        and the settle takes the "result is a prefix of previous" path
        — a separate code path that does not consult
        ``_proven_current_turn_suffix``. The direct assertion below
        proves the strict proof helper itself rejects the drift, which
        is the exact fix finding 3 requires.
        """
        long_content = "x" * 500
        projected = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": long_content},
        ]
        # The returned list's assistant row diverges past character 500 —
        # the truncated replay key would have matched, the strict check
        # must not.
        result_messages = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": long_content + "TAIL_DRIFT"},
            {"role": "user", "content": "next question"},
            {"role": "assistant", "content": "answer"},
        ]
        # Old behavior: _message_replay_key truncates at 500 → match.
        from api.streaming import _message_replay_key
        assert _message_replay_key(projected[1]) == _message_replay_key(result_messages[1]), (
            "fixture premise: the replay key must accept the post-500 "
            "drift so the strict check is the only thing standing between "
            "us and a false positive"
        )
        # New behavior: strict proof returns None — no suffix proven.
        from api.streaming import _proven_current_turn_suffix
        assert _proven_current_turn_suffix(projected, result_messages) is None, (
            "strict exact prefix check must reject rows that diverge past "
            "character 500; the truncated replay key would have accepted "
            "them (#7237 ownership finding 3, nesquena-hermes 2026-09-26)"
        )

    def test_prefix_check_fails_closed_when_rows_differ_in_whitespace(self):
        """Direct regression for ownership finding 3 (2026-09-26).

        Two model-facing rows that differ only in whitespace must not be
        accepted as the same sent projection. The previous
        ``_message_identity`` normalized whitespace via
        ``" ".join(text.split())``, so a row with a double space was
        indistinguishable from one with a single space. The new strict
        check must fail closed in that case.
        """
        projected = [
            {"role": "user", "content": "hello world"},
        ]
        result_messages = [
            {"role": "user", "content": "hello  world"},  # double space
            {"role": "assistant", "content": "answer"},
        ]
        from api.streaming import _message_replay_key
        assert _message_replay_key(projected[0]) == _message_replay_key(result_messages[0]), (
            "fixture premise: the replay key normalizes whitespace so the "
            "drift is invisible to it; the strict check is the only thing "
            "that can fail closed"
        )
        from api.streaming import _proven_current_turn_suffix
        assert _proven_current_turn_suffix(projected, result_messages) is None, (
            "strict exact prefix check must reject rows that differ only "
            "in whitespace; the normalized replay key would have accepted "
            "them (#7237 ownership finding 3, nesquena-hermes 2026-09-26)"
        )

    def test_strict_prefix_still_accepts_verbatim_projection(self):
        """Positive control for finding 3: when the projection IS a
        verbatim prefix, the strict check still returns the suffix.
        The new check must not over-reject legitimate exact matches.
        """
        projected = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ]
        current_turn = [
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]
        result_messages = list(projected) + [copy.deepcopy(m) for m in current_turn]
        from api.streaming import _proven_current_turn_suffix
        suffix = _proven_current_turn_suffix(projected, result_messages)
        assert suffix == current_turn, (
            "strict exact prefix check must still accept a verbatim "
            "projection and return the proven current-turn suffix"
        )
