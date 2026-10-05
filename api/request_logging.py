"""Request output independent of agent/tool stdout capture."""

import os
import threading


# Import before agent code. Own the descriptor so replacing or closing Python's
# sys.stdout cannot capture access/error records intended for the service log.
try:
    _STREAM = os.fdopen(
        os.dup(1), "w", encoding="utf-8", errors="backslashreplace", buffering=1,
    )
except OSError:
    _STREAM = None
_LOCK = threading.Lock()


def emit_request_log(message: str) -> None:
    """Flush one complete record without letting a broken sink break HTTP."""
    try:
        if _STREAM is not None:
            with _LOCK:
                _STREAM.write(message + "\n")
                _STREAM.flush()
    except Exception:
        pass
