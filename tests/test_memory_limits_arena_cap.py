"""#9052: server.py caps glibc malloc arenas at startup (startup_memory_limits.py).

Unit cases drive the decision logic with injected environments and a fake
libc, so they never call the real ``mallopt``. The placement cases prove the
call runs while only the main thread exists, before any Agent or api.* import
that could start a thread.
"""

from __future__ import annotations

import ast
import ctypes
import logging
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

import startup_memory_limits as memory_limits
from startup_memory_limits import ENV_KNOB, apply_startup_arena_cap

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = REPO_ROOT / "server.py"
M_ARENA_MAX = -8


class FakeLibc:
    def __init__(self, result: int = 1, exc: Exception | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.result = result
        self.exc = exc

    def mallopt(self, param: int, value: int) -> int:
        self.calls.append((param, value))
        if self.exc is not None:
            raise self.exc
        return self.result


def _apply(environ, launch_environ=None, libc=None):
    """Run the real entry point with injected state; ``launch_environ=None``
    means the exec-time block carried nothing beyond *environ*'s non-arena vars."""
    libc = FakeLibc() if libc is None else libc
    launch = {} if launch_environ is None else launch_environ
    return apply_startup_arena_cap(environ=environ, launch_environ=launch, libc=libc), libc


@pytest.fixture
def info_log(caplog):
    caplog.set_level(logging.DEBUG, logger="startup_memory_limits")
    return caplog


def _lines(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _info_lines(caplog):
    return _lines(caplog, logging.INFO)


def _warning_lines(caplog):
    return _lines(caplog, logging.WARNING)


# ── decision logic ─────────────────────────────────────────────────────────


def test_default_cap_is_two(info_log):
    applied, libc = _apply({})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]
    assert _info_lines(info_log) == ["malloc arena cap applied: M_ARENA_MAX=2 (default)"]


def test_exec_time_malloc_arena_max_is_a_noop(info_log):
    env = {"MALLOC_ARENA_MAX": "4"}
    applied, libc = _apply(env, launch_environ=dict(env))
    assert applied is False
    assert libc.calls == []
    (line,) = _info_lines(info_log)
    assert "not applied" in line and "MALLOC_ARENA_MAX='4' at launch" in line


def test_exec_time_glibc_tunable_is_a_noop(info_log):
    env = {"GLIBC_TUNABLES": "glibc.malloc.tcache_count=0:glibc.malloc.arena_max=1"}
    applied, libc = _apply(env, launch_environ=dict(env))
    assert applied is False
    assert libc.calls == []
    (line,) = _info_lines(info_log)
    assert "glibc.malloc.arena_max='1' at launch" in line


def test_exec_time_setting_wins_over_webui_knob():
    env = {"MALLOC_ARENA_MAX": "8", ENV_KNOB: "3"}
    applied, libc = _apply(env, launch_environ={"MALLOC_ARENA_MAX": "8"})
    assert applied is False
    assert libc.calls == []


def test_other_tunables_are_not_an_arena_setting():
    # Only the exact glibc.malloc.arena_max item counts, not a lookalike name.
    env = {"GLIBC_TUNABLES": "glibc.malloc.arena_test=1:xglibc.malloc.arena_max=9"}
    applied, libc = _apply(env, launch_environ=dict(env))
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]


def test_os_environ_only_value_is_applied_with_operator_number(info_log):
    applied, libc = _apply({"MALLOC_ARENA_MAX": "6", ENV_KNOB: "3"}, launch_environ={})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 6)]
    assert _info_lines(info_log) == [
        "malloc arena cap applied: M_ARENA_MAX=6 (MALLOC_ARENA_MAX set after launch)"
    ]


def test_os_environ_only_tunable_is_applied():
    applied, libc = _apply({"GLIBC_TUNABLES": "glibc.malloc.arena_max=5"}, launch_environ={})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 5)]


_HUGE_DIGITS = pytest.param("9" * 5000, id="5000-digits")  # past Python's int-string limit


