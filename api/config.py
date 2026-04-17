"""
Hermes Web UI -- Shared configuration, constants, and global state.
Imported by all other api/* modules and by server.py.

Discovery order for all paths:
  1. Explicit environment variable
  2. Filesystem heuristics (sibling checkout, parent dir, common install locations)
  3. Hardened defaults relative to $HOME
  4. Fail loudly with a human-readable fix-it message if required modules are missing
"""

import collections
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Basic layout ──────────────────────────────────────────────────────────────
HOME = Path.home()
# REPO_ROOT is the directory that contains this file's parent (api/ -> repo root)
REPO_ROOT = Path(__file__).parent.parent.resolve()

# ── Network config (env-overridable) ─────────────────────────────────────────
HOST = os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
PORT = int(os.getenv("HERMES_WEBUI_PORT", "8787"))

# ── TLS/HTTPS config (optional, env-overridable) ────────────────────────────
TLS_CERT = os.getenv("HERMES_WEBUI_TLS_CERT", "").strip() or None
TLS_KEY = os.getenv("HERMES_WEBUI_TLS_KEY", "").strip() or None
TLS_ENABLED = TLS_CERT is not None and TLS_KEY is not None

# ── State directory (env-overridable, never inside repo) ──────────────────────
STATE_DIR = (
    Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(HOME / ".hermes" / "webui")))
    .expanduser()
    .resolve()
)

SESSION_DIR = STATE_DIR / "sessions"
WORKSPACES_FILE = STATE_DIR / "workspaces.json"
SESSION_INDEX_FILE = SESSION_DIR / "_index.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
LAST_WORKSPACE_FILE = STATE_DIR / "last_workspace.txt"
PROJECTS_FILE = STATE_DIR / "projects.json"

logger = logging.getLogger(__name__)


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
    if os.getenv("HERMES_WEBUI_AGENT_DIR"):
        candidates.append(
            Path(os.getenv("HERMES_WEBUI_AGENT_DIR")).expanduser().resolve()
        )

    # 2. HERMES_HOME / hermes-agent
    hermes_home = os.getenv("HERMES_HOME", str(HOME / ".hermes"))
    candidates.append(Path(hermes_home).expanduser() / "hermes-agent")

    # 3. Sibling: <repo-root>/../hermes-agent
    candidates.append(REPO_ROOT.parent / "hermes-agent")

    # 4. Parent is the agent repo itself (repo cloned inside hermes-agent/)
    if (REPO_ROOT.parent / "run_agent.py").exists():
        candidates.append(REPO_ROOT.parent)

    # 5. ~/.hermes/hermes-agent (explicit common path)
    candidates.append(HOME / ".hermes" / "hermes-agent")

    # 6. ~/hermes-agent
    candidates.append(HOME / "hermes-agent")

    for path in candidates:
        if path.exists() and (path / "run_agent.py").exists():
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
    if os.getenv("HERMES_WEBUI_PYTHON"):
        return os.getenv("HERMES_WEBUI_PYTHON")

    if agent_dir:
        venv_py = agent_dir / "venv" / "bin" / "python"
        if venv_py.exists():
            return str(venv_py)

        # Windows layout
        venv_py_win = agent_dir / "venv" / "Scripts" / "python.exe"
        if venv_py_win.exists():
            return str(venv_py_win)

    # Local .venv inside this repo
    local_venv = REPO_ROOT / ".venv" / "bin" / "python"
    if local_venv.exists():
        return str(local_venv)

    # Fall back to system python3
    import shutil

    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found

    return "python3"


# Run discovery
_AGENT_DIR = _discover_agent_dir()
PYTHON_EXE = _discover_python(_AGENT_DIR)

# ── Inject agent dir into sys.path so Hermes modules are importable ──────────

# When users (or CI builds) run `pip install --target .` or
# `pip install -t .` inside the hermes-agent checkout, third-party
# package directories (openai/, pydantic/, requests/, etc.) end up
# alongside real Hermes source files.  Putting _AGENT_DIR at the
# FRONT of sys.path means Python resolves `import pydantic` from that
# local directory — which breaks whenever the host platform differs
# from the container (e.g. macOS .so files inside a Linux image).
#
# Fix: insert _AGENT_DIR at the END of sys.path.  Python searches
# entries in order, so site-packages resolves pip packages correctly,
# and Hermes-specific modules (run_agent, hermes/, etc.) still
# resolve because they do not exist in site-packages.

if _AGENT_DIR is not None:
    if str(_AGENT_DIR) not in sys.path:
        sys.path.append(str(_AGENT_DIR))
    _HERMES_FOUND = True
else:
    _HERMES_FOUND = False

# ── Config file (reloadable -- supports profile switching) ──────────────────
_cfg_cache = {}
_cfg_lock = threading.Lock()
_cfg_mtime: float = 0.0  # last known mtime of config.yaml; 0 = never loaded


def _get_config_path() -> Path:
    """Return config.yaml path for the active profile."""
    env_override = os.getenv("HERMES_CONFIG_PATH")
    if env_override:
        return Path(env_override).expanduser()
    try:
        from api.profiles import get_active_hermes_home

        return get_active_hermes_home() / "config.yaml"
    except ImportError:
        return HOME / ".hermes" / "config.yaml"


def get_config() -> dict:
    """Return the cached config dict, loading from disk if needed."""
    if not _cfg_cache:
        reload_config()
    return _cfg_cache


def reload_config() -> None:
    """Reload config.yaml from the active profile's directory."""
    global _cfg_mtime
    with _cfg_lock:
        _cfg_cache.clear()
        config_path = _get_config_path()
        try:
            import yaml as _yaml

            if config_path.exists():
                loaded = _yaml.safe_load(config_path.read_text())
                if isinstance(loaded, dict):
                    _cfg_cache.update(loaded)
                    try:
                        _cfg_mtime = Path(config_path).stat().st_mtime
                    except OSError:
                        _cfg_mtime = 0.0
        except Exception:
            logger.debug("Failed to load yaml config from %s", config_path)


