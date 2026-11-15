"""Best-effort backstop: no test may write into the real Hermes root.

``tests/conftest.py`` sandboxes ``HOME``/``HERMES_HOME`` for the whole session, so Agent code
that resolves the platform default ``~/.hermes`` lands in a per-session directory. That sandbox is
the prevention. This module is a best-effort alarm for whatever still slips through: an audit hook
refuses every write-mode filesystem call whose target (symlinks resolved) lies inside a protected
real root and records it, and the conftest fails the running test and the session on any record.

Best-effort by design: it catches the common mistakes, not every possible write. Known unchecked
paths include child processes (see Scope below), descriptors that cannot be named, hard links that
existed before the run, and writes through native code that bypasses Python's audit events. Change
it only to fix a wrongly blocked action or a leak out of the per-session sandbox; do not grow it to
chase every way around it.

The protected roots are computed ONCE, from the environment the suite was started with (the OS
account home from ``pwd``, the starting ``HOME`` and the root of the starting ``HERMES_HOME``),
and published in ``HERMES_WEBUI_TEST_REAL_HERMES_ROOTS`` so later imports and child processes
inherit them instead of re-deriving them from the already sandboxed environment. Allowed roots
(the checkout, the test state/sandbox dirs, the temp dir, pytest's ``--basetemp``) win over
protected ones: a checkout or scratch temp dir may legitimately live under ``~/.hermes``.

Descriptor-relative calls (``dir_fd``) are resolved against their directory through
``/proc/self/fd`` (Linux) or ``F_GETPATH`` (macOS); Windows has no ``dir_fd``. Where neither
can name the descriptor the call is left unchecked. ``os.open`` is wrapped because its audit
event omits ``dir_fd``. ``sqlite3`` ``file:`` URIs are decoded as SQLite does.

Scope: the audit hook guards THIS (pytest) process only. Audit hooks are per-interpreter and
child processes do not inherit them, and nothing injects the guard into children (no
sitecustomize or PYTHONPATH hook). Children (the test server, Agent CLIs) are covered by the
root fix instead: they inherit the sandboxed HOME, HERMES_REAL_HOME, LOCALAPPDATA and HERMES_HOME
(a test asserts the test server's environment), so their platform default resolves inside the
sandbox. The session-end check that each real root's ``profiles/`` listing did not change
sees profile creation or deletion by ANY process, which is the incident class this guards.
"""
import os
import pathlib
import re
import sys
import threading
import urllib.parse

ENV = "HERMES_WEBUI_TEST_REAL_HERMES_ROOTS"
PROTECTED: list = []
ALLOWED: list = []
AGENT_CHECKOUTS: list = []  # resolved Agent checkouts the suite imports from (bytecode may be written beneath them)
VIOLATIONS: list = []
_STATE = {"active": False, "installed": False, "profiles": {}}
_LOCAL = threading.local()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


class RealHermesRootWriteError(PermissionError):
    """A test tried to write into the real Hermes root."""


def _account_home() -> pathlib.Path:
    try:
        import pwd
        return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError, AttributeError):
        return pathlib.Path.home()


def _root_of(home: pathlib.PurePath) -> pathlib.PurePath:
    """The Hermes root holding *home*: its ``.hermes*`` ancestor, ``<root>`` of ``<root>/profiles/<name>``, else itself."""
    for p in (home, *home.parents):
        if p.name.startswith(".hermes"):
            return p
    return home.parent.parent if home.parent.name == "profiles" else home


def real_roots() -> list:
    if not os.environ.get(ENV):
        suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
        roots = {_account_home() / (".hermes" + suffix), pathlib.Path.home() / (".hermes" + suffix)}
        start = os.environ.get("HERMES_HOME", "").strip()
        if start:
            roots.add(_root_of(pathlib.Path(os.path.expanduser(start))))
        if sys.platform == "win32":  # the Agent's native root there is %LOCALAPPDATA%\\hermes
            local = os.environ.get("LOCALAPPDATA", "").strip()
            roots.add(pathlib.Path(local or pathlib.Path.home() / "AppData" / "Local") / ("hermes" + suffix))
        os.environ[ENV] = os.pathsep.join(sorted({os.path.realpath(r) for r in roots}))
    return [p for p in os.environ[ENV].split(os.pathsep) if p]