@pytest.mark.parametrize("value", ["2147483648", _HUGE_DIGITS])
def test_os_environ_only_value_past_mallopt_int_is_not_overridden(value, info_log):
    # A positive integer, so the operator asked for (at least) that many arenas.
    applied, libc = _apply({"MALLOC_ARENA_MAX": value}, launch_environ={})
    assert applied is False
    assert libc.calls == []
    (line,) = _info_lines(info_log)
    assert line.endswith("after launch; above mallopt's int range")
    assert _warning_lines(info_log) == []


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_os_environ_only_value_is_ignored_with_a_warning(value, info_log):
    applied, libc = _apply({"MALLOC_ARENA_MAX": value}, launch_environ={})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]
    assert _warning_lines(info_log) == [
        f"malloc arena cap: ignoring invalid MALLOC_ARENA_MAX={value!r} (not a positive integer)"
    ]


@pytest.mark.parametrize(
    "env",
    [
        {"MALLOC_ARENA_MAX": ""},
        {"MALLOC_ARENA_MAX": "  "},
        {"GLIBC_TUNABLES": "glibc.malloc.arena_max="},
        {"GLIBC_TUNABLES": "glibc.malloc.arena_max=", "MALLOC_ARENA_MAX": ""},
    ],
)
def test_blank_arena_setting_counts_as_unset(env, info_log):
    # glibc ignores a blank value, so the WebUI default still applies, quietly.
    applied, libc = _apply(env, launch_environ=dict(env))
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]
    assert _warning_lines(info_log) == []


# An arena setting glibc does not take (zero, negative, non-numeric) leaves the
# process uncapped, so it must not count as "operator set" (#9359 review).
_INVALID_LAUNCH_SETTINGS = [
    pytest.param("MALLOC_ARENA_MAX", "MALLOC_ARENA_MAX", "{}", id="MALLOC_ARENA_MAX"),
    pytest.param(
        "GLIBC_TUNABLES glibc.malloc.arena_max",
        "GLIBC_TUNABLES",
        "glibc.malloc.tcache_count=0:glibc.malloc.arena_max={}",
        id="GLIBC_TUNABLES",
    ),
]


@pytest.mark.parametrize(("shown_name", "var", "template"), _INVALID_LAUNCH_SETTINGS)
@pytest.mark.parametrize("value", ["0", "abc", "-3", "00", "0x0", "2.5", "4 4"])
def test_invalid_launch_value_is_ignored_and_the_default_applies(shown_name, var, template, value, info_log):
    env = {var: template.format(value), "SOME_SECRET": "do-not-log-me"}
    applied, libc = _apply(env, launch_environ={var: env[var]})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]
    assert _warning_lines(info_log) == [
        f"malloc arena cap: ignoring invalid {shown_name}={value!r} (not a positive integer)"
    ]
    assert _info_lines(info_log) == ["malloc arena cap applied: M_ARENA_MAX=2 (default)"]
    assert not any("do-not-log-me" in r.getMessage() or "tcache" in r.getMessage() for r in info_log.records)


@pytest.mark.parametrize(("shown_name", "var", "template"), _INVALID_LAUNCH_SETTINGS)
@pytest.mark.parametrize("value", ["", "  "])
def test_empty_launch_value_is_unset_without_a_warning(shown_name, var, template, value, info_log):
    env = {var: template.format(value)}
    applied, libc = _apply(env, launch_environ=dict(env))
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]
    assert _warning_lines(info_log) == []


@pytest.mark.parametrize(("shown_name", "var", "template"), _INVALID_LAUNCH_SETTINGS)
@pytest.mark.parametrize("value", ["4", " 4", "+4", "0x4", "010", "99999999999"])
def test_valid_launch_value_still_wins(shown_name, var, template, value, info_log):
    # Forms glibc itself accepts (measured on glibc 2.39: +4 and 0x4 give 4 arenas).
    env = {var: template.format(value), ENV_KNOB: "3"}
    applied, libc = _apply(env, launch_environ={var: env[var]})
    assert applied is False
    assert libc.calls == []
    assert _warning_lines(info_log) == []
    assert _info_lines(info_log) == [
        f"malloc arena cap not applied: operator set {shown_name}={value!r} at launch; glibc already applied it"
    ]


