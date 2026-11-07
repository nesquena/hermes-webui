"""Real-git fixtures for the worktree placement tests.

Everything here builds actual repositories with the real ``git`` binary —
``git init``, ``git worktree add``, real commits, ``--separate-git-dir``,
``git submodule add`` — and a producer that stands in for the installed
Agent's ``_setup_worktree`` so a test can observe *which root* the WebUI hands
the helper. The producer is not a shortcut for the behaviour under test: every
assertion is about the argument the WebUI passes and about real git state
afterwards.

``real_agent_setup()`` returns the **installed Agent's own** ``_setup_worktree``
— run in the Agent's interpreter, because that is the only environment that can
import it — and ``None`` when this machine has no Agent install. Tests that need
the Agent's base/include policy use it, so a green there is evidence about the
real helper rather than about a copy of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

#: The reason the stand-in producer locks its tree with: the installed Agent's
#: own shape (``hermes pid=<pid>``) and its own pid, so a test can check that the
#: relocated tree is still classified as ownable.
LOCK_REASON = f"hermes pid={os.getpid()}"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "worktree-test",
    "GIT_AUTHOR_EMAIL": "worktree@test.invalid",
    "GIT_COMMITTER_NAME": "worktree-test",
    "GIT_COMMITTER_EMAIL": "worktree@test.invalid",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_ATTR_NOSYSTEM": "1",
}


def git(cwd, *args, check=True) -> subprocess.CompletedProcess:
    """Run real git in *cwd* (hermetic env: no user/system config)."""
    result = subprocess.run(
        ["git", *[str(arg) for arg in args]],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
        env=GIT_ENV,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(str(a) for a in args)} failed in {cwd}: "
            f"{result.returncode}\n{result.stderr}"
        )
    return result


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def commit(cwd: Path, message: str = "commit", files: dict[str, str] | None = None) -> str:
    """Commit *files* (default: seed README) and return the new HEAD sha."""
    payload = files if files is not None else {"README.md": f"{message}\n"}
    for name, content in payload.items():
        write(Path(cwd) / name, content)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-qm", message)
    return head(cwd)


def head(cwd: Path) -> str:
    return git(cwd, "rev-parse", "HEAD").stdout.strip()


def gitignore_of(root: Path) -> str:
    return (Path(root) / ".gitignore").read_text(encoding="utf-8")


# ── layouts ──────────────────────────────────────────────────────────────────


def init_repo(path: Path, branch: str = "main") -> Path:
    """A plain repository with one commit, .worktrees/ already ignored."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    write(path / ".gitignore", ".worktrees/\n")
    commit(path, "init")
    return path


def clone_with_origin(root: Path, name: str = "primary") -> tuple[Path, Path]:
    """``(primary, origin)``: a clone with a real bare origin (a resolvable base)."""
    upstream = init_repo(root / "upstream", "main")
    origin = root / "origin.git"
    git(root, "clone", "-q", "--bare", str(upstream), str(origin))
    primary = root / name
    git(root, "clone", "-q", str(origin), str(primary))
    git(primary, "config", "user.name", "worktree-test")
    git(primary, "config", "user.email", "worktree@test.invalid")
    return primary, origin


