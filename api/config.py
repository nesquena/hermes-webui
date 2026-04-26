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
import copy
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

    # 7. XDG_DATA_HOME / hermes-agent  (e.g. ~/.local/share/hermes-agent)
    xdg_data = Path(os.getenv("XDG_DATA_HOME", str(HOME / ".local" / "share")))
    candidates.append(xdg_data.expanduser() / "hermes-agent")

    # 8. System-wide install paths (e.g. /opt/hermes-agent, /usr/local/hermes-agent)
    for sys_prefix in ("/opt", "/usr/local", "/usr/local/share"):
        candidates.append(Path(sys_prefix) / "hermes-agent")

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
        
        venv_py = agent_dir / ".venv" / "bin" / "python"
        if venv_py.exists():
            return str(venv_py)

        # Windows layout
        venv_py_win = agent_dir / "venv" / "Scripts" / "python.exe"
        if venv_py_win.exists():
            return str(venv_py_win)
        
        venv_py_win = agent_dir / ".venv" / "Scripts" / "python.exe"
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
        # Remember the old mtime so we can tell whether config actually changed
        # vs. first-ever load (mtime == 0.0, e.g. server start or profile switch).
        _old_cfg_mtime = _cfg_mtime
        try:
            import yaml as _yaml

            if config_path.exists():
                loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    _cfg_cache.update(loaded)
                    try:
                        _cfg_mtime = Path(config_path).stat().st_mtime
                    except OSError:
                        _cfg_mtime = 0.0
        except Exception:
            logger.debug("Failed to load yaml config from %s", config_path)
        # Bust the models cache so the next request sees fresh config values.
        # Only delete the disk cache when config has actually changed -- not on
        # first-ever load (when _old_cfg_mtime == 0.0, i.e. server start or
        # profile switch) -- preserving the disk cache so the next restart
        # still hits the fast path without a cold run.
        if _old_cfg_mtime != 0.0:
            _delete_models_cache_on_disk()


def _load_yaml_config_file(config_path: Path) -> dict:
    try:
        import yaml as _yaml
    except ImportError:
        return {}

    if not config_path.exists():
        return {}
    try:
        loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        logger.debug("Failed to parse yaml config from %s", config_path)
        return {}


def _save_yaml_config_file(config_path: Path, config_data: dict) -> None:
    try:
        import yaml as _yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to write Hermes config.yaml") from exc

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _yaml.safe_dump(config_data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


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
DEFAULT_MODEL = os.getenv("HERMES_WEBUI_DEFAULT_MODEL", "")  # Empty = use provider default; avoids showing unavailable OpenAI model to non-OpenAI users (#646)


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
    # Google — 3.x (latest preview) + 2.5 (stable GA)
    {"provider": "Google",    "id": "google/gemini-3.1-pro-preview",            "label": "Gemini 3.1 Pro Preview"},
    {"provider": "Google",    "id": "google/gemini-3-flash-preview",            "label": "Gemini 3 Flash Preview"},
    {"provider": "Google",    "id": "google/gemini-3.1-flash-lite-preview",     "label": "Gemini 3.1 Flash Lite Preview"},
    {"provider": "Google",    "id": "google/gemini-2.5-pro",                    "label": "Gemini 2.5 Pro"},
    {"provider": "Google",    "id": "google/gemini-2.5-flash",                  "label": "Gemini 2.5 Flash"},
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
    # MiniMax
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M2.7",             "label": "MiniMax M2.7"},
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M2.7-highspeed",   "label": "MiniMax M2.7 Highspeed"},
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
    "ollama-cloud": "Ollama Cloud",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "lmstudio": "LM Studio",
    "mistralai": "Mistral",
    "qwen": "Qwen",
    "x-ai": "xAI",
}

# Provider alias → canonical slug.  Users configure providers using the
# dotted/hyphenated form they see on the provider website (``z.ai``,
# ``x.ai``, ``google``) but the internal catalog (``_PROVIDER_MODELS``)
# uses slugs without punctuation (``zai``, ``xai``, ``gemini``).  Without
# normalisation the provider lands in the ``else`` branch of the group
# builder and no models are returned — the bug behind #815.
#
# This table is authoritative for the WebUI.  When ``hermes_cli.models``
# is importable we also merge its ``_PROVIDER_ALIASES`` on top so any
# new aliases added to the agent automatically apply.  Keeping the local
# copy means the fix works even in environments where the agent tree is
# not on ``sys.path`` (CI, installs without hermes-agent cloned
# alongside the WebUI).
_PROVIDER_ALIASES = {
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
    "github": "copilot",
    "github-copilot": "copilot",
    "github-models": "copilot",
    "github-model": "copilot",
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "kimi": "kimi-coding",
    "moonshot": "kimi-coding",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "deep-seek": "deepseek",
    "opencode": "opencode-zen",
    "grok": "xai",
    "x-ai": "xai",
    "x.ai": "xai",
    "aws": "bedrock",
    "aws-bedrock": "bedrock",
    "amazon": "bedrock",
    "amazon-bedrock": "bedrock",
    "qwen": "alibaba",
    "aliyun": "alibaba",
    "dashscope": "alibaba",
    "alibaba-cloud": "alibaba",
}


def _resolve_provider_alias(name: str) -> str:
    """Return the canonical provider slug for *name*.

    Applies the WebUI's local alias table first, then merges any
    additional aliases the agent provides (when hermes_cli is on
    sys.path). Lookup is case-insensitive and whitespace-trimmed.
    Unknown names pass through unchanged.
    """
    if not name:
        return name
    raw = str(name).strip().lower()
    # Prefer the agent's table when available so new aliases added there
    # work automatically; otherwise fall through to our local copy.
    try:
        from hermes_cli.models import _PROVIDER_ALIASES as _agent_aliases
        if raw in _agent_aliases:
            return _agent_aliases[raw]
    except Exception:
        pass
    return _PROVIDER_ALIASES.get(raw, name)


