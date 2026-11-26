"""Cap glibc malloc arenas when the WebUI server starts (#9052).

glibc gives every thread that contends on an arena lock a new arena, up to
8 x cores, and never unmaps them. A long-lived threaded server (one handler
thread per connection, plus background workers) fills every slot, and each
arena keeps its own free pages, so resident memory creeps up. Python object
allocations mostly run under the GIL, so a small cap made no measurable
throughput difference on the server's own request load (#9052). Native code
that allocates from many threads at once outside the GIL can be slower with
few arenas; ``HERMES_WEBUI_MALLOC_ARENA_MAX`` raises or disables the cap.

The cap is applied in-process with ``mallopt(M_ARENA_MAX)`` rather than by
exporting ``MALLOC_ARENA_MAX``, so exec'd children (terminal commands, MCP
servers, builds) keep glibc's default. A forked child shares this process's
allocator state until it execs.

This is a WebUI-owned equivalent of the Hermes Agent's
``hermes_cli.resource_limits.apply_malloc_arena_max``; it does not import the
Agent, because WebUI must work with older Agents and the Agent is not
discovered yet when ``server.py`` calls this.

Standard library only. Importing this module starts no thread and touches no
WebUI state, so ``server.py`` can call it before any other WebUI import. It
lives next to ``managed_agent_startup.py`` rather than under ``api/`` for the
same reason: no ``api.*`` module may load before the Agent's bootstrap (#8529).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

ENV_KNOB = "HERMES_WEBUI_MALLOC_ARENA_MAX"
DEFAULT_ARENA_MAX = 2
_M_ARENA_MAX = -8  # glibc <malloc.h> mallopt parameter
_C_INT_MAX = 2**31 - 1
_DISABLED = frozenset({"0", "off", "false"})
_EXEC_ENVIRON = "/proc/self/environ"  # exec-time env block; only the arena keys are read
_UNSET: Any = object()


def _glibc_malloc() -> Any | None:
    """This process's libc when it is Linux glibc with ``mallopt``, else ``None``."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None)
        if hasattr(libc, "gnu_get_libc_version") and hasattr(libc, "mallopt"):
            return libc
    except Exception:  # musl/odd builds: no glibc, nothing to do
        pass
    return None


def _arena_max_setting(env: Mapping[str, str]) -> tuple[str, str] | None:
    """``(name, value)`` of the arena cap *env* asks glibc for, or ``None``.

    The exact ``glibc.malloc.arena_max`` item in ``GLIBC_TUNABLES`` wins over
    ``MALLOC_ARENA_MAX``, as it does in glibc. A blank value counts as unset
    (glibc ignores it too).
    """
    for item in (env.get("GLIBC_TUNABLES") or "").split(":"):
        name, sep, value = item.partition("=")
        if sep and name == "glibc.malloc.arena_max" and value.strip():
            return "GLIBC_TUNABLES glibc.malloc.arena_max", value
    value = env.get("MALLOC_ARENA_MAX")
    return ("MALLOC_ARENA_MAX", value) if value and value.strip() else None


