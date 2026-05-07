"""Regression coverage for issue #1820: no-agent cron edits do not require prompts."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text()


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    paren = PANELS_JS.find("(", start)
    assert paren != -1, f"{name} params not found"
    depth = 0
    for idx in range(paren, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = PANELS_JS.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


def test_open_cron_edit_plumbs_no_agent_and_script_to_form():
    body = _function_body("openCronEdit")
    assert "no_agent: !!job.no_agent" in body
    assert "script: job.script || ''" in body


def test_no_agent_form_drops_prompt_required_attribute_and_shows_script_context():
    body = _function_body("_renderCronForm")
    assert "no_agent" in body and "script" in body
    assert "const isNoAgent = !!no_agent;" in body
    assert "cron-no-agent-hint" in body
    assert "No-agent script" in body
    assert "${isNoAgent ? ' disabled' : ' required'}" in body


def test_save_cron_form_keeps_agent_prompt_required_but_skips_no_agent_edits():
    body = _function_body("saveCronForm")
    assert "const isNoAgent = !!(_currentCronDetail && _currentCronDetail.no_agent);" in body
    assert "if(!isNoAgent && !prompt)" in body
    assert "cron_prompt_required" in body
    assert "if (!isNoAgent) updates.prompt = prompt;" in body


def test_no_agent_detail_displays_mode_and_script():
    body = _function_body("_renderCronDetail")
    assert "const isNoAgent = !!job.no_agent;" in body
    assert "No-agent script" in body
    assert "cronJobMode" in body
    assert "job.script" in body
