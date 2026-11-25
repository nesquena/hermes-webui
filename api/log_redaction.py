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
* When the Agent is importable the record is formatted with
  ``agent.redact.RedactingFormatter`` — the Agent's full redaction policy,
  which masks far more than URLs. When it is not, it falls back to a
  plain ``logging.Formatter`` and still emits (the log must not vanish).
* The handler owns the server-log boundary, so it enforces the issue's
  credential shape itself: the URL userinfo pair (``user:<password>@``
  becomes ``***:***@``) is masked on this handler's own output in EVERY
  state — no Agent, an Agent build whose formatter does not mask it, or a
  formatter that raises. The built-in mask is idempotent over the Agent's
  output, and an explicit Agent opt-out (``security.redact_secrets: false``)
  is honoured.
* Idempotent: repeated calls (import + startup call, re-entry) install at
  most ONE handler — the handler is tagged so import ordering cannot stack a
  second copy onto the root logger.

The formatter is resolved at FORMAT time, not at install time
--------------------------------------------------------------
The Agent can become importable *after* the WebUI installed its handler: the
bootstrap transition in ``server.main()`` (``auto_install_agent_deps()``)
installs the Agent's missing dependencies later in the process's life, and a
PM-managed Agent is activated by ``managed_agent_startup`` before its
dependencies are on ``sys.path``.

A formatter frozen at install time freezes the *fallback* too: the handler
would keep the plain ``logging.Formatter`` for the rest of the process's life
and write credentials in clear on the WebUI's stderr channel — a privacy
regression that appears only on the bootstrap transition, while the ordinary
startup path (Agent importable from the start) masks correctly. That is why
the resolution lives in ``_DeferredRedactingFormatter``: it looks the Agent's
formatter up when a record is formatted, and an already-imported Agent is
found through ``sys.modules`` without any import attempt.

The credential-output guarantee is enforced at THIS boundary
-------------------------------------------------------------
The Agent's policy is the primary mask, but it is not guaranteed to cover
the issue's shape across the supported Agent range.
``RedactingFormatter.format`` only started calling ``redact_log_urls`` —
the strict URL pass that masks the whole userinfo pair — in Agent v0.23.2.
Older builds mask the password alone (``redact_sensitive_text`` passes
``user:pass@`` through unless the caller opts into
``redact_url_credentials``), v0.22.0 masks neither, and a host without the
Agent has no policy at all. Left to the Agent alone, those states write
``alice:<password>@proxy.example.invalid`` to the server log.

So the handler enforces the shape itself: whatever text the formatter
chain produces, the userinfo pair is masked before the line reaches the
stream. The mask is idempotent over the Agent's own output (re-masking an
already-masked pair yields the same bytes), so a current Agent's line is
unchanged. An explicit Agent opt-out (``security.redact_secrets: false``)
still passes raw credentials through — the WebUI honours the operator's
policy rather than overriding it — and that opt-out is consulted only when
the Agent is importable, since without one there is no policy to honour.

Not in scope: the Logs panel (``api/logs``) reads agent.log/errors.log/
gateway.log under HERMES_HOME/logs, which the Agent writes with its own
handlers; that path is unaffected.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Optional, TextIO

__all__ = [
    "install_server_log_redaction",
    "is_installed",
    "refresh_server_log_redaction",
]

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


def _agent_is_explicitly_unavailable() -> bool:
    """True when ``sys.modules["agent"]`` is the "not available" sentinel.

    ``sys.modules["agent"] = None`` is the documented way to make an import of
    the Agent package fail. It must be honoured even when a stale
    ``agent.redact`` entry is still in ``sys.modules``.
    """
    return "agent" in sys.modules and sys.modules["agent"] is None


def _agent_formatter_class_from_modules() -> Optional[type]:
    """The Agent's formatter class if it is ALREADY imported, else None.

    This is the cheap, always-current lookup: ``sys.modules`` reflects the
    Agent's availability at this instant, so an Agent that became importable
    after the handler was installed is picked up on the very next record — no
    import attempt and no cache to invalidate.
    """
    if _agent_is_explicitly_unavailable():
        return None
    module = sys.modules.get("agent.redact")
    if module is None:
        return None
    formatter_cls = getattr(module, "RedactingFormatter", None)
    return formatter_cls if isinstance(formatter_cls, type) else None