def _launch_environ() -> dict[str, str] | None:
    """The arena keys of the environment this process was exec'd with, or ``None``.

    That is what glibc read at startup. ``/proc/self/environ`` keeps the
    exec-time block; ``os.environ`` (and ``posix.environ``, the same dict on
    POSIX) also holds values set in-process later, which glibc never saw.
    Only ``MALLOC_ARENA_MAX`` and ``GLIBC_TUNABLES`` are kept; nothing else
    from the block is retained or logged.
    """
    try:
        with open(_EXEC_ENVIRON, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    wanted = {b"MALLOC_ARENA_MAX", b"GLIBC_TUNABLES"}
    found = {}
    for entry in raw.split(b"\0"):
        name, sep, value = entry.partition(b"=")
        if sep and name in wanted:
            found[os.fsdecode(name)] = os.fsdecode(value)
    return found


def _positive_c_int(value: str) -> int | None:
    text = value.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    try:
        number = int(text)
    except ValueError:  # beyond Python's int-string digit limit: far past any C int
        return None
    return number if 0 < number <= _C_INT_MAX else None


def _shown(value: str) -> str:
    return repr(value[:40])


def _decide(environ: Mapping[str, str], launch_environ: Mapping[str, str] | None) -> tuple[int | None, str]:
    """``(cap, source)`` to apply, or ``(None, reason)`` when nothing should be applied."""
    current = _arena_max_setting(environ)
    launched = current if launch_environ is None else _arena_max_setting(launch_environ)
    if launched is not None:
        name, value = launched
        return None, f"operator set {name}={_shown(value)} at launch; glibc already applied it"
    if current is not None:
        name, value = current
        cap = _positive_c_int(value)
        if cap is None:
            logger.debug("Ignoring invalid %s=%r set after launch", name, value)
            return None, f"operator set invalid {name}={_shown(value)}"
        return cap, f"{name} set after launch"
    knob = (environ.get(ENV_KNOB) or "").strip()
    if knob.lower() in _DISABLED:
        return None, f"disabled by {ENV_KNOB}={_shown(knob)}"
    if knob:
        cap = _positive_c_int(knob)
        if cap is not None:
            return cap, ENV_KNOB
        logger.debug("Ignoring invalid %s=%r; using the default %d", ENV_KNOB, knob, DEFAULT_ARENA_MAX)
    return DEFAULT_ARENA_MAX, "default"


def _log_info(message: str) -> None:
    """One INFO line. WebUI configures no logging handlers, so an INFO record
    would be dropped by ``logging.lastResort`` (WARNING+); then write the line
    to stderr, which the launchers redirect into the WebUI log."""
    try:
        if logger.isEnabledFor(logging.INFO) and logger.hasHandlers():
            logger.info(message)
        else:
            sys.stderr.write(f"[memory] INFO {message}\n")
            sys.stderr.flush()
    except Exception:
        pass


def apply_startup_arena_cap(
    *,
    environ: Mapping[str, str] | None = None,
    launch_environ: Mapping[str, str] | None = _UNSET,
    libc: Any = _UNSET,
) -> bool:
    """Cap glibc malloc arenas for this process; ``True`` only when ``mallopt`` succeeded.

    Call before the process starts any thread: arenas that exist never go
    away, and glibc latches its limit once a thread asks for an arena.

    * Non-Linux or non-glibc (musl, macOS, Windows): nothing.
    * An arena setting in the exec-time environment (``MALLOC_ARENA_MAX`` or
      the ``glibc.malloc.arena_max`` tunable) was already applied by glibc and
      wins: nothing. One only in ``os.environ`` is applied if it is a positive
      C int.
    * Otherwise ``HERMES_WEBUI_MALLOC_ARENA_MAX``: a positive int sets the
      cap, ``0``/``off``/``false`` disables it, anything else is ignored
      (debug log) in favour of the default cap of 2.

    Never exports ``MALLOC_ARENA_MAX``, so children keep glibc's default.
    The keyword arguments exist for tests. Never raises.
    """
    applied = False
    try:
        if libc is _UNSET:
            libc = _glibc_malloc()
        if libc is None:
            message = "malloc arena cap not applied: not Linux glibc"
        else:
            env = os.environ if environ is None else environ
            launch = _launch_environ() if launch_environ is _UNSET else launch_environ
            cap, why = _decide(env, launch)
            if cap is None:
                message = f"malloc arena cap not applied: {why}"
            else:
                try:
                    applied = bool(libc.mallopt(_M_ARENA_MAX, cap))
                except Exception:
                    logger.debug("mallopt(M_ARENA_MAX, %d) raised", cap, exc_info=True)
                if applied:
                    message = f"malloc arena cap applied: M_ARENA_MAX={cap} ({why})"
                else:
                    message = f"malloc arena cap not applied: mallopt(M_ARENA_MAX, {cap}) failed ({why})"
    except Exception:
        logger.debug("Could not cap malloc arenas", exc_info=True)
        applied, message = False, "malloc arena cap not applied: unexpected error"
    _log_info(message)
    return applied


__all__ = ["DEFAULT_ARENA_MAX", "ENV_KNOB", "apply_startup_arena_cap"]