# Initial load
reload_config()
cfg = _cfg_cache  # alias for backward compat with existing references


# ── Default workspace discovery ───────────────────────────────────────────────
def _workspace_candidates(raw: str | Path | None = None) -> list[Path]:
    """Return ordered candidate workspace paths, de-duplicated."""
    candidates: list[Path] = []

    def add(candidate: str | Path | None) -> None:
        if candidate in (None, ""):
            return
        try:
            path = Path(candidate).expanduser().resolve()
        except Exception:
            return
        if path not in candidates:
            candidates.append(path)

    add(raw)
    if os.getenv("HERMES_WEBUI_DEFAULT_WORKSPACE"):
        add(os.getenv("HERMES_WEBUI_DEFAULT_WORKSPACE"))

    home_workspace = HOME / "workspace"
    home_work = HOME / "work"
    if home_workspace.exists():
        add(home_workspace)
    if home_work.exists():
        add(home_work)

    add(home_workspace)
    add(STATE_DIR / "workspace")
    return candidates



def _ensure_workspace_dir(path: Path) -> bool:
    """Best-effort check that a workspace directory exists and is writable."""
    try:
        path = path.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    except Exception:
        return False



def resolve_default_workspace(raw: str | Path | None = None) -> Path:
    """Return the first usable workspace path, creating it when possible."""
    for candidate in _workspace_candidates(raw):
        if _ensure_workspace_dir(candidate):
            return candidate
    raise RuntimeError(
        "Could not create or access any usable workspace directory. "
        "Set HERMES_WEBUI_DEFAULT_WORKSPACE to a writable path."
    )



def _discover_default_workspace() -> Path:
    """
    Resolve the default workspace in order:
      1. HERMES_WEBUI_DEFAULT_WORKSPACE env var
      2. ~/workspace if it already exists
      3. ~/work if it already exists
      4. ~/workspace (create if needed)
      5. STATE_DIR / workspace
    """
    return resolve_default_workspace()


DEFAULT_WORKSPACE = _discover_default_workspace()
DEFAULT_MODEL = os.getenv("HERMES_WEBUI_DEFAULT_MODEL", "openai/gpt-5.4-mini")


# ── Startup diagnostics ───────────────────────────────────────────────────────
def print_startup_config() -> None:
    """Print detected configuration at startup so the user can verify what was found."""
    ok = "\033[32m[ok]\033[0m"
    warn = "\033[33m[!!]\033[0m"
    err = "\033[31m[XX]\033[0m"

    lines = [
        "",
        "  Hermes Web UI -- startup config",
        "  --------------------------------",
        f"  repo root   : {REPO_ROOT}",
        f"  agent dir   : {_AGENT_DIR if _AGENT_DIR else 'NOT FOUND'}  {ok if _AGENT_DIR else err}",
        f"  python      : {PYTHON_EXE}",
        f"  state dir   : {STATE_DIR}",
        f"  workspace   : {DEFAULT_WORKSPACE}",
        f"  host:port   : {HOST}:{PORT}",
        f"  config file : {_get_config_path()}  {'(found)' if _get_config_path().exists() else '(not found, using defaults)'}",
        "",
    ]
    print("\n".join(lines), flush=True)

    if not _HERMES_FOUND:
        print(
            f"{err}  Could not find the Hermes agent directory.\n"
            "      The server will start but agent features will not work.\n"
            "\n"
            "      To fix, set one of:\n"
            "        export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent\n"
            "        export HERMES_HOME=/path/to/.hermes\n"
            "\n"
            "      Or clone hermes-agent as a sibling of this repo:\n"
            "        git clone <hermes-agent-repo> ../hermes-agent\n",
            flush=True,
        )


def verify_hermes_imports() -> tuple:
    """
    Attempt to import the key Hermes modules.
    Returns (ok: bool, missing: list[str], errors: dict[str, str]).
    """
    required = ["run_agent"]
    missing = []
    errors = {}
    for mod in required:
        try:
            __import__(mod)
        except Exception as e:
            missing.append(mod)
            # Capture the full error message so startup logs show WHY
            # (e.g. pydantic_core .so mismatch) instead of just the name.
            errors[mod] = f"{type(e).__name__}: {e}"
    return (len(missing) == 0), missing, errors


# ── Limits ───────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = 200_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# ── File type maps ───────────────────────────────────────────────────────────
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"}
MD_EXTS = {".md", ".markdown", ".mdown"}
CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".html",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".bash",
    ".txt",
    ".log",
    ".env",
    ".csv",
    ".xml",
    ".sql",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
}
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# ── Toolsets (from config.yaml or hardcoded default) ─────────────────────────
_DEFAULT_TOOLSETS = [
    "browser",
    "clarify",
    "code_execution",
    "cronjob",
    "delegation",
    "file",
    "image_gen",
    "memory",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "web",
    "webhook",
]
def _resolve_cli_toolsets(cfg=None):
    """Resolve CLI toolsets using the agent's _get_platform_tools() so that
    MCP server toolsets are automatically included, matching CLI behaviour."""
    if cfg is None:
        cfg = get_config()
    try:
        from hermes_cli.tools_config import _get_platform_tools
        return list(_get_platform_tools(cfg, "cli"))
    except Exception:
        # Fallback: read raw list from config (MCP toolsets will be missing)
        return cfg.get("platform_toolsets", {}).get("cli", _DEFAULT_TOOLSETS)

CLI_TOOLSETS = _resolve_cli_toolsets()

# ── Model / provider discovery ───────────────────────────────────────────────

