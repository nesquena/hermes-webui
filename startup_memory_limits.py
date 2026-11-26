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
import re
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
# How glibc reads the number at the start of an arena setting once leading
# spaces and tabs are skipped (_dl_strtoul): optional sign, then 0x hex, a
# leading-0 octal number or a decimal number.
_GLIBC_NUMBER = re.compile(r"(?P<sign>[+-]?)(?:0[xX](?P<hex>[0-9a-fA-F]*)|(?P<oct>0[0-7]*)|(?P<dec>[1-9][0-9]*))")
_GLIBC_DIGITS = {16: 16, 8: 22, 10: 20}  # most significant digits a 64-bit value can have
_U64_MAX = 2**64 - 1
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


def _glibc_version(libc: Any) -> tuple[int, int] | None:
    """``(major, minor)`` of the running glibc, or ``None`` when unknown."""
    try:
        fn = libc.gnu_get_libc_version
        if hasattr(fn, "restype"):  # a ctypes function: it returns const char *
            fn.restype = ctypes.c_char_p
        raw = fn()
        text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw)
        match = re.match(r"(\d+)\.(\d+)", text)
        return (int(match[1]), int(match[2])) if match else None
    except Exception:
        return None


# From glibc 2.39 a tunable value with text after the number is rejected; older
# versions apply the leading number and ignore the rest.
_STRICT_END_FROM = (2, 39)
_TRAILING_REJECTED = "glibc 2.39 and later reject text after the number"


def _arena_max_setting(
    env: Mapping[str, str], invalid: dict[tuple[str, str], str], strict_end: bool = False
) -> tuple[str, str] | None:
    """``(name, value)`` of the arena cap glibc put in force from *env*, or ``None``.

    Mirrors glibc (measured on 2.39): every ``glibc.malloc.arena_max`` item in
    ``GLIBC_TUNABLES`` is read in order and the last one glibc accepts wins;
    an accepted tunable wins over ``MALLOC_ARENA_MAX``, which is read only when
    no tunable item was accepted. A value glibc rejects (zero, no number,
    ``08``) is skipped, as glibc skips it. A negative value is accepted but
    wraps to a huge cap, so it is in force yet caps nothing; that ends the
    search with ``None``, because the setting glibc kept is no cap. Rejected
    and wrapping values are added to *invalid*. A blank value counts as unset,
    quietly. See ``_glibc_read`` for values with trailing text.
    """
    tunable = "GLIBC_TUNABLES glibc.malloc.arena_max"
    in_force: tuple[str, str, int | None] | None = None
    for name, sep, value in (item.partition("=") for item in (env.get("GLIBC_TUNABLES") or "").split(":")):
        if sep and name == "glibc.malloc.arena_max" and value.strip():
            accepted, cap, reason = _glibc_take(value, strict_end)
            if accepted:
                in_force = (tunable, value, cap)  # a later accepted item replaces it
            else:
                invalid[(tunable, value)] = reason
    if in_force is None:
        value = env.get("MALLOC_ARENA_MAX") or ""
        if value.strip():
            accepted, cap, reason = _glibc_take(value, strict_end)
            if accepted:
                in_force = ("MALLOC_ARENA_MAX", value, cap)
            else:
                invalid[("MALLOC_ARENA_MAX", value)] = reason
    if in_force is None:
        return None
    name, value, cap = in_force
    if cap is None:
        invalid[(name, value)] = "not a positive integer"
        return None
    return name, value


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


def _glibc_read(value: str) -> tuple[bool, int | None, bool]:
    """How glibc takes an arena setting: ``(accepted, cap, trailing)``.

    The rule is chosen so the module never overrides a cap that any glibc in
    use may have applied. glibc skips leading spaces and tabs and reads an
    optional sign, then ``0x`` hex digits, a leading-``0`` octal number
    (``010`` is 8; ``08`` reads as 0) or a decimal number:

    * No number, zero, or a number past 64 bits: no glibc version applies it,
      ``(False, None, ...)``.
    * A ``-`` sign: every version accepts it and wraps it to a huge cap,
      which is no cap, ``(True, None, ...)``.
    * A positive number followed by other text (``4 ``, ``2.5``): glibc
      before 2.39 applies the number and ignores the rest; 2.39 and later
      reject the whole value. It may be a cap in force, so it counts as one,
      with *trailing* set.
    """
    text = value.lstrip(" \t")
    match = _GLIBC_NUMBER.match(text)
    if match is None:
        return False, None, False
    trailing = match.end() != len(text)
    if match["hex"] is not None:
        digits, base = match["hex"], 16
    elif match["oct"] is not None:
        digits, base = match["oct"], 8
    else:
        digits, base = match["dec"], 10
    digits = digits.lstrip("0")
    if not digits or len(digits) > _GLIBC_DIGITS[base] or int(digits, base) > _U64_MAX:
        return False, None, trailing
    if match["sign"] == "-":
        return True, None, trailing
    return True, int(digits, base), trailing


