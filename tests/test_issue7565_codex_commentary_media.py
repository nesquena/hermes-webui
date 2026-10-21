"""Regression coverage for issue #7565: session MEDIA authorization
and snapshot capture must also inspect typed public assistant
commentary carried in ``codex_message_items`` (Agent phase:
"commentary"). The Agent producer (``agent/codex_responses_adapter.py``)
persists such items with ``role: assistant`` and a normalized phase;
the WebUI's auth predicate (``api/routes.py::
_session_media_token_allows_path``) and snapshot annotator
(``api/media_snapshots.py::annotate_media_snapshots``) previously
only scanned top-level assistant ``content``, so a safe image
emitted only inside commentary returned 403 and was never
snapshotted at settle time.
"""
from __future__ import annotations

from api.media_snapshots import codex_commentary_text


# ── unit tests for the shared helper ─────────────────────────────────────────


def _commentary_row(text="Synthetic preview is ready.\n\nMEDIA:/opt/artifacts/preview.png",
                    phase="commentary",
                    item_type="message",
                    item_role="assistant",
                    outer_role="assistant"):
    return {
        "role": outer_role,
        "content": "",
        "codex_message_items": [
            {
                "type": item_type,
                "role": item_role,
                "phase": phase,
                "content": [
                    {"type": "output_text", "text": text},
                ],
            }
        ],
    }


def test_commentary_text_returns_text_for_valid_shape():
    """The valid-shape case from the issue body — outer role
    assistant, item type message, item role assistant, phase
    commentary, list of output_text parts with a string text —
    must surface the embedded text so the auth predicate and
    snapshot scan can both find the MEDIA: token."""
    row = _commentary_row(text="MEDIA:/opt/artifacts/preview.png")
    text = codex_commentary_text(row)
    assert "MEDIA:/opt/artifacts/preview.png" in text, (
        "valid commentary shape must surface the textual MEDIA: "
        "token so the auth predicate can resolve it"
    )


def test_commentary_text_rejects_user_outer_role():
    """A user-row sidecar must not mint grants even when the
    sidecar contains a commentary-shaped item, preserving the
    implicit threat model that user-authored content cannot
    mint allow-list entries."""
    row = _commentary_row(outer_role="user", text="MEDIA:/etc/passwd")
    assert codex_commentary_text(row) == "", (
        "user outer role must short-circuit before the sidecar is read"
    )


def test_commentary_text_rejects_wrong_item_type():
    """Item type must be exactly ``message``. Reasoning, function
    call, and other non-message items must not be inspected even
    when their content is shaped like commentary."""
    row = _commentary_row(item_type="reasoning", text="MEDIA:/opt/x.png")
    assert codex_commentary_text(row) == ""


def test_commentary_text_rejects_wrong_item_role():
    """Item role must be exactly ``assistant``. User-sidecar
    assistant commentary (an item with role: user nested inside
    an outer assistant row) must not mint grants."""
    row = _commentary_row(item_role="user", text="MEDIA:/opt/x.png")
    assert codex_commentary_text(row) == ""


def test_commentary_text_rejects_wrong_phase():
    """Phase must normalize to exactly ``commentary``. Analysis,
    final, and arbitrary phases must not be inspected, even when
    the row carries a textual output_text part with a MEDIA:
    token. ``"Commentary"`` with a capital C is accepted because
    the helper lowercases the phase; the others are rejected."""
    for wrong_phase in ("analysis", "final", "tool_call", ""):
        row = _commentary_row(phase=wrong_phase, text="MEDIA:/opt/x.png")
        assert codex_commentary_text(row) == "", (
            f"phase={wrong_phase!r} must not be inspected"
        )


def test_commentary_text_accepts_case_insensitive_commentary():
    """Phase matching is case-insensitive; the Agent producer
    stamps a normalized phase, but the helper must still accept
    variants so a future producer change cannot silently break
    the auth predicate."""
    row = _commentary_row(phase="Commentary", text="MEDIA:/opt/x.png")
    assert codex_commentary_text(row) != ""