# Hardcoded fallback models (used when no config.yaml or agent is available)
# Also used as the OpenRouter model list — keep this curated to current, widely-used models.
_FALLBACK_MODELS = [
    # OpenAI
    {"provider": "OpenAI",    "id": "openai/gpt-5.4-mini",                "label": "GPT-5.4 Mini"},
    {"provider": "OpenAI",    "id": "openai/gpt-5.4",                     "label": "GPT-5.4"},
    # Anthropic — 4.6 flagship + 4.5 generation
    {"provider": "Anthropic", "id": "anthropic/claude-opus-4.6",          "label": "Claude Opus 4.6"},
    {"provider": "Anthropic", "id": "anthropic/claude-sonnet-4.6",        "label": "Claude Sonnet 4.6"},
    {"provider": "Anthropic", "id": "anthropic/claude-sonnet-4-5",        "label": "Claude Sonnet 4.5"},
    {"provider": "Anthropic", "id": "anthropic/claude-haiku-4-5",         "label": "Claude Haiku 4.5"},
    # Google
    {"provider": "Google",    "id": "google/gemini-3.1-pro-preview",              "label": "Gemini 3.1 Pro Preview"},
    {"provider": "Google",    "id": "google/gemini-3-flash-preview",              "label": "Gemini 3 Flash Preview"},
    # DeepSeek
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-chat-v3-0324",     "label": "DeepSeek V3"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-r1",               "label": "DeepSeek R1"},
    # Qwen (Alibaba) — strong coding and general models
    {"provider": "Qwen",      "id": "qwen/qwen3-coder",                   "label": "Qwen3 Coder"},
    {"provider": "Qwen",      "id": "qwen/qwen3.6-plus",                  "label": "Qwen3.6 Plus"},
    # xAI
    {"provider": "xAI",       "id": "x-ai/grok-4.20",                    "label": "Grok 4.20"},
    # Mistral
    {"provider": "Mistral",   "id": "mistralai/mistral-large-latest",     "label": "Mistral Large"},
]

# Provider display names for known Hermes provider IDs
_PROVIDER_DISPLAY = {
    "nous": "Nous Portal",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openai-codex": "OpenAI Codex",
    "copilot": "GitHub Copilot",
    "zai": "Z.AI / GLM",
    "kimi-coding": "Kimi / Moonshot",
    "deepseek": "DeepSeek",
    "minimax": "MiniMax",
    "google": "Google",
    "meta-llama": "Meta Llama",
    "huggingface": "HuggingFace",
    "alibaba": "Alibaba",
    "ollama": "Ollama",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "lmstudio": "LM Studio",
    "mistralai": "Mistral",
    "qwen": "Qwen",
    "x-ai": "xAI",
}