# Well-known models per provider (used to populate dropdown for direct API providers)
_PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-5.5",      "label": "GPT-5.5"},
        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.4",      "label": "GPT-5.4"},
    ],
    "openai-codex": [
        {"id": "gpt-5.5", "label": "GPT-5.5"},
        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"id": "gpt-5.2-codex", "label": "GPT-5.2 Codex"},
        {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max"},
        {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini"},
        {"id": "codex-mini-latest", "label": "Codex Mini (latest)"},
    ],
    "google": [
        {"id": "gemini-3.1-pro-preview",            "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview",            "label": "Gemini 3 Flash Preview"},
        {"id": "gemini-3.1-flash-lite-preview",     "label": "Gemini 3.1 Flash Lite Preview"},
        {"id": "gemini-2.5-pro",                    "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash",                  "label": "Gemini 2.5 Flash"},
    ],
    "deepseek": [
        {"id": "deepseek-chat-v3-0324", "label": "DeepSeek V3"},
        {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner"},
    ],
    "nous": [
        {"id": "@nous:anthropic/claude-opus-4.6",     "label": "Claude Opus 4.6 (via Nous)"},
        {"id": "@nous:anthropic/claude-sonnet-4.6",   "label": "Claude Sonnet 4.6 (via Nous)"},
        {"id": "@nous:openai/gpt-5.4-mini",           "label": "GPT-5.4 Mini (via Nous)"},
        {"id": "@nous:google/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview (via Nous)"},
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
        {"id": "kimi-k2.5", "label": "Kimi K2.5"},
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
        {"id": "gpt-5.5", "label": "GPT-5.5"},
        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-4o", "label": "GPT-4o"},
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
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
        {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash Lite Preview"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
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
    # Model IDs are bare — sent directly to:
    #   https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
    "gemini": [
        {"id": "gemini-3.1-pro-preview",            "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview",            "label": "Gemini 3 Flash Preview"},
        {"id": "gemini-3.1-flash-lite-preview",     "label": "Gemini 3.1 Flash Lite Preview"},
        {"id": "gemini-2.5-pro",                    "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash",                  "label": "Gemini 2.5 Flash"},
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


_AMBIENT_GH_CLI_MARKERS = frozenset({"gh_cli", "gh auth token"})


def _is_ambient_gh_cli_entry(source: str, label: str, key_source: str) -> bool:
    """True when a credential-pool entry is a seeded gh-cli token rather than
    one the user added explicitly. Filter these so Copilot doesn't appear in
    the dropdown just because `gh` is installed on the system.
    """
    return (
        source.strip().lower() in _AMBIENT_GH_CLI_MARKERS
        or label.strip().lower() == "gh auth token"
        or key_source.strip().lower() == "gh auth token"
    )


def _format_ollama_label(mid: str) -> str:
    """Turn an Ollama model id (Ollama tag format) into a readable display label.

    Examples: 'kimi-k2.5' → 'Kimi K2.5', 'qwen3-vl:235b-instruct' → 'Qwen3 VL (235B Instruct)'
    """
    name_part, _, variant = mid.partition(":")

    def _fmt(s: str) -> str:
        tokens = s.replace("-", " ").replace("_", " ").split()
        out = []
        for t in tokens:
            alpha_only = t.replace(".", "")
            if alpha_only.isalpha() and len(t) <= 3:
                out.append(t.upper())  # short acronym: glm → GLM, vl → VL, gpt → GPT
            elif alpha_only.isalnum() and alpha_only and alpha_only[0].isdigit():
                out.append(t.upper())  # size param: 235b → 235B, 1t → 1T
            else:
                out.append(t[0].upper() + t[1:] if t else t)  # capitalize: kimi → Kimi
        return " ".join(out)

    label = _fmt(name_part)
    if variant:
        label += f" ({_fmt(variant)})"
    return label


def _apply_provider_prefix(
    raw_models: list[dict],
    provider_id: str,
    active_provider: str | None,
) -> list[dict]:
    """Return *raw_models* with @provider: prefixes applied when needed.

    Prefixing is skipped when (a) the provider is already the active one, or
    (b) a model id already starts with '@' or contains '/' (already routable).
    """
    _active = (active_provider or "").lower()
    if not _active or provider_id == _active:
        return list(raw_models)
    result = []
    for m in raw_models:
        mid = m["id"]
        if mid.startswith("@") or "/" in mid:
            result.append({"id": mid, "label": m["label"]})
        else:
            result.append({"id": f"@{provider_id}:{mid}", "label": m["label"]})
    return result


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
        # Portal providers (Nous, OpenCode) serve models from multiple upstream
        # namespaces — check them BEFORE the config_base_url branch so that a
        # Nous user whose config.yaml also has a base_url doesn't accidentally
        # fall into the prefix-stripping path (#894: minimax/minimax-m2.7 → bare
        # name sent to Nous → 404 because Nous requires the full namespace path).
        _PORTAL_PROVIDERS = {"nous", "opencode-zen", "opencode-go"}
        if config_provider in _PORTAL_PROVIDERS:
            return model_id, config_provider, config_base_url
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


def get_effective_default_model(config_data: dict | None = None) -> str:
    """Resolve the effective Hermes default model from config, then env overrides."""
    active_cfg = config_data if config_data is not None else cfg
    default_model = DEFAULT_MODEL

    model_cfg = active_cfg.get("model", {})
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        cfg_default = str(model_cfg.get("default") or "").strip()
        if cfg_default:
            default_model = cfg_default

    env_model = (
        os.getenv("HERMES_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL")
    )
    if env_model:
        default_model = env_model.strip()
    return default_model


# ── Reasoning config (CLI parity for /reasoning) ─────────────────────────────
# Mirrors hermes_constants.parse_reasoning_effort so WebUI can validate without
# importing from the agent tree (which may not be installed).  Any drift here
# will show up in the shared test suite since both sides accept the same set.
VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def parse_reasoning_effort(effort):
    """Parse an effort level into the dict the agent expects.

    Returns None when *effort* is empty or unrecognised (caller interprets as
    "use default"), ``{"enabled": False}`` for ``"none"``, and
    ``{"enabled": True, "effort": <level>}`` for any of
    ``VALID_REASONING_EFFORTS``.
    """
    if not effort or not str(effort).strip():
        return None
    eff = str(effort).strip().lower()
    if eff == "none":
        return {"enabled": False}
    if eff in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": eff}
    return None


def get_reasoning_status() -> dict:
    """Return current reasoning configuration from the active profile's
    config.yaml — the same source of truth the CLI reads from.

    Keys:
      - show_reasoning: bool — from ``display.show_reasoning`` (default True)
      - reasoning_effort: str — from ``agent.reasoning_effort`` ('' = default)
    """
    config_data = _load_yaml_config_file(_get_config_path())
    display_cfg = config_data.get("display") or {}
    agent_cfg = config_data.get("agent") or {}
    show_raw = display_cfg.get("show_reasoning") if isinstance(display_cfg, dict) else None
    effort_raw = agent_cfg.get("reasoning_effort") if isinstance(agent_cfg, dict) else None
    return {
        # Match CLI default (True if unset in config.yaml)
        "show_reasoning": bool(show_raw) if isinstance(show_raw, bool) else True,
        "reasoning_effort": str(effort_raw or "").strip().lower(),
    }


def set_reasoning_display(show: bool) -> dict:
    """Persist ``display.show_reasoning`` to the active profile's config.yaml.

    Mirrors CLI ``/reasoning show|hide``: writes the same key that the CLI
    writes, so the preference is shared across the WebUI and the terminal
    REPL for the same profile.
    """
    config_path = _get_config_path()
    with _cfg_lock:
        config_data = _load_yaml_config_file(config_path)
        display_cfg = config_data.get("display")
        if not isinstance(display_cfg, dict):
            display_cfg = {}
        display_cfg["show_reasoning"] = bool(show)
        config_data["display"] = display_cfg
        _save_yaml_config_file(config_path, config_data)
    reload_config()
    return get_reasoning_status()


def set_reasoning_effort(effort: str) -> dict:
    """Persist ``agent.reasoning_effort`` to the active profile's config.yaml.

    Mirrors CLI ``/reasoning <level>``: same key, same valid values
    (``none`` | ``minimal`` | ``low`` | ``medium`` | ``high`` | ``xhigh``).
    Raises ``ValueError`` on an unrecognised level so callers can return 400.
    """
    raw = str(effort or "").strip().lower()
    if not raw:
        raise ValueError("effort is required")
    if raw != "none" and raw not in VALID_REASONING_EFFORTS:
        raise ValueError(
            f"Unknown reasoning effort '{effort}'. "
            f"Valid: none, {', '.join(VALID_REASONING_EFFORTS)}."
        )
    config_path = _get_config_path()
    with _cfg_lock:
        config_data = _load_yaml_config_file(config_path)
        agent_cfg = config_data.get("agent")
        if not isinstance(agent_cfg, dict):
            agent_cfg = {}
        agent_cfg["reasoning_effort"] = raw
        config_data["agent"] = agent_cfg
        _save_yaml_config_file(config_path, config_data)
    reload_config()
    return get_reasoning_status()


def set_hermes_default_model(model_id: str) -> dict:
    """Persist the Hermes default model in config.yaml and reload runtime config."""
    selected_model = str(model_id or "").strip()
    if not selected_model:
        raise ValueError("model is required")

    config_path = _get_config_path()
    # Hold _cfg_lock only around the read-modify-write of the YAML file.
    # reload_config() acquires _cfg_lock internally (it's not reentrant) so
    # it must be called AFTER releasing the lock to avoid deadlock.
    with _cfg_lock:
        config_data = _load_yaml_config_file(config_path)
        model_cfg = config_data.get("model", {})
        if not isinstance(model_cfg, dict):
            model_cfg = {}

        previous_provider = str(model_cfg.get("provider") or "").strip()
        resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(
            selected_model
        )
        # Persist the resolved bare/slash form, NOT the `@provider:` prefix. The
        # prefix is a WebUI-internal routing hint that the hermes-agent CLI does
        # not understand — if we wrote `@nous:anthropic/claude-opus-4.6` to
        # config.yaml, a user who ran `hermes` in the terminal right after
        # saving via WebUI would have the agent send that literal string to the
        # Nous API, which would reject it (Nous expects `anthropic/claude-opus-4.6`,
        # not the prefixed form). The Settings picker handles the resulting
        # CLI-shaped bare form via `_applyModelToDropdown()`'s normalising
        # matcher — see `static/panels.js` (#895).
        persisted_model = str(resolved_model or selected_model).strip()
        persisted_provider = str(resolved_provider or previous_provider or "").strip()

        model_cfg["default"] = persisted_model
        if persisted_provider:
            model_cfg["provider"] = persisted_provider

        if resolved_base_url:
            model_cfg["base_url"] = str(resolved_base_url).strip().rstrip("/")
        elif persisted_provider != previous_provider:
            if persisted_provider == "openai":
                model_cfg["base_url"] = "https://api.openai.com/v1"
            elif not persisted_provider.startswith("custom:"):
                model_cfg.pop("base_url", None)

        config_data["model"] = model_cfg
        _save_yaml_config_file(config_path, config_data)
    # Reload outside the lock — reload_config() acquires _cfg_lock itself.
    reload_config()
    # Invalidate the TTL cache so the next /api/models call returns fresh data
    # with the new default model. Do NOT call get_available_models() here —
    # it triggers a live provider fetch (up to 8s) that blocks the HTTP response
    # to the browser, causing a visible freeze on every Settings save (#895).
    invalidate_models_cache()
    return {"ok": True, "model": persisted_model}


# ── TTL cache for get_available_models() ─────────────────────────────────────
_available_models_cache: dict | None = None
_available_models_cache_ts: float = 0.0
_AVAILABLE_MODELS_CACHE_TTL: float = 86400.0  # 24 hours
_available_models_cache_lock = threading.RLock()  # must be RLock: cold path refactoring moved slow work inside this lock, requiring re-entry
_cache_build_cv = threading.Condition(_available_models_cache_lock)  # shares underlying RLock so notify_all() is safe inside with _available_models_cache_lock
_cache_build_in_progress = False  # True while a cold path is actively building

# Cache for credential pool results -- calling load_pool() per-provider per-server
# session is expensive (~10s for zai due to endpoint probing).  The credential pool
# only changes when the user adds/removes credentials, which is rare; a 24h TTL
# is plenty safe and ensures get_available_models() cold paths are fast.
_CREDENTIAL_POOL_CACHE: dict[str, tuple[float, "CredentialPool"]] = {}  # pid -> (ts, pool)
_provider_models_invalidated_ts: dict[str, float] = {}  # provider_id -> timestamp of last invalidation

# Disk-backed in-memory cache for get_available_models().
# Written to disk on every cache population so the cache survives server restarts.
# Invalidated (file deleted) whenever a provider is added/changed/removed or
# config.yaml changes.  A TTL is still used as a fallback in case the invalidation
# signal is somehow missed, but the cache will always be warm after the first
# page load following a server start.
_models_cache_path = Path("/dev/shm") / "hermes_webui_models_cache.json"


def _delete_models_cache_on_disk() -> None:
    try:
        os.unlink(str(_models_cache_path))
    except OSError:
        pass  # already absent


def _load_models_cache_from_disk() -> dict | None:
    """Load groups dict from disk cache if it exists and is valid."""
    try:
        import json as _j
        if not _models_cache_path.exists():
            return None
        with open(_models_cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        return cache if isinstance(cache, dict) and "groups" in cache else None
    except Exception:
        return None


def _save_models_cache_to_disk(cache: dict) -> None:
    """Save cache to disk so it survives server restarts."""
    try:
        import time as _cache_time
        tmp = str(_models_cache_path) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"groups": cache.get("groups", [])}, f, indent=2)
        os.rename(tmp, str(_models_cache_path))
    except Exception:
        pass  # Non-fatal -- cache will rebuild on next call


def invalidate_models_cache():
    """Force the TTL cache for get_available_models() to be cleared.

    Call this after modifying config.cfg in-memory (e.g. in tests) so
    the next call to get_available_models() picks up the changes rather
    than returning a stale cached result.
    """
    global _cache_build_in_progress, _available_models_cache, _available_models_cache_ts, _cache_build_cv
    with _available_models_cache_lock:
        _available_models_cache = None
        _available_models_cache_ts = 0.0
        _cache_build_in_progress = False
        _cache_build_cv.notify_all()
        # Clear the credential pool cache too. The cache key is provider_id
        # only, so without this, tests (and live provider key edits) see a
        # stale CredentialPool from a prior auth_store payload — the test_
        # credential_pool_providers suite was hitting this directly.
        _CREDENTIAL_POOL_CACHE.clear()


def invalidate_provider_models_cache(provider_id: str):
    """Invalidate cached models for a single provider.

    Also invalidates the full cache so that the next get_available_models()
    call rebuilds all groups cleanly (the rebuilt provider is merged with any
    other cached groups from the 24h TTL window).  After the next
    get_available_models() call, _provider_models_invalidated_ts[provider_id]
    is cleared so the provider's fresh models are used.

    Args:
        provider_id: canonical provider id (e.g. 'openai', 'anthropic', 'custom:my-key')
    """
    global _available_models_cache, _available_models_cache_ts, _CREDENTIAL_POOL_CACHE
    with _available_models_cache_lock:
        _available_models_cache = None
        _available_models_cache_ts = 0.0
        _provider_models_invalidated_ts[provider_id] = time.time()
        # Also evict the credential pool so the next cold path re-loads it.
        # Must evict both the original key and its canonical form (load_pool
        # may be called with either, and both paths cache under their own key).
        _CREDENTIAL_POOL_CACHE.pop(provider_id, None)
        _CREDENTIAL_POOL_CACHE.pop(_resolve_provider_alias(provider_id), None)
    _delete_models_cache_on_disk()


def _get_label_for_model(model_id: str, existing_groups: list) -> str:
    """Return a human-friendly label for *model_id*.

    Resolution order:
    1. If the model already appears in *existing_groups* with a label, use it.
    2. Strip @provider: prefix and namespace prefix, then title-case.

    This ensures the injected default model entry in the dropdown always shows
    the same label as the live-fetched or static-catalog version, rather than
    the raw lowercase ID string (#909).
    """
    # Strip @provider: prefix for lookup
    lookup_id = model_id
    if lookup_id.startswith("@") and ":" in lookup_id:
        lookup_id = lookup_id.split(":", 1)[1]

    # Check existing groups for a matching label
    _norm = lambda s: (s.split("/", 1)[-1] if "/" in s else s).replace("-", ".").lower()
    norm_lookup = _norm(lookup_id)
    for g in existing_groups:
        for m in g.get("models", []):
            if m.get("label") and _norm(str(m.get("id", ""))) == norm_lookup:
                return m["label"]

    # Fall back: capitalize each hyphen-separated word, preserve dots in version numbers.
    # The catalog lookup above handles well-known models; this only fires for unlisted IDs.
    bare = lookup_id.split("/")[-1] if "/" in lookup_id else lookup_id
    return " ".join(
        w.upper() if (len(w) <= 3 and w.replace(".", "").isalnum() and not w.isdigit()) else w.capitalize()
        for w in bare.replace("_", "-").split("-")
    )


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
    global _cache_build_in_progress, _available_models_cache, _available_models_cache_ts, _cache_build_cv
    # Config mtime check — must come before any config reads.
    # (Test #585 verifies _current_mtime appears before active_provider = None)
    try:
        _current_mtime = Path(_get_config_path()).stat().st_mtime
    except OSError:
        _current_mtime = 0.0
    if _current_mtime != _cfg_mtime:
        reload_config()
    # ── COLD PATH helper ─────────────────────────────────────────────────────
    # Extracted so it runs inside _available_models_cache_lock (RLock) to
    # prevent thundering-herd: only one thread rebuilds while others wait.
    def _build_available_models_uncached() -> dict:
        active_provider = None
        default_model = get_effective_default_model(cfg)
        groups = []

        # 1. Read config.yaml model section
        cfg_base_url = ""  # must be defined before conditional blocks (#117)
        model_cfg = cfg.get("model", {})
        cfg_base_url = ""
        if isinstance(model_cfg, str):
            pass  # default_model already set by get_effective_default_model
        elif isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_default = model_cfg.get("default", "")
            cfg_base_url = model_cfg.get("base_url", "")
            if cfg_default:
                default_model = cfg_default

        # Normalize active_provider to its canonical key
        if active_provider:
            active_provider = _resolve_provider_alias(active_provider)

        # 2. Read auth store (active_provider fallback + credential_pool inspection)
        auth_store = {}
        try:
            from api.profiles import get_active_hermes_home as _gah

            auth_store_path = _gah() / "auth.json"
        except ImportError:
            auth_store_path = HOME / ".hermes" / "auth.json"
        if auth_store_path.exists():
            try:
                import json as _j

                auth_store = _j.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = _resolve_provider_alias(auth_store.get("active_provider"))
            except Exception:
                logger.debug("Failed to load auth store from %s", auth_store_path)

        # 3. Detect available providers.
        detected_providers = set()
        if active_provider:
            detected_providers.add(active_provider)

        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict) and _pool:
                try:
                    from agent.credential_pool import load_pool as _load_pool

                    for _pid in list(_pool.keys()):
                        try:
                            _canonical_pid = _resolve_provider_alias(str(_pid))
                            # Check credential pool cache first
                            _cached = _CREDENTIAL_POOL_CACHE.get(_pid)
                            if _cached is not None:
                                _cp_ts, _cp_pool = _cached
                                if (time.time() - _cp_ts) < 86400.0:
                                    _all_entries = _cp_pool.entries()
                                else:
                                    _lp_t0 = time.monotonic()
                                    _cp_pool = _load_pool(_pid)
                                    _CREDENTIAL_POOL_CACHE[_pid] = (time.time(), _cp_pool)
                                    _all_entries = _cp_pool.entries()
                            else:
                                _lp_t0 = time.monotonic()
                                _cp_pool = _load_pool(_pid)
                                _CREDENTIAL_POOL_CACHE[_pid] = (time.time(), _cp_pool)
                                _all_entries = _cp_pool.entries()
                            _explicit = [
                                e for e in _all_entries
                                if not _is_ambient_gh_cli_entry(
                                    str(getattr(e, "source", "") or ""),
                                    str(getattr(e, "label", "") or ""),
                                    str(getattr(e, "key_source", "") or ""),
                                )
                            ]
                            if _explicit:
                                detected_providers.add(_canonical_pid)
                        except Exception:
                            logger.debug("credential_pool.load_pool(%s) failed", _pid)
                except ImportError:
                    for _pid, _entries in _pool.items():
                        if not isinstance(_entries, list) or len(_entries) == 0:
                            continue
                        _has_explicit_cred = any(
                            isinstance(_entry, dict)
                            and not _is_ambient_gh_cli_entry(
                                str(_entry.get("source", "") or ""),
                                str(_entry.get("label", "") or ""),
                                str(_entry.get("key_source", "") or ""),
                            )
                            for _entry in _entries
                        )
                        if _has_explicit_cred:
                            detected_providers.add(_resolve_provider_alias(str(_pid)))
        except Exception:
            logger.debug("Failed to inspect credential_pool from auth store")

        all_env: dict = {}

        _hermes_auth_used = False
        try:
            from hermes_cli.models import list_available_providers as _lap
            from hermes_cli.auth import get_auth_status as _gas

            for _p in _lap():
                if not _p.get("authenticated"):
                    continue
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
            try:
                from api.profiles import get_active_hermes_home as _gah2

                hermes_env_path = _gah2() / ".env"
            except ImportError:
                hermes_env_path = HOME / ".hermes" / ".env"
            env_keys = {}
            if hermes_env_path.exists():
                try:
                    for line in hermes_env_path.read_text(encoding="utf-8").splitlines():
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
                "GEMINI_API_KEY",
                "GLM_API_KEY",
                "KIMI_API_KEY",
                "DEEPSEEK_API_KEY",
                "OPENCODE_ZEN_API_KEY",
                "OPENCODE_GO_API_KEY",
                "MINIMAX_API_KEY",
                "MINIMAX_CN_API_KEY",
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
            if all_env.get("GEMINI_API_KEY"):
                detected_providers.add("gemini")
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

        # 4. Fetch models from custom endpoint if base_url is configured
        auto_detected_models = []
        if cfg_base_url:
            try:
                import ipaddress
                import urllib.request

                base_url = cfg_base_url.strip()
                if base_url.endswith("/v1"):
                    endpoint_url = base_url + "/models"
                else:
                    endpoint_url = base_url.rstrip("/") + "/v1/models"

                provider = "custom"
                parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
                host = (parsed.netloc or parsed.path).lower()

                if parsed.hostname:
                    try:
                        addr = ipaddress.ip_address(parsed.hostname)
                        if addr.is_private or addr.is_loopback or addr.is_link_local:
                            if "ollama" in host or "127.0.0.1" in host or "localhost" in host:
                                provider = "ollama"
                            elif "lmstudio" in host or "lm-studio" in host:
                                provider = "lmstudio"
                            else:
                                provider = "local"
                    except ValueError:
                        pass

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

                import socket

                parsed_url = urlparse(
                    endpoint_url if "://" in endpoint_url else f"http://{endpoint_url}"
                )
                if parsed_url.scheme not in ("", "http", "https"):
                    raise ValueError(f"Invalid URL scheme: {parsed_url.scheme}")
                if parsed_url.hostname:
                    try:
                        resolved_ips = socket.getaddrinfo(parsed_url.hostname, None)
                        for _, _, _, _, addr in resolved_ips:
                            addr_obj = ipaddress.ip_address(addr[0])
                            if addr_obj.is_private or addr_obj.is_loopback or addr_obj.is_link_local:
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
                        pass
                req = urllib.request.Request(endpoint_url, method="GET")
                req.add_header("User-Agent", "OpenAI/Python 1.0")
                for k, v in headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310
                    data = json.loads(response.read().decode("utf-8"))

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
                        label = _format_ollama_label(model_id) if provider in ("ollama", "ollama-cloud") else model_name
                        auto_detected_models.append({"id": model_id, "label": label})
                        detected_providers.add(provider.lower())
            except Exception:
                logger.debug("Custom endpoint unreachable or misconfigured for provider: %s", provider)

        _custom_providers_cfg = cfg.get("custom_providers", [])
        _named_custom_groups: dict = {}
        if isinstance(_custom_providers_cfg, list):
            _seen_custom_ids = {m["id"] for m in auto_detected_models}
            for _cp in _custom_providers_cfg:
                if not isinstance(_cp, dict):
                    continue
                _cp_model = _cp.get("model", "")
                _cp_name = (_cp.get("name") or "").strip()
                if _cp_model and _cp_model not in _seen_custom_ids:
                    _cp_label = _get_label_for_model(_cp_model, [])
                    _seen_custom_ids.add(_cp_model)
                    if _cp_name:
                        _slug = "custom:" + _cp_name.lower().replace(" ", "-")
                        if _slug not in _named_custom_groups:
                            _named_custom_groups[_slug] = (_cp_name, [])
                            detected_providers.add(_slug)
                        _named_custom_groups[_slug][1].append(
                            {"id": _cp_model, "label": _cp_label}
                        )
                    else:
                        auto_detected_models.append({"id": _cp_model, "label": _cp_label})
                        detected_providers.add("custom")

        _has_custom_providers = isinstance(_custom_providers_cfg, list) and len(_custom_providers_cfg) > 0
        if active_provider and active_provider != "custom" and not _has_custom_providers:
            detected_providers.discard("custom")
            for _slug in list(detected_providers):
                if _slug.startswith("custom:") and not _has_custom_providers:
                    detected_providers.discard(_slug)
        elif active_provider == "custom" and _has_custom_providers:
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
                    _nc_display, _nc_models = _named_custom_groups[pid]
                    if _nc_models:
                        groups.append({"provider": _nc_display, "provider_id": pid, "models": _nc_models})
                    continue
                provider_name = _PROVIDER_DISPLAY.get(pid, pid.title())
                if pid == "openrouter":
                    groups.append(
                        {
                            "provider": "OpenRouter",
                            "provider_id": "openrouter",
                            "models": [
                                {"id": m["id"], "label": m["label"]}
                                for m in _FALLBACK_MODELS
                            ],
                        }
                    )
                elif pid == "ollama-cloud":
                    raw_models = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        raw_models = [
                            {"id": mid, "label": _format_ollama_label(mid)}
                            for mid in (_provider_model_ids("ollama-cloud") or [])
                        ]
                    except Exception:
                        logger.warning("Failed to load Ollama Cloud models from hermes_cli")

                    if raw_models:
                        models = _apply_provider_prefix(raw_models, pid, active_provider)
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": models,
                            }
                        )
                elif pid in _PROVIDER_MODELS or pid in cfg.get("providers", {}):
                    raw_models = _PROVIDER_MODELS.get(pid, [])

                    provider_cfg = cfg.get("providers", {}).get(pid, {})
                    if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                        cfg_models = provider_cfg["models"]
                        if isinstance(cfg_models, dict):
                            raw_models = [{"id": k, "label": k} for k in cfg_models.keys()]
                        elif isinstance(cfg_models, list):
                            raw_models = [{"id": k, "label": k} for k in cfg_models]
                    models = _apply_provider_prefix(raw_models, pid, active_provider)
                    groups.append(
                        {
                            "provider": provider_name,
                            "provider_id": pid,
                            "models": models,
                        }
                    )
                else:
                    if auto_detected_models:
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": auto_detected_models,
                            }
                        )
        else:
            if default_model:
                label = _get_label_for_model(default_model, groups)
                groups.append(
                    {"provider": "Default", "provider_id": "default", "models": [{"id": default_model, "label": label}]}
                )

        if default_model:
            _norm = lambda mid: (mid.split("/", 1)[-1] if "/" in mid else mid).replace("-", ".")
            all_ids_norm = {_norm(m["id"]) for g in groups for m in g.get("models", [])}
            if _norm(default_model) not in all_ids_norm:
                label = _get_label_for_model(default_model, groups)
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
                    groups.append(
                        {
                            "provider": "Default",
                            "provider_id": active_provider or "default",
                            "models": [{"id": default_model, "label": label}],
                        }
                    )

        return {
            "active_provider": active_provider,
            "default_model": default_model,
            "groups": groups,
        }

    # ── FAST PATH ─────────────────────────────────────────────────────────────
    # Mark that a build may be in progress BEFORE acquiring the lock.
    # If another thread has already started the cold path, we will wait for
    # its result rather than running the cold path concurrently.
    should_wait = _cache_build_in_progress

    # Check config mtime OUTSIDE the lock so this cheap check doesn't serialize
    # concurrent requests.  Must come before any config reads in the cold path.
    try:
        _current_mtime = Path(_get_config_path()).stat().st_mtime
    except OSError:
        _current_mtime = 0.0
    _cfg_changed = _current_mtime != _cfg_mtime

    # Disk load BEFORE lock: ~0.1ms, lets concurrent requests skip entirely.
    # Then acquire lock and check memory cache.  Cold path runs inside the lock
    # so only one thread rebuilds while others wait.
    disk_groups = None
    if _available_models_cache is None:
        disk_groups = _load_models_cache_from_disk()

    with _available_models_cache_lock:
        # If another thread is already building, wait for its result instead
        # of re-entering the cold path (avoids duplicate 10s zai load_pool calls).
        if should_wait:
            _cache_build_cv.wait_for(
                lambda: not _cache_build_in_progress and _available_models_cache is not None,
                timeout=60
            )
            if _available_models_cache is not None and (time.monotonic() - _available_models_cache_ts) < _AVAILABLE_MODELS_CACHE_TTL:
                return copy.deepcopy(_available_models_cache)

        # Reload config if changed
        if _cfg_changed:
            reload_config()
            _available_models_cache = None
            _available_models_cache_ts = 0.0

        # Serve from memory cache if fresh
        now = time.monotonic()
        if _available_models_cache is not None and (now - _available_models_cache_ts) < _AVAILABLE_MODELS_CACHE_TTL:
            return copy.deepcopy(_available_models_cache)

        # Cold path: disk cache hit — use it (fast, no lock contention)
        if disk_groups is not None:
            _available_models_cache = disk_groups
            _available_models_cache_ts = now
            _save_models_cache_to_disk(disk_groups)
            return copy.deepcopy(disk_groups)

        # Cold path: full rebuild — only one thread reaches here at a time
        with _cache_build_cv:
            _cache_build_in_progress = True
        result = _build_available_models_uncached()
        with _cache_build_cv:
            _available_models_cache = result
            _available_models_cache_ts = time.monotonic()
            _cache_build_in_progress = False
            _cache_build_cv.notify_all()
        _save_models_cache_to_disk(result)
        return copy.deepcopy(result)


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
STREAM_PARTIAL_TEXT: dict = {}  # stream_id -> partial assistant text accumulated during streaming
SERVER_START_TIME = time.time()

