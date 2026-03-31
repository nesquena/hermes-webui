"""
Hermes Co-Work Web UI -- Shared configuration, constants, and global state.
Imported by all other api/* modules and by server.py.
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

# ── Runtime environment ──────────────────────────────────────────────────────
HOST = os.getenv('HERMES_WEBUI_HOST', '127.0.0.1')
PORT = int(os.getenv('HERMES_WEBUI_PORT', '8787'))
HOME = Path.home()
CONFIG_PATH    = Path(os.getenv('HERMES_CONFIG_PATH', str(HOME / '.hermes' / 'config.yaml')))
STATE_DIR      = Path(os.getenv('HERMES_WEBUI_STATE_DIR', str(HOME / '.hermes' / 'webui-mvp'))).expanduser()
SESSION_DIR    = STATE_DIR / 'sessions'
WORKSPACES_FILE       = STATE_DIR / 'workspaces.json'
SESSION_INDEX_FILE    = SESSION_DIR / '_index.json'
LAST_WORKSPACE_FILE   = STATE_DIR / 'last_workspace.txt'

# ── Hermes agent path setup ──────────────────────────────────────────────────
_AGENT_DIR = Path(__file__).parent.parent.parent / 'hermes-agent'
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# ── Config file (optional) ───────────────────────────────────────────────────
try:
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
except Exception:
    cfg = {}

DEFAULT_WORKSPACE = Path(os.getenv(
    'HERMES_WEBUI_DEFAULT_WORKSPACE',
    str(HOME / '.hermes' / 'webui-mvp' / 'test-workspace')
)).expanduser().resolve()
DEFAULT_MODEL = os.getenv('HERMES_WEBUI_DEFAULT_MODEL', 'openai/gpt-5.4-mini')

# ── Limits ───────────────────────────────────────────────────────────────────
MAX_FILE_BYTES   = 200_000       # max text file size served via /api/file
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB upload limit

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

# ── Toolsets ─────────────────────────────────────────────────────────────────
CLI_TOOLSETS = cfg.get('platform_toolsets', {}).get('cli', [
    'browser', 'clarify', 'code_execution', 'cronjob', 'delegation', 'file',
    'image_gen', 'memory', 'session_search', 'skills', 'terminal', 'todo',
    'web', 'webhook',
])

# ── Static file path ─────────────────────────────────────────────────────────
_INDEX_HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"

# ── Thread synchronisation ───────────────────────────────────────────────────
LOCK              = threading.Lock()           # protects SESSIONS dict
SESSIONS_MAX      = 100                        # LRU cap
CHAT_LOCK         = threading.Lock()           # serialises non-streaming chat (fallback)
STREAMS: dict     = {}                         # stream_id -> queue.Queue
STREAMS_LOCK      = threading.Lock()
CANCEL_FLAGS: dict = {}                        # stream_id -> threading.Event (Sprint 10)
SERVER_START_TIME = time.time()

# ── Thread-local env context (TD1) ───────────────────────────────────────────
_thread_ctx = threading.local()

def _set_thread_env(**kwargs):
    _thread_ctx.env = kwargs

def _clear_thread_env():
    _thread_ctx.env = {}

# ── Per-session agent locks (Phase B) ────────────────────────────────────────
SESSION_AGENT_LOCKS: dict = {}
SESSION_AGENT_LOCKS_LOCK  = threading.Lock()

def _get_session_agent_lock(session_id: str) -> threading.Lock:
    with SESSION_AGENT_LOCKS_LOCK:
        if session_id not in SESSION_AGENT_LOCKS:
            SESSION_AGENT_LOCKS[session_id] = threading.Lock()
        return SESSION_AGENT_LOCKS[session_id]

# ── SESSIONS in-memory cache (LRU OrderedDict) ───────────────────────────────
SESSIONS: collections.OrderedDict = collections.OrderedDict()