def _under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def violation(path, protected=None, allowed=None, follow=True):
    """Return the resolved target when writing *path* would touch a protected root, else None.

    With ``follow=False`` (unlink, rename, mkdir, ...) a final symlink is the target itself.
    Symlinks are resolved before ``..`` is collapsed, as the kernel does."""
    if path is None or isinstance(path, int):
        return None
    try:
        raw = os.fsdecode(path)
        target = os.path.realpath(raw) if follow else os.path.normpath(os.path.join(
            os.path.realpath(os.path.dirname(raw) or os.curdir), os.path.basename(raw)))
    except (TypeError, ValueError):
        return None
    protected = PROTECTED if protected is None else protected
    checkouts = [c for c in AGENT_CHECKOUTS if not any(_under(r, [c]) for r in protected)]  # never a whole root
    if f"{os.sep}__pycache__{os.sep}" in target + os.sep and _under(
            target, [os.path.join(r, "hermes-agent") for r in protected] + checkouts):
        return None  # bytecode (and its dir) next to Agent code imported from <root>/hermes-agent or a discovered checkout
    if _under(target, protected) and not _under(target, ALLOWED if allowed is None else allowed):
        return target
    return None


def _sqlite_target(database):
    """The file a sqlite3.connect may write: ``file:`` URIs decoded; read-only/memory opens write none."""
    text = os.fsdecode(database) if isinstance(database, (str, bytes, os.PathLike)) else ""
    if text.startswith("file:"):
        url = urllib.parse.urlsplit(text)
        if set(urllib.parse.parse_qs(url.query).get("mode", ["rwc"])) <= {"ro", "memory"}:
            return ()
        text = ("" if url.netloc in ("", "localhost") else "//" + url.netloc) + urllib.parse.unquote(url.path).split("\0", 1)[0]
        if os.name == "nt" and re.match(r"/[A-Za-z]:", text):  # file:///C:/x -> C:/x
            text = text[1:]
    return () if text in ("", ":memory:") else (text,)


