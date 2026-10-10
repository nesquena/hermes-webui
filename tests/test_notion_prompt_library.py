"""Tests for the Notion prompt library integration (api/prompts_notion.py).

Covers the three pieces the WebUI depends on:
  - _row_to_palette_entry: the publish gate (Status Ready/Tested AND Surfaces
    contains "Toolbelt" AND non-empty Trigger) from the library's contract
  - _extract_prompt_body: pulling the prompt text out of the "**The prompt**"
    code-block section of a library page's markdown
  - fill_variables: {{var}} replacement with unfilled placeholders preserved
  - _parseNotionPromptArgs-style key=value token parsing lives client-side;
    _derive_variables mirrors it for the save-back path

No network: everything runs against fixture payloads.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.prompts_notion import (  # noqa: E402
    _derive_trigger,
    _derive_variables,
    _extract_prompt_body,
    _row_to_palette_entry,
    fill_variables,
)


def _row(trigger="", status="", surfaces=(), title="A prompt", use_when=""):
    def rich(value):
        return {"rich_text": [{"plain_text": value}]}

    return {
        "id": "page-1",
        "url": "https://app.notion.com/p/page-1",
        "properties": {
            "Prompt": {"title": [{"plain_text": title}]},
            "Trigger": rich(trigger),
            "Variables": rich("target, code"),
            "Use when": rich(use_when),
            "Status": {"select": {"name": status}} if status else {"select": None},
            "Surfaces": {"multi_select": [{"name": s} for s in surfaces]},
        },
    }


def test_publish_gate_requires_status_and_surface():
    assert _row_to_palette_entry(_row(trigger="/cr", status="Ready", surfaces=["Toolbelt"])) is not None
    assert _row_to_palette_entry(_row(trigger="/cr", status="Tested", surfaces=["Toolbelt", "Agent"])) is not None
    # Draft stays invisible
    assert _row_to_palette_entry(_row(trigger="/cr", status="Draft", surfaces=["Toolbelt"])) is None
    # No Toolbelt surface stays invisible
    assert _row_to_palette_entry(_row(trigger="/cr", status="Ready", surfaces=["Obsidian"])) is None
    # Missing trigger stays invisible
    assert _row_to_palette_entry(_row(trigger="", status="Ready", surfaces=["Toolbelt"])) is None


def test_palette_entry_shape():
    entry = _row_to_palette_entry(_row(trigger="cr", status="Ready", surfaces=["Toolbelt"], title="Code Review", use_when="Any diff"))
    assert entry is not None
    assert entry["trigger"] == "/cr"  # leading slash normalised
    assert entry["label"] == "Code Review"
    assert entry["variables"] == ["target", "code"]
    assert entry["use_when"] == "Any diff"
    assert entry["source"] == "notion"


def test_extract_prompt_body_from_code_fence():
    md = (
        "**Use it**\n`/cr target=x`\n\n"
        "**Variables**\nsome table text\n\n"
        "**The prompt**\n```javascript\nReview this.\n\nTARGET: {{target}}\n```\n\n"
        "**Notes**\n- trailing"
    )
    body = _extract_prompt_body(md)
    assert "Review this." in body
    assert "{{target}}" in body
    assert "Notes" not in body
    assert "Use it" not in body


def test_extract_prompt_body_without_fence_falls_back_to_text():
    md = "**The prompt**\nLine one\nLine two\n\n**Notes**\nother"
    assert _extract_prompt_body(md) == "Line one\nLine two"


def test_extract_prompt_body_missing_heading():
    assert _extract_prompt_body("no headings here") == ""


def test_fill_variables_replaces_and_preserves():
    body = "TARGET: {{target}}\nFOCUS: {{focus}}\nCODE\n{{code}}"
    out = fill_variables(body, {"target": "src/a.ts", "code": "let x=1"})
    assert "TARGET: src/a.ts" in out
    assert "let x=1" in out
    assert "{{focus}}" in out  # unfilled stays visible for the agent


def test_fill_variables_handles_spaced_placeholders():
    assert fill_variables("X {{ name }} Y", {"name": "val"}) == "X val Y"


def test_derive_variables_sorted_unique():
    assert _derive_variables("{{b}} {{a}} {{b}}") == "a,b"


def test_derive_trigger_shapes():
    assert _derive_trigger("Code Review").startswith("/")
    assert len(_derive_trigger("Code Review")) <= 15
    assert _derive_trigger("single").startswith("/")