def test_invalid_launch_value_leaves_the_webui_knob_in_charge():
    applied, libc = _apply({"MALLOC_ARENA_MAX": "0", ENV_KNOB: "3"}, launch_environ={"MALLOC_ARENA_MAX": "0"})
    assert (applied, libc.calls) == (True, [(M_ARENA_MAX, 3)])
    applied, libc = _apply({"MALLOC_ARENA_MAX": "0", ENV_KNOB: "off"}, launch_environ={"MALLOC_ARENA_MAX": "0"})
    assert (applied, libc.calls) == (False, [])


def test_invalid_tunable_falls_through_to_a_valid_malloc_arena_max(info_log):
    env = {"GLIBC_TUNABLES": "glibc.malloc.arena_max=0", "MALLOC_ARENA_MAX": "4"}
    applied, libc = _apply(env, launch_environ=dict(env))
    assert (applied, libc.calls) == (False, [])
    assert _warning_lines(info_log) == [
        "malloc arena cap: ignoring invalid GLIBC_TUNABLES glibc.malloc.arena_max='0' (not a positive integer)"
    ]
    (line,) = _info_lines(info_log)
    assert "operator set MALLOC_ARENA_MAX='4' at launch" in line


def test_same_invalid_value_at_launch_and_in_process_warns_once(info_log):
    env = {"MALLOC_ARENA_MAX": "abc"}
    applied, _ = _apply(env, launch_environ=dict(env))
    assert applied is True
    assert len(_warning_lines(info_log)) == 1


def test_invalid_launch_value_with_valid_in_process_value_applies_it(info_log):
    applied, libc = _apply({"MALLOC_ARENA_MAX": "6"}, launch_environ={"MALLOC_ARENA_MAX": "0"})
    assert (applied, libc.calls) == (True, [(M_ARENA_MAX, 6)])
    assert len(_warning_lines(info_log)) == 1


def test_warning_reaches_stderr_without_logging_handlers(capsys, monkeypatch):
    monkeypatch.setattr(memory_limits.logger, "isEnabledFor", lambda level: False)
    env = {"MALLOC_ARENA_MAX": "0"}
    assert apply_startup_arena_cap(environ=env, launch_environ=dict(env), libc=FakeLibc()) is True
    assert capsys.readouterr().err == (
        "[memory] WARNING malloc arena cap: ignoring invalid MALLOC_ARENA_MAX='0' (not a positive integer)\n"
        "[memory] INFO malloc arena cap applied: M_ARENA_MAX=2 (default)\n"
    )


def test_unreadable_proc_environ_treats_os_environ_as_launch_env():
    libc = FakeLibc()
    applied = apply_startup_arena_cap(
        environ={"MALLOC_ARENA_MAX": "4"}, launch_environ=None, libc=libc
    )
    assert applied is False
    assert libc.calls == []


@pytest.mark.parametrize("value", ["0", "off", "false", "OFF", " False "])
def test_webui_knob_disables(value, info_log):
    applied, libc = _apply({ENV_KNOB: value})
    assert applied is False
    assert libc.calls == []
    (line,) = _info_lines(info_log)
    assert line.startswith("malloc arena cap not applied: disabled by HERMES_WEBUI_MALLOC_ARENA_MAX=")


def test_webui_knob_three_applies_three(info_log):
    applied, libc = _apply({ENV_KNOB: "3"})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 3)]
    assert _info_lines(info_log) == [
        "malloc arena cap applied: M_ARENA_MAX=3 (HERMES_WEBUI_MALLOC_ARENA_MAX)"
    ]