def _import_agent_formatter_class() -> Optional[type]:
    """Import the Agent's formatter class, or None when unavailable.

    WebUI embeds the Agent as a library; on a host without the Agent (or with
    an Agent build predating ``agent.redact.RedactingFormatter``) there is
    nothing to redact with and the caller falls back to a plain formatter.
    """
    if _agent_is_explicitly_unavailable():
        return None
    try:
        from agent.redact import RedactingFormatter
    except Exception:
        return None
    return RedactingFormatter if isinstance(RedactingFormatter, type) else None


def _resolve_agent_formatter_class() -> Optional[type]:
    """The Agent's formatter class, or None when it is not available.

    Consults ``sys.modules`` first (cheap and always current) and only then
    attempts the import.
    """
    return _agent_formatter_class_from_modules() or _import_agent_formatter_class()


# The credential shape issue #8984 is about: a URL whose userinfo part is
# ``user:<password>@host``. The Agent's formatter masks the whole pair —
# but only from Agent v0.23.2 on, when ``RedactingFormatter.format``
# started calling ``redact_log_urls``. Older builds mask the password alone
# (``redact_sensitive_text`` passes ``user:pass@`` through unless the caller
# opts into ``redact_url_credentials``), v0.22.0 masks neither, and a host
# without the Agent has no policy at all. The handler owns the server-log
# boundary, so it enforces the shape itself.
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)([^/@\s]+):([^/@\s]+)@")


def _mask_url_userinfo(text: str) -> str:
    """Mask the URL userinfo pair: the user-and-password part in front of
    the at-sign becomes asterisks, scheme and host stay readable.

    The same shape the Agent's ``RedactingFormatter`` produces, so the
    output is identical whether the mask came from the Agent or from here
    (re-masking an already-masked pair is a no-op). Standard library only.
    """
    if "://" not in text or "@" not in text:
        return text
    return _URL_USERINFO_RE.sub(lambda m: m.group(1) + "***:***@", text)


def _agent_redaction_disabled() -> bool:
    """True when the Agent is present and its policy opts out of redaction.

    ``agent.redact._redact_enabled`` is the Agent's own effective switch
    (``security.redact_secrets: false`` / ``HERMES_REDACT_SECRETS=false``)
    and it exists across the supported Agent range. An operator who
    deliberately allows raw credentials in logs must not be overridden
    here, so the built-in mask stands down for that policy. Only an
    ALREADY-IMPORTED ``agent.redact`` is consulted (no import attempt); a
    missing or failing predicate reads as "not disabled" — the safe
    direction.
    """
    module = sys.modules.get("agent.redact")
    if module is None:
        return False
    predicate = getattr(module, "_redact_enabled", None)
    if not callable(predicate):
        return False
    try:
        return not predicate()
    except Exception:
        return False


