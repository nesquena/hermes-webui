"""Hermes Web UI -- startup helpers."""
from __future__ import annotations
import os, stat, subprocess, sys
from pathlib import Path

# Credential files that should never be world-readable
_SENSITIVE_FILES = (
    '.env',
    'google_token.json',
    'google_client_secret.json',
    '.signing_key',
    'auth.json',
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