@pytest.mark.parametrize(
    "value", ["-1", "abc", "2.5", "1e3", "2147483648", "on", "true", "   ", _HUGE_DIGITS]
)
def test_invalid_webui_knob_falls_back_to_two(value, info_log):
    applied, libc = _apply({ENV_KNOB: value})
    assert applied is True
    assert libc.calls == [(M_ARENA_MAX, 2)]
    if value.strip():
        assert any(
            r.levelno == logging.DEBUG and "Ignoring invalid" in r.getMessage()
            for r in info_log.records
        )


def test_non_glibc_returns_false_with_a_debug_line_only(info_log):
    assert apply_startup_arena_cap(environ={}, launch_environ={}, libc=None) is False
    assert _info_lines(info_log) == []
    assert _lines(info_log, logging.DEBUG) == ["malloc arena cap not applied: not Linux glibc"]


def test_non_glibc_writes_nothing_to_stderr_without_logging_handlers(capsys, monkeypatch):
    # Every macOS/Windows/musl start takes this path: no line in the WebUI log.
    monkeypatch.setattr(memory_limits.logger, "isEnabledFor", lambda level: False)
    assert apply_startup_arena_cap(environ={}, launch_environ={}, libc=None) is False
    assert capsys.readouterr().err == ""


class _FakeCDLL:
    """``ctypes.CDLL(None)`` stand-in: a libc with ``mallopt`` and, when
    *glibc* is true, ``gnu_get_libc_version`` (musl has no such symbol)."""

    def __init__(self, glibc: bool) -> None:
        self.mallopt_calls: list[tuple[int, int]] = []
        if glibc:
            self.gnu_get_libc_version = lambda: b"2.39"

    def mallopt(self, param: int, value: int) -> int:
        self.mallopt_calls.append((param, value))
        return 1


@pytest.mark.parametrize("glibc", [False, True], ids=["musl-like", "glibc"])
def test_libc_without_gnu_get_libc_version_is_never_given_mallopt(monkeypatch, info_log, glibc):
    """Drive the real detection (no libc= injection) on a fake Linux libc. A
    libc without gnu_get_libc_version (musl) is not glibc even though it may
    export mallopt: mallopt must not be called. The glibc case proves the
    seam reaches mallopt, so the musl case cannot pass vacuously."""
    fake = _FakeCDLL(glibc)
    monkeypatch.setattr(memory_limits.sys, "platform", "linux")
    monkeypatch.setattr(memory_limits, "ctypes", types.SimpleNamespace(CDLL=lambda name: fake))
    applied = apply_startup_arena_cap(environ={}, launch_environ={})
    if glibc:
        assert applied is True
        assert fake.mallopt_calls == [(M_ARENA_MAX, 2)]
    else:
        assert applied is False
        assert fake.mallopt_calls == []
        assert _lines(info_log, logging.DEBUG)[-1] == "malloc arena cap not applied: not Linux glibc"
        assert _info_lines(info_log) == []


def test_libc_load_failure_is_treated_as_non_glibc(monkeypatch):
    def broken(name):
        raise OSError("no libc")

    monkeypatch.setattr(memory_limits.sys, "platform", "linux")
    monkeypatch.setattr(memory_limits, "ctypes", types.SimpleNamespace(CDLL=broken))
    assert apply_startup_arena_cap(environ={}, launch_environ={}) is False


def test_non_linux_platform_has_no_glibc(monkeypatch):
    monkeypatch.setattr(memory_limits.sys, "platform", "darwin")
    assert memory_limits._glibc_malloc() is None


def test_mallopt_exception_returns_false_without_raising(info_log):
    libc = FakeLibc(exc=OSError("boom"))
    applied, _ = _apply({}, libc=libc)
    assert applied is False
    assert libc.calls == [(M_ARENA_MAX, 2)]
    (line,) = _info_lines(info_log)
    assert line.startswith("malloc arena cap not applied: mallopt(M_ARENA_MAX, 2) failed")


def test_mallopt_zero_return_is_false():
    applied, _ = _apply({}, libc=FakeLibc(result=0))
    assert applied is False


