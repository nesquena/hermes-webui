"""#7681 re-gate round 4 — media-only human turns must count as user turns.

The 10-14 review replayed the real ``Session._compute_user_message_count``
against three supported shapes, using the installed Agent predicate as the
outcome authority and disabling only Agent delegation for the WebUI
fallback call:

* an ordinary image-only user row;
* a merged carrier with an image before the summary delimiter;
* a legacy carrier with an image after its end marker.

The Agent returns **True** for each. The exact fetched base
``adc5dad97621`` counts **1** for each; the previous head counted **0** for
each. The defect was already present on ``beda3d8b1be7`` — a cumulative PR
issue, not a regression from the three newest commits.

Root cause: ``api/compression_anchor.py::_is_internal_user_row`` treats the
absence of extracted text as an internal/blank row
(``if not text: return True``), even though ``_is_blank_user_turn``
correctly accepts non-text payloads (an image is real payload, not blank).
The carrier unwrapping also converted structured blocks into text and
dropped their media, so a media-only ask in front of the delimiter
unwrapped to nothing.

Bounded fix (as prescribed): keep valid non-text blocks when unwrapping
list carriers, and classify structured content rather than text-presence
alone. Mirror the installed Agent's blank-part and delimiter-part
semantics.

The blank-text and standalone-summary negatives are retained, and the
previously submitted image-only ``False`` oracle is corrected to ``True``
(the Agent counts it).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.compression_anchor import (  # noqa: E402
    is_user_originated_turn,
)

# The Agent predicate is the outcome authority when it is importable. These
# tests pin the WebUI FALLBACK path (the branch that runs when the installed
# Agent is unavailable), which is the path that regressed — so they force the
# fallback by pinning the agent predicate lookup to None.
import api.compression_anchor as _ca  # noqa: E402


def _fallback_only(monkeypatch) -> None:
    """Force the self-contained mirror (the regressed path)."""
    monkeypatch.setattr(_ca, "_agent_user_originated_turn", lambda _m: None)
    monkeypatch.setattr(_ca, "_agent_split_user_originated_turn", lambda: None)


_IMAGE_PART = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
}

# The production constant (api/compression_anchor.py): the merged carrier's
# own delimiter. Imported so the fixture can never drift from it.
from api.compression_anchor import _MERGED_SUMMARY_DELIMITER as _MERGED_DELIMITER  # noqa: E402


# ── ① an ordinary image-only user row ───────────────────────────────────


def test_an_ordinary_image_only_user_row_counts(monkeypatch) -> None:
    """The first review shape: a plain image-only turn.

    ``_message_search_text`` extracts no text from an image part, and the
    old ``_is_internal_user_row`` treated that as a blank/internal row —
    so the sidecar counted 0 where the Agent counts 1.
    """
    _fallback_only(monkeypatch)
    row = {"role": "user", "content": [dict(_IMAGE_PART)]}
    assert is_user_originated_turn(row) is True, (
        "an ordinary image-only user row is a real human turn — the Agent "
        "counts it (#7681 media-only finding)"
    )


def test_a_string_content_image_marker_row_counts(monkeypatch) -> None:
    """The same shape with the agent's string-content spelling."""
    _fallback_only(monkeypatch)
    row = {"role": "user", "content": "[image: photo.png]"}
    # A non-empty string is not blank; nothing marks it internal.
    assert is_user_originated_turn(row) is True


# ── ② a merged carrier with an image before the summary delimiter ───────


def test_a_merged_carrier_with_an_image_before_the_delimiter_counts(
    monkeypatch,
) -> None:
    """The second review shape: the live ask is an image in front of the
    merged prior-context delimiter.

    The carrier unwrapping must KEEP the non-text block: joining only the
    text parts dropped the image and left an empty payload, which the old
    classifier read as scaffolding.
    """
    _fallback_only(monkeypatch)
    carrier = {
        "role": "user",
        "content": [
            {"type": "text", "text": "please check this screenshot"},
            dict(_IMAGE_PART),
            {"type": "text", "text": _MERGED_DELIMITER},
            {"type": "text", "text": "[CONTEXT SUMMARY]: prior turns"},
        ],
    }
    assert is_user_originated_turn(carrier) is True, (
        "a merged carrier whose live ask is an image in front of the "
        "delimiter must be counted — the Agent counts it (#7681 media-only "
        "finding)"
    )


def test_a_merged_carrier_with_only_media_before_the_delimiter_counts(
    monkeypatch,
) -> None:
    """The purest form: nothing but the image in front of the delimiter."""
    _fallback_only(monkeypatch)
    carrier = {
        "role": "user",
        "content": [
            dict(_IMAGE_PART),
            {"type": "text", "text": _MERGED_DELIMITER},
            {"type": "text", "text": "[CONTEXT SUMMARY]: prior turns"},
        ],
    }
    assert is_user_originated_turn(carrier) is True


# ── ③ a legacy carrier with an image after its end marker ───────────────


