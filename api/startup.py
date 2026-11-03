"""Hermes Web UI -- startup helpers."""
from __future__ import annotations
import os, re, shutil, stat, subprocess, sys
import hashlib
from dataclasses import dataclass
from pathlib import Path

from api.paths import _platform_default_hermes_home

# Credential files that should never be world-readable
_SENSITIVE_FILES = (
    '.env',
    'google_token.json',
    'google_client_secret.json',
    '.signing_key',
    'auth.json',
)


def _walk_up_for_run_agent(start: Path) -> Path | None:
    """Return the first parent containing the legacy Agent entry point."""
    for parent in start.parents:
        if (parent / "run_agent.py").exists():
            return parent.resolve()
    return None


def _agent_dir_from_hermes_cli() -> Path | None:
    """Resolve an Agent root from a console-script shebang or bash wrapper."""
    hermes_path = shutil.which("hermes")
    if not hermes_path:
        return None
    try:
        with open(hermes_path, "r", encoding="utf-8", errors="replace") as launcher:
            lines = [launcher.readline() for _ in range(20)]
    except OSError:
        return None
    if not lines or not lines[0].startswith("#!"):
        return None

    candidates: list[Path] = []
    shebang = lines[0][2:].strip().split(None, 1)
    if shebang:
        interpreter = Path(shebang[0])
        if interpreter.is_absolute() and interpreter.name != "env":
            candidates.append(interpreter)
    for line in lines[1:]:
        for match in re.findall(r"""['"]([^'"]+)['"]""", line):
            candidate = Path(match)
            if candidate.is_absolute():
                candidates.append(candidate)
    for candidate in candidates:
        found = _walk_up_for_run_agent(candidate)
        if found:
            return found
    return None