def test_unexpected_error_never_raises():
    class BadEnv(dict):
        def get(self, *a, **k):
            raise RuntimeError("bad env")

    assert apply_startup_arena_cap(environ=BadEnv(), launch_environ={}, libc=FakeLibc()) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
def test_launch_environ_keeps_only_the_arena_keys():
    launch = memory_limits._launch_environ()
    assert launch is not None
    assert set(launch) <= {"MALLOC_ARENA_MAX", "GLIBC_TUNABLES"}


def test_never_exports_malloc_arena_max(monkeypatch):
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    monkeypatch.delenv("GLIBC_TUNABLES", raising=False)
    monkeypatch.delenv(ENV_KNOB, raising=False)
    apply_startup_arena_cap(launch_environ={}, libc=FakeLibc())
    assert "MALLOC_ARENA_MAX" not in os.environ
    assert "GLIBC_TUNABLES" not in os.environ


def test_one_line_reaches_stderr_without_logging_handlers(capsys, monkeypatch):
    """WebUI configures no logging handlers; the INFO line must still be written."""
    monkeypatch.setattr(memory_limits.logger, "isEnabledFor", lambda level: False)
    assert apply_startup_arena_cap(environ={}, launch_environ={}, libc=FakeLibc()) is True
    err = capsys.readouterr().err
    assert err == "[memory] INFO malloc arena cap applied: M_ARENA_MAX=2 (default)\n"


# ── placement in server.py ────────────────────────────────────────────────


def _module_level_lines(tree: ast.Module):
    """Line of the module-level call and of the first import that is not stdlib
    (WebUI's ``api.*``, ``managed_agent_startup`` or anything Agent-side)."""
    call_line = first_non_stdlib_import = None
    for node in tree.body:
        if (
            call_line is None
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "apply_startup_arena_cap"
        ):
            call_line = node.lineno
        if isinstance(node, ast.ImportFrom) and node.module:
            roots = [node.module.split(".")[0]]
        elif isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        else:
            continue
        if first_non_stdlib_import is None and any(
            r not in sys.stdlib_module_names and r != "startup_memory_limits" for r in roots
        ):
            first_non_stdlib_import = node.lineno
    return call_line, first_non_stdlib_import


def test_server_calls_cap_before_any_agent_or_api_import():
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    call_line, first_import = _module_level_lines(tree)
    assert call_line is not None, "server.py must call apply_startup_arena_cap() at module level"
    assert first_import is not None
    assert call_line < first_import, (
        f"apply_startup_arena_cap() (line {call_line}) must run before the first "
        f"non-stdlib import (line {first_import})"
    )
    # Nothing before the call may start a thread (no threading.Thread(...) etc).
    prefix = SERVER_PY.read_text(encoding="utf-8").splitlines()[:call_line]
    assert not any("Thread(" in line or "start_new_thread" in line for line in prefix)


