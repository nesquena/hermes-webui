"""Shortcut remap behavior checks for #3954."""

import base64
import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
ARCH = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_B14_MARKER = "if((e.metaKey||e.ctrlKey)&&e.shiftKey&&!e.altKey&&(e.key==='o'||e.key==='O'))"


def _extract_block(source: str, marker: str, *, label: str) -> str:
    start = source.find(marker)
    assert start >= 0, f"{label} marker not found in source"
    open_brace = source.index("{", start)
    depth = 1
    index = open_brace + 1
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"Failed to parse {label} block"
    return source[start : index]


@lru_cache(maxsize=1)
def _b14_statement() -> str:
    return _extract_block(BOOT_JS, _B14_MARKER, label="B14")


def _run_b14_handler(cases):
    payload = {
        "cases": cases,
        "block_b64": base64.b64encode(_b14_statement().encode("utf-8")).decode("ascii"),
    }

    script = r'''
const payload = JSON.parse(process.env.B14_PAYLOAD);
const block = Buffer.from(payload.block_b64, "base64").toString("utf-8");

(async () => {
  const results = [];

  for (const scenario of payload.cases) {
    const calls = [];
    const event = Object.assign({
      altKey: false,
      shiftKey: false,
      metaKey: false,
      ctrlKey: false,
      key: "",
      target: null,
      preventDefault: () => calls.push("preventDefault"),
      stopPropagation: () => {}
    }, scenario.event);

    const $ = (selector) => {
      if (selector === "msg") return { focus: () => calls.push("focus") };
      if (selector === "approvalCard")
        return { classList: { contains: () => false } };
      return null;
    };

    const runner = new Function(
      "e",
      "S",
      "$",
      "newSession",
      "renderSessionList",
      "closeMobileSidebar",
      "_restoreRememberedNewChatDraftSession",
      "return (async () => {" + block + "})();"
    );

    try {
      await runner(
        event,
        scenario.session,
        $,
        async () => calls.push("newSession"),
        async () => calls.push("renderSessionList"),
        () => calls.push("closeMobileSidebar"),
        async () => {
          if (scenario.restoreRememberedDraft) {
            calls.push("restoreRememberedDraft");
            return true;
          }
          return false;
        }
      );
      results.push({
        name: scenario.name,
        calls,
        prevented: calls.includes("preventDefault"),
        error: null
      });
    } catch (error) {
      results.push({
        name: scenario.name,
        calls,
        prevented: calls.includes("preventDefault"),
        error: String(error)
      });
    }
  }

  process.stdout.write(JSON.stringify(results));
})();
'''

    env = os.environ.copy()
    env["B14_PAYLOAD"] = json.dumps(payload)
    out = subprocess.check_output([NODE, "-e", script], text=True, env=env)
    return json.loads(out)


def _event(*, ctrl_key: bool, meta_key: bool, shift_key: bool, key: str, alt_key=False):
    return {
        "ctrlKey": ctrl_key,
        "metaKey": meta_key,
        "shiftKey": shift_key,
        "altKey": alt_key,
        "key": key,
    }


def _empty_idle_session():
    return {
        "message_count": 0,
        "active_stream_id": None,
        "pending_user_message": False,
    }


def _streaming_session():
    return {
        "message_count": 1,
        "active_stream_id": "stream-xyz",
        "pending_user_message": True,
        "busy": True,
    }


def _scenario(name, *, event, session_idle=False, include_busy=False):
    base_session = {"workspace": "default"}
    if session_idle:
        base_session["session"] = _empty_idle_session()
        base_session["busy"] = False
    else:
        base_session["session"] = _streaming_session() if include_busy else {"message_count": 1}
        base_session["busy"] = include_busy
    return {"name": name, "event": event, "session": base_session}


def _expect_order(result, expected):
    assert not result["error"], f"Handler crashed: {result['error']}"
    assert result["calls"] == expected, f"{result['name']} call order mismatch: {result['calls']}"