# Agent cache: reuse AIAgent across messages in the same WebUI session so that
# _user_turn_count survives between turns.  This mirrors the gateway's
# _agent_cache pattern and is required for injectionFrequency: "first-turn".
SESSION_AGENT_CACHE: dict = {}   # session_id -> (AIAgent, config_sig)
SESSION_AGENT_CACHE_LOCK = threading.Lock()


def _evict_session_agent(session_id: str) -> None:
    """Remove a cached agent for a session (on delete, clear, or model switch)."""
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.pop(session_id, None)

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
    """Return the per-session Lock used to serialize all Session mutations.

    Lock lifecycle invariant:
      - A Lock is created lazily on first access and lives in SESSION_AGENT_LOCKS
        for the lifetime of the session.
      - The entry is pruned in /api/session/delete (under SESSION_AGENT_LOCKS_LOCK)
        so deleted sessions don't leak a Lock forever.
      - During context compression the agent may rotate session_id.  The
        streaming thread migrates the lock entry atomically under
        SESSION_AGENT_LOCKS_LOCK: it aliases the new session_id to the *same*
        Lock object and pops the old-id entry (see streaming.py compression
        block).  This ensures that subsequent callers using the new ID still
        acquire the same Lock, while the old-id entry is removed to prevent a
        leak.  The streaming thread already holds the Lock during this
        migration, so the reference stays alive even after the dict entry is
        removed.
      - Lock contract: hold for the in-memory mutation + s.save() only; never
        across network I/O (LLM calls, HTTP requests).
    """
    with SESSION_AGENT_LOCKS_LOCK:
        if session_id not in SESSION_AGENT_LOCKS:
            SESSION_AGENT_LOCKS[session_id] = threading.Lock()
        return SESSION_AGENT_LOCKS[session_id]


