"""Test-only server for the public settle-frame gate's cross-channel race.

Hold the actual HTTP terminal frame after the worker has finished. The browser
keeps its native, open EventSource while /api/sessions independently reports idle.
No browser state, renderer, persistence result, or terminal payload is mocked.
"""

from pathlib import Path
import sys
import time


def main():
    if len(sys.argv) != 3:
        raise SystemExit("expected repository root and isolated barrier directory")
    repo_root, barrier_dir = (Path(value) for value in sys.argv[1:])
    sys.path.insert(0, str(repo_root))
    import server
    from api import routes

    ready = barrier_dir / "ready"
    release = barrier_dir / "release"
    original = routes._sse_with_id

    def terminal_barrier(handler, event, data, event_id=None):
        if event == "done" and handler.path.startswith("/api/chat/stream?"):
            ready.touch()
            deadline = time.monotonic() + 15
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test did not release the terminal HTTP frame")
                time.sleep(0.005)
        return original(handler, event, data, event_id)

    routes._sse_with_id = terminal_barrier
    server.main()


if __name__ == "__main__":
    main()