def test_a_legacy_carrier_with_an_image_after_the_end_marker_counts(
    monkeypatch,
) -> None:
    """The third review shape: the legacy carrier's live payload is an
    image after the end marker.

    The string-content spelling carries the marker text only, so the live
    payload rides in a sibling ``parts`` list — the same field the agent's
    reader consults for non-text blocks. The unwrap must look there when
    the string after the marker is empty.
    """
    _fallback_only(monkeypatch)
    carrier = {
        "role": "user",
        "display_kind": "hidden",
        "_compressed_summary": True,
        "content": (
            "[CONTEXT SUMMARY]: prior turns\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, "
            "not the summary above ---"
        ),
        # The live payload rides alongside the marker text as a part.
        "parts": [dict(_IMAGE_PART)],
    }
    assert is_user_originated_turn(carrier) is True, (
        "a legacy carrier whose live payload is an image after the end "
        "marker must be counted — the Agent counts it (#7681 media-only "
        "finding)"
    )


def test_a_legacy_string_carrier_with_nothing_after_the_marker_is_scaffolding(
    monkeypatch,
) -> None:
    """The mirror negative for the string spelling: a marker with no live
    payload in either ``content`` or ``parts`` stays scaffolding."""
    _fallback_only(monkeypatch)
    carrier = {
        "role": "user",
        "display_kind": "hidden",
        "_compressed_summary": True,
        "content": (
            "[CONTEXT SUMMARY]: prior turns\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, "
            "not the summary above ---"
        ),
    }
    assert is_user_originated_turn(carrier) is False, (
        "a legacy string carrier with no live payload is still scaffolding"
    )


def test_a_legacy_carrier_list_form_with_an_image_after_the_marker_counts(
    monkeypatch,
) -> None:
    """The list-content spelling of the legacy carrier."""
    _fallback_only(monkeypatch)
    carrier = {
        "role": "user",
        "display_kind": "hidden",
        "_compressed_summary": True,
        "content": [
            {"type": "text", "text": "[CONTEXT SUMMARY]: prior turns"},
            {
                "type": "text",
                "text": (
                    "--- END OF CONTEXT SUMMARY — respond to the message "
                    "below, not the summary above ---"
                ),
            },
            dict(_IMAGE_PART),
        ],
    }
    assert is_user_originated_turn(carrier) is True


# ── negatives retained: a media-only row is still not a blank echo ───────


def test_a_truly_blank_user_row_is_not_a_turn(monkeypatch) -> None:
    """The mirror must keep rejecting genuinely blank rows: the Agent's
    ``_is_blank_user_turn`` accepts non-text payloads, but an empty text
    part IS blank."""
    _fallback_only(monkeypatch)
    blank_text = {"role": "user", "content": [{"type": "text", "text": "   "}]}
    assert is_user_originated_turn(blank_text) is False, (
        "a whitespace-only text row is still a blank echo, not a turn"
    )
    empty_content = {"role": "user", "content": []}
    assert is_user_originated_turn(empty_content) is False


def test_a_standalone_summary_without_a_live_ask_is_not_a_turn(
    monkeypatch,
) -> None:
    """A pure handoff — no text ask and no media payload — stays
    scaffolding."""
    _fallback_only(monkeypatch)
    handoff = {
        "role": "user",
        "display_kind": "hidden",
        "_compressed_summary": True,
        "content": (
            "[CONTEXT SUMMARY]: prior turns\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, "
            "not the summary above ---\n"
        ),
    }
    assert is_user_originated_turn(handoff) is False, (
        "a standalone summary with no live payload is still scaffolding"
    )


def test_a_marker_row_is_not_a_turn(monkeypatch) -> None:
    """The internal markers keep their exclusion."""
    _fallback_only(monkeypatch)
    marker = {
        "role": "user",
        "content": "[important: background process finished — output follows]",
    }
    assert is_user_originated_turn(marker) is False


# ── the sidecar counter end to end ───────────────────────────────────────


def test_the_sidecar_counter_counts_each_media_shape_once(monkeypatch) -> None:
    """The review's actual probe: ``Session._compute_user_message_count``
    over one row of each shape, each counted exactly once."""
    from api.models import Session

    _fallback_only(monkeypatch)
    messages = [
        {"role": "assistant", "content": "here you go"},
        # ① ordinary image-only row
        {"role": "user", "content": [dict(_IMAGE_PART)]},
        {"role": "assistant", "content": "got it"},
        # ② merged carrier with an image before the delimiter
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "and this one"},
                dict(_IMAGE_PART),
                {"type": "text", "text": _MERGED_DELIMITER},
                {"type": "text", "text": "[CONTEXT SUMMARY]: prior turns"},
            ],
        },
        {"role": "assistant", "content": "noted"},
        # ③ legacy carrier with an image after the end marker
        {
            "role": "user",
            "display_kind": "hidden",
            "_compressed_summary": True,
            "content": [
                {"type": "text", "text": "[CONTEXT SUMMARY]: prior turns"},
                {
                    "type": "text",
                    "text": (
                        "--- END OF CONTEXT SUMMARY — respond to the message "
                        "below, not the summary above ---"
                    ),
                },
                dict(_IMAGE_PART),
            ],
        },
    ]
    assert Session._compute_user_message_count(messages) == 3, (
        "each media-only human turn must count exactly once — the Agent "
        "counts 3, the previous head counted 0 (#7681 media-only finding)"
    )