def _agent_dir_from_python(python_exe: str) -> Path | None:
    """Resolve an Agent root from a selected interpreter without importing it."""
    script = (
        "import importlib.util\n"
        "import os, sys\n"
        "cwd = os.path.normcase(os.path.realpath(os.getcwd()))\n"
        "sys.path[:] = [\n"
        "    entry for entry in sys.path\n"
        "    if os.path.normcase(os.path.realpath(entry or cwd)) != cwd\n"
        "]\n"
        'spec = importlib.util.find_spec("run_agent")\n'
        'print(spec.origin if spec else "")\n'
    )
    try:
        check = subprocess.run(
            [python_exe, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if check.returncode != 0:
        return None
    lines = check.stdout.splitlines()
    if not lines:
        return None
    origin = Path(lines[0].strip())
    if not origin.is_absolute() or origin.name != "run_agent.py" or not origin.is_file():
        return None
    return origin.parent.resolve()


def _looks_like_pip_style_agent_source_root(path: Path) -> bool:
    if not (path / "cron" / "jobs.py").exists():
        return False
    if (path / "hermes").exists():
        return True
    hermes_cli = path / "hermes_cli"
    return (hermes_cli / "__init__.py").exists() or (hermes_cli / "main.py").exists()


def _looks_like_agent_source_root(path: Path, *, allow_marker_only: bool = True) -> bool:
    return (path / "run_agent.py").exists() or (
        allow_marker_only and _looks_like_pip_style_agent_source_root(path)
    )


def _first_valid_agent_candidate(candidates: list[Path | None], *, marker_only=False) -> Path | None:
    for candidate in candidates:
        if candidate is None:
            continue
        valid = (
            _looks_like_pip_style_agent_source_root(candidate)
            if marker_only else (candidate / "run_agent.py").exists()
        )
        if valid:
            return candidate.resolve()
    return None


@dataclass(frozen=True)
class _AgentDiscovery:
    agent_dir: Path | None
    python_exe: str | None = None


def _discover_agent_identity(
    *,
    repo_root: Path | None = None,
    hermes_home: Path | None = None,
    default_hermes_home: Path | None = None,
    user_home: Path | None = None,
    python_exe: str | None = None,
    python_fallbacks: tuple[str, ...] = (),
    allow_marker_only: bool = True,
    launcher_finder=_agent_dir_from_hermes_cli,
    python_finder=_agent_dir_from_python,
) -> _AgentDiscovery:
    """Observe an Agent root and retain executable provenance when it is proved."""
    repo_root = (repo_root or Path.cwd()).expanduser()
    user_home = (user_home or Path.home()).expanduser()
    hermes_home = Path(
        hermes_home or os.getenv("HERMES_HOME", str(user_home / ".hermes"))
    ).expanduser()
    if default_hermes_home is None:
        default_hermes_home = _platform_default_hermes_home()
    default_hermes_home = Path(default_hermes_home).expanduser()
    explicit = os.getenv("HERMES_WEBUI_AGENT_DIR", "").strip()
    explicit_candidate = Path(explicit).expanduser() if explicit else None
    if explicit_candidate is not None and _looks_like_agent_source_root(
        explicit_candidate, allow_marker_only=allow_marker_only,
    ):
        return _AgentDiscovery(explicit_candidate.resolve())

    authoritative_candidates = [
        hermes_home / "hermes-agent",
        repo_root.parent / "hermes-agent",
        repo_root.parent if _looks_like_agent_source_root(
            repo_root.parent, allow_marker_only=allow_marker_only,
        ) else None,
        default_hermes_home / "hermes-agent",
        user_home / "hermes-agent",
        Path("/usr/local/lib/hermes-agent"),
    ]
    found = _first_valid_agent_candidate(authoritative_candidates)
    if found:
        return _AgentDiscovery(found)

    found = launcher_finder()
    if found:
        return _AgentDiscovery(found)
    configured_python = os.getenv("HERMES_WEBUI_PYTHON")
    selected_python = python_exe or configured_python or sys.executable
    candidates = [selected_python]
    if not configured_python:
        candidates.extend(python_fallbacks)
    for candidate in dict.fromkeys(candidates):
        found = python_finder(candidate)
        if found:
            return _AgentDiscovery(found, candidate)

    fallback_candidates = [
        user_home / ".hermes" / "hermes-agent",
        Path(os.getenv("XDG_DATA_HOME", str(user_home / ".local" / "share"))).expanduser()
        / "hermes-agent",
        Path("/opt/hermes-agent"),
        Path("/usr/local/hermes-agent"),
        Path("/usr/local/share/hermes-agent"),
    ]
    found = _first_valid_agent_candidate(fallback_candidates)
    if found:
        return _AgentDiscovery(found)
    if allow_marker_only:
        return _AgentDiscovery(_first_valid_agent_candidate(
            authoritative_candidates + fallback_candidates, marker_only=True,
        ))
    return _AgentDiscovery(None)


def discover_agent_dir(
    *,
    repo_root: Path | None = None,
    hermes_home: Path | None = None,
    default_hermes_home: Path | None = None,
    user_home: Path | None = None,
    python_exe: str | None = None,
    allow_marker_only: bool = True,
    launcher_finder=_agent_dir_from_hermes_cli,
    python_finder=_agent_dir_from_python,
) -> Path | None:
    """Observe the first valid Agent identity without authorizing mutation."""
    return _discover_agent_identity(
        repo_root=repo_root,
        hermes_home=hermes_home,
        default_hermes_home=default_hermes_home,
        user_home=user_home,
        python_exe=python_exe,
        allow_marker_only=allow_marker_only,
        launcher_finder=launcher_finder,
        python_finder=python_finder,
    ).agent_dir


def discover_runtime_agent_identity(
    *,
    repo_root: Path,
    default_hermes_home: Path | None = None,
    user_home: Path | None = None,
) -> _AgentDiscovery:
    """The Agent this WebUI process runs against, from one argument set.

    ``api.config`` (the runtime) and ``managed_agent_startup`` (which activates
    the Agent's dependency layer before ``api.config`` may be imported) must
    resolve the same root: activating one Agent's dependencies and then
    importing another Agent's code breaks chat (#8355 review, #8529). Both call
    this helper so the HERMES_HOME / platform-default home / interpreter
    arguments cannot drift apart again.

    Stdlib-only (``api.paths`` included), so it is safe to call before the
    Agent's dependencies are on ``sys.path``.
    """
    from api import paths as _paths

    if default_hermes_home is None:
        default_hermes_home = _paths._platform_default_hermes_home()
    if user_home is None:
        user_home = _paths.HOME
    return _discover_agent_identity(
        repo_root=repo_root,
        hermes_home=Path(os.getenv("HERMES_HOME", str(default_hermes_home))),
        default_hermes_home=default_hermes_home,
        user_home=user_home,
        python_exe=os.getenv("HERMES_WEBUI_PYTHON") or sys.executable,
    )


def fix_credential_permissions() -> None:
    """Ensure sensitive files in HERMES_HOME have safe permissions.

    Respects:
      - HERMES_SKIP_CHMOD=1  → bypass entirely
      - HERMES_HOME_MODE     → group bits are allowed if set by the operator,
                               only world-readable/world-writable files are fixed
    """
    if os.environ.get('HERMES_SKIP_CHMOD', '').strip() in ('1', 'true'):
        return

    # Parse operator-declared mode to know if group bits are intentional
    declared_mode = None
    raw_mode = os.environ.get('HERMES_HOME_MODE', '').strip()
    if raw_mode:
        try:
            declared_mode = int(raw_mode, 8)
        except ValueError:
            pass

    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    if not hermes_home.is_dir():
        return
    for name in _SENSITIVE_FILES:
        fpath = hermes_home / name
        if not fpath.exists():
            continue
        try:
            current = stat.S_IMODE(fpath.stat().st_mode)
            # If operator declared a mode, allow group bits but still fix world bits
            if declared_mode is not None:
                if current & 0o007:  # other bits set (world-readable/writable)
                    fpath.chmod(current & ~0o007)
                    print(f'  [security] removed world bits on {fpath.name} ({oct(current)} -> {oct(current & ~0o007)})', flush=True)
            else:
                if current & 0o077:  # group or other bits set
                    fpath.chmod(0o600)
                    print(f'  [security] fixed permissions on {fpath.name} ({oct(current)} -> 0600)', flush=True)
        except OSError:
            pass  # best-effort; don't abort startup


def _agent_dir() -> Path | None:
    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    for raw in [os.environ.get('HERMES_WEBUI_AGENT_DIR', '').strip(), str(hermes_home / 'hermes-agent')]:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    return None

def _trusted_agent_dir(agent_dir: Path) -> bool:
    """Return True if agent_dir passes ownership and permission checks.

    Validates that the directory is not world- or group-writable and,
    on POSIX systems, is owned by the current process user.

    Intentionally does NOT enforce a canonical path (i.e. does not require
    the dir to be ~/.hermes/hermes-agent), so custom HERMES_WEBUI_AGENT_DIR
    paths work correctly when HERMES_WEBUI_AUTO_INSTALL=1 is set.
    """
    try:
        st = agent_dir.stat()
        if stat.S_IMODE(st.st_mode) & 0o022:
            # World- or group-writable — untrusted
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            # Not owned by current user (POSIX only; Windows fallback skips)
            return False
        return True
    except OSError:
        return False


# docker_init.bash (`write_agent_stage_manifest`) publishes this file inside the
# staged copy. It names the Agent the stage was copied from (resolved path) and
# the sha256 of that Agent's install inputs at copy time ('-' when absent).
_STAGE_MANIFEST = '.hermes-webui-stage-source'
_STAGE_INPUTS = ('pyproject.toml', 'requirements.txt')


def _install_inputs(directory: Path) -> dict[str, str]:
    """sha256 of each file auto-install reads from ``directory``, '-' when absent."""
    digests = {}
    for name in _STAGE_INPUTS:
        path = directory / name
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else '-'
    return digests


def _stage_matches_agent(staged: Path, agent_dir: Path) -> bool:
    """True only when ``staged`` is a current copy of the authorized ``agent_dir``.

    Ownership and mode prove the stage is ours, not that it was copied from this
    Agent: the manifest must name ``agent_dir``, and the install inputs of both
    the Agent and the stage must still hash to what was recorded at copy time.
    That rejects an unrelated directory, a stage published for another Agent,
    a stage left behind after the Agent was upgraded, and a stage edited since.
    """
    manifest = staged / _STAGE_MANIFEST
    # Same ownership/mode bar as the directories it vouches for.
    if not manifest.is_file() or not _trusted_agent_dir(manifest):
        return False
    try:
        recorded = dict(
            line.split('=', 1)
            for line in manifest.read_text(encoding='utf-8').splitlines()
            if '=' in line
        )
        if recorded.get('source') != str(agent_dir.resolve()):
            return False
        published = {name: recorded.get(name) for name in _STAGE_INPUTS}
        return _install_inputs(agent_dir) == published == _install_inputs(staged)
    except (OSError, ValueError):
        return False


def _agent_install_dir(agent_dir: Path) -> Path:
    """Resolve the directory auto-install should actually install *from*.

    In the documented multi-container compose stacks the agent source is a
    read-only mount (:ro). setuptools' ``egg_info`` step writes into the source
    tree, so installing from that mount fails immediately with
    ``could not create 'hermes_agent.egg_info': Read-only file system`` (#8113).
    ``docker_init.bash`` stages a writable copy and exports it here, so the
    recovery path installs from the staged tree instead of the read-only mount.

    Falls back to ``agent_dir`` whenever the staged path is unset, missing,
    fails the same ownership/permission trust check that guards the mount, or
    is not a current copy of ``agent_dir`` (see ``_stage_matches_agent``) —
    an attacker-controlled env var must never redirect the install, and a
    trusted-but-unrelated or stale directory must not either.
    """
    raw = os.environ.get('HERMES_WEBUI_AGENT_STAGE_DIR', '').strip()
    if raw:
        # Resolve once, before any check, and check and return that same
        # canonical path. Resolving after the checks let a symlink swapped in
        # between redirect the install to a directory nothing had validated.
        try:
            staged = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            # Missing, unresolvable or `~unknown-user`: never fatal at startup.
            return agent_dir
        if staged.is_dir() and _trusted_agent_dir(staged) and _trusted_ancestry(staged):
            if _stage_matches_agent(staged, agent_dir):
                return staged
            print(f'[!!] Ignoring staged agent copy {staged}: it is not a current copy of {agent_dir}.', flush=True)
    return agent_dir


def _trusted_ancestry(path: Path) -> bool:
    """True when no other user can replace ``path`` through one of its parents.

    Each parent must be owned by root or by the current user, and must not be
    group- or world-writable unless it is sticky (like ``/tmp`` or the image's
    ``/app``), where only an entry's owner may rename or remove it. POSIX only:
    elsewhere ``_trusted_agent_dir`` skips the owner check too.
    """
    if not hasattr(os, 'getuid'):
        return True
    uid = os.getuid()
    try:
        for parent in path.parents:
            st = parent.stat()
            if st.st_uid not in (0, uid):
                return False
            if stat.S_IMODE(st.st_mode) & 0o022 and not st.st_mode & stat.S_ISVTX:
                return False
    except OSError:
        return False
    return True


def auto_install_agent_deps(expected_agent_dir: Path | None = None) -> bool:
    enabled = os.environ.get('HERMES_WEBUI_AUTO_INSTALL', '').strip().lower() in ('1', 'true', 'yes')
    if not enabled:
        print('[!!] Auto-install disabled. Set HERMES_WEBUI_AUTO_INSTALL=1 to enable.', flush=True)
        return False
    agent_dir = _agent_dir()
    if agent_dir is None:
        print('[!!] Auto-install skipped: agent directory not found.', flush=True)
        return False
    if expected_agent_dir is None or agent_dir != Path(expected_agent_dir).resolve():
        print('[!!] Auto-install skipped: authorized directory does not match the selected Agent.', flush=True)
        return False
    if not _trusted_agent_dir(agent_dir):
        print('[!!] Auto-install skipped: agent directory failed trust check (check ownership/permissions).', flush=True)
        return False
    # Identity is still validated against the *authorized* agent above; only the
    # install target moves to the writable staged copy (#8113).
    install_dir = _agent_install_dir(agent_dir)
    if install_dir != agent_dir:
        print(f'[ok] Agent source is mounted read-only — installing from the staged copy {install_dir}.', flush=True)
    req_file = install_dir / 'requirements.txt'
    pyproject = install_dir / 'pyproject.toml'
    if req_file.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', '-r', str(req_file)]
        print(f'     Installing from {req_file} ...', flush=True)
    elif pyproject.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', str(install_dir)]
        print(f'     Installing from {install_dir} (pyproject.toml) ...', flush=True)
    else:
        print('[!!] Auto-install skipped: no requirements.txt or pyproject.toml in agent dir.', flush=True)
        return False
    try:
        result = subprocess.run(install_args, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f'[!!] pip install failed (exit {result.returncode}):', flush=True)
            for line in (result.stderr or '').splitlines()[-10:]:
                print(f'     {line}', flush=True)
            return False
        print('[ok] pip install completed.', flush=True)
        return True
    except subprocess.TimeoutExpired:
        print('[!!] Auto-install timed out after 120s.', flush=True)
        return False
    except Exception as e:
        print(f'[!!] Auto-install error: {e}', flush=True)
        return False
