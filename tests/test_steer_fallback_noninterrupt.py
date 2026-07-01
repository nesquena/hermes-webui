from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:i]
    raise AssertionError(f"function body not found for {name}")


def test_steer_fallback_preserves_active_stream_without_forcing_interrupt():
    """Fallbacks must either queue intentionally or restore recovery UI, never cancel the current turn."""
    body = _function_body(COMMANDS_JS, "_trySteer")

    assert "queueSessionMessage" in body, "gateway/busy fallback should keep text for a later turn"
    assert "_showSteerRecovery" in body, "explicit legacy steer failure should restore the recovery banner"
    assert "gateway_steer_unavailable" in body, "gateway unsupported fallback must be explicit"
    assert "cancelStream" not in body, "steer fallback must not interrupt the active stream"
    assert "steer_leftover_queued" in body or "cmd_steer_fallback" in body or "busy_steer_fallback" in body


def test_interrupt_command_still_cancels_active_stream():
    body = _function_body(COMMANDS_JS, "cmdInterrupt")

    assert "queueSessionMessage" in body
    assert "cancelStream" in body, "/interrupt should remain the explicit cancel-and-send command"
