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
import queue
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
_cfg_path: Path | None = None  # active config.yaml path for the disk-loaded cache
_cfg_fingerprint: str | None = None  # serialized snapshot from the last disk load


def _fingerprint_config(data: dict) -> str:
    """Return a stable fingerprint for config dictionaries.

    A few tests and legacy call sites still mutate ``cfg`` directly for
    in-memory overrides.  Path-aware reloads should not immediately discard
    those overrides just because the active profile path differs from the last
    disk load, but an unchanged disk-loaded cache must still reload on profile
    switches.
    """
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return repr(data)


def _cfg_has_in_memory_overrides() -> bool:
    """True when cfg was changed after the last successful reload_config().

    Detects two override shapes:
      1. ``_cfg_cache`` was mutated in place (fingerprint differs).
      2. ``cfg`` (the module attribute) was rebound to a different dict —
         e.g. ``monkeypatch.setattr(config, "cfg", {...})`` in tests. The
         alias-with-the-cache pattern at module load means this is a common
         test-isolation override, and silently reloading from disk over it
         (the v0.51.7 path-aware reload regression) breaks any test that
         relies on the override.
    """
    if _cfg_fingerprint is not None and _fingerprint_config(_cfg_cache) != _cfg_fingerprint:
        return True
    # Module attribute rebound away from _cfg_cache by a test or runtime caller.
    try:
        return cfg is not _cfg_cache
    except NameError:
        # cfg not yet defined (during initial reload_config() at import time).
        return False


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


_WEBUI_SESSION_SAVE_MODES = {"deferred", "eager"}
_DEFAULT_WEBUI_SESSION_SAVE_MODE = "deferred"


def get_config() -> dict:
    """Return the cached config dict, loading from disk if needed."""
    config_path = _get_config_path()
    try:
        current_mtime = config_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    cache_stale = current_mtime != _cfg_mtime or _cfg_path != config_path
    if not _cfg_cache or (cache_stale and not _cfg_has_in_memory_overrides()):
        reload_config()
    # When a test (or runtime caller) has rebound ``cfg`` to a different dict
    # via monkeypatch.setattr(config, "cfg", ...), return that override rather
    # than the underlying _cfg_cache. Without this branch, get_config() would
    # silently bypass the override even though _cfg_has_in_memory_overrides()
    # correctly suppressed the reload.
    try:
        if cfg is not _cfg_cache:
            return cfg
    except NameError:
        pass
    return _cfg_cache


def get_webui_session_save_mode(config_data: dict | None = None) -> str:
    """Return the validated first-turn session persistence mode.

    ``deferred`` preserves the current first-turn sidecar behaviour: persist
    pending_user_message/runtime fields before streaming, then merge the turn
    after the agent finishes. ``eager`` additionally checkpoints the current
    user turn into ``messages`` before launching the agent thread. Unknown
    values fail closed to ``deferred`` so a typo never reintroduces eager disk
    writes unexpectedly.
    """
    active_cfg = config_data if isinstance(config_data, dict) else cfg
    webui_cfg = active_cfg.get("webui", {}) if isinstance(active_cfg, dict) else {}
    if not isinstance(webui_cfg, dict):
        return _DEFAULT_WEBUI_SESSION_SAVE_MODE
    mode = webui_cfg.get("session_save_mode", _DEFAULT_WEBUI_SESSION_SAVE_MODE)
    if isinstance(mode, str):
        normalized = mode.strip().lower()
        if normalized in _WEBUI_SESSION_SAVE_MODES:
            return normalized
    return _DEFAULT_WEBUI_SESSION_SAVE_MODE


def reload_config() -> None:
    """Reload config.yaml from the active profile's directory."""
    global _cfg_mtime, _cfg_path, _cfg_fingerprint
    with _cfg_lock:
        _cfg_cache.clear()
        config_path = _get_config_path()
        # Remember the old mtime so we can tell whether config actually changed
        # vs. first-ever load (mtime == 0.0, e.g. server start or profile switch).
        _old_cfg_mtime = _cfg_mtime
        _cfg_path = config_path
        _cfg_mtime = 0.0
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
        _cfg_fingerprint = _fingerprint_config(_cfg_cache)
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
    ".html": "text/html",
    ".htm": "text/html",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
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
    {"provider": "Anthropic", "id": "anthropic/claude-opus-4.7",          "label": "Claude Opus 4.7"},
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
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-v4-flash",          "label": "DeepSeek V4 Flash"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-v4-pro",            "label": "DeepSeek V4 Pro"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-chat-v3-0324",      "label": "DeepSeek V3 (legacy)"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-r1",                "label": "DeepSeek R1 (legacy)"},
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
    # Z.AI / GLM
    {"provider": "Z.AI",      "id": "zai/glm-5.1",                      "label": "GLM-5.1"},
    {"provider": "Z.AI",      "id": "zai/glm-5",                        "label": "GLM-5"},
    {"provider": "Z.AI",      "id": "zai/glm-5-turbo",                  "label": "GLM-5 Turbo"},
    {"provider": "Z.AI",      "id": "zai/glm-4.7",                      "label": "GLM-4.7"},
    {"provider": "Z.AI",      "id": "zai/glm-4.5",                      "label": "GLM-4.5"},
    {"provider": "Z.AI",      "id": "zai/glm-4.5-flash",                "label": "GLM-4.5 Flash"},
    # OpenRouter free-tier models — must appear in fallback list so they
    # are visible even when the tool-support filter in hermes_cli strips
    # them out of the live catalog (see #1426).
    {"provider": "OpenRouter", "id": "openrouter/elephant-alpha",                   "label": "Elephant Alpha (free)"},
    {"provider": "OpenRouter", "id": "openrouter/owl-alpha",                        "label": "Owl Alpha (free)"},
    {"provider": "OpenRouter", "id": "tencent/hy3-preview:free",                    "label": "Hy3 Preview (free)"},
    {"provider": "OpenRouter", "id": "nvidia/nemotron-3-super-120b-a12b:free",      "label": "Nemotron 3 Super (free)"},
    {"provider": "OpenRouter", "id": "arcee-ai/trinity-large-preview:free",         "label": "Trinity Large Preview (free)"},
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
    "minimax-cn": "MiniMax (China)",
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
    "nvidia": "NVIDIA NIM",
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
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",
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
    "nim": "nvidia",
    "nvidia-nim": "nvidia",
    "build-nvidia": "nvidia",
    "nemotron": "nvidia",
    # Legacy alias — earlier WebUI builds wrote ``provider: local`` for unknown
    # loopback endpoints, but ``local`` is not registered in
    # ``hermes_cli.auth.PROVIDER_REGISTRY``. Routing it through ``custom``
    # lets the agent's auxiliary client take the ``no-key-required``
    # OpenAI-compat path. See #1384.
    "local": "custom",
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


def _canonicalise_provider_id(name: object) -> str:
    """Normalise a provider id slug into a stable lowercase-hyphenated form.

    Folds underscores to hyphens and lowercases the result, so a user with
    ``providers.opencode_go.api_key`` in ``config.yaml`` and
    ``model.provider: opencode-go`` sees ONE provider group, not two
    (#1568). Then attempts alias resolution but only if the alias target
    is itself a known canonical id in ``_PROVIDER_DISPLAY`` —  this avoids
    converting ``x-ai`` (canonical in WebUI's data structures) to ``xai``
    (the hermes_cli alias target which the WebUI doesn't index by).

    Examples::

        opencode-go     -> opencode-go     (canonical, no change)
        opencode_go     -> opencode-go     (underscore folded)
        OpenCode-Go     -> opencode-go     (case folded)
        OPENCODE_GO     -> opencode-go     (both folded)
        z_ai            -> zai             (alias-resolved — zai is canonical)
        x-ai            -> x-ai            (preserved — x-ai is canonical)

    Empty input passes through as the empty string. Unknown ids preserve
    their normalised form.
    """
    if not name:
        return ""
    raw = str(name).strip().lower().replace("_", "-")
    if not raw:
        return ""
    # Already a canonical id known to _PROVIDER_DISPLAY/_PROVIDER_MODELS:
    # keep as-is to avoid round-tripping through aliases (e.g. x-ai → xai).
    if raw in _PROVIDER_DISPLAY or raw in _PROVIDER_MODELS:
        return raw
    # Try alias resolution. Only accept the result if it's itself a
    # canonical id in _PROVIDER_DISPLAY — that prevents aliases pointing
    # at non-canonical strings (legacy, hermes_cli-specific) from leaking
    # in. Falls back to the normalised input otherwise.
    resolved = _resolve_provider_alias(raw)
    if resolved and resolved.lower() in _PROVIDER_DISPLAY:
        return resolved.lower()
    return raw