# Well-known models per provider (used to populate dropdown for direct API providers)
_PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.4",      "label": "GPT-5.4"},
    ],
    "openai-codex": [
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"id": "gpt-5.2-codex", "label": "GPT-5.2 Codex"},
        {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max"},
        {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini"},
        {"id": "codex-mini-latest", "label": "Codex Mini (latest)"},
    ],
    "google": [
        {"id": "gemini-3.1-pro-preview",   "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
    ],
    "deepseek": [
        {"id": "deepseek-chat-v3-0324", "label": "DeepSeek V3"},
        {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner"},
    ],
    "nous": [
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6 (via Nous)"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6 (via Nous)"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini (via Nous)"},
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview (via Nous)"},
    ],
    "zai": [
        {"id": "glm-5.1", "label": "GLM-5.1"},
        {"id": "glm-5", "label": "GLM-5"},
        {"id": "glm-5-turbo", "label": "GLM-5 Turbo"},
        {"id": "glm-4.7", "label": "GLM-4.7"},
        {"id": "glm-4.5", "label": "GLM-4.5"},
        {"id": "glm-4.5-flash", "label": "GLM-4.5 Flash"},
    ],
    "kimi-coding": [
        {"id": "moonshot-v1-8k", "label": "Moonshot v1 8k"},
        {"id": "moonshot-v1-32k", "label": "Moonshot v1 32k"},
        {"id": "moonshot-v1-128k", "label": "Moonshot v1 128k"},
        {"id": "kimi-latest", "label": "Kimi Latest"},
    ],
    "minimax": [
        {"id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
        {"id": "MiniMax-M2.7-highspeed", "label": "MiniMax M2.7 Highspeed"},
        {"id": "MiniMax-M2.5", "label": "MiniMax M2.5"},
        {"id": "MiniMax-M2.5-highspeed", "label": "MiniMax M2.5 Highspeed"},
        {"id": "MiniMax-M2.1", "label": "MiniMax M2.1"},
    ],
    # GitHub Copilot — model IDs served via the Copilot API
    "copilot": [
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-4o", "label": "GPT-4o"},
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
    ],
    # OpenCode Zen — curated models via opencode.ai/zen (pay-as-you-go credits)
    "opencode-zen": [
        {"id": "gpt-5.4-pro", "label": "GPT-5.4 Pro"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.4-nano", "label": "GPT-5.4 Nano"},
        {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"},
        {"id": "gpt-5.2", "label": "GPT-5.2"},
        {"id": "gpt-5.2-codex", "label": "GPT-5.2 Codex"},
        {"id": "gpt-5.1", "label": "GPT-5.1"},
        {"id": "gpt-5.1-codex", "label": "GPT-5.1 Codex"},
        {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max"},
        {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini"},
        {"id": "gpt-5", "label": "GPT-5"},
        {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
        {"id": "gpt-5-nano", "label": "GPT-5 Nano"},
        {"id": "claude-opus-4-6", "label": "Claude Opus 4.6"},
        {"id": "claude-opus-4-5", "label": "Claude Opus 4.5"},
        {"id": "claude-opus-4-1", "label": "Claude Opus 4.1"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
        {"id": "claude-3-5-haiku", "label": "Claude 3.5 Haiku"},
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
        {"id": "glm-5.1", "label": "GLM-5.1"},
        {"id": "glm-5", "label": "GLM-5"},
        {"id": "kimi-k2.5", "label": "Kimi K2.5"},
        {"id": "minimax-m2.5", "label": "MiniMax M2.5"},
        {"id": "minimax-m2.5-free", "label": "MiniMax M2.5 Free"},
        {"id": "nemotron-3-super-free", "label": "Nemotron 3 Super Free"},
        {"id": "big-pickle", "label": "Big Pickle"},
    ],
    # OpenCode Go — flat-rate models via opencode.ai/go ($10/month)
    "opencode-go": [
        {"id": "glm-5.1", "label": "GLM-5.1"},
        {"id": "glm-5", "label": "GLM-5"},
        {"id": "kimi-k2.5", "label": "Kimi K2.5"},
        {"id": "mimo-v2-pro", "label": "MiMo V2 Pro"},
        {"id": "mimo-v2-omni", "label": "MiMo V2 Omni"},
        {"id": "minimax-m2.7", "label": "MiniMax M2.7"},
        {"id": "minimax-m2.5", "label": "MiniMax M2.5"},
    ],
    # 'gemini' is the hermes_cli provider ID for Google AI Studio
    "gemini": [
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
    ],
    # Mistral — prefix used in OpenRouter model IDs (mistralai/mistral-large-latest)
    "mistralai": [
        {"id": "mistral-large-latest", "label": "Mistral Large"},
        {"id": "mistral-small-latest", "label": "Mistral Small"},
    ],
    # Qwen (Alibaba) — prefix used in OpenRouter model IDs (qwen/qwen3-coder)
    "qwen": [
        {"id": "qwen3-coder",   "label": "Qwen3 Coder"},
        {"id": "qwen3.6-plus",  "label": "Qwen3.6 Plus"},
    ],
    # xAI — prefix used in OpenRouter model IDs (x-ai/grok-4-20)
    "x-ai": [
        {"id": "grok-4.20", "label": "Grok 4.20"},
    ],
}


def resolve_model_provider(model_id: str) -> tuple:
    """Resolve model name, provider, and base_url for AIAgent.

    Model IDs from the dropdown can be in several formats:
      - 'claude-sonnet-4.6'            (bare name, uses config default provider)
      - 'anthropic/claude-sonnet-4.6'  (OpenRouter-style provider/model)
      - '@minimax:MiniMax-M2.7'        (explicit provider hint from dropdown)

    The @provider:model format is used for models from non-default provider
    groups in the dropdown, so we can route them through the correct provider
    via resolve_runtime_provider(requested=provider) instead of the default.

    Custom OpenAI-compatible endpoints are special: their model IDs often look
    like provider/model (for example ``google/gemma-4-26b-a4b``), which would be
    mistaken for an OpenRouter model if we only looked at the slash. To avoid
    that, first check whether the selected model matches an entry in
    config.yaml -> custom_providers and route it through that named custom
    provider.

    Returns (model, provider, base_url) where provider and base_url may be None.
    """
    config_provider = None
    config_base_url = None
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        config_provider = model_cfg.get("provider")
        config_base_url = model_cfg.get("base_url")

    model_id = (model_id or "").strip()
    if not model_id:
        return model_id, config_provider, config_base_url

    # Custom providers declared in config.yaml should win over slash-based
    # OpenRouter heuristics. Their model IDs commonly contain '/' too.
    custom_providers = cfg.get("custom_providers", [])
    if isinstance(custom_providers, list):
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            entry_model = (entry.get("model") or "").strip()
            entry_name = (entry.get("name") or "").strip()
            entry_base_url = (entry.get("base_url") or "").strip()
            if entry_model and entry_name and model_id == entry_model:
                provider_hint = "custom:" + entry_name.lower().replace(" ", "-")
                return model_id, provider_hint, entry_base_url or None

    # @provider:model format — explicit provider hint from the dropdown.
    # Route through that provider directly (resolve_runtime_provider will
    # resolve credentials in streaming.py).
    if model_id.startswith("@") and ":" in model_id:
        provider_hint, bare_model = model_id[1:].split(":", 1)
        return bare_model, provider_hint, None

    if "/" in model_id:
        prefix, bare = model_id.split("/", 1)
        # OpenRouter always needs the full provider/model path (e.g. openrouter/free,
        # anthropic/claude-sonnet-4.6). Never strip the prefix for OpenRouter.
        if config_provider == "openrouter":
            return model_id, "openrouter", config_base_url
        # If prefix matches config provider exactly, strip it and use that provider directly.
        # e.g. config=anthropic, model=anthropic/claude-... → bare name to anthropic API
        if config_provider and prefix == config_provider:
            return bare, config_provider, config_base_url
        # If a custom endpoint base_url is configured, don't reroute through OpenRouter
        # just because the model name contains a slash (e.g. google/gemma-4-26b-a4b).
        # The user has explicitly pointed at a base_url, so trust their routing config.
        if config_base_url:
            # Only strip the provider prefix when it's a known provider namespace
            # (e.g. "openai/gpt-5.4" → "gpt-5.4" for a custom OpenAI-compatible proxy).
            # Unknown prefixes (e.g. "zai-org/GLM-5.1" on DeepInfra) are intrinsic to
            # the model ID and must be preserved — stripping them causes model_not_found.
            if prefix in _PROVIDER_MODELS:
                return bare, config_provider, config_base_url
            # Unknown prefix (not a named provider) — pass full model_id through.
            return model_id, config_provider, config_base_url
        # If prefix does NOT match config provider, the user picked a cross-provider model
        # from the OpenRouter dropdown (e.g. config=anthropic but picked openai/gpt-5.4-mini).
        # In this case always route through openrouter with the full provider/model string.
        if prefix in _PROVIDER_MODELS and prefix != config_provider:
            return model_id, "openrouter", None

    return model_id, config_provider, config_base_url


def get_available_models() -> dict:
    """
    Return available models grouped by provider.

    Discovery order:
      1. Read config.yaml 'model' section for active provider info
      2. Check for known API keys in env or ~/.hermes/.env
      3. Fetch models from custom endpoint if base_url is configured
      4. Fall back to hardcoded model list (OpenRouter-style)

    Returns: {
        'active_provider': str|None,
        'default_model': str,
        'groups': [{'provider': str, 'models': [{'id': str, 'label': str}]}]
    }
    """
    # Reload config from disk if config.yaml has changed since last load.
    # This ensures CLI model changes are picked up on page refresh without
    # a server restart, while avoiding clearing in-memory mocks during tests. (#585)
    try:
        _current_mtime = Path(_get_config_path()).stat().st_mtime
    except OSError:
        _current_mtime = 0.0
    if _current_mtime != _cfg_mtime:
        reload_config()
    active_provider = None
    default_model = DEFAULT_MODEL
    groups = []

    # 1. Read config.yaml model section
    cfg_base_url = ""  # must be defined before conditional blocks (#117)
    model_cfg = cfg.get("model", {})
    cfg_base_url = ""
    if isinstance(model_cfg, str):
        default_model = model_cfg
    elif isinstance(model_cfg, dict):
        active_provider = model_cfg.get("provider")
        cfg_default = model_cfg.get("default", "")
        cfg_base_url = model_cfg.get("base_url", "")
        if cfg_default:
            default_model = cfg_default

    # 2. Also check env vars for model override
    env_model = (
        os.getenv("HERMES_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL")
    )
    if env_model:
        default_model = env_model.strip()

    # 3. Try to read auth store for active provider (if hermes is installed)
    if not active_provider:
        try:
            from api.profiles import get_active_hermes_home as _gah

            auth_store_path = _gah() / "auth.json"
        except ImportError:
            auth_store_path = HOME / ".hermes" / "auth.json"
        if auth_store_path.exists():
            try:
                import json as _j

                auth_store = _j.loads(auth_store_path.read_text())
                active_provider = auth_store.get("active_provider")
            except Exception:
                logger.debug("Failed to load auth store from %s", auth_store_path)

    # 4. Detect available providers.
    # Primary: ask hermes-agent's auth layer — the authoritative source. It checks
    # auth.json, credential pools, and env vars the same way the agent does at runtime,
    # so the dropdown reflects exactly what the user has configured.
    # Fallback: scan raw API key env vars (matches old behaviour if hermes not available).
    detected_providers = set()
    if active_provider:
        detected_providers.add(active_provider)
    all_env: dict = {}  # profile .env keys — populated below, used by custom endpoint auth

    _hermes_auth_used = False
    try:
        from hermes_cli.models import list_available_providers as _lap
        from hermes_cli.auth import get_auth_status as _gas

        for _p in _lap():
            if not _p.get("authenticated"):
                continue
            # Exclude providers whose credential came from an ambient token
            # (e.g. 'gh auth token' for Copilot on a machine with gh CLI auth).
            # Only include providers with an explicit, dedicated credential.
            try:
                _src = _gas(_p["id"]).get("key_source", "")
                if _src == "gh auth token":
                    continue
            except Exception:
                logger.debug("Failed to get key source for provider %s", _p.get("id", "unknown"))
            detected_providers.add(_p["id"])
        _hermes_auth_used = True
    except Exception:
        logger.debug("Failed to detect auth providers from hermes")

    if not _hermes_auth_used:
        # Fallback: scan .env and os.environ for known API key variables
        try:
            from api.profiles import get_active_hermes_home as _gah2

            hermes_env_path = _gah2() / ".env"
        except ImportError:
            hermes_env_path = HOME / ".hermes" / ".env"
        env_keys = {}
        if hermes_env_path.exists():
            try:
                for line in hermes_env_path.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_keys[k.strip()] = v.strip().strip('"').strip("'")
            except Exception:
                logger.debug("Failed to parse hermes env file")
        all_env = {**env_keys}
        for k in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "GOOGLE_API_KEY",
            "GLM_API_KEY",
            "KIMI_API_KEY",
            "DEEPSEEK_API_KEY",
            "OPENCODE_ZEN_API_KEY",
            "OPENCODE_GO_API_KEY",
        ):
            val = os.getenv(k)
            if val:
                all_env[k] = val
        if all_env.get("ANTHROPIC_API_KEY"):
            detected_providers.add("anthropic")
        if all_env.get("OPENAI_API_KEY"):
            detected_providers.add("openai")
        if all_env.get("OPENROUTER_API_KEY"):
            detected_providers.add("openrouter")
        if all_env.get("GOOGLE_API_KEY"):
            detected_providers.add("google")
        if all_env.get("GLM_API_KEY"):
            detected_providers.add("zai")
        if all_env.get("KIMI_API_KEY"):
            detected_providers.add("kimi-coding")
        if all_env.get("MINIMAX_API_KEY") or all_env.get("MINIMAX_CN_API_KEY"):
            detected_providers.add("minimax")
        if all_env.get("DEEPSEEK_API_KEY"):
            detected_providers.add("deepseek")
        if all_env.get("OPENCODE_ZEN_API_KEY"):
            detected_providers.add("opencode-zen")
        if all_env.get("OPENCODE_GO_API_KEY"):
            detected_providers.add("opencode-go")

    # 3. Fetch models from custom endpoint if base_url is configured
    auto_detected_models = []
    if cfg_base_url:
        try:
            import ipaddress
            import urllib.request

            # Normalize the base_url and build models endpoint
            base_url = cfg_base_url.strip()
            if base_url.endswith("/v1"):
                endpoint_url = base_url + "/models"  # /v1/models
            else:
                endpoint_url = base_url.rstrip("/") + "/v1/models"

            # Detect provider from base_url
            provider = "custom"
            parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
            host = (parsed.netloc or parsed.path).lower()

            if parsed.hostname:
                try:
                    addr = ipaddress.ip_address(parsed.hostname)
                    if addr.is_private or addr.is_loopback or addr.is_link_local:
                        if (
                            "ollama" in host
                            or "127.0.0.1" in host
                            or "localhost" in host
                        ):
                            provider = "ollama"
                        elif "lmstudio" in host or "lm-studio" in host:
                            provider = "lmstudio"
                        else:
                            provider = "local"
                except ValueError:
                    pass

            # Resolve API key for the custom / OpenAI-compatible endpoint.
            # Priority:
            #   1. model.api_key in config.yaml
            #   2. provider-specific providers.<active>.api_key / providers.custom.api_key
            #   3. env/.env fallbacks
            headers = {}
            api_key = ""
            if isinstance(model_cfg, dict):
                api_key = (model_cfg.get("api_key") or "").strip()
            if not api_key:
                providers_cfg = cfg.get("providers", {})
                if isinstance(providers_cfg, dict):
                    for provider_key in filter(None, [active_provider, "custom"]):
                        provider_cfg = providers_cfg.get(provider_key, {})
                        if isinstance(provider_cfg, dict):
                            api_key = (provider_cfg.get("api_key") or "").strip()
                            if api_key:
                                break
            if not api_key:
                api_key_vars = (
                    "HERMES_API_KEY",
                    "HERMES_OPENAI_API_KEY",
                    "OPENAI_API_KEY",
                    "LOCAL_API_KEY",
                    "OPENROUTER_API_KEY",
                    "API_KEY",
                )
                for key in api_key_vars:
                    api_key = (all_env.get(key) or os.getenv(key) or "").strip()
                    if api_key:
                        break
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            # Fetch model list from endpoint (with SSRF protection)
            import socket

            # Resolve hostname and check against private IPs after DNS lookup
            parsed_url = urlparse(
                endpoint_url if "://" in endpoint_url else f"http://{endpoint_url}"
            )
            # Validate URL scheme to prevent file:// and other dangerous schemes
            if parsed_url.scheme not in ("", "http", "https"):
                raise ValueError(f"Invalid URL scheme: {parsed_url.scheme}")
            if parsed_url.hostname:
                try:
                    resolved_ips = socket.getaddrinfo(parsed_url.hostname, None)
                    for _, _, _, _, addr in resolved_ips:
                        addr_obj = ipaddress.ip_address(addr[0])
                        if (
                            addr_obj.is_private
                            or addr_obj.is_loopback
                            or addr_obj.is_link_local
                        ):
                            # Allow known local providers (ollama, lmstudio)
                            is_known_local = any(
                                k in (parsed_url.hostname or "").lower()
                                for k in (
                                    "ollama",
                                    "localhost",
                                    "127.0.0.1",
                                    "lmstudio",
                                    "lm-studio",
                                )
                            )
                            if not is_known_local:
                                raise ValueError(
                                    f"SSRF: resolved hostname to private IP {addr[0]}"
                                )
                except socket.gaierror:
                    pass  # DNS resolution failed -- let urllib handle it
            req = urllib.request.Request(endpoint_url, method="GET")
            req.add_header("User-Agent", "OpenAI/Python 1.0")
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310
                data = json.loads(response.read().decode("utf-8"))

            # Handle both OpenAI-compatible and llama.cpp response formats
            models_list = []
            if "data" in data and isinstance(data["data"], list):
                models_list = data["data"]
            elif "models" in data and isinstance(data["models"], list):
                models_list = data["models"]

            for model in models_list:
                if not isinstance(model, dict):
                    continue
                model_id = (
                    model.get("id", "")
                    or model.get("name", "")
                    or model.get("model", "")
                )
                model_name = model.get("name", "") or model.get("model", "") or model_id
                if model_id and model_name:
                    auto_detected_models.append({"id": model_id, "label": model_name})
                    detected_providers.add(provider.lower())
        except Exception:
            logger.debug("Custom endpoint unreachable or misconfigured for provider: %s", provider)

    # 3b. Include models from custom_providers config entries.
    # These are explicitly configured and should always appear even when the
    # /v1/models endpoint is unreachable or returns a subset.
    #
    # Each entry may carry a `name` field (e.g. "Agent37").  When present we
    # use it as the dropdown section header instead of the generic "Custom"
    # label.  Internally we key these providers as "custom:<slug>" so that
    # multiple named custom providers can coexist as separate groups.
    _custom_providers_cfg = cfg.get("custom_providers", [])
    # Maps "custom:<slug>" -> (display_name, [model_dicts])
    _named_custom_groups: dict = {}
    if isinstance(_custom_providers_cfg, list):
        _seen_custom_ids = {m["id"] for m in auto_detected_models}
        for _cp in _custom_providers_cfg:
            if not isinstance(_cp, dict):
                continue
            _cp_model = _cp.get("model", "")
            _cp_name = (_cp.get("name") or "").strip()
            if _cp_model and _cp_model not in _seen_custom_ids:
                _cp_label = _cp_model.split("/")[-1] if "/" in _cp_model else _cp_model
                _seen_custom_ids.add(_cp_model)
                if _cp_name:
                    # Named custom provider — own group keyed by slug
                    _slug = "custom:" + _cp_name.lower().replace(" ", "-")
                    if _slug not in _named_custom_groups:
                        _named_custom_groups[_slug] = (_cp_name, [])
                        detected_providers.add(_slug)
                    _named_custom_groups[_slug][1].append(
                        {"id": _cp_model, "label": _cp_label}
                    )
                else:
                    # Unnamed — falls into the generic "Custom" bucket
                    auto_detected_models.append({"id": _cp_model, "label": _cp_label})
                    detected_providers.add("custom")

    # If the user configured a real model.provider, the base_url belongs to
    # THAT provider, not to a separate "Custom" group. hermes_cli reports
    # 'custom' as authenticated whenever base_url is set, which would otherwise
    # build a phantom "Custom" bucket next to the real provider's group. Drop
    # it unless (a) the user explicitly chose 'custom' as their active provider,
    # or (b) the user has custom_providers entries in config.yaml (those models
    # were already added above and should still be shown).
    _has_custom_providers = isinstance(_custom_providers_cfg, list) and len(_custom_providers_cfg) > 0
    if active_provider and active_provider != "custom" and not _has_custom_providers:
        detected_providers.discard("custom")
        # Also drop named custom slugs when active provider is a real named one
        # and there are no custom_providers entries to show.
        for _slug in list(detected_providers):
            if _slug.startswith("custom:") and not _has_custom_providers:
                detected_providers.discard(_slug)
    elif active_provider == "custom" and _has_custom_providers:
        # When the active provider is 'custom' and all custom_providers entries
        # are named (i.e. every entry produced a "custom:<slug>" key), the bare
        # "custom" bucket is empty noise — discard it so the dropdown only shows
        # the named groups.  We keep "custom" if there are unnamed entries (they
        # were added to auto_detected_models and will render under the generic
        # "Custom" header via the else branch in the group builder).
        _has_unnamed = any(
            isinstance(_cp, dict) and not (_cp.get("name") or "").strip()
            for _cp in _custom_providers_cfg
        )
        if not _has_unnamed:
            detected_providers.discard("custom")

    # 5. Build model groups
    if detected_providers:
        for pid in sorted(detected_providers):
            if pid.startswith("custom:") and pid in _named_custom_groups:
                # Named custom provider — use the stored display name and its own model list
                _nc_display, _nc_models = _named_custom_groups[pid]
                if _nc_models:
                    groups.append({"provider": _nc_display, "models": _nc_models})
                continue
            provider_name = _PROVIDER_DISPLAY.get(pid, pid.title())
            if pid == "openrouter":
                # OpenRouter uses provider/model format -- show the fallback list
                groups.append(
                    {
                        "provider": "OpenRouter",
                        "models": [
                            {"id": m["id"], "label": m["label"]}
                            for m in _FALLBACK_MODELS
                        ],
                    }
                )
            elif pid in _PROVIDER_MODELS:
                # For non-default providers, prefix model IDs with @provider:model
                # so resolve_model_provider() routes through that specific provider
                # via resolve_runtime_provider(requested=provider).
                # The default provider's models keep bare names for direct API routing.
                raw_models = _PROVIDER_MODELS[pid]
                _active = (active_provider or "").lower()
                if _active and pid != _active:
                    models = []
                    for m in raw_models:
                        mid = m["id"]
                        # Don't double-prefix; use @provider: hint for bare names
                        if mid.startswith("@") or "/" in mid:
                            models.append({"id": mid, "label": m["label"]})
                        else:
                            models.append({"id": f"@{pid}:{mid}", "label": m["label"]})
                else:
                    models = list(raw_models)
                groups.append(
                    {
                        "provider": provider_name,
                        "models": models,
                    }
                )
            else:
                # Unknown provider -- use auto-detected models if available,
                # otherwise fall back to default model placeholder
                if auto_detected_models:
                    groups.append(
                        {
                            "provider": provider_name,
                            "models": auto_detected_models,
                        }
                    )
                else:
                    groups.append(
                        {
                            "provider": provider_name,
                            "models": [
                                {
                                    "id": default_model,
                                    "label": default_model.split("/")[-1],
                                }
                            ],
                        }
                    )
    else:
        # No providers detected. Show only the configured default model so the user
        # can at least send messages with their current setting. Avoid showing a
        # generic multi-provider list — those models wouldn't be routable anyway.
        label = default_model.split("/")[-1] if "/" in default_model else default_model
        groups.append(
            {"provider": "Default", "models": [{"id": default_model, "label": label}]}
        )

    # Ensure the user's configured default_model always appears in the dropdown.
    # It may be missing if the model isn't in any hardcoded list (e.g. openrouter/free,
    # a custom local model, or any model.default not in _FALLBACK_MODELS).
    # Normalize before comparing: strip provider prefix and unify separators so
    # 'anthropic/claude-opus-4.6' matches 'claude-opus-4.6' and 'claude-sonnet-4-6'
    # matches 'claude-sonnet-4.6' (hermes-agent uses hyphens, webui uses dots).
    if default_model:
        _norm = lambda mid: (mid.split("/", 1)[-1] if "/" in mid else mid).replace("-", ".")
        all_ids_norm = {_norm(m["id"]) for g in groups for m in g.get("models", [])}
        if _norm(default_model) not in all_ids_norm:
            # Determine which group to inject into. Compare against the
            # provider's display name from _PROVIDER_DISPLAY rather than
            # doing a substring match on active_provider — substring
            # matching breaks on hyphenated provider IDs like 'openai-codex'
            # vs display name 'OpenAI Codex' (hyphen vs. space), which
            # silently falls through to groups[0] and lands the model in
            # the wrong group.
            label = (
                default_model.split("/")[-1] if "/" in default_model else default_model
            )
            target_display = (
                _PROVIDER_DISPLAY.get(active_provider, active_provider or "").lower()
                if active_provider
                else ""
            )
            injected = False
            for g in groups:
                if target_display and g.get("provider", "").lower() == target_display:
                    g["models"].insert(0, {"id": default_model, "label": label})
                    injected = True
                    break
            if not injected and groups:
                groups[0]["models"].insert(0, {"id": default_model, "label": label})
            elif not groups:
                groups.append(
                    {
                        "provider": active_provider or "Default",
                        "models": [{"id": default_model, "label": label}],
                    }
                )

    return {
        "active_provider": active_provider,
        "default_model": default_model,
        "groups": groups,
    }


# ── Static file path ─────────────────────────────────────────────────────────
_INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"

# ── Thread synchronisation ───────────────────────────────────────────────────
LOCK = threading.Lock()
SESSIONS_MAX = 100
CHAT_LOCK = threading.Lock()
STREAMS: dict = {}
STREAMS_LOCK = threading.Lock()
CANCEL_FLAGS: dict = {}
AGENT_INSTANCES: dict = {}  # stream_id -> AIAgent instance for interrupt propagation
SERVER_START_TIME = time.time()

# ── Thread-local env context ─────────────────────────────────────────────────
_thread_ctx = threading.local()


def _set_thread_env(**kwargs):
    _thread_ctx.env = kwargs


def _clear_thread_env():
    _thread_ctx.env = {}


# ── Per-session agent locks ───────────────────────────────────────────────────
SESSION_AGENT_LOCKS: dict = {}
SESSION_AGENT_LOCKS_LOCK = threading.Lock()


def _get_session_agent_lock(session_id: str) -> threading.Lock:
    with SESSION_AGENT_LOCKS_LOCK:
        if session_id not in SESSION_AGENT_LOCKS:
            SESSION_AGENT_LOCKS[session_id] = threading.Lock()
        return SESSION_AGENT_LOCKS[session_id]


# ── Settings persistence ─────────────────────────────────────────────────────

_SETTINGS_DEFAULTS = {
    "default_model": DEFAULT_MODEL,
    "default_workspace": str(DEFAULT_WORKSPACE),
    "onboarding_completed": False,
    "send_key": "enter",  # 'enter' or 'ctrl+enter'
    "show_token_usage": False,  # show input/output token badge below assistant messages
    "show_cli_sessions": False,  # merge CLI sessions from state.db into the sidebar
    "sync_to_insights": False,  # mirror WebUI token usage to state.db for /insights
    "check_for_updates": True,  # check if webui/agent repos are behind upstream
    "theme": "dark",  # active UI theme name (no enum gate -- allows custom themes)
    "language": "en",  # UI locale code; must match a key in static/i18n.js LOCALES
    "bot_name": os.getenv(
        "HERMES_WEBUI_BOT_NAME", "Hermes"
    ),  # display name for the assistant
    "sound_enabled": False,  # play notification sound when assistant finishes
    "notifications_enabled": False,  # browser notification when tab is in background
    "bubble_layout": False,  # right-aligned user / left-aligned assistant chat bubbles
    "password_hash": None,  # PBKDF2-HMAC-SHA256 hash; None = auth disabled
}
_SETTINGS_LEGACY_DROP_KEYS = {"assistant_language"}


def load_settings() -> dict:
    """Load settings from disk, merging with defaults for any missing keys."""
    settings = dict(_SETTINGS_DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(
                    {
                        k: v
                        for k, v in stored.items()
                        if k not in _SETTINGS_LEGACY_DROP_KEYS
                    }
                )
        except Exception:
            logger.debug("Failed to load settings from %s", SETTINGS_FILE)
    return settings


_SETTINGS_ALLOWED_KEYS = set(_SETTINGS_DEFAULTS.keys()) - {"password_hash"}
_SETTINGS_ENUM_VALUES = {
    "send_key": {"enter", "ctrl+enter"},
}
_SETTINGS_BOOL_KEYS = {
    "onboarding_completed",
    "show_token_usage",
    "show_cli_sessions",
    "sync_to_insights",
    "check_for_updates",
    "sound_enabled",
    "notifications_enabled",
    "bubble_layout",
}
# Language codes are validated as short alphanumeric BCP-47-like tags (e.g. 'en', 'zh', 'fr')
_SETTINGS_LANG_RE = __import__("re").compile(r"^[a-zA-Z]{2,10}(-[a-zA-Z0-9]{2,8})?$")


def save_settings(settings: dict) -> dict:
    """Save settings to disk. Returns the merged settings. Ignores unknown keys."""
    current = load_settings()
    # Handle _set_password: hash and store as password_hash
    raw_pw = settings.pop("_set_password", None)
    if raw_pw and isinstance(raw_pw, str) and raw_pw.strip():
        # Use PBKDF2 from auth module (600k iterations) -- never raw SHA-256
        from api.auth import _hash_password

        current["password_hash"] = _hash_password(raw_pw.strip())
    # Handle _clear_password: explicitly disable auth
    if settings.pop("_clear_password", False):
        current["password_hash"] = None
    for k, v in settings.items():
        if k in _SETTINGS_ALLOWED_KEYS:
            # Validate enum-constrained keys
            if k in _SETTINGS_ENUM_VALUES and v not in _SETTINGS_ENUM_VALUES[k]:
                continue
            # Validate language codes (BCP-47-like: 'en', 'zh', 'fr', 'zh-CN')
            if k == "language" and (
                not isinstance(v, str) or not _SETTINGS_LANG_RE.match(v)
            ):
                continue
            # Coerce bool keys
            if k in _SETTINGS_BOOL_KEYS:
                v = bool(v)
            current[k] = v

    current["default_workspace"] = str(
        resolve_default_workspace(current.get("default_workspace"))
    )
    SETTINGS_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Update runtime defaults so new sessions use them immediately
    global DEFAULT_MODEL, DEFAULT_WORKSPACE
    if "default_model" in current:
        DEFAULT_MODEL = current["default_model"]
    if "default_workspace" in current:
        DEFAULT_WORKSPACE = resolve_default_workspace(current["default_workspace"])
    return current


# Apply saved settings on startup (override env-derived defaults)
# Exception: if HERMES_WEBUI_DEFAULT_WORKSPACE is explicitly set in the
# environment, it wins over whatever settings.json has stored.  Persisted
# config must never shadow an explicit env-var override (Docker deployments
# rely on this — otherwise deleting settings.json is the only escape).
_startup_settings = load_settings()
if SETTINGS_FILE.exists():
    if _startup_settings.get("default_model"):
        DEFAULT_MODEL = _startup_settings["default_model"]
    if not os.getenv("HERMES_WEBUI_DEFAULT_WORKSPACE"):
        DEFAULT_WORKSPACE = resolve_default_workspace(
            _startup_settings.get("default_workspace")
        )
    if _startup_settings.get("default_workspace") != str(DEFAULT_WORKSPACE):
        _startup_settings["default_workspace"] = str(DEFAULT_WORKSPACE)
        try:
            SETTINGS_FILE.write_text(
                json.dumps(_startup_settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

# ── SESSIONS in-memory cache (LRU OrderedDict) ───────────────────────────────
SESSIONS: collections.OrderedDict = collections.OrderedDict()

# ── Profile state initialisation ────────────────────────────────────────────
# Must run after all imports are resolved to correctly patch module-level caches
try:
    from api.profiles import init_profile_state

    init_profile_state()
except ImportError:
    pass  # hermes_cli not available -- default profile only
