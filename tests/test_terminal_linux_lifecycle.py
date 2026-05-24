import queue
import os
import sys
import threading
import time

import pytest


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux-only terminal process lifecycle regression",
)


def test_terminal_survives_short_lived_request_thread(tmp_path):
    # Mirrors ThreadingHTTPServer: the request worker exits after spawning the
    # shell, so terminal lifetime must not be tied to that worker thread.
    from api.terminal import close_terminal, start_terminal, write_terminal

    sid = f"terminal-linux-lifecycle-{os.getpid()}-{id(tmp_path)}"
    result = queue.Queue()

    def request_thread():
        try:
            result.put(start_terminal(sid, tmp_path, rows=8, cols=40, restart=True))
        except Exception as exc:
            result.put(exc)

    thread = threading.Thread(target=request_thread)
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()

    term = result.get(timeout=1.0)
    if isinstance(term, Exception):
        raise AssertionError("terminal worker thread failed") from term
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            assert term.proc.poll() is None
            time.sleep(0.02)
        assert term.is_alive()

        marker = f"lifecycle-ok-{os.getpid()}"
        write_terminal(sid, f"printf '{marker}\\n'\n")
        deadline = time.monotonic() + 1.0
        seen = ""
        while time.monotonic() < deadline:
            try:
                event, payload = term.output.get(timeout=0.1)
            except queue.Empty:
                continue
            if event == "output":
                seen += payload.get("text", "")
                if f"{marker}\r\n" in seen or f"{marker}\n" in seen:
                    break
        assert f"{marker}\r\n" in seen or f"{marker}\n" in seen
    finally:
        close_terminal(sid)