# ── Settings persistence ─────────────────────────────────────────────────────

_SETTINGS_DEFAULTS = {
    "default_workspace": str(DEFAULT_WORKSPACE),
    "onboarding_completed": False,
    "send_key": "enter",  # 'enter' or 'ctrl+enter'
    "show_token_usage": False,  # show input/output token badge below assistant messages
    "show_cli_sessions": False,  # merge CLI sessions from state.db into the sidebar
    "sync_to_insights": False,  # mirror WebUI token usage to state.db for /insights
    "check_for_updates": True,  # check if webui/agent repos are behind upstream
    "theme": "dark",  # light | dark | system
    "skin": "default",  # accent color skin: default | ares | mono | slate | poseidon | sisyphus | charizard
    "language": "en",  # UI locale code; must match a key in static/i18n.js LOCALES
    "bot_name": os.getenv(
        "HERMES_WEBUI_BOT_NAME", "Hermes"
    ),  # display name for the assistant
    "sound_enabled": False,  # play notification sound when assistant finishes
    "notifications_enabled": False,  # browser notification when tab is in background
    "show_thinking": True,  # show/hide thinking/reasoning blocks in chat view
    "sidebar_density": "compact",  # compact | detailed
    "auto_title_refresh_every": "0",  # adaptive title refresh: 0=off, 5/10/20=every N exchanges
    "password_hash": None,  # PBKDF2-HMAC-SHA256 hash; None = auth disabled
}
_SETTINGS_LEGACY_DROP_KEYS = {"assistant_language", "bubble_layout", "default_model"}
_SETTINGS_THEME_VALUES = {"light", "dark", "system"}
_SETTINGS_SKIN_VALUES = {
    "default",
    "ares",
    "mono",
    "slate",
    "poseidon",
    "sisyphus",
    "charizard",
}
_SETTINGS_LEGACY_THEME_MAP = {
    # Legacy full themes now map onto the closest supported theme + accent skin pair.
    "slate": ("dark", "slate"),
    "solarized": ("dark", "poseidon"),
    "monokai": ("dark", "sisyphus"),
    "nord": ("dark", "slate"),
    "oled": ("dark", "default"),
}


