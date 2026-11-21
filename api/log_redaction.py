"""Server-side log redaction for the Hermes WebUI server (issue #8984).

Background
----------
The WebUI server installs no logging handler. Agent library WARNINGs raised
inside the server process therefore fall through to Python's last-resort
stderr handler, *without* the Agent's RedactingFormatter. A provider base_url
that carries credentials (a URL whose userinfo part is
``alice:<password>@proxy.example.invalid``) is then written in
clear text to the server's stdout/stderr log — output that is often pasted
into bug reports.

Mechanism
---------
The Agent redacts at the formatter: ``agent.redact.RedactingFormatter`` masks
the userinfo part of a URL — the whole user-and-password pair in front of the
at-sign becomes asterisks — while the scheme and the host stay readable. Its own
entry points
(``gateway/run.py``, ``gateway/run_turn.py``, ``acp_adapter/entry.py``) install
it. WebUI embeds the Agent as a library, and nothing in ``server.py`` or
``api/`` called ``addHandler``/``setFormatter``/``basicConfig``/``dictConfig``,
so the root logger had no handlers after importing ``api.streaming`` /
``api.routes`` and anything at WARNING or above went to ``logging.lastResort``
on stderr, unredacted.

Design constraints
------------------
* Standard library only; no new dependencies.
* ``install_server_log_redaction()`` never raises — a logging setup failure
  must not block server startup (same rule as ``api/crash_visibility.py``).
* The visible log changes by masking only: ONE root handler on stderr at
  WARNING (``lastResort``'s level) with the default message format, so
  DEBUG/INFO still do not print and WARNING+ lines keep their exact shape.
* When the Agent is importable the handler uses
  ``agent.redact.RedactingFormatter``; when it is not, it falls back to a
  plain ``logging.Formatter`` and still emits (the log must not vanish).
* Idempotent: repeated calls (import + startup call, re-entry) install at
  most ONE handler — the handler is tagged so import ordering cannot stack a
  second copy onto the root logger.

Not in scope: the Logs panel (``api/logs``) reads agent.log/errors.log/
gateway.log under HERMES_HOME/logs, which the Agent writes with its own
handlers; that path is unaffected.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional, TextIO

__all__ = ["install_server_log_redaction", "is_installed"]

# Guard so repeated calls (import + explicit startup call, re-entry, or a test
# that re-imports) don't stack a second handler onto the root logger.
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()

# Attribute tag marking the handler this module installed, so an install call
# can recognise its own handler on the root logger even if _INSTALLED was
# reset (e.g. across a test that snapshots module state).
_HANDLER_MARK = "_hermes_webui_log_redaction"


def is_installed() -> bool:
    """Return True once :func:`install_server_log_redaction` has run."""
    return _INSTALLED


def _load_redacting_formatter():
    """Return the Agent's ``RedactingFormatter``, or None when unavailable.

    WebUI embeds the Agent as a library; on a host without the Agent (or with
    an Agent build predating ``agent.redact.RedactingFormatter``) there is
    nothing to redact with and the caller falls back to a plain formatter.
    """
    try:
        from agent.redact import RedactingFormatter
    except Exception:
        return None
    return RedactingFormatter


def install_server_log_redaction(*, stream: Optional[TextIO] = None) -> bool:
    """Install ONE root handler on stderr that masks credentials in logs.

    The handler sits at WARNING — ``logging.lastResort``'s level — with the
    default message format, so the visible log stays the same apart from
    masking. When the Agent is importable the handler's formatter is
    ``agent.redact.RedactingFormatter`` (the same formatter the Agent's own
    entry points install); otherwise a plain ``logging.Formatter`` is used and
    the record still lands on the stream.

    Parameters
    ----------
    stream:
        Where records are written. Defaults to ``sys.stderr`` (redirected
        into the WebUI log file by the bootstrap). Tests inject a buffer.

    Returns True if it installed on this call, False if it was already active.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        root = logging.getLogger()
        if _INSTALLED or any(
            getattr(h, _HANDLER_MARK, False) for h in root.handlers
        ):
            _INSTALLED = True
            return False

        handler = logging.StreamHandler(sys.stderr if stream is None else stream)
        handler.setLevel(logging.WARNING)
        formatter_cls = _load_redacting_formatter()
        handler.setFormatter((formatter_cls or logging.Formatter)())
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
        _INSTALLED = True
        return True
