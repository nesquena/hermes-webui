"""Structural guard: every subprocess spawn declares Win32 creationflags.

Why this exists: "spawns a console window on Windows" has been fixed three
separate times in this repo — #3710 (test helpers), #4626 (update/restart
respawn), #5692 (workspace git) — and came back each time, because each fix
added a private ``_windows_hide_flags()`` and patched only the call sites in
front of it. The next contributor writing a perfectly reasonable
``subprocess.run(["git", ...])`` reintroduced the bug class. The user-visible
symptom of the most recent recurrence: a burst of black console windows
flashing on every page navigation, because ``workspace._run_git`` runs once per
workspace on a listing.

Per-call-site regression tests (``test_workspace_git.py``) can't prevent that —
they only cover the function someone already thought about. This guard is
exhaustive by construction: it AST-scans the whole API surface and fails on ANY
spawn that doesn't declare its creationflags, including ones that don't exist
yet.

A spawn passes if any of:

1. It passes ``creationflags=`` explicitly (the normal case). Use
   ``windows_hide_flags()`` from ``api._subprocess_compat`` for anything whose
   output you read, ``windows_detach_flags()`` for background children that
   must outlive this process.
2. It unpacks a kwargs dict AND the enclosing function assigns
   ``[...]["creationflags"]`` into a dict — the platform-branching pattern in
   ``providers.py``, where POSIX gets ``preexec_fn`` and Windows gets the
   flags. Deleting that assignment re-fails the guard.
3. Its module is in :data:`ALLOWLIST` with a written reason.

The helpers return 0 on non-Windows, which is the ``subprocess`` default — so
this rule costs POSIX call sites nothing and needs no platform branching at the
point of use. This test therefore asserts the same thing on every platform.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# subprocess entry points that create a process (and therefore a console
# window on Windows). `subprocess.getoutput`/`getstatusoutput` are excluded
# deliberately: they route through the shell and aren't used in this codebase.
SPAWN_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}

# Modules exempt from the rule, each with the reason it can't spawn a Windows
# console window. Keep this list SHORT and justified — an entry here is a
# permanent hole in the guard.
ALLOWLIST = {
    "api/terminal.py": (
        "Module is hard-disabled on Windows: _TERMINAL_SUPPORTED = "
        "sys.platform != 'win32', and the spawn sits behind a pty/fcntl path "
        "that only imports on POSIX."
    ),
}


def _iter_scanned_files():
    yield from sorted((REPO / "api").glob("*.py"))
    yield REPO / "server.py"


def _assigns_creationflags(func_node) -> bool:
    """True if *func_node* stores a 'creationflags' key into a dict.

    Matches the ``kwargs["creationflags"] = windows_hide_flags()`` pattern used
    where the flags are set on a platform branch and then unpacked into Popen.
    """
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "creationflags"
            ):
                return True
    return False


def _enclosing_functions(tree):
    """Map each node in a function body back to that function definition."""
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node)
    return owner


def _find_bare_spawns(path: Path):
    """Return [(lineno, source_excerpt)] for spawns missing creationflags."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)
    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in SPAWN_FUNCS):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            continue

        if any(kw.arg == "creationflags" for kw in node.keywords):
            continue

        # Indirect case: **kwargs unpacked in, flags set on a branch above.
        if any(kw.arg is None for kw in node.keywords):
            enclosing = owner.get(node)
            if enclosing is not None and _assigns_creationflags(enclosing):
                continue

        argv = ast.unparse(node.args[0])[:70] if node.args else "<kwargs-only>"
        offenders.append((node.lineno, f"subprocess.{fn.attr}({argv} ...)"))

    return offenders


def test_no_subprocess_spawn_without_creationflags():
    failures = []
    for path in _iter_scanned_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWLIST:
            continue
        for lineno, excerpt in _find_bare_spawns(path):
            failures.append(f"  {rel}:{lineno}  {excerpt}")

    assert not failures, (
        "These subprocess spawns don't declare creationflags, so each one pops a "
        "console window on Windows (see #3710 / #4626 / #5692):\n"
        + "\n".join(failures)
        + "\n\nFix: add `creationflags=windows_hide_flags()` (import from "
        "api._subprocess_compat) — or windows_detach_flags() for a background "
        "child that must outlive this process and whose output you do NOT read. "
        "Both return 0 off Windows, so no platform branching is needed."
    )


def test_allowlisted_modules_still_exist_and_are_justified():
    """An allowlist entry for a deleted/renamed module silently widens the guard."""
    for rel, reason in ALLOWLIST.items():
        assert (REPO / rel).exists(), f"ALLOWLIST names a missing file: {rel}"
        assert len(reason) > 40, f"ALLOWLIST entry for {rel} needs a real reason"


def test_terminal_module_remains_posix_only():
    """Pins the assumption behind api/terminal.py's allowlist entry.

    If the embedded terminal ever gains a Windows implementation, its spawn
    starts creating console windows and must be removed from ALLOWLIST — this
    test fails at that moment rather than letting it through silently.
    """
    source = (REPO / "api" / "terminal.py").read_text(encoding="utf-8")
    assert '_TERMINAL_SUPPORTED = sys.platform != "win32"' in source, (
        "api/terminal.py is no longer unconditionally disabled on Windows; "
        "re-audit its subprocess.Popen call and drop the ALLOWLIST entry in "
        "this file."
    )


def test_hide_and_detach_flags_are_noop_off_windows():
    """The 'safe to call everywhere' property the whole rule depends on."""
    import sys

    from api._subprocess_compat import windows_detach_flags, windows_hide_flags

    if sys.platform == "win32":
        # CREATE_NO_WINDOW; detach adds DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP.
        assert windows_hide_flags() == 0x08000000
        assert windows_detach_flags() == 0x08000000 | 0x00000008 | 0x00000200
        # Hiding must NOT detach — DETACHED_PROCESS severs stdio and would
        # silently break every capture_output=True caller.
        assert not windows_hide_flags() & 0x00000008
    else:
        assert windows_hide_flags() == 0
        assert windows_detach_flags() == 0