def _normalize_appearance(theme, skin) -> tuple[str, str]:
    """Normalize a (theme, skin) pair, migrating legacy theme names.

    Legacy migration table (from `_SETTINGS_LEGACY_THEME_MAP`):

        slate     → ("dark", "slate")
        solarized → ("dark", "poseidon")
        monokai   → ("dark", "sisyphus")
        nord      → ("dark", "slate")
        oled      → ("dark", "default")

    Unknown / custom theme names fall back to ("dark", "default").  This is a
    behavior change vs. the pre-PR-#627 state, where the `theme` field was
    open-ended ("no enum gate -- allows custom themes").  Users who set a
    custom CSS theme via `data-theme` will need to re-apply via skin or
    custom CSS — see CHANGELOG entry for details.

    The same mapping is mirrored in `static/boot.js` (`_LEGACY_THEME_MAP`)
    so client and server normalize identically; keep them in sync.
    """
    raw_theme = theme.strip().lower() if isinstance(theme, str) else ""
    raw_skin = skin.strip().lower() if isinstance(skin, str) else ""
    legacy = _SETTINGS_LEGACY_THEME_MAP.get(raw_theme)
    if legacy:
        next_theme, legacy_skin = legacy
    elif raw_theme in _SETTINGS_THEME_VALUES:
        next_theme, legacy_skin = raw_theme, "default"
    else:
        # Unknown themes used to exist; default to dark so upgrades stay visually stable.
        next_theme, legacy_skin = "dark", "default"
    next_skin = (
        raw_skin
        if raw_skin in _SETTINGS_SKIN_VALUES
        else legacy_skin
    )
    return next_theme, next_skin