class _DeferredRedactingFormatter(logging.Formatter):
    """Format each record with the Agent's formatter when it is available.

    The Agent's ``RedactingFormatter`` is resolved when a record is FORMATTED,
    not when the handler is installed, because the Agent can become importable
    after the WebUI installed its handler (the bootstrap transition that
    installs missing Agent dependencies runs after server startup). A formatter
    frozen at install time would keep the plain fallback forever and write
    credentials in clear.

    While the Agent is unavailable the record is formatted with the plain
    ``logging.Formatter`` — the log line must never disappear.

    The handler owns the server-log boundary, so the #8984 credential shape
    (the URL userinfo pair) is masked on this handler's own output whatever
    the Agent's state: a missing Agent, an Agent build whose formatter does
    not mask userinfo, and a formatter that raises are all covered. The
    built-in mask is idempotent over the Agent's output, and it stands down
    when the Agent's policy explicitly opts out of redaction.
    """

    # Minimum delay between two failed IMPORT attempts, so a busy log cannot
    # turn the import into a hot path. An already-imported Agent is still
    # found immediately through ``sys.modules`` — only the import attempt
    # waits.
    _RETRY_INTERVAL_SECONDS = 0.5

    def __init__(self) -> None:
        super().__init__()
        self._agent_formatter: Optional[logging.Formatter] = None
        self._next_import_attempt_at = 0.0
        # Guards the resolution against re-entry: importing the Agent can
        # emit a log record (an Agent module warning), which would format
        # through this handler again while the import is still in flight.
        self._resolving = False

    def _agent_formatter_or_none(self) -> Optional[logging.Formatter]:
        """The Agent's formatter instance, or None while it is unavailable."""
        if self._agent_formatter is not None:
            return self._agent_formatter
        if self._resolving:
            # Re-entered while resolving: format plainly instead of
            # recursing into the import that is already in flight.
            return None

        formatter_cls = _agent_formatter_class_from_modules()
        if formatter_cls is None:
            now = time.monotonic()
            if now < self._next_import_attempt_at:
                return None
            self._resolving = True
            try:
                formatter_cls = _import_agent_formatter_class()
            finally:
                self._resolving = False
            if formatter_cls is None:
                self._next_import_attempt_at = time.monotonic() + self._RETRY_INTERVAL_SECONDS
                return None

        self._resolving = True
        try:
            self._agent_formatter = formatter_cls()
        except Exception:
            # A formatter that cannot even be constructed is treated as
            # unavailable; the plain fallback keeps the line alive.
            self._next_import_attempt_at = time.monotonic() + self._RETRY_INTERVAL_SECONDS
            return None
        finally:
            self._resolving = False
        return self._agent_formatter

    def format(self, record: logging.LogRecord) -> str:
        text: Optional[str] = None
        agent_formatter = self._agent_formatter_or_none()
        if agent_formatter is not None:
            try:
                text = agent_formatter.format(record)
            except Exception:
                # A failing Agent formatter must not swallow the record —
                # fall through to the plain path below.
                text = None
        if text is None:
            text = super().format(record)
        if not _agent_redaction_disabled():
            # The boundary guarantee: the userinfo pair never lands in clear,
            # whatever produced the text above. Idempotent over the Agent's
            # own mask; skipped only for an explicit Agent opt-out.
            text = _mask_url_userinfo(text)
        return text

    def refresh(self) -> bool:
        """Re-resolve the Agent's formatter now; True when one is in use."""
        self._next_import_attempt_at = 0.0
        return self._agent_formatter_or_none() is not None


def _iter_installed_handlers():
    """Yield the WebUI handlers currently on the root logger."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            yield handler


def refresh_server_log_redaction() -> int:
    """Re-resolve the Agent's formatter on the installed handler(s).

    The bootstrap transition makes the Agent importable after the WebUI
    installed its handler; this forces the lookup to happen immediately
    instead of waiting for the retry interval. Returns the number of handlers
    that now use the Agent's formatter.
    """
    refreshed = 0
    for handler in _iter_installed_handlers():
        formatter = getattr(handler, "formatter", None)
        if isinstance(formatter, _DeferredRedactingFormatter) and formatter.refresh():
            refreshed += 1
    return refreshed


def install_server_log_redaction(*, stream: Optional[TextIO] = None) -> bool:
    """Install ONE root handler on stderr that masks credentials in logs.

    The handler sits at WARNING — ``logging.lastResort``'s level — with the
    default message format, so the visible log stays the same apart from
    masking. When the Agent is importable the record is formatted with
    ``agent.redact.RedactingFormatter`` (the same formatter the Agent's own
    entry points install); otherwise a plain ``logging.Formatter`` is used and
    the record still lands on the stream. The lookup happens per record, so an
    Agent that becomes importable later (the bootstrap transition) is picked
    up without reinstalling the handler.

    The URL-userinfo credential shape of issue #8984 is masked on this
    handler's output in every state — no Agent, an Agent whose formatter
    does not mask it, or a formatter that raises — unless the Agent's own
    policy explicitly opts out of redaction.

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

        try:
            handler = logging.StreamHandler(sys.stderr if stream is None else stream)
            handler.setLevel(logging.WARNING)
            # The Agent's formatter is resolved when a record is formatted —
            # see _DeferredRedactingFormatter.
            handler.setFormatter(_DeferredRedactingFormatter())
            setattr(handler, _HANDLER_MARK, True)
            root.addHandler(handler)
        except Exception:
            # A logging setup failure must never block server startup.
            return False
        _INSTALLED = True
        return True