def linked_worktree(primary: Path, path: Path, branch: str | None = None) -> Path:
    """``git worktree add`` a second checkout — a session's tree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    git(primary, "worktree", "add", "-q", "-b", branch or f"side-{uuid.uuid4().hex[:6]}", str(path))
    return path


def separate_git_dir(root: Path, dot_git: bool) -> tuple[Path, Path]:
    """``--separate-git-dir`` layout: ``(checkout, metadata_dir)``.

    ``dot_git=True`` uses ``<storage>/.git`` (the layout git cannot tell from a
    plain repository when queried from a linked worktree); ``False`` uses a
    metadata directory of another name.
    """
    checkout = root / "checkout"
    checkout.mkdir(parents=True)
    storage = root / "storage"
    storage.mkdir(parents=True)
    metadata = storage / ".git" if dot_git else storage / "gitdir"
    git(checkout, "init", "-q", "-b", "main", "--separate-git-dir", str(metadata))
    write(checkout / ".gitignore", ".worktrees/\n")
    commit(checkout, "init")
    return checkout, metadata


def submodule_repo(root: Path) -> tuple[Path, Path]:
    """``(superproject, submodule_checkout)`` with a committed submodule."""
    child_src = init_repo(root / "child-source", "main")
    superproject = init_repo(root / "super", "main")
    git(
        superproject,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child_src),
        "child",
    )
    git(superproject, "commit", "-qm", "add submodule")
    return superproject, superproject / "child"


def bare_with_worktree(root: Path) -> tuple[Path, Path]:
    """``(bare_repo, worktree_of_bare)``."""
    seed = init_repo(root / "seed", "main")
    bare = root / "bare.git"
    git(root, "clone", "-q", "--bare", str(seed), str(bare))
    tree = root / "from-bare"
    git(bare, "worktree", "add", "-q", "-b", "work", str(tree))
    return bare, tree


def registry(primary: Path) -> str:
    return git(primary, "worktree", "list", "--porcelain").stdout


def git_argv_log(tmp_path: Path, monkeypatch, fail_on: tuple[str, ...] = ()) -> Path:
    """Put a logging ``git`` first on ``PATH``; return the argv log path.

    Used to prove a *negative*: that the creation path never spawns an index or
    content query. ``fail_on`` makes the shim refuse those subcommands, so a
    test can also show that their failure changes nothing.
    """
    real = shutil.which("git")
    assert real, "git must be on PATH for these tests"
    bindir = Path(tmp_path) / "shim-bin"
    bindir.mkdir(exist_ok=True)
    log = Path(tmp_path) / "git-argv.log"
    refusals = "|".join(fail_on)
    script = bindir / "git"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        + (
            'for arg in "$@"; do\n'
            f'  case "$arg" in {refusals}) printf "shim: refused\\n" >&2; exit 1;; esac\n'
            "done\n"
            if refusals
            else ""
        )
        + f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def git_argv_count(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def git_argv_since(log: Path, start: int) -> list[str]:
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()[start:]


def snapshot(root: Path) -> dict[str, str]:
    """``{relative path: sha256}`` for every file, skipping ``.git``."""
    hashes: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return hashes


# ── producers ────────────────────────────────────────────────────────────────


def agent_producer(name: str | None = None, *, include: bool = True):
    """A stand-in for the Agent's ``_setup_worktree`` that records its argument.

    It does what the installed helper does to the root it is given: create
    ``<root>/.worktrees/<name>`` on a fresh ``hermes/<name>`` branch from that
    root's ``HEAD``, append ``.worktrees/`` to that root's ``.gitignore``, copy
    ``.worktreeinclude`` entries from that root, and lock the new tree. Tests
    assert on ``calls`` — the root the WebUI chose — not on this code.
    """
    calls: list[str] = []

    def setup(root: str) -> dict:
        calls.append(str(root))
        root_path = Path(root)
        worktrees_dir = root_path / ".worktrees"
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        wt_name = name or f"hermes-{uuid.uuid4().hex[:8]}"
        branch = f"hermes/{wt_name}"
        target = worktrees_dir / wt_name
        git(root_path, "worktree", "add", "-q", "-b", branch, str(target), "HEAD")

        ignore = root_path / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if ".worktrees/" not in existing.splitlines():
            with open(ignore, "a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(".worktrees/\n")

        if include:
            manifest = root_path / ".worktreeinclude"
            if manifest.exists():
                for line in manifest.read_text(encoding="utf-8").splitlines():
                    entry = line.strip()
                    if not entry or entry.startswith("#"):
                        continue
                    source = root_path / entry
                    destination = target / entry
                    if source.is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)

        git(root_path, "worktree", "lock", "--reason", LOCK_REASON, str(target))
        return {
            "path": str(target),
            "branch": branch,
            "repo_root": str(root_path),
            "base": "HEAD",
        }

    setup.calls = calls  # type: ignore[attr-defined]
    return setup


_AGENT_RUNNER = """
import contextlib, io, json, sys
sys.path.insert(0, {agent!r})
import cli
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
    info = cli._setup_worktree(sys.argv[1])
if not info:
    sys.stderr.write(buffer.getvalue())
    raise SystemExit("the Agent's _setup_worktree returned no metadata")
print(json.dumps(info))
"""


def _agent_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    try:  # the same discovery the WebUI uses
        from api.config import _AGENT_DIR  # noqa: PLC0415

        if _AGENT_DIR:
            candidates.append(Path(_AGENT_DIR))
    except Exception:  # noqa: BLE001 - discovery is optional here
        pass
    candidates.extend(
        [Path("/opt/hermes-agent"), Path.home() / "Projects" / "hermes-agent"]
    )
    return candidates


def _agent_interpreter(agent_dir: Path) -> str | None:
    """An interpreter that can ``import cli`` from *agent_dir* (probed, not assumed)."""
    probe = "import sys; sys.path.insert(0, {!r}); import cli; print('ok')".format(
        str(agent_dir)
    )
    for candidate in (
        agent_dir / "venv" / "bin" / "python",
        agent_dir / ".venv" / "bin" / "python",
        Path(sys.executable),
    ):
        if not Path(candidate).exists():
            continue
        result = subprocess.run(
            [str(candidate), "-c", probe],
            cwd=str(agent_dir),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and "ok" in result.stdout:
            return str(candidate)
    return None


def real_agent_setup():
    """The installed Agent's own ``_setup_worktree``, or ``None``.

    Runs in the Agent's interpreter (its ``venv``), because ``cli.py`` imports
    the Agent's dependencies — the WebUI's test venv does not have them. The
    helper is *the real one*, so a test that passes here is evidence about the
    Agent's actual base/include policy, not about a copy of it.
    ``real_agent_setup.last_error`` says why it was unavailable.
    """
    errors: list[str] = []
    real_agent_setup.last_error = errors
    for agent_dir in _agent_candidates():
        if not (agent_dir / "cli.py").is_file():
            continue
        interpreter = _agent_interpreter(agent_dir)
        if interpreter is None:
            errors.append(f"{agent_dir}: no interpreter could import cli")
            continue
        code = _AGENT_RUNNER.format(agent=str(agent_dir))

        def setup(root: str, _interpreter=interpreter, _code=code) -> dict:
            result = subprocess.run(
                [_interpreter, "-c", _code, str(root)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
                env=GIT_ENV,
            )
            if result.returncode != 0:
                raise AssertionError(
                    "the installed Agent's _setup_worktree failed: "
                    f"{result.returncode}\n{result.stderr[-2000:]}"
                )
            return json.loads(result.stdout.strip().splitlines()[-1])

        setup.interpreter = interpreter  # type: ignore[attr-defined]
        setup.agent_dir = str(agent_dir)  # type: ignore[attr-defined]
        return setup
    return None