def _glibc_take(value: str, strict_end: bool) -> tuple[bool, int | None, str]:
    """``(accepted, cap, reason)``: ``_glibc_read`` under the running glibc's
    end-of-value rule, with the reason a rejected value is ignored."""
    accepted, cap, trailing = _glibc_read(value)
    if accepted and trailing and strict_end:
        return False, None, _TRAILING_REJECTED
    return accepted, cap, "not a positive integer"


def _arena_number(value: str, strict_end: bool = False) -> int | None:
    """The positive cap glibc reads from *value*, or ``None`` when it sets none."""
    return _glibc_take(value, strict_end)[1]


def _trailing_text(value: str) -> bool:
    return _glibc_read(value)[2]


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


def _decide(
    environ: Mapping[str, str], launch_environ: Mapping[str, str] | None, strict_end: bool = False
) -> tuple[int | None, str]:
    """``(cap, source)`` to apply, or ``(None, reason)`` when nothing should be applied.

    *strict_end*: the running glibc rejects a value with text after the number
    (2.39 and later). Unknown versions use the older rule, so a cap glibc may
    have applied is never overridden."""
    invalid: dict[tuple[str, str], str] = {}
    launched = _arena_max_setting(environ if launch_environ is None else launch_environ, invalid, strict_end)
    current = None if launched is not None else _arena_max_setting(environ, invalid, strict_end)
    for (name, value), reason in invalid.items():
        _log(logging.WARNING, f"malloc arena cap: ignoring invalid {name}={_shown(value)} ({reason})")
    if launched is not None:
        name, value = launched
        if _trailing_text(value):
            return None, (f"operator set {name}={_shown(value)} at launch; left to glibc, which applies "
                          "the leading number before 2.39 and ignores the value from 2.39")
        return None, f"operator set {name}={_shown(value)} at launch; glibc already applied it"
    if current is not None:
        name, value = current
        cap = _arena_number(value, strict_end)
        if cap is None or cap > _C_INT_MAX:
            return None, f"operator set {name}={_shown(value)} after launch; above mallopt's int range"
        return cap, f"{name} set after launch"
    knob = (environ.get(ENV_KNOB) or "").strip()
    if knob.lower() in _DISABLED:
        return None, f"disabled by {ENV_KNOB}={_shown(knob)}"
    if knob:
        cap = _positive_c_int(knob)
        if cap is not None:
            return cap, ENV_KNOB
        logger.debug("Ignoring invalid %s=%s; using the default %d", ENV_KNOB, _shown(knob), DEFAULT_ARENA_MAX)
    return DEFAULT_ARENA_MAX, "default"


def _log(level: int, message: str) -> None:
    """One log line. WebUI configures no logging handlers, so an INFO record
    would be dropped by ``logging.lastResort`` (WARNING+); then write INFO and
    WARNING lines to stderr, which the launchers redirect into the WebUI log.
    A DEBUG line goes only through logging."""
    try:
        if logger.isEnabledFor(level) and logger.hasHandlers():
            logger.log(level, message)
        elif level >= logging.INFO:
            sys.stderr.write(f"[memory] {logging.getLevelName(level)} {message}\n")
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

    * Non-Linux or non-glibc (musl, macOS, Windows): nothing (debug log).
    * An arena setting in the exec-time environment (``MALLOC_ARENA_MAX`` or
      the ``glibc.malloc.arena_max`` tunable) was already applied by glibc and
      wins: nothing. One only in ``os.environ`` is applied if it is a positive
      C int. A setting glibc applies no cap for (``0``, ``-3``, ``abc``,
      ``08``) counts as unset, with one warning per setting. A number followed
      by other text (``4 ``, ``2.5``) counts as unset the same way on glibc
      2.39 and later, which reject it; older or unknown versions apply the
      number, so there it is left to glibc and never overridden.
    * Otherwise ``HERMES_WEBUI_MALLOC_ARENA_MAX``: a positive int sets the
      cap, ``0``/``off``/``false`` disables it, anything else is ignored
      (debug log) in favour of the default cap of 2.

    Never exports ``MALLOC_ARENA_MAX``, so children keep glibc's default.
    The keyword arguments exist for tests. Never raises.
    """
    applied = False
    level = logging.INFO
    try:
        if libc is _UNSET:
            libc = _glibc_malloc()
        if libc is None:
            # Every macOS/Windows/musl start lands here: not worth an INFO line.
            level, message = logging.DEBUG, "malloc arena cap not applied: not Linux glibc"
        else:
            env = os.environ if environ is None else environ
            launch = _launch_environ() if launch_environ is _UNSET else launch_environ
            version = _glibc_version(libc)
            cap, why = _decide(env, launch, strict_end=version is not None and version >= _STRICT_END_FROM)
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
        applied, level, message = False, logging.INFO, "malloc arena cap not applied: unexpected error"
    _log(level, message)
    return applied


__all__ = ["DEFAULT_ARENA_MAX", "ENV_KNOB", "apply_startup_arena_cap"]