def load_settings() -> dict:
    """Load settings from disk, merging with defaults for any missing keys."""
    settings = dict(_SETTINGS_DEFAULTS)
    stored = None
    try:
        settings_exists = SETTINGS_FILE.exists()
    except OSError:
        # PermissionError or other OS-level error (e.g. UID mismatch in Docker)
        # Treat as missing — start with defaults rather than crashing.
        logger.debug("Cannot stat settings file %s (inaccessible?)", SETTINGS_FILE)
        settings_exists = False
    if settings_exists:
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
    settings["theme"], settings["skin"] = _normalize_appearance(
        stored.get("theme") if isinstance(stored, dict) else settings.get("theme"),
        stored.get("skin") if isinstance(stored, dict) else settings.get("skin"),
    )
    settings["default_model"] = get_effective_default_model()
    return settings


_SETTINGS_ALLOWED_KEYS = set(_SETTINGS_DEFAULTS.keys()) - {
    "password_hash",
    "default_model",
}
_SETTINGS_ENUM_VALUES = {
    "send_key": {"enter", "ctrl+enter"},
    "sidebar_density": {"compact", "detailed"},
    "auto_title_refresh_every": {"0", "5", "10", "20"},
}
_SETTINGS_BOOL_KEYS = {
    "onboarding_completed",
    "show_token_usage",
    "show_cli_sessions",
    "sync_to_insights",
    "check_for_updates",
    "sound_enabled",
    "notifications_enabled",
    "show_thinking",
}
# Language codes are validated as short alphanumeric BCP-47-like tags (e.g. 'en', 'zh', 'fr')
_SETTINGS_LANG_RE = __import__("re").compile(r"^[a-zA-Z]{2,10}(-[a-zA-Z0-9]{2,8})?$")