def test_cap_module_is_stdlib_only():
    tree = ast.parse((REPO_ROOT / "startup_memory_limits.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
    assert roots and roots <= set(sys.stdlib_module_names) | {"__future__"}, roots


def _is_glibc() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return hasattr(ctypes.CDLL(None), "gnu_get_libc_version")
    except Exception:
        return False


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MALLOC_ARENA_MAX", "GLIBC_TUNABLES", ENV_KNOB, "PYTHONSTARTUP")
    }
    env.update(extra)
    return env


def _run_python(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(not _is_glibc(), reason="Linux glibc only")
def test_server_runs_cap_with_only_the_main_thread(tmp_path):
    """Run the real server.py in a fresh interpreter and stop inside the call:
    record ``threading.active_count()`` there, which local modules are already
    loaded, and the real ``mallopt`` result."""
    script = f"""
        import runpy, sys, threading
        sys.path.insert(0, {str(REPO_ROOT)!r})
        assert threading.active_count() == 1
        import startup_memory_limits as m
        assert threading.active_count() == 1, "importing startup_memory_limits started a thread"
        real = m.apply_startup_arena_cap
        def probe(**kw):
            count = threading.active_count()
            applied = real(**kw)
            early = sorted(n for n in sys.modules
                           if n in ("api", "managed_agent_startup") or n.startswith("api."))
            print(f"active_count_at_call={{count}} applied={{applied}} early_local_modules={{early}}")
            raise SystemExit(0)  # stop server.py right after the call
        m.apply_startup_arena_cap = probe
        runpy.run_path({str(SERVER_PY)!r}, run_name="_server_probe")
        raise SystemExit("apply_startup_arena_cap() was never called")
    """
    result = _run_python(script, _clean_env(), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "active_count_at_call=1 applied=True early_local_modules=[]" in result.stdout
    assert "[memory] INFO malloc arena cap applied: M_ARENA_MAX=2 (default)" in result.stderr


@pytest.mark.skipif(not _is_glibc(), reason="Linux glibc only")
@pytest.mark.parametrize(
    ("launch_env", "set_in_process", "expected"),
    [
        ({"MALLOC_ARENA_MAX": "4"}, None,
         "not applied: operator set MALLOC_ARENA_MAX='4' at launch"),
        ({"MALLOC_ARENA_MAX": "4"}, "6",
         "not applied: operator set MALLOC_ARENA_MAX='4' at launch"),
        ({"GLIBC_TUNABLES": "glibc.malloc.arena_max=1"}, None,
         "not applied: operator set GLIBC_TUNABLES glibc.malloc.arena_max='1' at launch"),
        ({}, "3", "applied: M_ARENA_MAX=3 (MALLOC_ARENA_MAX set after launch)"),
        ({ENV_KNOB: "off"}, None, "not applied: disabled by HERMES_WEBUI_MALLOC_ARENA_MAX='off'"),
        ({"MALLOC_ARENA_MAX": "0"}, None, "applied: M_ARENA_MAX=2 (default)"),
        ({"MALLOC_ARENA_MAX": "abc"}, None, "applied: M_ARENA_MAX=2 (default)"),
        ({"GLIBC_TUNABLES": "glibc.malloc.arena_max=-3"}, None, "applied: M_ARENA_MAX=2 (default)"),
    ],
)
def test_real_exec_time_env_versus_in_process_env(tmp_path, launch_env, set_in_process, expected):
    """Real /proc/self/environ and real mallopt: an exec-time setting is left to
    glibc; one set only in os.environ after exec is applied."""
    script = f"""
        import os, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        if {set_in_process!r} is not None:
            os.environ["MALLOC_ARENA_MAX"] = {set_in_process!r}
        import startup_memory_limits as m
        print("applied=%s" % m.apply_startup_arena_cap())
    """
    result = _run_python(script, _clean_env(**launch_env), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[memory] INFO malloc arena cap {expected}" in result.stderr
    assert ("applied=True" in result.stdout) is expected.startswith("applied")


@pytest.mark.skipif(not _is_glibc(), reason="Linux glibc only")
@pytest.mark.parametrize(
    ("launch_env", "shown"),
    [
        ({"MALLOC_ARENA_MAX": "0"}, "MALLOC_ARENA_MAX='0'"),
        ({"MALLOC_ARENA_MAX": "-3"}, "MALLOC_ARENA_MAX='-3'"),
        ({"GLIBC_TUNABLES": "glibc.malloc.arena_max=abc"}, "GLIBC_TUNABLES glibc.malloc.arena_max='abc'"),
    ],
)
def test_real_invalid_launch_value_warns_once_and_caps(tmp_path, launch_env, shown):
    """Real exec-time env and real mallopt: glibc set no cap for these values,
    so the server applies its own and says once that the value was ignored."""
    script = f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        import startup_memory_limits as m
        print("applied=%s" % m.apply_startup_arena_cap())
    """
    result = _run_python(script, _clean_env(**launch_env), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "applied=True" in result.stdout
    warnings = [line for line in result.stderr.splitlines() if line.startswith("[memory] WARNING")]
    assert warnings == [f"[memory] WARNING malloc arena cap: ignoring invalid {shown} (not a positive integer)"]
