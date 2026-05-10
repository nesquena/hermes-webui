"""Tests for #1623: SSE app heartbeat must fire well under the kernel keepalive timeout.

Bug shape: server.py's per-connection TCP keepalive (added v0.50.289 / #1581)
declares a peer dead at KEEPIDLE=10s + KEEPINTVL=5s * KEEPCNT=3 = 25s. The
SSE handlers in api/routes.py used a 30s app-level heartbeat. When the LLM
is thinking and the queue is idle, the kernel could tear down the socket
before the app sent its first heartbeat byte — flaky-network drops at ~10s
that the user perceived as "the stream died around 10 seconds in."

Fix: lower the heartbeat to 5s at every SSE handler and pin the inequality
with a regression test so future tuning of either timer can't re-introduce
the misalignment.
"""

from pathlib import Path


REPO = Path(__file__).parent.parent


def test_sse_heartbeat_constant_below_kernel_keepalive_window():
    """The named constant exists and is at most half the kernel keepalive
    timeout (10 + 5*3 = 25s). 5s gives the kernel ~5x headroom."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

    # The constant must be defined.
    assert "_SSE_HEARTBEAT_INTERVAL_SECONDS" in src, (
        "Named SSE heartbeat constant must exist (#1623)"
    )

    # Pull the literal value.
    import re

    m = re.search(r"_SSE_HEARTBEAT_INTERVAL_SECONDS\s*=\s*(\d+)", src)
    assert m, "Could not parse _SSE_HEARTBEAT_INTERVAL_SECONDS literal"
    heartbeat = int(m.group(1))

    # Reproduce the kernel-keepalive window from server.py setsockopt block.
    server_src = (REPO / "server.py").read_text(encoding="utf-8")
    assert "TCP_KEEPIDLE" in server_src, (
        "TCP_KEEPIDLE must be set on accepted connections"
    )
    keepidle = int(re.search(r"TCP_KEEPIDLE.*?(\d+)\)", server_src, re.S).group(1))
    keepintvl = int(re.search(r"TCP_KEEPINTVL.*?(\d+)\)", server_src, re.S).group(1))
    keepcnt = int(re.search(r"TCP_KEEPCNT.*?(\d+)\)", server_src, re.S).group(1))
    kernel_window = keepidle + keepintvl * keepcnt

    # The acceptance criterion from the bug: app heartbeat <= kernel window / 2.
    assert heartbeat * 2 <= kernel_window, (
        f"App SSE heartbeat ({heartbeat}s) must be at most half of the kernel "
        f"keepalive window ({kernel_window}s = {keepidle} + {keepintvl}*{keepcnt}). "
        f"Otherwise flaky-network probes can tear down the socket before the "
        f"app sends a heartbeat byte. (#1623)"
    )


def test_no_sse_handler_uses_30s_or_higher_timeout():
    """No SSE/long-poll handler in routes.py should still be using the old
    30s/25s timeout. Every queue.get(timeout=...) call inside an SSE handler
    must reference the named constant, not a hard-coded number."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

    import re

    # Catch q.get(timeout=30), subscriber.get(timeout=30), term.output.get(timeout=25), etc.
    bad = re.findall(r"\.get\(timeout=3[05]\)", src)
    assert not bad, (
        f"Found {len(bad)} SSE handler call(s) still using a 25/30s timeout: {bad}. "
        "All should use _SSE_HEARTBEAT_INTERVAL_SECONDS (#1623)."
    )


def test_each_named_sse_handler_uses_constant():
    """Each known SSE handler queue-poll site must reference the constant."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

    expected_callers = [
        "subscriber.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)",  # main agent SSE
        "term.output.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)",  # terminal SSE
    ]
    for caller in expected_callers:
        assert caller in src, (
            f"Expected SSE handler to call {caller!r} (#1623). "
            "If this assertion fails, the SSE heartbeat misalignment may have regressed."
        )

    # Also: at least 3 sites should be using the constant overall (main agent,
    # terminal, plus the gateway watcher and approval/clarify pollers).
    n_uses = src.count("get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)")
    assert n_uses >= 4, (
        f"Expected at least 4 SSE/long-poll sites using the named constant; found {n_uses}. "
        "Every long-lived idle queue poll must align below the kernel keepalive window."
    )