def save_settings(settings: dict) -> dict:
    """Save settings to disk. Returns the merged settings. Ignores unknown keys."""
    current = load_settings()
    pending_theme = current.get("theme")
    pending_skin = current.get("skin")
    theme_was_explicit = False
    skin_was_explicit = False
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
            if k == "theme":
                if isinstance(v, str) and v.strip():
                    pending_theme = v
                    theme_was_explicit = True
                continue
            if k == "skin":
                if isinstance(v, str) and v.strip():
                    pending_skin = v
                    skin_was_explicit = True
                continue
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
    theme_value = pending_theme
    skin_value = pending_skin
    if theme_was_explicit and not skin_was_explicit:
        raw_theme = pending_theme.strip().lower() if isinstance(pending_theme, str) else ""
        if raw_theme not in _SETTINGS_THEME_VALUES:
            skin_value = None
    current["theme"], current["skin"] = _normalize_appearance(theme_value, skin_value)

    current["default_workspace"] = str(
        resolve_default_workspace(current.get("default_workspace"))
    )
    persisted = {k: v for k, v in current.items() if k != "default_model"}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(persisted, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Update runtime defaults so new sessions use them immediately
    global DEFAULT_WORKSPACE
    if "default_workspace" in current:
        DEFAULT_WORKSPACE = resolve_default_workspace(current["default_workspace"])
    current["default_model"] = get_effective_default_model()
    return current


# Apply saved settings on startup (override env-derived defaults)
# Exception: if HERMES_WEBUI_DEFAULT_WORKSPACE is explicitly set in the
# environment, it wins over whatever settings.json has stored.  Persisted
# config must never shadow an explicit env-var override (Docker deployments
# rely on this — otherwise deleting settings.json is the only escape).
_startup_settings = load_settings()
try:
    _settings_file_exists = SETTINGS_FILE.exists()
except OSError:
    _settings_file_exists = False
if _settings_file_exists:
    if not os.getenv("HERMES_WEBUI_DEFAULT_WORKSPACE"):
        DEFAULT_WORKSPACE = resolve_default_workspace(
            _startup_settings.get("default_workspace")
        )
    _startup_settings.pop("default_model", None)  # always drop stale value; model comes from config.yaml
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
