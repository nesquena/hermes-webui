"""Hermes Web UI -- startup helpers."""
from __future__ import annotations
import os, re, shutil, stat, subprocess, sys
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


def _looks_like_agent_source_root(path: Path) -> bool:
    return (path / "run_agent.py").exists() or _looks_like_pip_style_agent_source_root(path)


def _first_valid_agent_candidate(candidates: list[Path | None]) -> Path | None:
    for marker in ("run_agent.py", "pip"):
        for candidate in candidates:
            if candidate is None:
                continue
            if marker == "run_agent.py" and (candidate / marker).exists():
                return candidate.resolve()
            if marker == "pip" and _looks_like_pip_style_agent_source_root(candidate):
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
    if explicit_candidate is not None and _looks_like_agent_source_root(explicit_candidate):
        return _AgentDiscovery(explicit_candidate.resolve())

    authoritative_candidates = [
        hermes_home / "hermes-agent",
        repo_root.parent / "hermes-agent",
        repo_root.parent if _looks_like_agent_source_root(repo_root.parent) else None,
        default_hermes_home / "hermes-agent",
        user_home / ".hermes" / "hermes-agent",
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
        Path(os.getenv("XDG_DATA_HOME", str(user_home / ".local" / "share"))).expanduser()
        / "hermes-agent",
        Path("/opt/hermes-agent"),
        Path("/usr/local/hermes-agent"),
        Path("/usr/local/share/hermes-agent"),
    ]
    return _AgentDiscovery(_first_valid_agent_candidate(fallback_candidates))


def discover_agent_dir(
    *,
    repo_root: Path | None = None,
    hermes_home: Path | None = None,
    default_hermes_home: Path | None = None,
    user_home: Path | None = None,
    python_exe: str | None = None,
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
        launcher_finder=launcher_finder,
        python_finder=python_finder,
    ).agent_dir


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


def auto_install_agent_deps() -> bool:
    enabled = os.environ.get('HERMES_WEBUI_AUTO_INSTALL', '').strip().lower() in ('1', 'true', 'yes')
    if not enabled:
        print('[!!] Auto-install disabled. Set HERMES_WEBUI_AUTO_INSTALL=1 to enable.', flush=True)
        return False
    agent_dir = _agent_dir()
    if agent_dir is None:
        print('[!!] Auto-install skipped: agent directory not found.', flush=True)
        return False
    if not _trusted_agent_dir(agent_dir):
        print('[!!] Auto-install skipped: agent directory failed trust check (check ownership/permissions).', flush=True)
        return False
    req_file = agent_dir / 'requirements.txt'
    pyproject = agent_dir / 'pyproject.toml'
    if req_file.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', '-r', str(req_file)]
        print(f'     Installing from {req_file} ...', flush=True)
    elif pyproject.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', str(agent_dir)]
        print(f'     Installing from {agent_dir} (pyproject.toml) ...', flush=True)
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