# Well-known models per provider (used to populate dropdown for direct API providers)
_PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4.7", "label": "Claude Opus 4.7"},
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
        {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
        {"id": "deepseek-chat-v3-0324", "label": "DeepSeek V3 (legacy)"},
        {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner (legacy)"},
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
    "minimax-cn": [
        {"id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
        {"id": "MiniMax-M2.5", "label": "MiniMax M2.5"},
        {"id": "MiniMax-M2.1", "label": "MiniMax M2.1"},
        {"id": "MiniMax-M2", "label": "MiniMax M2"},
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
        {"id": "claude-opus-4-7", "label": "Claude Opus 4.7"},
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
        {"id": "glm-5.1",          "label": "GLM-5.1"},
        {"id": "glm-5",            "label": "GLM-5"},
        {"id": "kimi-k2.5",        "label": "Kimi K2.5"},
        {"id": "kimi-k2.6",        "label": "Kimi K2.6"},
        {"id": "deepseek-v4-pro",  "label": "DeepSeek V4 Pro"},
        {"id": "deepseek-v4-flash","label": "DeepSeek V4 Flash"},
        {"id": "mimo-v2-pro",      "label": "MiMo V2 Pro"},
        {"id": "mimo-v2-omni",     "label": "MiMo V2 Omni"},
        {"id": "mimo-v2.5-pro",    "label": "MiMo V2.5 Pro"},
        {"id": "mimo-v2.5",        "label": "MiMo V2.5"},
        {"id": "minimax-m2.7",     "label": "MiniMax M2.7"},
        {"id": "minimax-m2.5",     "label": "MiniMax M2.5"},
        {"id": "qwen3.6-plus",     "label": "Qwen3.6 Plus"},
        {"id": "qwen3.5-plus",     "label": "Qwen3.5 Plus"},
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
    # NVIDIA NIM — NVIDIA's inference platform
    "nvidia": [
        {"id": "nvidia/nemotron-3-super-120b-a12b", "label": "Nemotron 3 Super 120B"},
        {"id": "nvidia/nemotron-3-nano-30b-a3b", "label": "Nemotron 3 Nano 30B"},
        {"id": "nvidia/llama-3.3-nemotron-super-49b-v1.5", "label": "Llama 3.3 Nemotron Super 49B"},
        {"id": "qwen/qwen3-next-80b-a3b-instruct", "label": "Qwen3 Next 80B"},
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


def _format_nous_label(mid: str) -> str:
    """Turn a Nous Portal model id into a readable display label.

    Nous IDs are ``<vendor>/<model>[:<variant>]`` (e.g. ``anthropic/claude-opus-4.7``);
    drop the vendor namespace, prettify the model name with the same token
    rules as :func:`_format_ollama_label` (short acronyms uppercase, size
    suffixes uppercase, capitalize the rest), then append ``" (via Nous)"``
    so the entry is visually distinct from same-named models in other
    provider groups (e.g. direct Anthropic).

    Examples (matches the helper's actual output — labels are produced by
    :func:`_format_ollama_label`'s token rules, so 3-letter tokens like
    ``GPT`` and ``PRO`` render uppercase)::

        anthropic/claude-opus-4.7         -> Claude Opus 4.7 (via Nous)
        openai/gpt-5.4-mini               -> GPT 5.4 Mini (via Nous)
        google/gemini-3.1-pro-preview     -> Gemini 3.1 PRO Preview (via Nous)
        moonshotai/kimi-k2.6              -> Kimi K2.6 (via Nous)
        qwen/qwen3.5-plus-02-15           -> Qwen3.5 Plus 02 15 (via Nous)
        nvidia/nemotron-3-super-120b-a12b -> Nemotron 3 Super 120B A12b (via Nous)
        minimax/minimax-m2.5:free         -> MiniMax M2.5 (Free) (via Nous)
    """
    name_part = mid.split("/", 1)[-1] if "/" in mid else mid
    # MiniMax-CN ids come back lowercase on the live wire (`minimax-m2.5`) but
    # the curated label convention is mixed-case "MiniMax M2.5" — match that.
    if name_part.lower().startswith("minimax"):
        name_part = "MiniMax" + name_part[len("minimax"):]
    base = _format_ollama_label(name_part)
    return f"{base} (via Nous)"


# Soft cap on how many Nous Portal models surface in the picker dropdown.
# Above this count, _build_nous_featured_set() trims the visible list to
# ~_NOUS_FEATURED_TARGET entries; the full catalog is still returned to the
# client under ``extra_models`` so /model autocomplete covers everything.
# Caps reflect human scannability — a 25-row dropdown is the practical UX
# ceiling, and per-vendor sampling at 15 keeps the flagship shape visible
# without one vendor dominating.
_NOUS_FEATURED_THRESHOLD = 25
_NOUS_FEATURED_TARGET = 15

# Vendor-prefix priority order for featured selection. Lower index = picked
# earlier when sampling the live catalog. Reflects which vendors users have
# historically reached for first via Nous Portal (driven by the curated
# static list maintained in _PROVIDER_MODELS["nous"] and Discord feedback).
_NOUS_VENDOR_PRIORITY = (
    "anthropic", "openai", "google", "moonshotai", "z-ai",
    "minimax", "qwen", "x-ai", "deepseek", "stepfun",
    "xiaomi", "tencent", "nvidia", "arcee-ai",
)


def _build_nous_featured_set(
    live_ids: list[str],
    *,
    selected_model_id: str | None = None,
    target: int = _NOUS_FEATURED_TARGET,
) -> tuple[list[str], list[str]]:
    """Trim a Nous Portal catalog into a (featured, extras) split.

    ``featured`` is what the picker dropdown renders. ``extras`` is everything
    else — kept available so the slash-command `/model` autocomplete and the
    ``_dynamicModelLabels`` map cover the full catalog.

    Selection rules (in order, deterministic):

    1. Always include the user's currently-selected model if it's in the
       catalog (preserves selection stickiness — no orphan IDs in the
       dropdown after a refresh).
    2. Always include every entry from the curated static
       ``_PROVIDER_MODELS["nous"]`` list whose id maps onto a live id —
       those four are explicitly maintained as flagship picks.
    3. Top up to ``target`` by walking ``_NOUS_VENDOR_PRIORITY`` round-robin
       (one model per vendor each pass) so no vendor monopolises the slot
       budget. Within a vendor, the original ``live_ids`` order is preserved
       — that's the order Nous Portal returned, which approximates recency.

    Returns ``(featured_ids, extras_ids)`` — both lists are subsets of
    ``live_ids`` with disjoint membership and union equal to ``live_ids``.

    For catalogs ≤ ``_NOUS_FEATURED_THRESHOLD`` entries the function is a
    no-op: ``featured == live_ids``, ``extras == []``.
    """
    if not live_ids:
        return [], []
    if len(live_ids) <= _NOUS_FEATURED_THRESHOLD:
        return list(live_ids), []

    chosen: list[str] = []  # preserves insertion order
    chosen_set: set[str] = set()

    def _add(mid: str) -> None:
        if mid and mid not in chosen_set:
            chosen.append(mid)
            chosen_set.add(mid)

    # Rule 1: sticky selection. Strip "@nous:" prefix if present so we can
    # match against the live id space (which is bare "vendor/model").
    if selected_model_id:
        sel = selected_model_id
        if sel.startswith("@nous:"):
            sel = sel[len("@nous:"):]
        if sel in live_ids:
            _add(sel)

    # Rule 2: curated flagships. Extract the bare ids from the static list
    # entries (which are stored as "@nous:vendor/model").
    for static in _PROVIDER_MODELS.get("nous", []):
        sid = static.get("id", "")
        if sid.startswith("@nous:"):
            sid = sid[len("@nous:"):]
        if sid in live_ids:
            _add(sid)

    # Rule 3: vendor-priority round-robin top-up.
    by_vendor: dict[str, list[str]] = {}
    for mid in live_ids:
        if mid in chosen_set:
            continue
        vendor = mid.split("/", 1)[0] if "/" in mid else ""
        by_vendor.setdefault(vendor, []).append(mid)

    # Walk vendors in priority order, then any leftover vendors alphabetically.
    priority = list(_NOUS_VENDOR_PRIORITY)
    leftover = sorted(v for v in by_vendor if v not in set(priority))
    vendor_order = priority + leftover

    # Round-robin: one model per vendor per pass until we hit the target or
    # exhaust every bucket.
    while len(chosen) < target:
        added_this_pass = 0
        for vendor in vendor_order:
            if len(chosen) >= target:
                break
            bucket = by_vendor.get(vendor)
            if not bucket:
                continue
            _add(bucket.pop(0))
            added_this_pass += 1
        if added_this_pass == 0:
            break  # all buckets empty

    # Anything not chosen becomes extras (full-catalog completion surface).
    extras = [m for m in live_ids if m not in chosen_set]
    return chosen, extras


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


def _deduplicate_model_ids(groups: list[dict]) -> None:
    """Ensure every model ID across groups is globally unique.

    When multiple providers expose the same model ID (either bare names like
    ``gpt-5.4`` or slash-qualified IDs like ``google/gemma-4-27b``), the
    dropdown cannot distinguish them. This post-process detects such
    collisions and prefixes colliding entries with ``@provider_id:`` so the
    frontend can treat them as distinct options.

    The first occurrence (in provider-id order) is left unchanged for backward
    compatibility with sessions that already store the original bare/slash
    model name. If that provider is later removed from the config, the next
    cache rebuild re-runs dedup — the remaining provider becomes the sole
    occurrence and is left unchanged, so the session still matches.

    .. note::
       The "first occurrence wins" rule means the unchanged ID is not stable
       across config changes (adding, removing, or reordering providers).
       This is acceptable because the dedup runs on every cache rebuild,
       so sessions always resolve to the current canonical unchanged ID.

    The ``@provider_id:model`` format is consistent with the existing
    ``_apply_provider_prefix()`` function and is handled by
    ``resolve_model_provider()`` (rsplits on the last ``:`` to handle
    provider_ids that themselves contain ``:``).

    Operates in-place on *groups*.
    """
    if not groups:
        return

    # Collect {model_id: [(group_idx, model_idx), ...]} in alphabetical
    # provider_id order so that the "first occurrence stays unchanged" rule is
    # deterministic across config edits (adding/removing/reordering providers).
    sorted_group_indices = sorted(
        range(len(groups)),
        key=lambda i: groups[i].get("provider_id", ""),
    )
    id_map: dict[str, list[tuple[int, int]]] = {}
    for gi in sorted_group_indices:
        group = groups[gi]
        for mi, model in enumerate(group.get("models", [])):
            mid = str(model.get("id", "") or "").strip()
            # Skip IDs that are already provider-qualified.
            if not mid or mid.startswith("@"):
                continue
            id_map.setdefault(mid, []).append((gi, mi))

    # For any ID appearing in 2+ groups, prefix all but the first occurrence.
    # This handles N>2 providers correctly: the loop iterates over all
    # occurrences after the first, prefixing each with its own provider_id.
    for original_id, locations in id_map.items():
        if len(locations) < 2:
            continue
        for gi, mi in locations[1:]:
            group = groups[gi]
            model = group["models"][mi]
            pid = group.get("provider_id", "")
            model["id"] = f"@{pid}:{original_id}"
            provider_name = group.get("provider", pid)
            if model.get("label") != original_id:
                model["label"] = f"{model['label']} ({provider_name})"
            else:
                model["label"] = f"{original_id} ({provider_name})"


# ── Local-server provider preservation (#1625) ─────────────────────────────
#
# LM Studio, Ollama, llama.cpp, vLLM, TabbyAPI etc. are inference servers,
# not OpenAI-compatible proxies. They register models under their FULL path
# as the registry key (the HuggingFace-style "namespace/model" id, e.g.
# "qwen/qwen3.6-27b"). Stripping the namespace prefix would cause a registry
# miss and the server loads a brand-new instance with default settings,
# silently ignoring the user's tuned context length / parallel slots.
#
# This is distinct from OpenAI-compatible proxies (LiteLLM, OpenRouter relays)
# where stripping "openai/gpt-5.4" → "gpt-5.4" is the correct behavior.
#
# Detection has two layers:
#   1. Static set of known local-server provider names (canonical + common
#      custom-provider naming).
#   2. Loopback / private-host base_url heuristic: an OpenAI-compatible URL
#      pointing at 127.0.0.1, localhost, or a private IP block is almost
#      certainly a local model server, regardless of the provider name.
#      Reuses the same private-IP detection logic used elsewhere in
#      api/config.py for SSRF host trust.
_LOCAL_SERVER_PROVIDERS = {
    "lmstudio",     # canonical (in hermes_cli.models.CANONICAL_PROVIDERS)
    "lm-studio",    # alias used in some custom_providers configs (#1625 Opus NIT)
    "ollama",       # via custom_providers, common pattern
    "llamacpp",     # via custom_providers
    "llama-cpp",    # alias
    "vllm",         # via custom_providers
    "tabby",        # via custom_providers (TabbyAPI)
    "tabbyapi",     # alias
    "koboldcpp",    # local llama.cpp UI fork
    "textgen",      # text-generation-webui (oobabooga) OpenAI-compat extension
    "localai",      # LocalAI project (#1625 Opus NIT)
}


def _base_url_points_at_local_server(base_url: str) -> bool:
    """True if base_url's host is a loopback or private IP (likely local server).

    Reuses ipaddress.is_loopback / is_private / is_link_local — the same
    heuristic used in the `api/config.py` SSRF/credential-routing code.
    Errors (DNS failure, malformed URL) return False so callers fall back to
    the static-provider-name check.
    """
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse
        import ipaddress
        host = (urlparse(base_url).hostname or "").lower()
        if not host:
            return False
        # Plain-text "localhost" doesn't ipaddress-parse but is unambiguous.
        if host in ("localhost", "ip6-localhost", "ip6-loopback"):
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            # Not an IP literal — could be a hostname like "ollama.internal".
            # Don't try DNS resolution here (slow + ambient): only IP literals
            # and the `localhost` alias get the no-strip treatment via this path.
            return False
        return addr.is_loopback or addr.is_private or addr.is_link_local
    except Exception:
        return False


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

    # Heal legacy ``provider: local`` entries (written by WebUI < v0.50.252)
    # at read time. ``local`` is not a registered provider, so passing it
    # downstream raises a ``LOCAL_API_KEY`` error from the auxiliary client
    # mid-conversation when compression/vision/web-extract fires. Route
    # through ``custom`` instead — it takes the ``no-key-required``
    # OpenAI-compat path that local servers (Ollama, LM Studio, llama.cpp,
    # vLLM, TabbyAPI) actually use. See #1384.
    if isinstance(config_provider, str) and config_provider.strip().lower() == "local":
        config_provider = "custom"

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
            entry_model_ids = set()
            if entry_model:
                entry_model_ids.add(entry_model)
            entry_models = entry.get("models")
            if isinstance(entry_models, dict):
                entry_model_ids.update(
                    key.strip()
                    for key in entry_models.keys()
                    if isinstance(key, str) and key.strip()
                )
            if entry_name and model_id in entry_model_ids:
                provider_hint = "custom:" + entry_name.lower().replace(" ", "-")
                return model_id, provider_hint, entry_base_url or None

    # @provider:model format — explicit provider hint from the dropdown.
    # Route through that provider directly (resolve_runtime_provider will
    # resolve credentials in streaming.py).
    # Use rsplit to handle provider_ids that contain ':' (e.g. custom:my-key).
    # With rsplit, "@custom:my-key:model" → provider="custom:my-key", model="model".
    # BUT: model IDs that end in :free / :beta / :thinking collide with the
    # rsplit grammar (e.g. "@openrouter:tencent/hy3-preview:free" would split
    # into provider="openrouter:tencent/hy3-preview", model="free").  Guard
    # against that by falling back to split(":") when the rsplit result is not
    # a recognised provider (#1744).
    #
    # Edge case (#1776): for custom providers with the same suffix
    # ("@custom:my-key:some-model:free"), rsplit yields
    # provider_hint="custom:my-key:some-model", bare_model="free", and the
    # custom-prefix guard below skips the split-fallback. Detect the
    # over-split structurally — custom hints carry exactly one segment after
    # "custom:", so any provider_hint with 2+ colons that starts with
    # "custom:" has eaten part of the model name. Peel one segment back.
    if model_id.startswith("@") and ":" in model_id:
        inner = model_id[1:]
        provider_hint, bare_model = inner.rsplit(":", 1)
        if provider_hint.startswith("custom:") and provider_hint.count(":") >= 2:
            provider_hint, extra = provider_hint.rsplit(":", 1)
            bare_model = f"{extra}:{bare_model}"
        elif (provider_hint not in _PROVIDER_MODELS
                and provider_hint not in _PROVIDER_DISPLAY
                and not provider_hint.startswith("custom:")):
            provider_hint, bare_model = inner.split(":", 1)
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
        # NVIDIA NIM also serves models from multiple namespaces (qwen, nvidia, etc.)
        # and requires the full model path.
        _PORTAL_PROVIDERS = {"nous", "opencode-zen", "opencode-go", "nvidia"}
        if config_provider in _PORTAL_PROVIDERS:
            return model_id, config_provider, config_base_url
        # The OpenAI Codex provider uses a real base_url, but its default
        # ChatGPT endpoint cannot serve OpenRouter-style provider/model IDs.
        # Keep that narrow exception before the custom endpoint protection so
        # selecting openai/gpt-5.5 from OpenRouter under active Codex still
        # routes through OpenRouter. Other base_url-backed real providers may be
        # custom/proxy endpoints, so they must fall through to the branch below.
        if (
            config_provider == "openai-codex"
            and str(config_base_url or "").strip().rstrip("/")
            == "https://chatgpt.com/backend-api/codex"
            and prefix in _PROVIDER_MODELS
            and prefix != config_provider
        ):
            return model_id, "openrouter", None
        # If a custom endpoint base_url is configured, don't reroute through OpenRouter
        # just because the model name contains a slash (e.g. google/gemma-4-26b-a4b).
        # The user has explicitly pointed at a base_url, so trust their routing config.
        if config_base_url:
            # Local model servers (LM Studio, Ollama, llama.cpp, vLLM, TabbyAPI)
            # register models under their full HuggingFace-style id. Stripping the
            # prefix breaks the lookup and causes a fresh instance to load with
            # default settings, ignoring user-tuned context length / parallel slots.
            # See #1625. Detect either by canonical provider name OR by base_url
            # pointing at a loopback/private host.
            if (str(config_provider or "").lower() in _LOCAL_SERVER_PROVIDERS
                    or _base_url_points_at_local_server(config_base_url)):
                return model_id, config_provider, config_base_url
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


def model_with_provider_context(model_id: str, model_provider: str | None = None) -> str:
    """Return the model string to pass to ``resolve_model_provider()``.

    Session persistence keeps the user's selected provider in ``model_provider``
    instead of forcing every selected model into ``@provider:model`` form. At
    runtime, however, ``resolve_model_provider()`` still understands that
    internal disambiguation form, so use it only when the provider context is
    needed to route away from the current default provider.
    """
    model = str(model_id or "").strip()
    provider = str(model_provider or "").strip().lower()
    if not model or not provider or provider == "default" or model.startswith("@"):
        return model

    model_cfg = cfg.get("model", {})
    config_provider = None
    if isinstance(model_cfg, dict):
        config_provider = str(model_cfg.get("provider") or "").strip().lower()

    # If the selected provider is already the configured provider, leaving the
    # model bare preserves provider-specific base_url/proxy settings.
    if provider == config_provider:
        return model

    # OpenRouter selections with slash IDs are explicit provider/model paths.
    if provider == "openrouter":
        return f"@{provider}:{model}"

    # For non-OpenRouter slash IDs, keep the ID intact so existing custom/proxy
    # base_url routing and portal-provider handling remain in charge.
    if "/" in model:
        return model

    return f"@{provider}:{model}"


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
        # Never persist the bogus ``local`` value — see #1384. The auto-detect
        # block in ``_build_available_models_uncached`` was rewriting unknown
        # loopback hosts to ``provider: "local"``, which is not registered and
        # broke compression/vision mid-conversation. Route through ``custom``
        # so the agent's auxiliary client uses the ``no-key-required`` path.
        if persisted_provider.lower() == "local":
            persisted_provider = "custom"

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
_available_models_cache_source_fingerprint: dict | None = None
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
# Cache file lives inside STATE_DIR so each server instance (different
# HERMES_WEBUI_STATE_DIR / port) has its own file and test runs never
# pollute the production server's cache. Also works on macOS and Windows
# where /dev/shm does not exist.
def _current_webui_version() -> str | None:
    """Lazy resolver for the WebUI version, used to stamp the disk cache (#1633).

    `api.updates` imports `api.config` at module-load time, so we cannot
    `from api.updates import WEBUI_VERSION` at the top of this module without a
    circular import. Instead we resolve lazily on each cache load/save.

    Returns the runtime version string (e.g. ``v0.50.293``) when api.updates
    has been imported, or None if it isn't loaded yet (boot-time corner case
    before the server has finished initializing). A None return is treated as
    "do not stamp / do not validate" by the cache layer so cache reads/writes
    that happen during early init still work — the next call after init will
    stamp normally.
    """
    try:
        # Read attribute via dotted lookup so we don't add an import-time edge.
        import sys as _sys
        mod = _sys.modules.get('api.updates')
        if mod is None:
            return None
        v = getattr(mod, 'WEBUI_VERSION', None)
        return str(v) if v else None
    except Exception:
        return None


# Disk-cache schema version (#1633).
#
# Bumped any time the disk cache shape changes in a backward-incompatible way
# (e.g. new required field, renamed key). Independent of the WebUI version
# stamp — _webui_version forces a rebuild on every release; _schema_version
# guarantees that even if a future release accidentally reuses the same
# WebUI version string (or a debug build doesn't have a version), a structural
# change still invalidates the cache.
_MODELS_CACHE_SCHEMA_VERSION = 3


_models_cache_path = STATE_DIR / "models_cache.json"


def _get_auth_store_path() -> Path:
    """Return the auth.json path for the active Hermes profile."""
    try:
        from api.profiles import get_active_hermes_home as _gah

        return _gah() / "auth.json"
    except ImportError:
        return HOME / ".hermes" / "auth.json"


def _models_cache_file_fingerprint(path: Path) -> dict:
    """Return non-secret identity metadata for a cache dependency file.

    The /api/models response depends on config.yaml (model/provider defaults)
    and auth.json (active_provider + credential_pool).  The cache only needs
    cheap invalidation signals here, not file contents; never include secrets.
    """
    fingerprint = {"path": str(Path(path).expanduser())}
    try:
        st = Path(path).stat()
    except OSError:
        fingerprint["missing"] = True
        return fingerprint
    fingerprint["mtime_ns"] = st.st_mtime_ns
    fingerprint["size"] = st.st_size
    return fingerprint


def _models_cache_source_fingerprint() -> dict:
    """Return the current config/auth-store fingerprint for /api/models cache."""
    return {
        "config_yaml": _models_cache_file_fingerprint(_get_config_path()),
        "auth_json": _models_cache_file_fingerprint(_get_auth_store_path()),
    }


def _delete_models_cache_on_disk() -> None:
    try:
        os.unlink(str(_models_cache_path))
    except OSError:
        pass  # already absent


def _is_valid_models_cache(cache: object) -> bool:
    """Return True when a cache payload has the full /api/models shape.

    SHAPE-only check: validates structural correctness of an in-memory or
    on-disk cache. Use _is_loadable_disk_cache() for the strictness needed
    when reading from disk (it adds version-stamp invalidation per #1633).

    Kept loose so in-memory cache writes (which never touch disk and so don't
    need version stamping) can use this validator unchanged.
    """
    if not isinstance(cache, dict):
        return False
    if not {"active_provider", "default_model", "configured_model_badges", "groups"}.issubset(cache):
        return False
    active_provider = cache.get("active_provider")
    return (
        (active_provider is None or isinstance(active_provider, str))
        and isinstance(cache.get("default_model"), str)
        and isinstance(cache.get("configured_model_badges"), dict)
        and isinstance(cache.get("groups"), list)
    )


def _is_loadable_disk_cache(cache: object) -> bool:
    """Return True when an on-disk cache is safe to use after a process boot.

    Adds two checks on top of _is_valid_models_cache (#1633):
      1. ``_schema_version`` matches `_MODELS_CACHE_SCHEMA_VERSION`. A bumped
         schema version unconditionally invalidates older cache files.
      2. ``_webui_version`` matches the current runtime version. Forces a
         rebuild after every release so users see picker-shape fixes
         immediately, instead of waiting up to 24 hours for the TTL to expire.
         If the runtime version cannot be resolved (early-init edge case),
         skip this check rather than wedge the boot.

    Note: ``_webui_version`` is a string equality check, not a semver compare —
    two debug builds with the same `WEBUI_VERSION` string but different actual
    code wouldn't invalidate via this axis. ``_schema_version`` is the
    independent invalidation axis for breaking changes that lack a tag bump;
    bump it whenever the cache shape changes incompatibly.
    """
    if not _is_valid_models_cache(cache):
        return False
    if not isinstance(cache, dict):  # appease type-narrowing — already guarded above
        return False
    cached_schema = cache.get("_schema_version")
    if cached_schema != _MODELS_CACHE_SCHEMA_VERSION:
        # DEBUG telemetry per stage-294 absorption: makes "why did my cache
        # rebuild" investigations one log-grep away.
        logger.debug(
            "models cache rejected: schema=%r vs runtime=%r",
            cached_schema, _MODELS_CACHE_SCHEMA_VERSION,
        )
        return False
    runtime_version = _current_webui_version()
    if runtime_version is not None:
        cached_version = cache.get("_webui_version")
        if not isinstance(cached_version, str) or cached_version != runtime_version:
            logger.debug(
                "models cache rejected: webui_version=%r vs runtime=%r",
                cached_version, runtime_version,
            )
            return False
    cached_sources = cache.get("_source_fingerprint")
    runtime_sources = _models_cache_source_fingerprint()
    if cached_sources != runtime_sources:
        logger.debug(
            "models cache rejected: source_fingerprint=%r vs runtime=%r",
            cached_sources,
            runtime_sources,
        )
        return False
    return True


def _load_models_cache_from_disk() -> dict | None:
    """Load /api/models cache from disk if it exists and has current metadata.

    Adds the per-release version check from #1633: a cache stamped with a
    different WebUI version is treated as missing, forcing a fresh rebuild
    that picks up any picker-shape fixes shipped in the new release. The
    returned dict is the SHAPE-only cache (without the `_webui_version` /
    `_schema_version` stamps) so callers don't have to know about the
    on-disk metadata fields.
    """
    try:
        import json as _j

        if not _models_cache_path.exists():
            return None
        with open(_models_cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        if not _is_loadable_disk_cache(cache):
            return None
        # Strip the disk-only metadata before returning, so the in-memory
        # cache shape stays exactly what the rest of the code expects.
        return {
            "active_provider": cache["active_provider"],
            "default_model": cache["default_model"],
            "configured_model_badges": cache["configured_model_badges"],
            "groups": cache["groups"],
        }
    except Exception:
        return None


def _save_models_cache_to_disk(cache: dict) -> None:
    """Save cache to disk so it survives server restarts.

    Stamps the payload with `_webui_version` and `_schema_version` (#1633) so
    a subsequent process running a different WebUI version, or a future
    release that bumps the schema, will treat the file as invalid and
    rebuild from live provider data on its first /api/models call.

    The version stamp is omitted (not the literal None — the field is just
    skipped) when the runtime version cannot be resolved at the moment of
    save, which would happen only in a very early boot path before
    api.updates is loaded. _is_loadable_disk_cache treats a missing field as
    a mismatch (since runtime_version is non-None on every subsequent call),
    so this is safe — at worst we write one cache file that gets rejected
    once on the next boot.
    """
    try:
        if not _is_valid_models_cache(cache):
            return
        payload = {
            "_schema_version": _MODELS_CACHE_SCHEMA_VERSION,
            "_source_fingerprint": _models_cache_source_fingerprint(),
            "active_provider": cache["active_provider"],
            "default_model": cache["default_model"],
            "configured_model_badges": cache["configured_model_badges"],
            "groups": cache["groups"],
        }
        runtime_version = _current_webui_version()
        if runtime_version is not None:
            payload["_webui_version"] = runtime_version
        tmp = str(_models_cache_path) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.rename(tmp, str(_models_cache_path))
    except Exception:
        pass  # Non-fatal -- cache will rebuild on next call


def _get_fresh_memory_models_cache(now: float) -> dict | None:
    """Return a valid fresh in-memory /api/models cache, or clear stale shapes."""
    global _available_models_cache, _available_models_cache_ts, _available_models_cache_source_fingerprint
    if _available_models_cache is None:
        return None
    if (now - _available_models_cache_ts) >= _AVAILABLE_MODELS_CACHE_TTL:
        return None
    current_sources = _models_cache_source_fingerprint()
    if _available_models_cache_source_fingerprint != current_sources:
        logger.debug(
            "models memory cache rejected: source_fingerprint=%r vs runtime=%r",
            _available_models_cache_source_fingerprint,
            current_sources,
        )
        _available_models_cache = None
        _available_models_cache_ts = 0.0
        _available_models_cache_source_fingerprint = None
        return None
    if _is_valid_models_cache(_available_models_cache):
        return copy.deepcopy(_available_models_cache)
    _available_models_cache = None
    _available_models_cache_ts = 0.0
    _available_models_cache_source_fingerprint = None
    return None


def invalidate_models_cache():
    """Force the TTL cache for get_available_models() to be cleared.

    Call this after modifying config.cfg in-memory (e.g. in tests) so
    the next call to get_available_models() picks up the changes rather
    than returning a stale cached result.

    Also deletes the on-disk cache so that a subsequent cold build does
    not immediately reload a stale disk snapshot and skip the fresh build.
    This is essential for test isolation: without the disk delete, tests
    that call invalidate_models_cache() still get back the previous test's
    result from the disk cache because the disk hit is checked before the memory
    cache rebuild runs.
    """
    global _cache_build_in_progress, _available_models_cache, _available_models_cache_ts, _available_models_cache_source_fingerprint, _cache_build_cv
    with _available_models_cache_lock:
        _available_models_cache = None
        _available_models_cache_ts = 0.0
        _available_models_cache_source_fingerprint = None
        _cache_build_in_progress = False
        _cache_build_cv.notify_all()
        # Clear the credential pool cache too. The cache key is provider_id
        # only, so without this, tests (and live provider key edits) see a
        # stale CredentialPool from a prior auth_store payload — the test_
        # credential_pool_providers suite was hitting this directly.
        _CREDENTIAL_POOL_CACHE.clear()
    # Also delete the disk cache so the next cold build starts fresh.
    # Disk delete is outside the lock — file I/O shouldn't block other readers.
    _delete_models_cache_on_disk()


def invalidate_credential_pool_cache(provider_id: str):
    """Invalidate the credential pool cache for a specific provider.

    Used by the streaming layer's credential self-heal logic (#1401) to
    force a fresh credential pool load after re-reading auth.json.
    """
    global _CREDENTIAL_POOL_CACHE
    with _available_models_cache_lock:
        _CREDENTIAL_POOL_CACHE.pop(provider_id, None)
        _CREDENTIAL_POOL_CACHE.pop(_resolve_provider_alias(provider_id), None)


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
    global _available_models_cache, _available_models_cache_ts, _available_models_cache_source_fingerprint, _CREDENTIAL_POOL_CACHE
    with _available_models_cache_lock:
        _available_models_cache = None
        _available_models_cache_ts = 0.0
        _available_models_cache_source_fingerprint = None
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


def _read_live_provider_model_ids(provider_id: str) -> list[str]:
    """Return live model IDs from Hermes CLI for a provider, or [] on failure.

    WebUI's static ``_PROVIDER_MODELS`` table is only a fallback.  The agent CLI
    owns the provider registry and catalog-discovery logic, so ordinary picker
    groups should ask ``hermes_cli.models.provider_model_ids()`` first (#1240).
    Provider aliases are tried as a secondary lookup because WebUI keeps a few
    display-facing IDs (for example ``google`` / ``x-ai``) that Hermes CLI may
    normalize internally.
    """
    pid = str(provider_id or "").strip()
    if not pid:
        return []
    try:
        from hermes_cli.models import provider_model_ids as _provider_model_ids
    except Exception:
        return []

    candidates = [pid]
    try:
        alias = _resolve_provider_alias(pid)
    except Exception:
        alias = ""
    if alias and alias not in candidates:
        candidates.append(alias)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            live_ids = _provider_model_ids(candidate) or []
        except Exception:
            logger.debug("Failed to load %s models from hermes_cli", candidate)
            continue
        result: list[str] = []
        for mid in live_ids:
            mid_s = str(mid or "").strip()
            if mid_s and mid_s not in seen:
                seen.add(mid_s)
                result.append(mid_s)
        if result:
            return result
    return []


def _models_from_live_provider_ids(provider_id: str, live_ids: list[str]) -> list[dict]:
    """Convert Hermes CLI model ids into WebUI picker model entries."""
    formatter = _format_ollama_label if provider_id in ("ollama", "ollama-cloud") else None
    models: list[dict] = []
    seen: set[str] = set()
    for mid in live_ids:
        mid_s = str(mid or "").strip()
        if not mid_s or mid_s in seen:
            continue
        seen.add(mid_s)
        label = formatter(mid_s) if formatter else _get_label_for_model(mid_s, [])
        models.append({"id": mid_s, "label": label})
    return models


def _read_visible_codex_cache_model_ids() -> list[str]:
    """Return visible model slugs from Codex's local models_cache.json.

    The agent's provider_model_ids('openai-codex') intentionally filters IDs
    with ``supported_in_api: false``. Codex CLI still lists some of those models
    in its picker (notably ``gpt-5.3-codex-spark`` from #1680), so the WebUI
    merges this visible local catalog to stay in sync with Codex itself.
    """
    codex_home = Path(os.getenv("CODEX_HOME", "").strip() or (HOME / ".codex")).expanduser()
    cache_path = codex_home / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    sortable: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in ("hide", "hidden"):
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug.strip()))

    sortable.sort(key=lambda item: (item[0], item[1]))
    ordered: list[str] = []
    for _, slug in sortable:
        if slug not in ordered:
            ordered.append(slug)
    return ordered


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
    global _cache_build_in_progress, _available_models_cache, _available_models_cache_ts, _available_models_cache_source_fingerprint, _cache_build_cv
    # Config mtime check — must come before any config reads.
    # (Test #585 verifies _current_mtime appears before active_provider = None)
    try:
        _current_path = _get_config_path()
        _current_mtime = _current_path.stat().st_mtime
    except OSError:
        _current_path = _get_config_path()
        _current_mtime = 0.0
    if (
        (_current_mtime != _cfg_mtime or _current_path != _cfg_path)
        and not _cfg_has_in_memory_overrides()
    ):
        reload_config()
    # ── COLD PATH helper ─────────────────────────────────────────────────────
    # Extracted so it runs inside _available_models_cache_lock (RLock) to
    # prevent thundering-herd: only one thread rebuilds while others wait.
    def _build_available_models_uncached() -> dict:
        active_provider = None
        default_model = get_effective_default_model(cfg)
        groups = []

        def _norm_model_id(model_id: str) -> str:
            s = str(model_id or "").strip().lower()
            # Strip @provider: prefix (e.g., @custom:jingdong:GLM-5 -> GLM-5).
            # Defensive: if the last segment is empty (trailing colon, malformed
            # config), keep the original to avoid collapsing distinct IDs to ''.
            if s.startswith("@") and ":" in s:
                parts = s.split(":")
                s = parts[-1] or s
            # Strip provider/model prefix (e.g., custom:jingdong/GLM-5 -> GLM-5).
            # Same trailing-empty guard.
            if "/" in s:
                parts = s.split("/")
                s = parts[-1] or s
            return s.replace("-", ".")

        def _build_configured_model_badges() -> dict[str, dict[str, str]]:
            configured_entries: list[dict[str, str]] = []
            if active_provider and default_model:
                configured_entries.append(
                    {
                        "provider": active_provider,
                        "model": default_model,
                        "role": "primary",
                        "label": "Primary",
                    }
                )
            fallback_cfg = cfg.get("fallback_providers", [])
            if isinstance(fallback_cfg, list):
                for idx, entry in enumerate(fallback_cfg, start=1):
                    if not isinstance(entry, dict):
                        continue
                    provider = _resolve_provider_alias(entry.get("provider"))
                    model = str(entry.get("model") or "").strip()
                    if not provider or not model:
                        continue
                    configured_entries.append(
                        {
                            "provider": provider,
                            "model": model,
                            "role": "fallback",
                            "label": f"Fallback {idx}",
                        }
                    )

            option_ids = [m.get("id", "") for g in groups for m in g.get("models", []) if m.get("id")]
            option_lookup = {str(opt_id): str(opt_id) for opt_id in option_ids}
            option_provider_lookup = {
                str(m.get("id")): str(g.get("provider_id") or "")
                for g in groups
                for m in g.get("models", [])
                if m.get("id")
            }
            norm_lookup: dict[str, list[str]] = {}
            for opt_id in option_ids:
                norm_lookup.setdefault(_norm_model_id(opt_id), []).append(opt_id)

            badges: dict[str, dict[str, str]] = {}
            for entry in configured_entries:
                provider = entry["provider"]
                model = entry["model"]
                raw_candidates = []
                for candidate in (
                    model,
                    f"{provider}/{model}",
                    f"@{provider}:{model}",
                ):
                    if candidate and candidate not in raw_candidates:
                        raw_candidates.append(candidate)

                match_id = None
                exact_match = next((option_lookup[c] for c in raw_candidates if c in option_lookup), None)
                for candidate in raw_candidates:
                    if candidate in option_lookup and option_provider_lookup.get(candidate) == provider:
                        match_id = option_lookup[candidate]
                        break
                if match_id is None:
                    for candidate in raw_candidates:
                        normalized = _norm_model_id(candidate)
                        matches = norm_lookup.get(normalized, [])
                        if not matches:
                            continue
                        provider_match = next(
                            (m for m in matches if option_provider_lookup.get(m) == provider),
                            None,
                        )
                        match_id = provider_match or exact_match or matches[0]
                        if match_id:
                            break

                badge_payload = {"role": entry["role"], "label": entry["label"], "provider": provider}
                for candidate in raw_candidates:
                    candidate_provider = option_provider_lookup.get(candidate)
                    if candidate_provider and candidate_provider != provider:
                        continue
                    badges[candidate] = badge_payload
                if match_id:
                    badges[match_id] = badge_payload
            return badges

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
        auth_store_path = _get_auth_store_path()
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

            # Belt-and-braces: list_available_providers() is the primary signal
            # for OAuth providers, but its `authenticated` field can disagree
            # with `get_auth_status(<id>).logged_in` on some hermes_cli versions
            # (the two fields are computed via different code paths). When the
            # disagreement happens for Nous Portal, the Settings → Providers
            # card renders the live catalog (because api/providers.py iterates
            # all OAuth providers regardless of authentication state) but the
            # picker dropdown comes up empty — a confusing asymmetry reported
            # in #1567. Add Nous explicitly when get_auth_status agrees so the
            # picker stays in sync with the providers card.
            try:
                if _gas("nous").get("logged_in"):
                    detected_providers.add("nous")
            except Exception:
                logger.debug("Failed to check Nous Portal auth status")
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
                "XAI_API_KEY",
                "MISTRAL_API_KEY",
            ):
                val = os.getenv(k)
                if val:
                    all_env[k] = val
            if all_env.get("ANTHROPIC_API_KEY"):
                detected_providers.add("anthropic")
            if all_env.get("OPENAI_API_KEY"):
                detected_providers.add("openai")
                # openai-codex uses ChatGPT OAuth (not OPENAI_API_KEY) for its default endpoint.
                # Detecting it here lets users who have both credentials configured find it in the
                # picker without a manual config.yaml edit. Users without Codex OAuth will see
                # picker entries but hit auth errors at inference time (#1189 known limitation).
                detected_providers.add("openai-codex")
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
            if all_env.get("MINIMAX_API_KEY"):
                detected_providers.add("minimax")
            if all_env.get("MINIMAX_CN_API_KEY"):
                detected_providers.add("minimax-cn")
            if all_env.get("DEEPSEEK_API_KEY"):
                detected_providers.add("deepseek")
            if all_env.get("XAI_API_KEY"):
                detected_providers.add("x-ai")
            if all_env.get("MISTRAL_API_KEY"):
                detected_providers.add("mistralai")
            if all_env.get("OPENCODE_ZEN_API_KEY"):
                detected_providers.add("opencode-zen")
            if all_env.get("OPENCODE_GO_API_KEY"):
                detected_providers.add("opencode-go")

        # Also detect providers explicitly listed in config.yaml providers section.
        # A user may configure a provider key via config.yaml providers.<name>.api_key
        # without setting the corresponding env var. (#604)
        #
        # Canonicalise the id slug here so a user with ``providers.opencode_go``
        # (underscore variant) doesn't see TWO provider groups in the picker —
        # one for the canonical ``opencode-go`` from active_provider detection
        # and a phantom ``Opencode_Go`` group for the config-key form (#1568).
        # The same applies to mixed-case ids like ``OpenCode-Go`` and to
        # legitimate aliases like ``z-ai`` → ``zai``.
        _cfg_providers = cfg.get("providers", {})
        if isinstance(_cfg_providers, dict):
            for _pid_key in _cfg_providers:
                _canonical = _canonicalise_provider_id(_pid_key)
                if not _canonical:
                    continue
                if _canonical in _PROVIDER_MODELS or _canonical in _cfg_providers or _pid_key in _cfg_providers:
                    detected_providers.add(_canonical)

        def _normalize_base_url_for_match(value: object) -> str:
            url = str(value or "").strip().rstrip("/")
            if not url:
                return ""
            parsed_url = urlparse(url if "://" in url else f"http://{url}")
            scheme = (parsed_url.scheme or "http").lower()
            netloc = (parsed_url.netloc or parsed_url.path).lower().rstrip("/")
            path = parsed_url.path.rstrip("/")
            if not parsed_url.netloc:
                path = ""
            return f"{scheme}://{netloc}{path}"

        def _configured_provider_for_base_url(base_url: object) -> str:
            target = _normalize_base_url_for_match(base_url)
            if not target:
                return ""

            if isinstance(model_cfg, dict):
                model_base_url = _normalize_base_url_for_match(model_cfg.get("base_url"))
                if model_base_url == target:
                    provider_hint = _resolve_provider_alias(model_cfg.get("provider"))
                    if provider_hint:
                        return str(provider_hint).strip().lower()

            providers_cfg = cfg.get("providers", {})
            if isinstance(providers_cfg, dict):
                for provider_key, provider_cfg in providers_cfg.items():
                    if not isinstance(provider_cfg, dict):
                        continue
                    provider_base_url = _normalize_base_url_for_match(
                        provider_cfg.get("base_url")
                    )
                    if provider_base_url == target:
                        provider_hint = _resolve_provider_alias(provider_key)
                        if provider_hint:
                            return str(provider_hint).strip().lower()

            custom_providers_cfg = cfg.get("custom_providers", [])
            if isinstance(custom_providers_cfg, list):
                for entry in custom_providers_cfg:
                    if not isinstance(entry, dict):
                        continue
                    entry_base_url = _normalize_base_url_for_match(entry.get("base_url"))
                    if entry_base_url != target:
                        continue
                    entry_name = str(entry.get("name") or "").strip()
                    if entry_name:
                        return "custom:" + entry_name.lower().replace(" ", "-")
                    return "custom"

            return ""

        # 4. Fetch models from custom endpoint if base_url is configured
        auto_detected_models = []
        auto_detected_models_by_provider: dict[str, list[dict]] = {}
        if cfg_base_url:
            try:
                import ipaddress
                import urllib.request

                base_url = cfg_base_url.strip()
                if base_url.endswith("/v1"):
                    endpoint_url = base_url + "/models"
                else:
                    endpoint_url = base_url.rstrip("/") + "/v1/models"

                configured_provider = _configured_provider_for_base_url(base_url)
                provider = configured_provider or "custom"
                provider_from_config = bool(configured_provider)
                parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
                host = (parsed.netloc or parsed.path).lower()

                if parsed.hostname and not provider_from_config:
                    try:
                        addr = ipaddress.ip_address(parsed.hostname)
                        if addr.is_private or addr.is_loopback or addr.is_link_local:
                            if "ollama" in host or "127.0.0.1" in host or "localhost" in host:
                                provider = "ollama"
                            elif "lmstudio" in host or "lm-studio" in host:
                                provider = "lmstudio"
                            else:
                                # Unknown loopback/private endpoint: route through
                                # the generic ``custom`` provider so the agent's
                                # auxiliary client (compression, vision, web
                                # extraction) takes the OpenAI-compat custom path
                                # with ``no-key-required`` semantics. Writing
                                # ``provider: local`` here used to break
                                # compression mid-conversation because ``local``
                                # is not a registered provider in
                                # ``hermes_cli.auth.PROVIDER_REGISTRY`` — see #1384.
                                provider = "custom"
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

                # Build set of hostnames from custom_providers config — these are
                # user-explicitly configured endpoints and should not be blocked by SSRF.
                _ssrf_trusted_hosts: set[str] = set()
                # Also trust the base_url from model config (explicitly configured by user)
                if cfg_base_url:
                    _base_parsed = urlparse(cfg_base_url if "://" in cfg_base_url else f"http://{cfg_base_url}")
                    if _base_parsed.hostname:
                        _ssrf_trusted_hosts.add(_base_parsed.hostname.lower())
                _custom_providers_cfg = cfg.get("custom_providers", [])
                if isinstance(_custom_providers_cfg, list):
                    for _cp in _custom_providers_cfg:
                        if not isinstance(_cp, dict):
                            continue
                        _cp_base = (_cp.get("base_url") or "").strip()
                        if _cp_base:
                            _cp_parsed = urlparse(_cp_base if "://" in _cp_base else f"http://{_cp_base}")
                            if _cp_parsed.hostname:
                                _ssrf_trusted_hosts.add(_cp_parsed.hostname.lower())

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
                                ) or (parsed_url.hostname or "").lower() in _ssrf_trusted_hosts
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
                        auto_model = {"id": model_id, "label": label}
                        auto_detected_models.append(auto_model)
                        provider_key = provider.lower()
                        auto_detected_models_by_provider.setdefault(provider_key, []).append(auto_model)
                        detected_providers.add(provider_key)
            except Exception:
                logger.debug("Custom endpoint unreachable or misconfigured for provider: %s", provider)

        _custom_providers_cfg = cfg.get("custom_providers", [])
        _named_custom_groups: dict = {}
        if isinstance(_custom_providers_cfg, list):
            _seen_custom_ids = {m["id"] for m in auto_detected_models}
            for _cp in _custom_providers_cfg:
                if not isinstance(_cp, dict):
                    continue
                _cp_name = (_cp.get("name") or "").strip()
                _slug = ("custom:" + _cp_name.lower().replace(" ", "-")) if _cp_name else None

                # Collect model IDs: singular "model" field first, then "models" dict keys
                _cp_model_ids: list[str] = []
                _cp_model = _cp.get("model", "")
                if _cp_model:
                    _cp_model_ids.append(_cp_model)
                _cp_models_dict = _cp.get("models")
                if isinstance(_cp_models_dict, dict):
                    for _m_id in _cp_models_dict:
                        if isinstance(_m_id, str) and _m_id.strip() and _m_id not in _cp_model_ids:
                            _cp_model_ids.append(_m_id.strip())

                for _cp_model in _cp_model_ids:
                    if _cp_model and _cp_model not in _seen_custom_ids:
                        _cp_label = _get_label_for_model(_cp_model, [])
                        _seen_custom_ids.add(_cp_model)
                        if _slug:
                            if _slug not in _named_custom_groups:
                                _named_custom_groups[_slug] = (_cp_name, [])
                            detected_providers.add(_slug)
                            _cp_option_id = _cp_model
                            if active_provider != _slug and not _cp_option_id.startswith("@"):
                                _cp_option_id = f"@{_slug}:{_cp_option_id}"
                            _named_custom_groups[_slug][1].append(
                                {"id": _cp_option_id, "label": _cp_label}
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

        # Filter providers if providers.only_configured is set
        providers_cfg = cfg.get("providers", {})
        only_show_configured = providers_cfg.get("only_configured", False) if isinstance(providers_cfg, dict) else False
        if only_show_configured:
            configured_providers = set()
            if active_provider:
                configured_providers.add(active_provider)
            cfg_providers = cfg.get("providers", {})
            if isinstance(cfg_providers, dict):
                # Canonicalise here too — same rationale as #1568 detection
                # path. Without this, only_show_configured mode could
                # exclude detected ``opencode-go`` because configured_providers
                # only has the underscore-variant key from config.yaml.
                configured_providers.update(
                    _canonicalise_provider_id(k) or k for k in cfg_providers.keys()
                )
            # Only show providers that are both detected and configured
            detected_providers = detected_providers.intersection(configured_providers)

        # Post-collection dedup: re-canonicalise every entry so any path that
        # added a non-canonical id (mixed-case from auth-store, raw config-key,
        # legacy alias) gets folded onto the canonical key. Belt-and-braces for
        # #1568 — protects against future regressions in any of the ~25
        # `detected_providers.add(...)` callsites without auditing each one.
        # The fold is idempotent for already-canonical ids, so safe to run
        # unconditionally.
        if detected_providers:
            _canonicalised_detected = set()
            for _pid in detected_providers:
                _c = _canonicalise_provider_id(_pid) or _pid
                _canonicalised_detected.add(_c)
            detected_providers = _canonicalised_detected

        # 5. Build model groups
        if detected_providers:
            for pid in sorted(detected_providers):
                if pid.startswith("custom:") and pid in _named_custom_groups:
                    _nc_display, _nc_models = _named_custom_groups[pid]
                    # If all named-group models were deduped (already auto-detected
                    # from base_url /v1/models), fall back to auto-detected models
                    # instead of silently dropping the group (issue #1619).
                    #
                    # Per Opus advisor on stage-295: the load-bearing fix for the
                    # reporter's symptom is the api/routes.py:/api/models/live
                    # broadening to handle custom:* slugs. This block is defensive
                    # belt-and-braces — under current _named_custom_groups
                    # population logic (atomic add+append inside the same dedup
                    # guard at line ~2640), an empty list shouldn't reach here.
                    # Kept for future-proofing in case the population logic
                    # changes (e.g. supporting model-less custom_providers entries).
                    if not _nc_models:
                        _nc_models = auto_detected_models_by_provider.get(pid, [])
                    if _nc_models:
                        groups.append({"provider": _nc_display, "provider_id": pid, "models": _nc_models})
                    continue
                provider_name = _PROVIDER_DISPLAY.get(pid, pid.title())
                if pid == "openrouter":
                    # OpenRouter has two model surfaces:
                    #   (1) curated tool-supporting catalog via hermes_cli.models.fetch_openrouter_models()
                    #       — the canonical agent-ready list, applies a tool-support filter
                    #       (Kilo-Org/kilocode#9068) that hides image/completion-only models
                    #   (2) free-tier `:free` variants — newly-added models OpenRouter ships
                    #       experimentally that may not yet advertise `tools` in supported_parameters
                    #       (see #1426). These get filtered out of (1) but users want them visible.
                    #
                    # Strategy: take the live curated list as the base, then augment with a
                    # separate live-fetch of OpenRouter's /v1/models filtered to free-tier-only.
                    # Free-tier entries get a "(free)" label suffix so the picker is honest about
                    # what the user is selecting. Falls back to the static _FALLBACK_MODELS list
                    # when both live fetches fail (offline, transient API error, test env).
                    raw_models = []
                    seen_ids = set()
                    try:
                        from hermes_cli.models import (
                            fetch_openrouter_models as _fetch_or_models,
                        )
                        live_curated = _fetch_or_models() or []
                        for mid, _desc in live_curated:
                            if mid and mid not in seen_ids:
                                seen_ids.add(mid)
                                raw_models.append({"id": mid, "label": mid})
                    except Exception:
                        logger.warning("Failed to load OpenRouter curated catalog from hermes_cli")

                    # Free-tier live fetch — bypasses the tool-support filter so models
                    # OpenRouter has flagged free but hasn't yet annotated with tools=[]
                    # (or that have tools=[] but the user explicitly wants to try) appear.
                    try:
                        import urllib.request as _urlreq
                        _req = _urlreq.Request(
                            "https://openrouter.ai/api/v1/models",
                            headers={"Accept": "application/json"},
                        )
                        with _urlreq.urlopen(_req, timeout=8.0) as _resp:
                            _payload = json.loads(_resp.read().decode())
                        _free_count = 0
                        _free_cap = 30  # don't drown the picker — top 30 free tier
                        for _item in _payload.get("data", []) or []:
                            if not isinstance(_item, dict):
                                continue
                            _mid = str(_item.get("id") or "").strip()
                            if not _mid or _mid in seen_ids:
                                continue
                            _pricing = _item.get("pricing") or {}
                            try:
                                _is_free = (
                                    float(_pricing.get("prompt", "0") or "0") == 0
                                    and float(_pricing.get("completion", "0") or "0") == 0
                                )
                            except (TypeError, ValueError):
                                _is_free = False
                            # Also include explicit `:free` suffix variants
                            _is_free = _is_free or _mid.endswith(":free")
                            if not _is_free:
                                continue
                            _name = (
                                str(_item.get("name") or "").strip() or _mid
                            )
                            # Strip provider prefix from name for display, append (free)
                            _label = _name.split("/")[-1] if "/" in _name else _name
                            if "(free)" not in _label.lower():
                                _label = f"{_label} (free)"
                            seen_ids.add(_mid)
                            raw_models.append({"id": _mid, "label": _label})
                            _free_count += 1
                            if _free_count >= _free_cap:
                                break
                    except Exception:
                        logger.debug("OpenRouter free-tier live fetch unavailable; using fallback")

                    if not raw_models:
                        # Both live fetches failed — fall back to the curated static list.
                        # Deepcopy so dedup/prefix mutation downstream does not bleed
                        # into the module-level catalog.
                        raw_models = [
                            {"id": m["id"], "label": m["label"]}
                            for m in _FALLBACK_MODELS
                            if m.get("provider") == "OpenRouter"
                        ]

                    groups.append(
                        {
                            "provider": "OpenRouter",
                            "provider_id": "openrouter",
                            "models": raw_models,
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
                elif pid == "openai-codex":
                    # Codex account catalogs drift faster than WebUI releases
                    # (for example gpt-5.3-codex-spark in #1680). Ask the
                    # agent's Codex resolver first so /api/models inherits the
                    # live Codex API / local ~/.codex cache / static fallback
                    # chain instead of freezing the picker to WebUI's curated
                    # _PROVIDER_MODELS snapshot.
                    raw_models = []
                    codex_ids = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        codex_ids = [mid for mid in (_provider_model_ids("openai-codex") or []) if mid]
                    except Exception:
                        logger.warning("Failed to load OpenAI Codex models from hermes_cli")

                    for mid in _read_visible_codex_cache_model_ids():
                        if mid not in codex_ids:
                            codex_ids.append(mid)

                    raw_models = [
                        {"id": mid, "label": _get_label_for_model(mid, [])}
                        for mid in codex_ids
                    ]

                    if not raw_models:
                        raw_models = copy.deepcopy(_PROVIDER_MODELS.get("openai-codex", []))

                    if raw_models:
                        models = _apply_provider_prefix(raw_models, pid, active_provider)
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": models,
                            }
                        )
                elif pid == "nous":
                    # Nous Portal exposes a curated catalog (~30 models on most
                    # accounts, up to several hundred for enterprise tiers) via
                    # inference-api.nousresearch.com. Like ollama-cloud, we
                    # live-fetch through hermes_cli.models.provider_model_ids()
                    # rather than relying on the static four-entry list, which
                    # chronically drifts out of date (#1538).
                    #
                    # When the catalog exceeds _NOUS_FEATURED_THRESHOLD (~25)
                    # the picker dropdown gets a curated subset to stay
                    # scannable — the full list is still returned under
                    # "extra_models" for the slash-command autocomplete and
                    # the dynamic-label map (#1567). The optgroup label is
                    # decorated with the truncation count so users know more
                    # exists.
                    raw_models = []
                    extra_models: list[dict] = []
                    truncated_label_suffix = ""
                    live_fetch_failed = False
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        live_ids = _provider_model_ids("nous") or []
                    except Exception:
                        logger.warning("Failed to load Nous Portal models from hermes_cli")
                        live_ids = []
                        live_fetch_failed = True

                    if live_ids:
                        # Sticky-selection signal: prefer the explicitly-active
                        # model from cfg["model"]["model"] (what the user is
                        # currently using) over cfg["model"]["default"] (the
                        # configured default suggestion). Falls back to the
                        # latter so first-load before any selection still works.
                        _model_cfg = cfg.get("model", {})
                        _selected = (
                            (isinstance(_model_cfg, dict) and _model_cfg.get("model"))
                            or default_model
                            or None
                        )
                        featured_ids, extras_ids = _build_nous_featured_set(
                            live_ids,
                            selected_model_id=_selected,
                        )
                        # Prefix every live id with "@nous:" so routing matches
                        # the explicit-provider-hint branch of resolve_model_provider
                        # (same convention as the curated static list — see
                        # tests/test_nous_portal_routing.py for the invariant).
                        raw_models = [
                            {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                            for mid in featured_ids
                        ]
                        extra_models = [
                            {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                            for mid in extras_ids
                        ]
                        if extras_ids:
                            # Show "(15 of 397)" so the user understands the picker
                            # is showing a featured subset, not a broken short list.
                            truncated_label_suffix = (
                                f" ({len(featured_ids)} of {len(live_ids)})"
                            )
                    elif not live_fetch_failed:
                        # Live-fetch returned an empty list AND did not raise —
                        # the user is gated as authenticated by detection above
                        # but the catalog endpoint replied with no models.
                        # Showing the static 4-entry curated list here would
                        # contradict the providers card (which always shows
                        # the live catalog) — exactly the asymmetry #1567
                        # reports. Omit the Nous group entirely; the providers
                        # card already tells the truth, and a transient empty
                        # response will self-heal on the next cache rebuild.
                        logger.warning(
                            "Nous Portal authenticated but live-fetch returned empty — "
                            "omitting from picker (will retry on next cache rebuild)"
                        )
                    else:
                        # hermes_cli unavailable / raised — fall back to the
                        # curated 4-entry static list so the picker is never
                        # empty in this degraded state. This matches pre-#1538
                        # behaviour for environments without hermes_cli (test
                        # envs, package mismatches, isolated WebUI builds).
                        raw_models = copy.deepcopy(_PROVIDER_MODELS.get("nous", []))

                    if raw_models:
                        models = _apply_provider_prefix(raw_models, pid, active_provider)
                        # Apply the same prefix transform to extras so /model
                        # autocomplete sees consistent IDs across the two lists.
                        extras = _apply_provider_prefix(extra_models, pid, active_provider) if extra_models else []
                        group_entry = {
                            "provider": provider_name + truncated_label_suffix,
                            "provider_id": pid,
                            "models": models,
                        }
                        if extras:
                            group_entry["extra_models"] = extras
                        groups.append(group_entry)
                elif pid in _PROVIDER_MODELS or pid in cfg.get("providers", {}):
                    provider_cfg = cfg.get("providers", {}).get(pid, {})
                    raw_models = []

                    # User-configured model allowlists are explicit local
                    # source-of-truth and should still beat auto-discovery.
                    # Otherwise, ask Hermes CLI first so WebUI tracks the same
                    # live catalog as the agent/CLI picker; WebUI's static
                    # _PROVIDER_MODELS table is now a fallback only (#1240).
                    if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                        cfg_models = provider_cfg["models"]
                        if isinstance(cfg_models, dict):
                            raw_models = [{"id": k, "label": k} for k in cfg_models.keys()]
                        elif isinstance(cfg_models, list):
                            raw_models = [{"id": k, "label": k} for k in cfg_models]

                    if not raw_models:
                        raw_models = _models_from_live_provider_ids(
                            pid,
                            _read_live_provider_model_ids(pid),
                        )

                    if not raw_models:
                        raw_models = copy.deepcopy(_PROVIDER_MODELS.get(pid, []))

                    detected_models = auto_detected_models_by_provider.get(pid, [])
                    if detected_models and not raw_models:
                        raw_models = copy.deepcopy(detected_models)
                    models = _apply_provider_prefix(raw_models, pid, active_provider)
                    groups.append(
                        {
                            "provider": provider_name,
                            "provider_id": pid,
                            "models": models,
                        }
                    )
                else:
                    detected_models = auto_detected_models_by_provider.get(pid)
                    if detected_models:
                        models_for_group = copy.deepcopy(detected_models)
                    elif auto_detected_models:
                        models_for_group = copy.deepcopy(auto_detected_models)
                    else:
                        models_for_group = []
                    if models_for_group:
                        # Per-group deep copy so subsequent mutation by
                        # _deduplicate_model_ids() (which prefixes ids with
                        # @provider_id:) does not bleed into other groups
                        # that also fall through to this branch (#1511 root
                        # cause: multiple unconfigured providers all sharing
                        # the same auto_detected_models list reference would
                        # see every group's id rewritten to the FIRST
                        # provider's prefix, and labels accumulated every
                        # provider's name).
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": models_for_group,
                            }
                        )
        else:
            if default_model:
                label = _get_label_for_model(default_model, groups)
                groups.append(
                    {"provider": "Default", "provider_id": "default", "models": [{"id": default_model, "label": label}]}
                )

        if default_model:
            # Guard against provider-id values mistakenly stored in
            # ``model.default``. The injection logic below puts ANY string
            # into the picker as a fake option, so a stray provider id
            # surfaces as a self-referential phantom model labelled e.g.
            # ``Opencode GO`` — a 15th entry under the OpenCode Go group
            # (#1568). The user's misconfig is real, but the picker is
            # the wrong surface to surface it; we'd rather skip injection
            # and emit a warning so the underlying config issue is logged.
            _looks_like_provider_id = (
                str(default_model).strip().lower().replace("_", "-") in _PROVIDER_DISPLAY
                or _canonicalise_provider_id(default_model) in _PROVIDER_DISPLAY
            )
            if _looks_like_provider_id:
                logger.warning(
                    "Suspicious model.default value %r — looks like a provider id, "
                    "not a model id. Skipping picker injection. Check `model.default` "
                    "in config.yaml.",
                    default_model,
                )
            else:
                all_ids_norm = {_norm_model_id(m["id"]) for g in groups for m in g.get("models", [])}
                if _norm_model_id(default_model) not in all_ids_norm:
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

        # Post-process: ensure model IDs are globally unique across groups.
        # When multiple providers expose the same bare model ID, prefix
        # collisions with @provider_id: so the frontend can distinguish them.
        _deduplicate_model_ids(groups)

        # Defense-in-depth: drop any optgroup that ended up with zero models
        # — those are pure UI noise. A zero-model group typically means a
        # detection path added an id that has no static catalog AND the
        # live-fetch returned empty (#1568 — the user's
        # ``providers.opencode_go`` config-key path produced an empty
        # ``Opencode_Go`` group at the end of the picker before this fix).
        # Custom providers from ``custom_providers`` config are exempt —
        # they may legitimately render with zero entries when the user
        # hasn't filled in models yet but wants the card visible.
        groups = [
            g for g in groups
            if g.get("models")
            or (g.get("provider_id") or "").startswith("custom:")
        ]

        return {
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _build_configured_model_badges(),
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
            cached = _get_fresh_memory_models_cache(time.monotonic())
            if cached is not None:
                return cached

        # Reload config if changed
        if _cfg_changed:
            reload_config()
            _available_models_cache = None
            _available_models_cache_ts = 0.0
            _available_models_cache_source_fingerprint = None
            disk_groups = None

        # Serve from memory cache if fresh
        now = time.monotonic()
        cached = _get_fresh_memory_models_cache(now)
        if cached is not None:
            return cached

        # Cold path: disk cache hit — use it (fast, no lock contention)
        if disk_groups is not None:
            _available_models_cache = disk_groups
            _available_models_cache_ts = now
            _available_models_cache_source_fingerprint = _models_cache_source_fingerprint()
            _save_models_cache_to_disk(disk_groups)
            return copy.deepcopy(disk_groups)

        # Cold path: full rebuild — only one thread reaches here at a time
        with _cache_build_cv:
            _cache_build_in_progress = True
        try:
            result = _build_available_models_uncached()
        except Exception:
            # Always reset the flag so waiting threads don't block for 60s
            with _cache_build_cv:
                _cache_build_in_progress = False
                _cache_build_cv.notify_all()
            raise
        with _cache_build_cv:
            _available_models_cache = result
            _available_models_cache_ts = time.monotonic()
            _available_models_cache_source_fingerprint = _models_cache_source_fingerprint()
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


class StreamChannel:
    """Broadcast SSE events to every connected browser tab for a stream.

    While no tab is connected, events are buffered so the first/reconnected
    subscriber still receives the stream tail that arrived during the gap.
    Once one or more subscribers are attached, new events are broadcast to all
    of them instead of being consumed destructively by a single queue reader.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._offline_buffer: list[tuple[str, object]] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            # Replay buffered events to the new subscriber INSIDE the lock so a
            # concurrent put_nowait() can't broadcast a newer event before we
            # finish replaying the older buffered tail. queue.Queue.put_nowait
            # is non-blocking on an unbounded queue, so holding the lock here
            # is safe. Per Opus advisor on stage-292.
            for item in self._offline_buffer:
                q.put_nowait(item)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def put_nowait(self, item: tuple[str, object]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            if not subscribers:
                self._offline_buffer.append(item)
                return
            self._offline_buffer.clear()
        for q in subscribers:
            q.put_nowait(item)


def create_stream_channel() -> StreamChannel:
    return StreamChannel()


STREAMS: dict = {}
STREAMS_LOCK = threading.Lock()
CANCEL_FLAGS: dict = {}
AGENT_INSTANCES: dict = {}  # stream_id -> AIAgent instance for interrupt propagation
STREAM_PARTIAL_TEXT: dict = {}  # stream_id -> partial assistant text accumulated during streaming
STREAM_REASONING_TEXT: dict = {}  # stream_id -> reasoning trace accumulated during streaming (#1361 §A)
STREAM_LIVE_TOOL_CALLS: dict = {}  # stream_id -> live tool calls accumulated during streaming (#1361 §B)
SERVER_START_TIME = time.time()

# Agent cache: reuse AIAgent across messages in the same WebUI session so that
# _user_turn_count survives between turns.  This mirrors the gateway's
# _agent_cache pattern and is required for injectionFrequency: "first-turn".
# LRU cache with size limit to prevent memory bloat.
# All cache operations (get, set, move_to_end, popitem) are protected by
# SESSION_AGENT_CACHE_LOCK for thread safety in multi-threaded ASGI servers.
import collections
SESSION_AGENT_CACHE: collections.OrderedDict = collections.OrderedDict()  # LRU cache
SESSION_AGENT_CACHE_MAX = 50  # Maximum cached agents (each holds full conversation history)
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
    "show_tps": False,  # show tokens-per-second chip in assistant message headers
    "show_cli_sessions": False,  # merge CLI sessions from state.db into the sidebar
    "sync_to_insights": False,  # mirror WebUI token usage to state.db for /insights
    "check_for_updates": True,  # check if webui/agent repos are behind upstream
    "theme": "dark",  # light | dark | system
    "skin": "default",  # accent color skin: default | ares | mono | slate | poseidon | sisyphus | charizard
    "font_size": "default",  # small | default | large
    "language": "en",  # UI locale code; must match a key in static/i18n.js LOCALES
    "bot_name": os.getenv(
        "HERMES_WEBUI_BOT_NAME", "Hermes"
    ),  # display name for the assistant
    "sound_enabled": False,  # play notification sound when assistant finishes
    "notifications_enabled": False,  # browser notification when tab is in background
    "show_thinking": True,  # show/hide thinking/reasoning blocks in chat view
    "simplified_tool_calling": True,  # group tools/thinking into one quiet activity disclosure
    "api_redact_enabled": True,  # redact sensitive data (API keys, secrets) from API responses
    "sidebar_density": "compact",  # compact | detailed
    "auto_title_refresh_every": "0",  # adaptive title refresh: 0=off, 5/10/20=every N exchanges
    "busy_input_mode": "queue",  # behavior when sending while agent is running: queue | interrupt | steer
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
    "font_size": {"small", "default", "large"},
    "auto_title_refresh_every": {"0", "5", "10", "20"},
    "busy_input_mode": {"queue", "interrupt", "steer"},
}
_SETTINGS_BOOL_KEYS = {
    "onboarding_completed",
    "show_token_usage",
    "show_tps",
    "show_cli_sessions",
    "sync_to_insights",
    "check_for_updates",
    "sound_enabled",
    "notifications_enabled",
    "show_thinking",
    "simplified_tool_calling",
    "api_redact_enabled",
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
