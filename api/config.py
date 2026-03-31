"""
Hermes Co-Work Web UI -- Shared configuration, constants, and global state.
Imported by all other api/* modules and by server.py.

Discovery order for all paths:
  1. Explicit environment variable
  2. Filesystem heuristics (sibling checkout, parent dir, common install locations)
  3. Hardened defaults relative to $HOME
  4. Fail loudly with a human-readable fix-it message if required modules are missing
"""
import collections
import json
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Basic layout ──────────────────────────────────────────────────────────────
HOME    = Path.home()
# REPO_ROOT is the directory that contains this file's parent (api/ -> repo root)
REPO_ROOT = Path(__file__).parent.parent.resolve()

# ── Network config (env-overridable) ─────────────────────────────────────────
HOST = os.getenv('HERMES_WEBUI_HOST', '127.0.0.1')
PORT = int(os.getenv('HERMES_WEBUI_PORT', '8787'))

# ── State directory (env-overridable, never inside repo) ──────────────────────
STATE_DIR = Path(os.getenv(
    'HERMES_WEBUI_STATE_DIR',
    str(HOME / '.hermes' / 'webui-mvp')
)).expanduser().resolve()

SESSION_DIR           = STATE_DIR / 'sessions'
WORKSPACES_FILE       = STATE_DIR / 'workspaces.json'
SESSION_INDEX_FILE    = SESSION_DIR / '_index.json'
LAST_WORKSPACE_FILE   = STATE_DIR / 'last_workspace.txt'

# ── Hermes agent directory discovery ─────────────────────────────────────────
def _discover_agent_dir() -> Path:
    """
    Locate the hermes-agent checkout using a multi-strategy search.

    Priority:
      1. HERMES_WEBUI_AGENT_DIR env var  -- explicit override always wins
      2. HERMES_HOME / hermes-agent      -- e.g. ~/.hermes/hermes-agent
      3. Sibling of this repo            -- ../hermes-agent
      4. Parent of this repo             -- ../../hermes-agent (nested layout)
      5. Common install paths            -- ~/.hermes/hermes-agent (again as fallback)
      6. HOME / hermes-agent             -- ~/hermes-agent (simple flat layout)
    """
    candidates = []

    # 1. Explicit env var
    if os.getenv('HERMES_WEBUI_AGENT_DIR'):
        candidates.append(Path(os.getenv('HERMES_WEBUI_AGENT_DIR')).expanduser().resolve())

    # 2. HERMES_HOME / hermes-agent
    hermes_home = os.getenv('HERMES_HOME', str(HOME / '.hermes'))
    candidates.append(Path(hermes_home).expanduser() / 'hermes-agent')

    # 3. Sibling: <repo-root>/../hermes-agent
    candidates.append(REPO_ROOT.parent / 'hermes-agent')

    # 4. Parent is the agent repo itself (repo cloned inside hermes-agent/)
    if (REPO_ROOT.parent / 'run_agent.py').exists():
        candidates.append(REPO_ROOT.parent)

    # 5. ~/.hermes/hermes-agent (explicit common path)
    candidates.append(HOME / '.hermes' / 'hermes-agent')

    # 6. ~/hermes-agent
    candidates.append(HOME / 'hermes-agent')

    for path in candidates:
        if path.exists() and (path / 'run_agent.py').exists():
            return path.resolve()

    return None


def _discover_python(agent_dir: Path) -> str:
    """
    Locate a Python executable that has the Hermes agent dependencies installed.

    Priority:
      1. HERMES_WEBUI_PYTHON env var
      2. Agent venv at <agent_dir>/venv/bin/python
      3. Local .venv inside this repo
      4. System python3
    """
    if os.getenv('HERMES_WEBUI_PYTHON'):
        return os.getenv('HERMES_WEBUI_PYTHON')

    if agent_dir:
        venv_py = agent_dir / 'venv' / 'bin' / 'python'
        if venv_py.exists():
            return str(venv_py)

        # Windows layout
        venv_py_win = agent_dir / 'venv' / 'Scripts' / 'python.exe'
        if venv_py_win.exists():
            return str(venv_py_win)

    # Local .venv inside this repo
    local_venv = REPO_ROOT / '.venv' / 'bin' / 'python'
    if local_venv.exists():
        return str(local_venv)

    # Fall back to system python3
    import shutil
    for name in ('python3', 'python'):
        found = shutil.which(name)
        if found:
            return found

    return 'python3'


# Run discovery
_AGENT_DIR = _discover_agent_dir()
PYTHON_EXE = _discover_python(_AGENT_DIR)

# ── Inject agent dir into sys.path so Hermes modules are importable ───────────
if _AGENT_DIR is not None:
    if str(_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_DIR))
    _HERMES_FOUND = True
else:
    _HERMES_FOUND = False

# ── Config file (optional YAML) ──────────────────────────────────────────────
CONFIG_PATH = Path(os.getenv(
    'HERMES_CONFIG_PATH',
    str(HOME / '.hermes' / 'config.yaml')
)).expanduser()

try:
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
except Exception:
    cfg = {}