def _fd_path(fd):
    """The directory *fd* names, or None where the platform cannot tell."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    except (ImportError, AttributeError, OSError):
        return None


def _at(path, dir_fd):
    """*path* as seen by a call made relative to *dir_fd* (None: unresolvable, left unchecked)."""
    if not isinstance(dir_fd, int) or dir_fd < 0 or not isinstance(path, (str, bytes, os.PathLike)):
        return path
    if os.path.isabs(os.fsdecode(path)):
        return path
    base = _fd_path(dir_fd)
    return None if base is None else os.path.join(base, os.fsdecode(path))


def _open_targets(args):
    path, mode, flags = args
    writing = any(c in mode for c in "wax+") if isinstance(mode, str) else bool((flags or 0) & _WRITE_FLAGS)
    return (_at(path, getattr(_LOCAL, "dir_fd", None)),) if writing else ()


def _at_args(*pairs):
    return lambda a: tuple(_at(a[p], a[d] if d < len(a) else None) for p, d in pairs)


# audit event -> written paths, each resolved against its dir_fd argument
# (https://docs.python.org/3/library/audit_events.html)
_EVENTS = {
    "open": _open_targets, "sqlite3.connect": lambda a: _sqlite_target(a[0]),
    "os.mkdir": _at_args((0, 2)), "os.remove": _at_args((0, 1)), "os.rmdir": _at_args((0, 1)),
    "os.truncate": _at_args((0, 2)), "os.chmod": _at_args((0, 2)), "shutil.rmtree": _at_args((0, 1)),
    "os.rename": _at_args((0, 2), (1, 3)), "os.link": _at_args((0, 2), (1, 3)), "os.symlink": _at_args((1, 2)),
}
# os.link checks its source entry as Linux link() does (no final-symlink follow); a link made THROUGH a
# symlink with following semantics (dir_fd + follow_symlinks=True, or a following link()) is unchecked.
_NOFOLLOW = {"os.mkdir", "os.remove", "os.rmdir", "shutil.rmtree", "os.rename", "os.link", "os.symlink"}
_REAL_OS_OPEN = os.open


def _os_open(path, flags, mode=0o777, *, dir_fd=None):
    """``os.open`` whose 'open' audit event omits *dir_fd*: hand it to the hook."""
    _LOCAL.dir_fd = dir_fd
    try:
        return _REAL_OS_OPEN(path, flags, mode, dir_fd=dir_fd)
    finally:
        _LOCAL.dir_fd = None


def _hook(event, args):
    if not _STATE["active"] or event not in _EVENTS or getattr(_LOCAL, "busy", False):
        return
    _LOCAL.busy = True
    try:
        for path in _EVENTS[event](args):
            target = violation(path, follow=event not in _NOFOLLOW)
            if target:
                VIOLATIONS.append(f"{event} {target}")
                raise RealHermesRootWriteError(
                    f"REFUSED: a test tried to write into the real Hermes root ({event} {target}). "
                    f"Tests must resolve a sandboxed HERMES_HOME/HOME; see tests/_real_hermes_root_guard.py.")
    finally:
        _LOCAL.busy = False


def _profiles_listing():
    listing = {}
    for root in PROTECTED:
        try:
            listing[root] = sorted(os.listdir(os.path.join(root, "profiles")))
        except OSError:
            listing[root] = None
    return listing


def install(allowed) -> None:
    """Protect the real roots (minus *allowed*) for the rest of this process."""
    PROTECTED[:] = real_roots()
    # Union, never replace: conftest.py can run again mid-session (a test loads it by path), and a path
    # allowed earlier, such as --basetemp from pytest_configure, must stay allowed.
    ALLOWED[:] = sorted(set(ALLOWED) | {os.path.realpath(os.fspath(p)) for p in allowed})
    _STATE["profiles"] = _profiles_listing()
    _STATE["active"] = True
    if not _STATE["installed"]:  # audit hooks cannot be removed; never stack a second one
        sys.addaudithook(_hook)
        if _REAL_OS_OPEN in os.supports_dir_fd:  # keep `os.open in os.supports_dir_fd` probes true
            os.supports_dir_fd.add(_os_open)
        os.open = _os_open
        _STATE["installed"] = True


def allow(path) -> None:
    ALLOWED.append(os.path.realpath(os.fspath(path)))


def register_agent_checkout(path) -> None:
    """Allow bytecode writes beneath an Agent checkout the suite imports from. Discovery prefers
    <HERMES_HOME>/hermes-agent, which for a profile home is <root>/profiles/<name>/hermes-agent."""
    AGENT_CHECKOUTS.append(os.path.realpath(os.fspath(path)))


def link_real_dirs(sandbox, pairs) -> list:
    """Link each existing real directory into the sandbox home at the same relative place, so lookups that follow
    HOME still find it (missing parents are created; an existing entry is kept). Returns the paths that failed."""
    failed = []
    for src, rel in pairs:
        dest = pathlib.Path(sandbox) / rel
        if not pathlib.Path(src).is_dir() or os.path.lexists(dest):
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(src, target_is_directory=True)
        except OSError:
            failed.append(rel)
    return failed


PLAYWRIGHT_PATH_VARS = ("PLAYWRIGHT_BROWSERS_PATH", "npm_config_playwright_browsers_path",
                        "npm_package_config_playwright_browsers_path")


def playwright_uses_default_cache(environ) -> bool:
    """True when Playwright would use its default (HOME/LOCALAPPDATA-based) cache: the first of its path variables that is
    defined, in Playwright's own order, is empty or there is none ("0" selects package-local browsers instead)."""
    return not next((environ[k] for k in PLAYWRIGHT_PATH_VARS if k in environ), None)


def finish() -> list:
    """Stop guarding and return every problem found during the session."""
    _STATE["active"] = False
    problems = list(VIOLATIONS)
    for root, before in _STATE["profiles"].items():
        after = _profiles_listing().get(root)
        if after != before:
            problems.append(f"{root}/profiles changed during the run (by this run or by another process on this host):"
                            f" {before} -> {after}")
    return problems