def test_commentary_text_rejects_non_output_text_parts():
    """Only textual ``output_text`` parts are read. Tool-call
    parts, image parts, file parts, and arbitrary typed parts
    must not be inspected even if they happen to carry a
    MEDIA-shaped string."""
    row = {
        "role": "assistant",
        "content": "",
        "codex_message_items": [
            {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [
                    {"type": "tool_call", "name": "x", "text": "MEDIA:/opt/x.png"},
                    {"type": "image", "text": "MEDIA:/opt/x.png"},
                    {"type": "input_text", "text": "MEDIA:/opt/x.png"},
                ],
            }
        ],
    }
    assert codex_commentary_text(row) == "", (
        "non-output_text parts must not be inspected even when they "
        "carry a MEDIA-shaped string"
    )


def test_commentary_text_rejects_non_text_output_text_part():
    """``output_text`` parts with a non-string ``text`` value must
    be skipped, not coerced. This pins the fail-closed contract:
    the helper will not stringify a dict, list, or number just
    because the part type is correct."""
    row = {
        "role": "assistant",
        "content": "",
        "codex_message_items": [
            {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [
                    {"type": "output_text", "text": {"nested": "MEDIA:/opt/x.png"}},
                    {"type": "output_text", "text": ["MEDIA:/opt/x.png"]},
                    {"type": "output_text", "text": 42},
                ],
            }
        ],
    }
    assert codex_commentary_text(row) == ""


def test_commentary_text_handles_malformed_messages():
    """The helper must never raise on malformed input. Empty,
    None, list, string, and arbitrary objects must all return
    the safe empty default."""
    assert codex_commentary_text(None) == ""
    assert codex_commentary_text("") == ""
    assert codex_commentary_text([]) == ""
    assert codex_commentary_text({}) == ""
    assert codex_commentary_text({"role": "assistant"}) == ""  # no codex_message_items
    assert codex_commentary_text({"role": "assistant", "codex_message_items": "not a list"}) == ""
    # Malformed item shape: not a dict.
    assert codex_commentary_text({
        "role": "assistant",
        "codex_message_items": [None, "string", 42, ["list"]],
    }) == ""


# ── auth-predicate integration via the public surface ───────────────────────


def test_session_media_token_allows_path_imports_commentary_helper():
    """The authorization predicate must import and use the
    commentary helper so a safe image emitted only inside
    commentary gets the same exact-path grant as a top-level
    one. Without the import, the auth surface silently regresses
    to the top-level-only shape that #7565 reports."""
    src = open("api/routes.py", encoding="utf-8").read()
    assert "from api.media_snapshots import codex_commentary_text" in src
    # The helper is actually consumed (not just imported) inside
    # the predicate body.
    auth_idx = src.find("def _session_media_token_allows_path")
    assert auth_idx != -1
    end_idx = src.find("\n\ndef ", auth_idx + 1)
    if end_idx == -1:
        end_idx = len(src)
    body = src[auth_idx:end_idx]
    assert "codex_commentary_text(message)" in body, (
        "the auth predicate must consult the commentary helper so "
        "tokens emitted only inside commentary are honored"
    )


def test_annotate_media_snapshots_uses_commentary_helper():
    """The snapshot annotator must also use the commentary helper
    so commentary-emitted artifacts are snapshotted at settle time
    rather than only the first time the live file is read."""
    src = open("api/media_snapshots.py", encoding="utf-8").read()
    # The helper is defined at module scope.
    assert "def codex_commentary_text(" in src
    # The annotator consumes it.
    annotate_idx = src.find("def annotate_media_snapshots(")
    assert annotate_idx != -1
    end_idx = src.find("\n\ndef ", annotate_idx + 1)
    if end_idx == -1:
        end_idx = len(src)
    body = src[annotate_idx:end_idx]
    assert "codex_commentary_text(msg)" in body, (
        "the snapshot annotator must consult the commentary helper "
        "so commentary-emitted artifacts are captured at settle time"
    )