def test_ctrl_k_no_longer_creates_new_chat():
    """Ctrl/Cmd+K should be ignored and must not create a new session."""
    results = _run_b14_handler([
        _scenario("ctrl_k", event=_event(ctrl_key=True, meta_key=False, shift_key=False, key="k")),
        _scenario("cmd_k", event=_event(ctrl_key=False, meta_key=True, shift_key=False, key="k")),
    ])
    by_name = {row["name"]: row for row in results}

    for name in ("ctrl_k", "cmd_k"):
        row = by_name[name]
        assert row["error"] is None, f"{name}: handler crashed {row['error']}"
        assert not row["prevented"], f"{name}: must not call e.preventDefault()"
        assert "newSession" not in row["calls"], f"{name}: must not call newSession()"
        assert row["calls"] == [], f"{name}: unexpected calls: {row['calls']}"


def test_ctrl_shift_o_creates_new_chat_and_keeps_mobile_close_sequence():
    """Ctrl+Shift+O must create and activate the new chat surface."""
    results = _run_b14_handler([
        _scenario(
            "ctrl_shift_o",
            event=_event(ctrl_key=True, meta_key=False, shift_key=True, key="o"),
            include_busy=True,
        ),
    ])
    row = results[0]
    assert row["error"] is None, f"ctrl_shift_o: handler crashed {row['error']}"
    assert row["prevented"], "ctrl_shift_o: must call e.preventDefault()"
    _expect_order(
        row,
        ["preventDefault", "newSession", "renderSessionList", "closeMobileSidebar", "focus"],
    )


def test_cmd_shift_o_creates_new_chat():
    """Cmd+Shift+O must follow the same new-chat sequence."""
    results = _run_b14_handler([
        _scenario(
            "cmd_shift_o",
            event=_event(ctrl_key=False, meta_key=True, shift_key=True, key="O"),
            include_busy=True,
        ),
    ])
    row = results[0]
    assert row["error"] is None, f"cmd_shift_o: handler crashed {row['error']}"
    assert row["prevented"], "cmd_shift_o: must call e.preventDefault()"
    _expect_order(
        row,
        ["preventDefault", "newSession", "renderSessionList", "closeMobileSidebar", "focus"],
    )


def test_ctrl_shift_o_keeps_empty_idle_guard():
    """Empty, in-flight-free sessions must only focus the composer."""
    results = _run_b14_handler([
        _scenario(
            "ctrl_shift_o_empty",
            event=_event(ctrl_key=True, meta_key=False, shift_key=True, key="o"),
            session_idle=True,
        ),
        _scenario(
            "cmd_shift_o_empty",
            event=_event(ctrl_key=False, meta_key=True, shift_key=True, key="o"),
            session_idle=True,
        ),
    ])
    by_name = {row["name"]: row for row in results}
    expected = ["preventDefault", "focus"]
    for name, row in by_name.items():
        assert row["error"] is None, f"{name}: handler crashed {row['error']}"
        assert row["prevented"], f"{name}: should prevent default before guard returns"
        assert row["calls"] == expected, f"{name}: expected {expected}, got {row['calls']}"


def test_ctrl_shift_o_restores_remembered_draft_before_new_session():
    """The shortcut should match the New Conversation button's draft restore path."""
    results = _run_b14_handler([
        {
            **_scenario(
                "ctrl_shift_o_restore",
                event=_event(ctrl_key=True, meta_key=False, shift_key=True, key="o"),
                include_busy=True,
            ),
            "restoreRememberedDraft": True,
        },
    ])
    row = results[0]
    assert row["error"] is None, f"ctrl_shift_o_restore: handler crashed {row['error']}"
    assert row["prevented"], "ctrl_shift_o_restore: must call e.preventDefault()"
    _expect_order(
        row,
        ["preventDefault", "restoreRememberedDraft", "renderSessionList", "closeMobileSidebar", "focus"],
    )


def test_new_chat_tooltip_advertises_shift_o():
    """The visible tooltip should advertise Cmd+Shift+O."""
    assert "New conversation (Cmd+Shift+O)" in INDEX
    assert "Cmd/Ctrl+Shift+O" in ARCH


def test_ctrl_shift_o_binding_is_unique_in_checked_shortcut_surface():
    """Keep uniqueness and legacy-surface checks for the remapped binding."""
    assert BOOT_JS.count(_B14_MARKER) == 1
    assert "Cmd/Ctrl+K" not in ARCH and "Cmd/Ctrl+K" not in INDEX
    assert "(e.key==='k')" not in BOOT_JS
    assert _B14_MARKER in BOOT_JS
