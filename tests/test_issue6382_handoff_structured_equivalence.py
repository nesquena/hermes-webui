"""Issue #6382 handoff — structured equivalence and terminal-prose parity.

Written as the specification for the maintainer review of 2026-09-29
(``4d14336ae834``), which asked for two repairs:

1. Structured message equivalence must not drop identity-bearing fields.
   ``_canonical_tool_calls`` / ``_canonical_message_content`` were narrower
   than the repository's own message authority
   (``api/routes.py::_anchor_scene_content_tool`` accepts ids under
   ``id``/``tid``/``tool_call_id``/``tool_use_id``/``call_id``, names under
   ``name``/``tool_name``/``function.name`` and arguments under
   ``args``/``input``/``function.arguments``).  Two distinct newest
   exchanges could therefore canonicalize identically and a genuine
   exchange was suppressed from the handoff context.

2. Terminal prose classification must match
   ``api/streaming.py::_assistant_content_part_is_tool_use`` (which also
   recognizes ``tool_call`` and untyped alias-shaped declarations) and the
   ordering rule of ``_assistant_message_has_final_visible_text`` (prose
   after the last tool boundary is settled; progress that exists only
   before an unresolved boundary is not).
"""
import copy

from api.routes import (
    _build_handoff_context_messages,
    _extract_latest_completed_exchange,
    _is_terminal_assistant_message,
    _message_content_equivalent,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _source_context():
    return [
        {"role": "system", "content": "Summary of previous conversation."},
        {"role": "user", "content": "Earlier question?"},
        {"role": "assistant", "content": "Earlier answer."},
    ]


def _alias_call(call_id, *, command="echo hi"):
    """Untyped, alias-shaped embedded tool declaration (supported shape)."""
    return {"tool_use_id": call_id, "tool_name": "terminal", "args": {"cmd": command}}


def _assistant(content):
    return {"role": "assistant", "content": content}


# ── 1. Structured equivalence: embedded alias-shaped declarations ────────────


class TestEmbeddedAliasIdentity:
    def test_same_prose_with_different_alias_ids_is_not_equivalent(self):
        a = _assistant([{"type": "text", "text": "Checking the workspace."},
                        _alias_call("A")])
        b = _assistant([{"type": "text", "text": "Checking the workspace."},
                        _alias_call("B")])
        assert _message_content_equivalent(a, b) is False

    def test_same_prose_with_different_alias_args_is_not_equivalent(self):
        a = _assistant([{"type": "text", "text": "Running it."},
                        _alias_call("A", command="ls")])
        b = _assistant([{"type": "text", "text": "Running it."},
                        _alias_call("A", command="rm -rf build")])
        assert _message_content_equivalent(a, b) is False

    def test_distinct_alias_shaped_exchange_is_appended_to_handoff(self):
        """Composition level: the newest, distinct exchange must be carried.

        The user turn is identical in both positions, so only the embedded
        alias-shaped declaration differs — exactly the collision the review
        describes.
        """
        previous_user = {"role": "user", "content": "Run the tests."}
        previous_assistant = _assistant([{"type": "text", "text": "Checking the workspace."},
                                         _alias_call("A")])
        source = _source_context() + [previous_user, previous_assistant]
        last_user = {"role": "user", "content": "Run the tests."}
        last_assistant = _assistant([{"type": "text", "text": "Checking the workspace."},
                                     _alias_call("B")])

        handoff = _build_handoff_context_messages(
            copy.deepcopy(source), last_user, last_assistant
        )

        assert len(handoff) == 1 + len(source) + 2
        assert handoff[-2] == last_user
        assert handoff[-1] == last_assistant

    def test_identical_alias_shaped_exchange_is_still_deduplicated(self):
        """Control: value-identical tail must not be duplicated."""
        user = {"role": "user", "content": "Run the tests."}
        assistant = _assistant([{"type": "text", "text": "Checking the workspace."},
                                _alias_call("A")])
        source = _source_context() + [user, assistant]

        handoff = _build_handoff_context_messages(
            copy.deepcopy(source), copy.deepcopy(user), copy.deepcopy(assistant)
        )

        assert len(handoff) == 1 + len(source)


# ── 1b. Structured equivalence: top-level tool calls ────────────────────────


class TestTopLevelToolCallIdentity:
    def test_top_level_calls_with_different_args_alias_are_not_equivalent(self):
        a = {"role": "assistant", "content": "Running it.",
             "tool_calls": [{"name": "terminal", "args": {"cmd": "ls"}}]}
        b = {"role": "assistant", "content": "Running it.",
             "tool_calls": [{"name": "terminal", "args": {"cmd": "rm -rf build"}}]}
        assert _message_content_equivalent(a, b) is False

    def test_top_level_calls_with_same_args_alias_are_equivalent(self):
        """Control: the same alias-shaped declaration still matches itself."""
        a = {"role": "assistant", "content": "Running it.",
             "tool_calls": [{"id": "c1", "name": "terminal", "args": {"cmd": "ls"}}]}
        b = {"role": "assistant", "content": "Running it.",
             "tool_calls": [{"id": "c1", "name": "terminal", "args": {"cmd": "ls"}}]}
        assert _message_content_equivalent(a, b) is True


# ── 1c. Pure-text collapse only without extra payload ───────────────────────


class TestPureTextCollapse:
    def test_string_and_pure_text_list_stay_equivalent(self):
        """Control: plain string == single pure-text part."""
        a = {"role": "user", "content": "hello there"}
        b = {"role": "user", "content": [{"type": "text", "text": "hello there"}]}
        assert _message_content_equivalent(a, b) is True

    def test_text_part_with_extra_payload_is_not_equivalent_to_plain_string(self):
        a = {"role": "user", "content": "hello there"}
        b = {"role": "user",
             "content": [{"type": "text", "text": "hello there",
                          "cache_control": {"type": "ephemeral"}}]}
        assert _message_content_equivalent(a, b) is False


# ── 2. Terminal prose classification parity ─────────────────────────────────


class TestTerminalProseContract:
    def test_typed_tool_call_part_is_not_terminal(self):
        msg = _assistant([
            {"type": "text", "text": "Working on it..."},
            {"type": "tool_call", "tool_call_id": "X", "name": "terminal",
             "args": {"cmd": "ls"}},
        ])
        assert _is_terminal_assistant_message(msg) is False

    def test_untyped_alias_declaration_is_not_terminal(self):
        msg = _assistant([
            {"type": "text", "text": "Working on it..."},
            _alias_call("X"),
        ])
        assert _is_terminal_assistant_message(msg) is False

    def test_progress_before_tool_boundary_only_is_not_terminal(self):
        """Control: no prose after the boundary."""
        msg = _assistant([
            {"type": "text", "text": "Working on it..."},
            {"type": "tool_use", "id": "X", "name": "terminal", "input": {"cmd": "ls"}},
        ])
        assert _is_terminal_assistant_message(msg) is False

    def test_final_prose_after_tool_boundary_is_terminal(self):
        msg = _assistant([
            {"type": "tool_use", "id": "X", "name": "terminal", "input": {"cmd": "ls"}},
            {"type": "text", "text": "Done: 3 files changed."},
        ])
        assert _is_terminal_assistant_message(msg) is True

    def test_final_prose_after_typed_tool_call_boundary_is_terminal(self):
        msg = _assistant([
            {"type": "tool_call", "tool_call_id": "X", "name": "terminal",
             "args": {"cmd": "ls"}},
            {"type": "text", "text": "Done: nothing to change."},
        ])
        assert _is_terminal_assistant_message(msg) is True


# ── 2b. Composition: extraction across the same boundaries ──────────────────


class TestExtractionComposition:
    def test_progress_with_unresolved_typed_declaration_yields_no_exchange(self):
        messages = [
            {"role": "user", "content": "Run the tests."},
            _assistant([{"type": "text", "text": "Working on it..."},
                        {"type": "tool_call", "tool_call_id": "X",
                         "name": "terminal", "args": {"cmd": "pytest"}}]),
        ]
        assert _extract_latest_completed_exchange(messages) == (None, None)

    def test_progress_with_unresolved_untyped_declaration_yields_no_exchange(self):
        messages = [
            {"role": "user", "content": "Run the tests."},
            _assistant([{"type": "text", "text": "Working on it..."},
                        _alias_call("X")]),
        ]
        assert _extract_latest_completed_exchange(messages) == (None, None)

    def test_prose_after_tool_use_boundary_completes_the_exchange(self):
        messages = [
            {"role": "user", "content": "Run the tests."},
            _assistant([{"type": "tool_use", "id": "X", "name": "terminal",
                         "input": {"cmd": "pytest"}}]),
            {"role": "tool", "tool_call_id": "X", "content": "45 passed"},
            _assistant([{"type": "tool_use", "id": "X", "name": "terminal",
                         "input": {"cmd": "pytest"}},
                        {"type": "text", "text": "45 passed, no failures."}]),
        ]
        user, assistant = _extract_latest_completed_exchange(messages)
        assert user is not None and assistant is not None
        assert assistant["content"][-1]["text"] == "45 passed, no failures."

    def test_typed_tool_call_then_final_prose_completes_the_exchange(self):
        """Control: settled final prose after a resolved typed declaration."""
        messages = [
            {"role": "user", "content": "Run the tests."},
            _assistant([{"type": "tool_call", "tool_call_id": "X",
                         "name": "terminal", "args": {"cmd": "pytest"}}]),
            {"role": "tool", "tool_call_id": "X", "content": "45 passed"},
            _assistant("45 passed, no failures."),
        ]
        user, assistant = _extract_latest_completed_exchange(messages)
        assert user is not None and assistant is not None
        assert assistant["content"] == "45 passed, no failures."