# ── Default workspace discovery ───────────────────────────────────────────────
def _discover_default_workspace() -> Path:
    """
    Resolve the default workspace in order:
      1. HERMES_WEBUI_DEFAULT_WORKSPACE env var
      2. ~/workspace (common Hermes convention)
      3. STATE_DIR / workspace (isolated fallback)
    """
    if os.getenv('HERMES_WEBUI_DEFAULT_WORKSPACE'):
        return Path(os.getenv('HERMES_WEBUI_DEFAULT_WORKSPACE')).expanduser().resolve()

    common = HOME / 'workspace'
    if common.exists():
        return common.resolve()

    return (STATE_DIR / 'workspace').resolve()

DEFAULT_WORKSPACE = _discover_default_workspace()
DEFAULT_MODEL     = os.getenv('HERMES_WEBUI_DEFAULT_MODEL', 'openai/gpt-5.4-mini')

# ── Startup diagnostics ───────────────────────────────────────────────────────
def print_startup_config():
    """Print detected configuration at startup so the user can verify what was found."""
    ok   = '\033[32m[ok]\033[0m'
    warn = '\033[33m[!!]\033[0m'
    err  = '\033[31m[XX]\033[0m'

    lines = [
        '',
        '  Hermes Web UI -- startup config',
        '  --------------------------------',
        f'  repo root   : {REPO_ROOT}',
        f'  agent dir   : {_AGENT_DIR if _AGENT_DIR else "NOT FOUND"}  {ok if _AGENT_DIR else err}',
        f'  python      : {PYTHON_EXE}',
        f'  state dir   : {STATE_DIR}',
        f'  workspace   : {DEFAULT_WORKSPACE}',
        f'  host:port   : {HOST}:{PORT}',
        f'  config file : {CONFIG_PATH}  {"(found)" if CONFIG_PATH.exists() else "(not found, using defaults)"}',
        '',
    ]
    print('\n'.join(lines), flush=True)

    if not _HERMES_FOUND:
        print(
            f'{err}  Could not find the Hermes agent directory.\n'
            '      The server will start but agent features will not work.\n'
            '\n'
            '      To fix, set one of:\n'
            '        export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent\n'
            '        export HERMES_HOME=/path/to/.hermes\n'
            '\n'
            '      Or clone hermes-agent as a sibling of this repo:\n'
            '        git clone <hermes-agent-repo> ../hermes-agent\n',
            flush=True
        )

def verify_hermes_imports():
    """
    Attempt to import the key Hermes modules.
    Returns (ok: bool, missing: list[str]).
    """
    required = ['run_agent']
    optional = ['tools.approval']
    missing  = []
    for mod in required:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return (len(missing) == 0), missing

# ── Limits ───────────────────────────────────────────────────────────────────
MAX_FILE_BYTES   = 200_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# ── File type maps ───────────────────────────────────────────────────────────
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp'}
MD_EXTS    = {'.md', '.markdown', '.mdown'}
CODE_EXTS  = {'.py', '.js', '.ts', '.jsx', '.tsx', '.css', '.html', '.json',
              '.yaml', '.yml', '.toml', '.sh', '.bash', '.txt', '.log', '.env',
              '.csv', '.xml', '.sql', '.rs', '.go', '.java', '.c', '.cpp', '.h'}
MIME_MAP = {
    '.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg',
    '.gif':'image/gif', '.svg':'image/svg+xml', '.webp':'image/webp',
    '.ico':'image/x-icon', '.bmp':'image/bmp',
    '.pdf':'application/pdf', '.json':'application/json',
}

# ── Toolsets (from config.yaml or hardcoded default) ─────────────────────────
CLI_TOOLSETS = cfg.get('platform_toolsets', {}).get('cli', [
    'browser', 'clarify', 'code_execution', 'cronjob', 'delegation', 'file',
    'image_gen', 'memory', 'session_search', 'skills', 'terminal', 'todo',
    'web', 'webhook',
])

# ── Static file path ─────────────────────────────────────────────────────────
_INDEX_HTML_PATH = REPO_ROOT / 'static' / 'index.html'

# ── Thread synchronisation ───────────────────────────────────────────────────
LOCK              = threading.Lock()
SESSIONS_MAX      = 100
CHAT_LOCK         = threading.Lock()
STREAMS: dict     = {}
STREAMS_LOCK      = threading.Lock()
CANCEL_FLAGS: dict = {}
SERVER_START_TIME = time.time()

# ── Thread-local env context ─────────────────────────────────────────────────
_thread_ctx = threading.local()

def _set_thread_env(**kwargs):
    _thread_ctx.env = kwargs

def _clear_thread_env():
    _thread_ctx.env = {}

# ── Per-session agent locks ───────────────────────────────────────────────────
SESSION_AGENT_LOCKS: dict = {}
SESSION_AGENT_LOCKS_LOCK  = threading.Lock()

def _get_session_agent_lock(session_id: str) -> threading.Lock:
    with SESSION_AGENT_LOCKS_LOCK:
        if session_id not in SESSION_AGENT_LOCKS:
            SESSION_AGENT_LOCKS[session_id] = threading.Lock()
        return SESSION_AGENT_LOCKS[session_id]

# ── SESSIONS in-memory cache (LRU OrderedDict) ───────────────────────────────
SESSIONS: collections.OrderedDict = collections.OrderedDict()
