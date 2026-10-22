#!/usr/bin/env python3
"""Decide which interpreter the Docker agent venv must use (#8113).

The WebUI image ships a Python 3.12 base, but the agent it stages gates its
core dependency set on ``python_version >= '3.14'``. A bare ``uv venv`` inherits
the base image interpreter, every one of those markers evaluates false, and the
agent silently fails to import while the container still reports healthy.

Two things have to line up for the fix to hold, so this module answers both
questions in one place:

1. *Which* interpreters does the staged agent accept? Two constraints apply
   together: the complete ``requires-python`` range (lower *and* upper bounds,
   exclusions, wildcards) and the highest ``python_version >= '3.x'`` gate
   across its dependencies — ``requires-python`` alone is not sufficient,
   because the agent still declares ``>=3.11,<3.15`` while gating its core deps
   on 3.14, and the gate alone is not sufficient either, because it says
   nothing about the declared maximum.
2. *Does the existing venv still satisfy that?* A restart reuses
   ``/app/venv`` and skips the install when ``.deps_installed`` is present, so a
   venv on an inadmissible interpreter has to be rebuilt explicitly — otherwise
   it pins the broken state across every subsequent restart. A venv that is
   already admissible is kept, and when *no* interpreter is admissible the venv
   is left alone rather than deleted for a rebuild that cannot succeed.

Contract with ``docker_init.bash``: stdout is exactly ``"<action> <python>"``
where action is ``create`` / ``reuse`` / ``rebuild`` and python is
``major.minor`` or ``-``. Anything unreadable or unparseable fails closed to
``reuse -`` so a staging problem never blocks container startup.

The ``requires-python`` comparator is self-contained on purpose: this script
runs on the base image's bare interpreter before any venv exists, where
``packaging`` is not guaranteed to be importable. It implements
``packaging.specifiers.SpecifierSet`` semantics for plain release numbers
(the form interpreter bounds take) and is pinned to that oracle by the tests;
any other syntax is reported as unparseable rather than approximated.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:  # Python 3.11+ (the base image is 3.12); degrade to regex-only below.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    tomllib = None  # type: ignore[assignment]

# `python_version >= '3.14'` inside a PEP 508 environment marker.
_GATE_RE = re.compile(r"python_version\s*>=\s*['\"](\d+)\.(\d+)['\"]")
# `requires-python = "..."`, for the no-tomllib / unparseable-TOML fallback.
_REQUIRES_PYTHON_RE = re.compile(r"""^\s*requires-python\s*=\s*["']([^"']*)["']""", re.MULTILINE)
# One clause of a `requires-python` specifier, restricted to plain release
# numbers: `>=3.11`, `<3.15`, `!=3.12.*`, `~=3.11`. Pre/post/dev/local
# versions, epochs and `===` are deliberately not matched (see module doc).
_CLAUSE_RE = re.compile(r"^\s*(~=|==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+)*)(\.\*)?\s*$")

DEPS_MARKER = ".deps_installed"

# `python-dotenv` is the first thing #8113 dropped: `run_agent` imports
# `hermes_cli.env_loader`, whose module scope does `import dotenv`. Probing it
# directly reports the dropped dependency instead of only its symptom
# (ModuleNotFoundError deep inside the agent's own import chain).
CRITICAL_IMPORT = "dotenv"
_PROBE_TIMEOUT = 60


def _dependency_strings(pyproject_text: str) -> list[str]:
    """Every PEP 508 requirement in the project, base plus optional extras."""
    if tomllib is not None:
        try:
            data = tomllib.loads(pyproject_text)
        except Exception:
            data = None
        if isinstance(data, dict):
            project = data.get("project")
            if isinstance(project, dict):
                reqs = list(project.get("dependencies") or [])
                optional = project.get("optional-dependencies")
                if isinstance(optional, dict):
                    for extra in optional.values():
                        reqs.extend(extra or [])
                return [str(r) for r in reqs]
    # Fallback: scan the raw text. The gate regex requires the literal
    # `python_version`, so the `requires-python` line cannot be misread as one.
    return [pyproject_text]


def _from_dependencies(pyproject_text: str) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    for requirement in _dependency_strings(pyproject_text):
        for major, minor in _GATE_RE.findall(requirement):
            candidate = (int(major), int(minor))
            if best is None or candidate > best:
                best = candidate
    return best


def _requires_python(pyproject_text: str) -> object:
    """The raw ``[project] requires-python`` value, or None when not declared."""
    if tomllib is not None:
        try:
            data = tomllib.loads(pyproject_text)
        except Exception:
            data = None
        if isinstance(data, dict):
            project = data.get("project")
            return project.get("requires-python") if isinstance(project, dict) else None
    match = _REQUIRES_PYTHON_RE.search(pyproject_text)
    return match.group(1) if match else None


def _parse_requires_python(spec: object) -> list[tuple[str, tuple[int, ...], bool]] | None:
    """``[(operator, release, is_wildcard), ...]``, or None if not evaluable here.

    Empty clauses are skipped exactly as ``SpecifierSet`` skips them, so ``""``
    admits every interpreter.
    """
    if not isinstance(spec, str):
        return None
    clauses = []
    for part in spec.split(","):
        if not part.strip():
            continue
        match = _CLAUSE_RE.match(part)
        if not match:
            return None
        operator, number, wildcard = match.group(1), match.group(2), bool(match.group(3))
        release = tuple(int(piece) for piece in number.split("."))
        if wildcard and operator not in ("==", "!="):
            return None  # `>=3.*` is not a valid PEP 440 specifier
        if operator == "~=" and (wildcard or len(release) < 2):
            return None  # `~=3` is not a valid PEP 440 specifier
        clauses.append((operator, release, wildcard))
    return clauses


def _clause_admits(operator: str, release: tuple[int, ...], wildcard: bool, version: tuple[int, ...]) -> bool:
    if wildcard:
        # Prefix match: zero-pad the candidate to the prefix length, then compare.
        padded = version + (0,) * (len(release) - len(version))
        matched = padded[: len(release)] == release
        return matched if operator == "==" else not matched
    if operator == "~=":
        # `~=3.11.2` is `>=3.11.2, ==3.11.*`.
        return _clause_admits(">=", release, False, version) and _clause_admits("==", release[:-1], True, version)
    # Release comparison with zero padding: 3.14 == 3.14.0.
    width = max(len(release), len(version))
    left = version + (0,) * (width - len(version))
    right = release + (0,) * (width - len(release))
    return {
        "==": left == right,
        "!=": left != right,
        "<=": left <= right,
        ">=": left >= right,
        "<": left < right,
        ">": left > right,
    }[operator]


def requires_python_admits(spec: str, version: tuple[int, ...]) -> bool | None:
    """Whether a ``requires-python`` specifier admits ``version``.

    Mirrors ``packaging.specifiers.SpecifierSet(spec).contains(version)`` for
    final releases; returns None for syntax outside the supported subset.
    """
    clauses = _parse_requires_python(spec)
    if clauses is None:
        return None
    return all(_clause_admits(op, release, wildcard, tuple(version)) for op, release, wildcard in clauses)


def required_agent_python(pyproject_text: str) -> tuple[int, int] | None:
    """Lowest interpreter the staged agent's dependency gates need, or None.

    This is a floor, not the whole answer: the agent declares
    ``requires-python = ">=3.11,<3.15"`` while gating its core set on 3.14, so
    the gate is what rules out the base image's 3.12 (#8113), and the declared
    range is what rules out anything at or above 3.15 — see
    :func:`agent_python_constraints`.
    """
    try:
        return _from_dependencies(pyproject_text)
    except Exception:
        # Unknown input is not a licence to guess (GUIDELINES rule 3).
        return None


def agent_python_constraints(pyproject_text: str):
    """``(requires-python clauses, dependency gate)`` for the staged agent, or None.

    None means there is nothing to enforce — no declared range and no gate — or
    that the declared range cannot be evaluated; either way the caller keeps
    today's behaviour rather than rebuild against bounds it cannot respect.
    """
    try:
        gate = _from_dependencies(pyproject_text)
        declared = _requires_python(pyproject_text)
        clauses = [] if declared is None else _parse_requires_python(declared)
        if clauses is None or (not clauses and gate is None):
            return None
        return clauses, gate
    except Exception:
        return None


def python_admitted(constraints, version: tuple[int, int]) -> bool:
    """Whether an interpreter satisfies the declared range *and* the gate."""
    clauses, gate = constraints
    if gate is not None and tuple(version[:2]) < gate:
        return False
    return all(_clause_admits(op, release, wildcard, tuple(version)) for op, release, wildcard in clauses)


def select_agent_python(constraints, base_python: tuple[int, int]) -> tuple[int, int] | None:
    """The admissible interpreter to build on, nearest the base image's; None if none is.

    The base interpreter wins whenever it is admissible (no download). Otherwise
    the nearest admissible minor is chosen (the newer of two equally near ones),
    so a 3.14 floor over a 3.12 base resolves to 3.14, not to anything newer,
    and a `<3.12` ceiling resolves to 3.11. Every minor above the highest one a
    clause or the gate mentions behaves like the first of them, so the search
    window is finite and complete.
    """
    clauses, gate = constraints
    major = gate[0] if gate is not None else (base_python[0] or 3)
    anchor = base_python[1] if base_python[0] == major else (gate[1] if gate is not None else 0)
    bounds = [anchor] + [release[1] for _, release, _ in clauses if release[0] == major and len(release) > 1]
    if gate is not None:
        bounds.append(gate[1])
    for minor in sorted(range(max(bounds) + 2), key=lambda m: (abs(m - anchor), -m)):
        # The base interpreter is a concrete release: judge it by its full
        # version, so 3.12.13 satisfies `>=3.12.1` (#8360 gate). Any other
        # minor is a request uv resolves later; rebuild_agent_venv re-checks the
        # interpreter uv actually installed (``--admits``).
        candidate = tuple(base_python) if (major, minor) == tuple(base_python[:2]) else (major, minor)
        if python_admitted(constraints, candidate):
            return candidate
    return None


def venv_python_version(venv_dir: Path) -> tuple[int, ...] | None:
    """Release of an existing venv's interpreter (major, minor[, micro]), from ``pyvenv.cfg``.

    The micro version is kept: a ``requires-python`` such as ``>=3.12.1`` is
    decided by it.

    Returns None when the venv is absent or structurally unusable, which the
    caller treats as incompatible rather than as "no venv".
    """
    cfg = venv_dir / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*version(?:_info)?\s*=\s*(\d+)\.(\d+)(?:\.(\d+))?", text, re.MULTILINE)
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def plan_agent_venv(
    pyproject_path: Path,
    venv_dir: Path,
    base_python: tuple[int, int],
) -> dict:
    """Decide whether to create, reuse or rebuild the Docker agent venv.

    ``base_python`` is the interpreter the venv would otherwise silently
    inherit; it is preferred whenever the agent admits it. ``clear_deps_marker``
    tells the caller to drop ``.deps_installed`` so a rebuilt venv actually
    reinstalls instead of inheriting the fast-restart skip.

    ``python`` is the interpreter the venv runs on after the plan, or None for
    "the base image's, nothing to pin". A rebuild always names its interpreter:
    leaving that choice to uv's default discovery could re-select the very
    interpreter being replaced.
    """
    constraints = None
    try:
        if pyproject_path.is_file():
            constraints = agent_python_constraints(
                pyproject_path.read_text(encoding="utf-8", errors="replace")
            )
    except OSError:
        constraints = None

    has_venv = (venv_dir / "bin" / "python3").exists()

    if constraints is None:
        # Nothing authoritative to enforce: keep today's behaviour exactly.
        return {
            "action": "reuse" if has_venv else "create",
            "python": None,
            "clear_deps_marker": False,
            "required": None,
            "constrained": False,
            "base_python": base_python,
        }

    target = select_agent_python(constraints, base_python)
    current = venv_python_version(venv_dir) if has_venv else None

    if current is not None and python_admitted(constraints, current):
        # Already admissible — even if a fresh container would pick another
        # interpreter, an operator's in-range choice is not torn down.
        action, python = "reuse", tuple(current[:2])
    elif has_venv and target is None:
        # Nothing admissible to rebuild onto: deleting this venv would only
        # trade a degraded-but-running WebUI for a slower, equally broken boot.
        action, python = "reuse", None
    elif has_venv:
        action, python = "rebuild", target
    else:
        action, python = "create", target

    if python is not None and action != "rebuild" and tuple(python) == tuple(base_python[: len(python)]):
        python = None

    return {
        "action": action,
        "python": python,
        "clear_deps_marker": action == "rebuild",
        "required": target,
        "constrained": True,
        "base_python": base_python,
    }


def _venv_python_exe(venv_dir: Path) -> Path | None:
    for name in ("python3", "python"):
        candidate = venv_dir / "bin" / name
        if candidate.exists():
            return candidate
    win = venv_dir / "Scripts" / "python.exe"
    if win.exists():
        return win
    return None


def check_critical_imports(venv_dir: Path, expected: tuple[int, int] | None = None) -> tuple[bool, str]:
    """Import the agent's critical dependency with the venv's own interpreter.

    Returns ``(ok, detail)``; ``detail`` is empty on success and otherwise names
    the missing module together with the interpreter that was actually used, so
    an operator reading the log can tell a dropped dependency apart from any
    other import failure. Never raises — a broken probe must not be what stops a
    container from booting.
    """
    expected_txt = "the staged agent" if expected is None else "Python {}.{}".format(*expected)

    exe = _venv_python_exe(venv_dir)
    if exe is None:
        return False, (
            f"no interpreter found in {venv_dir}; cannot verify the {CRITICAL_IMPORT} import "
            f"required by {expected_txt}"
        )

    current = venv_python_version(venv_dir)
    current_txt = "an unknown Python" if current is None else "Python " + _version_text(current)

    try:
        result = subprocess.run(
            [str(exe), "-c", f"import {CRITICAL_IMPORT}"],
            cwd=str(venv_dir),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as exc:  # timeout, EACCES, exec format, ...
        return False, (
            f"could not run the critical-import check with {current_txt} in {venv_dir}: "
            f"{type(exc).__name__}: {exc}"
        )

    if result.returncode == 0:
        return True, ""

    stderr = (result.stderr or "").strip().splitlines()
    tail = stderr[-1] if stderr else f"exit status {result.returncode}"
    return False, (
        f"the agent's critical dependency '{CRITICAL_IMPORT}' is not importable with "
        f"{current_txt} in {venv_dir}, but {expected_txt} requires it. "
        f"This is the #8113 signature: dependencies gated on a newer python_version were "
        f"dropped during install. {tail}"
    )


def _version_text(version) -> str:
    return ".".join(str(part) for part in version)


def _parse_version(text: str) -> tuple[int, ...] | None:
    """``3.12`` or ``3.12.13`` -> (3, 12[, 13]); None when unreadable."""
    try:
        parts = text.strip().split(".")
        if len(parts) not in (2, 3):
            return None
        return tuple(int(part) for part in parts)
    except (ValueError, AttributeError):
        return None


def _run_admits(argv: list[str]) -> int:
    """``docker_venv_python.py --admits <pyproject> <venv_dir>``.

    Exit 0 when the venv's interpreter satisfies the staged agent's declared
    range and dependency gate (or there is nothing to enforce), 1 otherwise.
    rebuild_agent_venv uses it on the interpreter uv actually installed.
    """
    if len(argv) != 4:
        print("usage: docker_venv_python.py --admits <pyproject> <venv_dir>", file=sys.stderr)
        return 2
    pyproject_path, venv_dir = Path(argv[2]), Path(argv[3])
    try:
        text = pyproject_path.read_text(encoding="utf-8", errors="replace") if pyproject_path.is_file() else ""
    except OSError:
        text = ""
    constraints = agent_python_constraints(text) if text else None
    if constraints is None:
        return 0
    current = venv_python_version(venv_dir)
    if current is not None and python_admitted(constraints, current):
        return 0
    found = "an unreadable interpreter" if current is None else "Python " + _version_text(current)
    print(f"-- {venv_dir} runs {found}, which the staged agent does not accept", file=sys.stderr)
    return 1


def _run_verify(argv: list[str]) -> int:
    """``docker_venv_python.py --verify <venv_dir> <expected>``.

    The exit status is the contract: 0 when the agent's critical dependency
    imports in that venv, non-zero with the reason on stdout otherwise.
    """
    if len(argv) != 4:
        print("usage: docker_venv_python.py --verify <venv_dir> <major.minor>", file=sys.stderr)
        return 2
    ok, detail = check_critical_imports(Path(argv[2]), _parse_version(argv[3]))
    if ok:
        return 0
    print(detail)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--verify":
        return _run_verify(argv)
    if len(argv) >= 2 and argv[1] == "--admits":
        return _run_admits(argv)

    if len(argv) != 4:
        print("usage: docker_venv_python.py <pyproject> <venv_dir> <major.minor[.micro]>", file=sys.stderr)
        return 2
    pyproject_path, venv_dir, base = Path(argv[1]), Path(argv[2]), argv[3]
    # The planner prefers the base interpreter when the agent admits it, so an
    # unreadable argument falls back to the interpreter running this script —
    # docker_init.bash runs it with the same `python3` it measured.
    base_python = _parse_version(base) or (sys.version_info[0], sys.version_info[1])

    plan = plan_agent_venv(pyproject_path, venv_dir, base_python)

    version = "-" if plan["python"] is None else _version_text(plan["python"])
    if plan["action"] == "rebuild":
        current = venv_python_version(venv_dir)
        current_txt = (
            "an unreadable interpreter" if current is None else "Python " + _version_text(current)
        )
        print(
            "-- existing venv uses {}, which the staged agent does not accept; rebuilding on Python {}".format(
                current_txt, version
            ),
            file=sys.stderr,
        )
    elif plan["constrained"] and plan["required"] is None:
        print(
            "-- no interpreter satisfies both the staged agent's requires-python and its "
            "dependency gates; leaving the venv as found",
            file=sys.stderr,
        )
    elif plan["action"] == "create" and plan["python"] is not None:
        print(
            "-- no usable venv; creating on Python {} (base image interpreter is {})".format(
                version, _version_text(base_python)
            ),
            file=sys.stderr,
        )
    else:
        print("-- no interpreter change needed for the staged agent", file=sys.stderr)

    print(f"{plan['action']} {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))