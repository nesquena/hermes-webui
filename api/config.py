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
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# ── Basic layout ──────────────────────────────────────────────────────────────
import api.paths as _paths
from api.plugin_providers import (
    effective_provider_display_name as _effective_provider_display_name,
    is_plugin_model_provider as _is_plugin_model_provider,
)

HOME = _paths.HOME
_hermes_home_has_webui_state = _paths._hermes_home_has_webui_state
_platform_default_hermes_home = _paths._platform_default_hermes_home

# REPO_ROOT is the directory that contains this file's parent (api/ -> repo root)
REPO_ROOT = Path(__file__).parent.parent.resolve()

# ── Network config (env-overridable) ─────────────────────────────────────────
HOST = os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
PORT = int(os.getenv("HERMES_WEBUI_PORT", "8787"))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive int from the environment, falling back on bad input.

    Used for operator-tunable memory caps (issue #3506) so large installs can
    shrink the agent/session caches without editing source. A missing, empty,
    non-numeric, or below-``minimum`` value falls back to ``default`` so a typo
    can never disable a cache bound entirely.
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default

# ── TLS/HTTPS config (optional, env-overridable) ────────────────────────────
TLS_CERT = os.getenv("HERMES_WEBUI_TLS_CERT", "").strip() or None
TLS_KEY = os.getenv("HERMES_WEBUI_TLS_KEY", "").strip() or None
TLS_ENABLED = TLS_CERT is not None and TLS_KEY is not None

# ── State directory (env-overridable, never inside repo) ──────────────────────
_DEFAULT_HERMES_HOME = _platform_default_hermes_home()
_DEFAULT_STATE_HOME = Path(os.getenv("HERMES_HOME") or _DEFAULT_HERMES_HOME).expanduser()

STATE_DIR = (
    Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(_DEFAULT_STATE_HOME / "webui")))
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

# Keep custom provider /v1/models probes below the frontend's generic request
# timeout even when one upstream is slow or unreachable. The models cache rebuild
# path probes configured custom endpoints serially, so each provider needs a
# short hard cap and graceful degradation.
CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS = 5.0


def _env_mb_bytes(name: str, default_mb: int) -> int:
    """Parse an optional megabyte environment variable into bytes.

    Accepts values like ``200``, ``200MB``, or ``200MiB``. Invalid or
    non-positive values fall back to the provided default.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default_mb * 1024 * 1024
    m = re.match(r"^(\d+)\s*(?:m|mb|mib)?$", raw, re.IGNORECASE)
    if not m:
        logger.warning(
            "Invalid %s=%r; expected a positive integer in MB. Falling back to %sMB.",
            name,
            raw,
            default_mb,
        )
        return default_mb * 1024 * 1024
    value_mb = int(m.group(1))
    if value_mb <= 0:
        logger.warning(
            "Invalid %s=%r; expected a value greater than zero. Falling back to %sMB.",
            name,
            raw,
            default_mb,
        )
        return default_mb * 1024 * 1024
    return value_mb * 1024 * 1024


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
    hermes_home = os.getenv("HERMES_HOME", str(_DEFAULT_HERMES_HOME))
    candidates.append(Path(hermes_home).expanduser() / "hermes-agent")

    # 3. Sibling: <repo-root>/../hermes-agent
    candidates.append(REPO_ROOT.parent / "hermes-agent")

    # 4. Parent is the agent repo itself (repo cloned inside hermes-agent/)
    if (REPO_ROOT.parent / "run_agent.py").exists():
        candidates.append(REPO_ROOT.parent)

    # 5. ~/.hermes/hermes-agent (explicit common path)
    candidates.append(_DEFAULT_HERMES_HOME / "hermes-agent")

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
    for subdir, binary in (("bin", "python"), ("Scripts", "python.exe")):
        local_venv = REPO_ROOT / ".venv" / subdir / binary
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

# ── Thread-local env context ─────────────────────────────────────────────────
# Defined BEFORE the config-file section because _expand_env_vars() (below) calls
# _thread_local_env_value() and the import-time reload_config() runs during module
# load — a forward reference here would NameError on any startup config.yaml that
# uses a ${VAR} reference. Depends only on os + threading (both imported above).
_thread_ctx = threading.local()


def _thread_local_env_value(name: str, default: str = "") -> str:
    """Return thread-local profile env first, then process env, for provider reads."""
    env_name = str(name or "").strip()
    if not env_name:
        return default or ""

    thread_env = getattr(_thread_ctx, "env", {})
    if isinstance(thread_env, dict) and env_name in thread_env:
        thread_value = thread_env.get(env_name)
        if thread_value is None:
            return default or ""
        return str(thread_value)

    if bool(getattr(_thread_ctx, "block_process_env_fallback", False)):
        return default or ""

    return str(os.getenv(env_name, default or ""))


# ── Config file (reloadable -- supports profile switching) ──────────────────

def _expand_env_vars(obj):
    """Recursively expand ${VAR} references in config values.

    Uses the thread-local-first profile env lookup (_thread_local_env_value) so a
    ${VAR} reference in a profile's config.yaml resolves to that profile's value,
    and — critically — does NOT fall back to the server process os.environ when a
    profile-scoped readonly/background scope set block_process_env_fallback. The
    raw (unexpanded) dict is what gets cached; this expansion re-runs on every
    read against the current thread's scope, so a cross-profile credential
    (e.g. config api_key: ${ANTHROPIC_TOKEN}) can't be reconstructed from the
    server process env for a named profile that has no such value (#3961)."""
    if isinstance(obj, str):
        return re.sub(
            r"\${([^}]+)}",
            lambda m: _thread_local_env_value(m.group(1), m.group(0)),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


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
        return _DEFAULT_HERMES_HOME / "config.yaml"


_WEBUI_SESSION_SAVE_MODES = {"deferred", "eager"}
_DEFAULT_WEBUI_SESSION_SAVE_MODE = "deferred"
_DEFAULT_EXPERIMENTAL_CONFIG = {
    # Dormant first slice for the unified SessionDB migration. Runtime WebUI
    # session call sites must continue using the existing JSON paths unless a
    # later PR deliberately enables and wires this flag.
    "unified_session_db": False,
}
_DEFAULT_AGENT_PERSONALITIES = {
    # Mirrors the Hermes Agent CLI built-ins so WebUI's config-derived
    # /personality path is not empty for fresh profiles.
    "helpful": "You are a helpful, friendly AI assistant.",
    "concise": "You are a concise assistant. Keep responses brief and to the point.",
    "technical": "You are a technical expert. Provide detailed, accurate technical information.",
    "creative": "You are a creative assistant. Think outside the box and offer innovative solutions.",
    "teacher": "You are a patient teacher. Explain concepts clearly with examples.",
    "kawaii": "You are a kawaii assistant! Use cute expressions like (◕‿◕), ★, ♪, and ~! Add sparkles and be super enthusiastic about everything! Every response should feel warm and adorable desu~! ヽ(>∀<☆)ノ",
    "catgirl": "You are Neko-chan, an anime catgirl AI assistant, nya~! Add 'nya' and cat-like expressions to your speech. Use kaomoji like (=^･ω･^=) and ฅ^•ﻌ•^ฅ. Be playful and curious like a cat, nya~!",
    "pirate": "Arrr! Ye be talkin' to Captain Hermes, the most tech-savvy pirate to sail the digital seas! Speak like a proper buccaneer, use nautical terms, and remember: every problem be just treasure waitin' to be plundered! Yo ho ho!",
    "shakespeare": "Hark! Thou speakest with an assistant most versed in the bardic arts. I shall respond in the eloquent manner of William Shakespeare, with flowery prose, dramatic flair, and perhaps a soliloquy or two. What light through yonder terminal breaks?",
    "surfer": "Duuude! You're chatting with the chillest AI on the web, bro! Everything's gonna be totally rad. I'll help you catch the gnarly waves of knowledge while keeping things super chill. Cowabunga!",
    "noir": "The rain hammered against the terminal like regrets on a guilty conscience. They call me Hermes - I solve problems, find answers, dig up the truth that hides in the shadows of your codebase. In this city of silicon and secrets, everyone's got something to hide. What's your story, pal?",
    "uwu": "hewwo! i'm your fwiendwy assistant uwu~ i wiww twy my best to hewp you! *nuzzles your code* OwO what's this? wet me take a wook! i pwomise to be vewy hewpful >w<",
    "philosopher": "Greetings, seeker of wisdom. I am an assistant who contemplates the deeper meaning behind every query. Let us examine not just the 'how' but the 'why' of your questions. Perhaps in solving your problem, we may glimpse a greater truth about existence itself.",
    "hype": "YOOO LET'S GOOOO!!! I am SO PUMPED to help you today! Every question is AMAZING and we're gonna CRUSH IT together! This is gonna be LEGENDARY! ARE YOU READY?! LET'S DO THIS!",
}


def _apply_config_defaults(config_data: dict) -> None:
    """Populate documented default-only config keys in-place."""
    agent_cfg = config_data.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config_data["agent"] = agent_cfg

    personalities = agent_cfg.get("personalities")
    if isinstance(personalities, dict):
        merged = copy.deepcopy(_DEFAULT_AGENT_PERSONALITIES)
        merged.update(copy.deepcopy(personalities))
        agent_cfg["personalities"] = merged
    else:
        # Keep behavior aligned with CLI loader defaults: if personalities are
        # absent or malformed, replace the section entirely with built-ins.
        agent_cfg["personalities"] = copy.deepcopy(_DEFAULT_AGENT_PERSONALITIES)

    experimental = config_data.get("experimental")
    if not isinstance(experimental, dict):
        experimental = {}
        config_data["experimental"] = experimental
    for key, value in _DEFAULT_EXPERIMENTAL_CONFIG.items():
        experimental.setdefault(key, value)


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


def is_unified_session_db_enabled(config_data: dict | None = None) -> bool:
    """Return the dormant unified-session-db feature flag.

    The default is intentionally false so adding the JSON adapter cannot change
    runtime persistence until a later migration PR switches call sites.
    """
    active_cfg = config_data if isinstance(config_data, dict) else cfg
    experimental = active_cfg.get("experimental", {}) if isinstance(active_cfg, dict) else {}
    if not isinstance(experimental, dict):
        return False
    return experimental.get("unified_session_db") is True


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
            if config_path.exists():
                # Route the parse through the mtime-keyed cache (#4652) so an
                # unchanged config.yaml isn't re-parsed (~125ms+ on a large file)
                # on every reload_config() on the hot path (profile switch /
                # load_settings, #4662 Phase 2). We take the RAW cached dict and
                # run the env expansion HERE, pinned to the unscoped process-env
                # view (below) — never the helper's per-call expansion — for the
                # #798 TLS reason documented in the pin block.
                loaded = _load_yaml_config_file_raw(config_path)
                if isinstance(loaded, dict):
                    if loaded:
                        # The process-global _cfg_cache must reflect PROCESS-env
                        # expansion, never a profile-scoped block_process_env_fallback
                        # view — otherwise a reload that fires while a readonly/worker
                        # scope is active (profile alternation resolves _get_config_path
                        # to the named profile, #798 TLS) would bake under-expanded
                        # literal ${VAR}s into the shared cache and starve concurrent
                        # readers of the module-level `cfg` alias. Expansion re-runs
                        # per-read elsewhere; here we pin the cache to the unscoped view.
                        _prev_block = getattr(_thread_ctx, "block_process_env_fallback", False)
                        _prev_env = getattr(_thread_ctx, "env", None)
                        try:
                            _thread_ctx.block_process_env_fallback = False
                            _thread_ctx.env = {}
                            _cfg_cache.update(_expand_env_vars(loaded))
                        finally:
                            _thread_ctx.block_process_env_fallback = _prev_block
                            if _prev_env is None:
                                try:
                                    del _thread_ctx.env
                                except AttributeError:
                                    pass
                            else:
                                _thread_ctx.env = _prev_env
                    # Stamp _cfg_mtime whenever the file parsed to a dict — INCLUDING
                    # an empty {} config. The cache-update above is skipped for {} (it's
                    # a no-op), but _cfg_mtime MUST still be set or get_config()'s
                    # `current_mtime != _cfg_mtime` stale check fires on every call and
                    # spins reload_config() under _cfg_lock forever (a `{}` config from a
                    # freshly created/reset profile is reachable on the switch hot path).
                    # This matches master's pre-#4662 behavior (it entered the block for
                    # {} and set the mtime); the inner `if loaded:` only gates the no-op
                    # cache update, not the mtime stamp.
                    try:
                        _cfg_mtime = Path(config_path).stat().st_mtime
                    except OSError:
                        _cfg_mtime = 0.0
        except Exception:
            logger.debug("Failed to load yaml config from %s", config_path)
        _apply_config_defaults(_cfg_cache)
        _cfg_fingerprint = _fingerprint_config(_cfg_cache)
        # Bust the models cache so the next request sees fresh config values.
        # Only delete the disk cache when config has actually changed -- not on
        # first-ever load (when _old_cfg_mtime == 0.0, i.e. server start or
        # profile switch) -- preserving the disk cache so the next restart
        # still hits the fast path without a cold run.
        if _old_cfg_mtime != 0.0:
            _delete_models_cache_on_disk()


# Memoized parse cache for _load_yaml_config_file, keyed on (resolved path,
# st_mtime_ns, st_size). yaml.safe_load on an ~800-line / 24KB config.yaml costs
# ~125ms of pure-Python parsing, and hot read paths (e.g. GET /api/reasoning ->
# get_reasoning_status) call this on every request. Without a cache, a UI sync
# storm turns into a YAML-reparse storm (#4650). We cache the RAW parsed dict and
# re-run _expand_env_vars() on every call: env expansion is cheap, always returns
# a fresh structure (so callers that read-modify-save the result never corrupt the
# cache), and keeps ${VAR} references live against the current os.environ. The
# (mtime_ns, size) key means any on-disk edit (including by _save_yaml_config_file)
# is picked up on the next read.
_yaml_file_cache: dict[str, tuple] = {}
_yaml_file_cache_lock = threading.Lock()


def _load_yaml_config_file_raw(config_path: Path, *, _copy: bool = True) -> dict:
    """Return the RAW (un-env-expanded) parsed config dict, memoized on
    (resolved path, st_mtime_ns, st_size). Shared parse core for
    _load_yaml_config_file() and reload_config(): the former runs the helper's
    own per-call env expansion on the result; the latter must run expansion
    under its own process-env-pinned thread context (#798), so it takes the raw
    dict and expands it itself. Either way the file is parsed at most once per
    (mtime, size) — a UI sync storm can't turn into a YAML-reparse storm (#4650),
    and an unchanged config.yaml isn't reparsed on the profile-switch hot path
    (#4662 Phase 2).

    By default returns a deep copy so a caller can never mutate the shared cache
    entry (greptile #4741). Internal callers that immediately pass the result
    through _expand_env_vars() (which itself returns a fresh structure and never
    mutates its input) pass _copy=False to skip the redundant copy on the hot path.
    """
    try:
        import yaml as _yaml
    except ImportError:
        return {}

    try:
        st = config_path.stat()
    except OSError:
        # Missing or unstattable file — preserve the original "no config" contract.
        return {}

    cache_key = str(config_path)
    stat_key = (st.st_mtime_ns, st.st_size)
    with _yaml_file_cache_lock:
        cached = _yaml_file_cache.get(cache_key)
        if cached is not None and cached[0] == stat_key:
            raw = cached[1]
            if not isinstance(raw, dict):
                return {}
            return copy.deepcopy(raw) if _copy else raw

    # Cache miss / stale: parse off disk. Done outside the lock so a slow parse
    # doesn't serialize unrelated paths; a concurrent duplicate parse is harmless.
    try:
        loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to parse yaml config from %s", config_path)
        return {}

    raw = loaded if isinstance(loaded, dict) else {}
    with _yaml_file_cache_lock:
        _yaml_file_cache[cache_key] = (stat_key, raw)
    return copy.deepcopy(raw) if _copy else raw


def _load_yaml_config_file(config_path: Path) -> dict:
    # _copy=False: _expand_env_vars returns a fresh structure and never mutates
    # its input, so the env-expanded result is already cache-safe — no need to
    # deep-copy the raw dict first (keeps the /api/reasoning hot path cheap).
    raw = _load_yaml_config_file_raw(config_path, _copy=False)
    if not raw:
        return {}
    expanded = _expand_env_vars(raw)
    return expanded if isinstance(expanded, dict) else {}


def get_config_for_profile_home(profile_home: "Path | str | None") -> dict:
    """Return the config dict for an explicit profile home directory.

    The streaming agent runs on a detached worker thread that does NOT inherit
    the per-request thread-local profile context (set from the ``hermes_profile``
    cookie on the HTTP handler thread). On that worker, the ambient
    ``get_config()`` resolves through ``get_active_profile_name()`` which falls
    back to the process-global ``_active_profile`` (usually ``default``) — so a
    session running under a non-default profile would silently read the
    **default** profile's ``config.yaml`` for toolsets, prefill context, and
    fallback chains (issue #3294).

    This helper reads the config for a *known* profile home directly off disk,
    bypassing the thread-local resolver entirely. When ``profile_home`` matches
    the path the ambient resolver would pick (the common single-profile case),
    we return the cached ``get_config()`` to preserve in-memory overrides used
    by tests and runtime callers. Only when the session's profile home diverges
    from the ambient path do we read the session profile's file directly — a
    pure read with no global cache mutation, so it is race-free across
    concurrent sessions on different profiles.
    """
    if not profile_home:
        return get_config()
    try:
        target = Path(profile_home).expanduser()
    except Exception:
        return get_config()
    # If the ambient resolver already points at this profile home, defer to
    # get_config() so in-memory overrides (monkeypatched cfg) are honored. This
    # MUST run before the nonexistent-home guard below: a matching ambient home
    # whose directory doesn't physically exist yet (fresh install, monkeypatched
    # cfg) must still resolve through get_config(), not return {} (#4516 gate).
    try:
        if _get_config_path().parent == target:
            return get_config()
    except Exception:
        pass
    if not target.exists():
        return {}
    # Read the profile file directly and apply documented defaults locally so the
    # returned dict matches ambient get_config() shape (including built-in
    # personalities) without mutating any global cache state.
    profile_cfg = _load_yaml_config_file(target / "config.yaml")
    _apply_config_defaults(profile_cfg)
    return profile_cfg


def _config_for_yaml_save(config_data: dict) -> dict:
    """Return a YAML-safe config copy without runtime-only expanded defaults."""
    if not isinstance(config_data, dict):
        return {}
    data = copy.deepcopy(config_data)
    agent_cfg = data.get("agent")
    if isinstance(agent_cfg, dict):
        personalities = agent_cfg.get("personalities")
        if isinstance(personalities, dict):
            custom_personalities = {
                name: value
                for name, value in personalities.items()
                if _DEFAULT_AGENT_PERSONALITIES.get(name) != value
            }
            if custom_personalities:
                agent_cfg["personalities"] = custom_personalities
            else:
                agent_cfg.pop("personalities", None)
        if not agent_cfg:
            data.pop("agent", None)
    return data


def _save_yaml_config_file(config_path: Path, config_data: dict) -> None:
    try:
        import yaml as _yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to write Hermes config.yaml") from exc

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _yaml.safe_dump(_config_for_yaml_save(config_data), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # Invalidate the memoized parse for this path so the next read re-parses the
    # bytes we just wrote. mtime_ns+size keying normally catches edits, but a
    # WebUI save that preserves size with a coarse/unchanged mtime could otherwise
    # serve a stale dict (#4650 review) — evicting on our own write closes that gap.
    with _yaml_file_cache_lock:
        _yaml_file_cache.pop(str(config_path), None)


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
def _warn_state_dir_divergence(warn_prefix: str) -> None:
    """Check if SESSION_DIR is empty but a sibling state directory has session data.

    If the session store looks empty (no *.json files besides _index.json in SESSION_DIR,
    or SESSION_INDEX_FILE is absent/empty/contains only {}|[]|null), scan STATE_DIR.parent
    for sibling directories with a sessions/ child that has .json files.

    Prints a diagnostic warning if a divergence is detected, helping users identify when
    they may have switched launch methods and the HERMES_WEBUI_STATE_DIR env var differs.
    """
    try:
        # Check if session store is empty
        session_dir_empty = False

        # Check for .json files in SESSION_DIR (excluding _index.json)
        if SESSION_DIR.exists():
            json_files = [f for f in SESSION_DIR.glob("*.json") if f.name != "_index.json"]
            session_dir_empty = len(json_files) == 0
        else:
            session_dir_empty = True

        # Check if SESSION_INDEX_FILE is absent, empty, or contains only {}|[]|null
        index_file_empty = True
        if SESSION_INDEX_FILE.exists():
            try:
                with open(SESSION_INDEX_FILE, "r") as f:
                    content = f.read().strip()
                    if content and content not in ("{}", "[]", "null"):
                        index_file_empty = False
            except Exception:
                pass

        # If session store looks empty, scan for siblings with sessions
        if session_dir_empty and index_file_empty:
            state_parent = STATE_DIR.parent
            if state_parent.exists():
                for sibling in state_parent.iterdir():
                    if not sibling.is_dir() or sibling == STATE_DIR:
                        continue
                    sibling_sessions = sibling / "sessions"
                    if sibling_sessions.exists():
                        json_files = [f for f in sibling_sessions.glob("*.json") if f.name != "_index.json"]
                        if json_files:
                            # Found a sibling with session data
                            print(
                                f"{warn_prefix}  STATE_DIR is empty but a sibling state directory has session data.\n"
                                f"        Current : {STATE_DIR}\n"
                                f"        Sibling : {sibling}\n"
                                f"        If you switched launch methods (bootstrap.py / ctl.sh / systemd),\n"
                                f"        the active HERMES_WEBUI_STATE_DIR env var may differ from the\n"
                                f"        previous run. Set it explicitly to restore access:\n"
                                f"          export HERMES_WEBUI_STATE_DIR={sibling}",
                                flush=True,
                            )
                            return
    except Exception:
        pass


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

    try:
        _warn_state_dir_divergence(warn)
    except Exception:
        pass

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
MAX_FILE_BYTES = 400_000
MAX_UPLOAD_BYTES = _env_mb_bytes("HERMES_WEBUI_MAX_UPLOAD_MB", 20)

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

_LEGACY_CLI_TOOLSET_ALIASES = {
    # Older Hermes configs used "hermes" as the CLI composite toolset. Modern
    # Hermes Agent exposes that split as these two registered composites; keep
    # WebUI sessions usable when pointed at an older shared config.yaml.
    "hermes": ("hermes-cli", "hermes-api-server"),
}


def _normalize_cli_toolsets(toolsets):
    """Expand legacy CLI toolset aliases while preserving order and de-duping."""
    normalized = []
    seen = set()
    for name in toolsets or []:
        replacements = _LEGACY_CLI_TOOLSET_ALIASES.get(name, (name,))
        for replacement in replacements:
            if replacement and replacement not in seen:
                seen.add(replacement)
                normalized.append(replacement)
    return normalized


def _resolve_cli_toolsets(cfg=None):
    """Resolve CLI toolsets using the agent's _get_platform_tools() so that
    MCP server toolsets are automatically included, matching CLI behaviour."""
    if cfg is None:
        cfg = get_config()
    try:
        from hermes_cli.tools_config import _get_platform_tools
        return _normalize_cli_toolsets(_get_platform_tools(cfg, "cli"))
    except Exception:
        # Fallback: read raw list from config (MCP toolsets will be missing)
        return _normalize_cli_toolsets(cfg.get("platform_toolsets", {}).get("cli", _DEFAULT_TOOLSETS))

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
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M3",               "label": "MiniMax M3"},
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M2.7",             "label": "MiniMax M2.7"},
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M2.7-highspeed",   "label": "MiniMax M2.7 Highspeed"},
    # Z.AI / GLM
    {"provider": "Z.AI",      "id": "zai/glm-5.2",                      "label": "GLM-5.2"},
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
    "openai-api": "OpenAI API",
    "openai-codex": "OpenAI Codex",
    "xai-oauth": "xAI Grok OAuth",
    "copilot": "GitHub Copilot",
    "cursor-acp": "Cursor ACP",
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
    "xiaomi": "Xiaomi",
    "bedrock": "AWS Bedrock",
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
    "mimo": "xiaomi",
    "xiaomi-mimo": "xiaomi",
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


def _is_known_model_provider(provider_id: str) -> bool:
    """True when *provider_id* names a model provider WebUI can render.

    The credential pool (``auth.json`` → ``credential_pool``) stores keys for
    BOTH model providers (whose API keys belong in the model picker) and
    non-model platform plugins.  The Photon iMessage plugin, for example,
    writes ``photon`` / ``photon_project`` / ``photon_user`` pool entries that
    are messaging-platform credentials, not LLM API keys.  Only the former
    should surface as provider groups.

    Without this gate, #4247's pool-detection loop added *every* pool key to
    ``detected_providers``; unknown ids then fell through to the global
    auto-detected catalog and each phantom provider was painted with the full
    model list (#4324).  ``provider_id`` is expected to be the canonical slug
    (post ``_resolve_provider_alias``); the lookup is case-insensitive.

    A provider is "known" when it is a configured custom-provider slug
    (``custom:*``), appears in WebUI's static ``_PROVIDER_DISPLAY`` /
    ``_PROVIDER_MODELS`` tables, or is a registered model-provider plugin.
    """
    pid = (provider_id or "").strip().lower()
    if not pid:
        return False
    if pid.startswith("custom:"):
        return True
    if pid in _PROVIDER_DISPLAY or pid in _PROVIDER_MODELS:
        return True
    try:
        if _is_plugin_model_provider(pid):
            return True
    except Exception:
        # A transient failure here (import/IO hiccup in the plugin registry) makes
        # a real plugin-backed provider briefly look unknown and drop from the
        # picker until the next successful check. Surface at warning so it's
        # visible in default production logs rather than silently swallowed.
        logger.warning("plugin model-provider check failed for %s", pid, exc_info=True)
    return False


def _custom_provider_slug_from_name(name: object) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("custom:"):
        return raw
    # Keep name-derived custom provider slugs out of the @provider:model colon
    # grammar. Endpoint-derived slugs may still be custom:<host>:<port>, but a
    # friendly name like "Local (127.0.0.1:15721)" should not preserve ':'.
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return ""
    return "custom:" + slug


def _custom_provider_entries(config_obj: dict | None = None) -> list[dict]:
    source = config_obj if isinstance(config_obj, dict) else cfg
    entries = source.get("custom_providers", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _named_custom_provider_slugs(config_obj: dict | None = None) -> set[str]:
    return {
        slug
        for slug in (
            _custom_provider_slug_from_name(entry.get("name"))
            for entry in _custom_provider_entries(config_obj)
        )
        if slug
    }


def _named_custom_provider_slug_for_provider(
    provider: object,
    config_obj: dict | None = None,
) -> str:
    raw = str(provider or "").strip().lower()
    if not raw:
        return ""
    raw_suffix = raw.removeprefix("custom:")
    for entry in _custom_provider_entries(config_obj):
        entry_name = str(entry.get("name") or "").strip().lower()
        slug = _custom_provider_slug_from_name(entry_name)
        if not entry_name or not slug:
            continue
        if raw in {entry_name, slug} or raw_suffix == slug.removeprefix("custom:"):
            return slug
    return ""


def _resolve_configured_provider_id(
    provider: object,
    config_obj: dict | None = None,
    *,
    base_url: object = None,
    resolve_alias: bool = True,
) -> str:
    """Normalize a configured provider id.

    When ``resolve_alias`` is True (default, used for active-provider /
    badge surfaces), falls through to ``_resolve_provider_alias`` after the
    named-custom check. When False (used by ``resolve_model_provider``),
    preserves the raw provider value so downstream local-server detection
    (`_LOCAL_SERVER_PROVIDERS` membership in #1625) sees the original name
    like ``ollama`` / ``lm-studio`` rather than alias-collapsed ``custom`` /
    ``lmstudio``. The base-url-to-named-slug fallback still runs in both
    modes when applicable.

    See in-stage absorption note on stage-313 for the #1625 regression that
    motivated the ``resolve_alias`` flag.
    """
    named_slug = _named_custom_provider_slug_for_provider(provider, config_obj)
    if named_slug:
        return named_slug

    if not resolve_alias:
        raw = str(provider or "").strip().lower()
        if base_url and raw == "custom":
            by_base_url = _named_custom_provider_slug_for_base_url(base_url, config_obj)
            if by_base_url:
                return by_base_url
        return str(provider or "")

    resolved = _resolve_provider_alias(provider)
    if (
        base_url
        and str(resolved or "").strip().lower() == "custom"
    ):
        by_base_url = _named_custom_provider_slug_for_base_url(base_url, config_obj)
        if by_base_url:
            return by_base_url

    return resolved


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


def _custom_endpoint_slugs_for_base_url(value: object) -> set[str]:
    """Return custom provider slugs that WebUI may derive from a base URL.

    Model picker values for endpoint-discovered models have historically used
    both ``custom:<host>:<port>`` and ``custom:<host>-<port>`` forms. When the
    active config already names a local-server provider such as Ollama for that
    same base URL, those endpoint slugs are just UI routing hints and should
    resolve back to the configured provider rather than requiring a CUSTOM_* API
    key.
    """
    url = str(value or "").strip().rstrip("/")
    if not url:
        return set()
    parsed_url = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed_url.hostname or "").strip().lower()
    if not host:
        return set()
    port = parsed_url.port
    if port is None:
        scheme = (parsed_url.scheme or "http").lower()
        port = 443 if scheme == "https" else 80
    return {f"custom:{host}:{port}", f"custom:{host}-{port}"}


_LEGACY_CUSTOM_API_KEY_ENV_WARNED: set[str] = set()


def _api_key_env_name(provider_id: object) -> str:
    """Return the POSIX-safe default API-key env var for a custom provider id."""
    sanitized = re.sub(r"[^A-Za-z0-9]", "_", str(provider_id or "")).upper().strip("_")
    if not sanitized:
        sanitized = "CUSTOM"
    if not sanitized.startswith("CUSTOM_"):
        sanitized = f"CUSTOM_{sanitized}"
    return f"{sanitized}_API_KEY"


def _legacy_custom_api_key_env_name(provider_id: object) -> str:
    """Return the pre-#2541 custom-provider env hint shape, if any."""
    raw = str(provider_id or "").strip().upper()
    if not raw:
        return ""
    return f"{raw}_API_KEY"


def _lookup_custom_api_key_env(provider_id: object) -> str | None:
    """Look up sanitized custom-provider env first, then legacy broken shape."""
    env_name = _api_key_env_name(provider_id)
    api_key = _thread_local_env_value(env_name).strip()
    if api_key:
        return api_key

    legacy_env_name = _legacy_custom_api_key_env_name(provider_id)
    if legacy_env_name and legacy_env_name != env_name:
        legacy_key = _thread_local_env_value(legacy_env_name).strip()
        if legacy_key:
            if legacy_env_name not in _LEGACY_CUSTOM_API_KEY_ENV_WARNED:
                _LEGACY_CUSTOM_API_KEY_ENV_WARNED.add(legacy_env_name)
                logger.warning(
                    "Custom provider API key env var %s is deprecated; use %s instead",
                    legacy_env_name,
                    env_name,
                )
            return legacy_key
    return None


def _named_custom_provider_slug_for_base_url(
    base_url: object,
    config_obj: dict | None = None,
) -> str:
    target = _normalize_base_url_for_match(base_url)
    if not target:
        return ""
    for entry in _custom_provider_entries(config_obj):
        entry_base_url = _normalize_base_url_for_match(entry.get("base_url"))
        if entry_base_url != target:
            continue
        return _custom_provider_slug_from_name(entry.get("name")) or "custom"
    return ""


def _provider_is_known_or_configured(
    provider_id: object,
    config_obj: dict | None = None,
) -> bool:
    """True when ``provider_id`` is a provider Hermes recognizes (static registry)
    or the user has configured (named custom provider), decided from the STATIC
    registry + config state only — never from a live/cold catalog snapshot.

    This distinguishes a provider Hermes knows how to route (e.g. ``ollama-cloud``,
    whose model group simply isn't folded into the current cached catalog yet, or a
    named ``custom_providers`` entry) from a *genuinely unknown* one
    (``@removed:...`` that is in no registry and configured nowhere). The former's
    explicitly-qualified selection is preserved across a cold catalog; the latter
    falls back to the default so chat/start doesn't route to an unrecognized
    provider.

    DELIBERATE SCOPE (see the @provider:model guard in
    ``_resolve_compatible_session_model_state``): registry membership counts as
    "known" even when the user has no key configured for that built-in. We do NOT
    require authenticated-credential evidence here, on purpose. The only fully
    reliable "is this provider authenticated" signal is the live auth store /
    catalog rebuild — exactly the cost the caller's ``prefer_cached_catalog`` hot
    path avoids — and a cheap env/config-only credential check would mis-classify
    providers authenticated via OAuth/auth-store (``ollama-cloud`` among them),
    re-introducing the original silent-revert bug for them. A known-but-unconfigured
    pick is therefore kept and surfaces a clear run-time auth error rather than a
    silent swap to the default.

    Deliberately does NOT consult ``get_available_models()`` / the catalog groups,
    which are exactly what is cold here — re-deriving them live would defeat the
    ``prefer_cached_catalog`` hot-path win this guards.
    """
    raw = str(provider_id or "").strip().lower()
    if not raw:
        return False
    # Configured custom provider: a named slug in custom_providers, or any
    # ``custom`` / ``custom:<slug>`` form when custom_providers are defined.
    if _named_custom_provider_slug_for_provider(raw, config_obj):
        return True
    if raw == "custom" or raw.startswith("custom:"):
        return bool(_custom_provider_entries(config_obj))
    # Known first-party / built-in provider id (alias-resolved). Static registry
    # knowledge that is always available, so a live-discovery provider whose
    # catalog group is momentarily absent still counts as known.
    canonical = _resolve_provider_alias(raw)
    return (
        raw in _PROVIDER_DISPLAY
        or canonical in _PROVIDER_DISPLAY
        or raw in _PROVIDER_MODELS
        or canonical in _PROVIDER_MODELS
    )


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
    "openai-api": [
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
        {"id": "glm-5.2", "label": "GLM-5.2"},
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
        {"id": "MiniMax-M3", "label": "MiniMax M3"},
        {"id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
        {"id": "MiniMax-M2.7-highspeed", "label": "MiniMax M2.7 Highspeed"},
    ],
    "minimax-cn": [
        {"id": "MiniMax-M3", "label": "MiniMax M3"},
        {"id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
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
    # Cursor ACP — models served via Cursor CLI agent acp
    "cursor-acp": [
        {"id": "cursor/composer-2.5", "label": "Composer 2.5"},
        {"id": "cursor/composer-2", "label": "Composer 2"},
        {"id": "cursor/default", "label": "Default"},
        {"id": "cursor-acp", "label": "Cursor ACP"},
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
    # Xiaomi MiMo — direct API via api.xiaomimimo.com
    "xiaomi": [
        {"id": "mimo-v2.5-pro",    "label": "MiMo V2.5 Pro"},
        {"id": "mimo-v2.5",        "label": "MiMo V2.5"},
        {"id": "mimo-v2-pro",      "label": "MiMo V2 Pro"},
        {"id": "mimo-v2-omni",     "label": "MiMo V2 Omni"},
        {"id": "mimo-v2-flash",    "label": "MiMo V2 Flash"},
    ],
    # xAI — prefix used in OpenRouter model IDs (x-ai/grok-4-20)
    "x-ai": [
        {"id": "grok-4.20", "label": "Grok 4.20"},
    ],
    "xai-oauth": [
        {"id": "grok-4.20", "label": "Grok 4.20"},
    ],
    # AWS Bedrock — static fallback list; live model list is fetched via
    # hermes_cli.models.provider_model_ids("bedrock") when available (#2720).
    "bedrock": [
        {"id": "global.anthropic.claude-opus-4-7",                 "label": "Global Anthropic Claude Opus 4.7"},
        {"id": "global.anthropic.claude-opus-4-6-v1",              "label": "Global Anthropic Claude Opus 4.6"},
        {"id": "global.anthropic.claude-sonnet-4-6",               "label": "Global Anthropic Claude Sonnet 4.6"},
        {"id": "global.anthropic.claude-opus-4-5-20251101-v1:0",   "label": "GLOBAL Anthropic Claude Opus 4.5"},
        {"id": "global.anthropic.claude-sonnet-4-5-20250929-v1:0", "label": "Global Claude Sonnet 4.5"},
        {"id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",  "label": "Global Anthropic Claude Haiku 4.5"},
    ],
}


def _seed_provider_models_from_core() -> None:
    """Enrich existing provider model lists with missing IDs from hermes_cli.

    The core's _PROVIDER_MODELS is the authoritative curated list of agent-capable
    models per provider.  The WebUI's static dict above is a display-oriented copy
    (with {id, label} entries) that can go stale when new models are added to the
    core without a matching WebUI update.  This function bridges the gap by
    injecting any missing model IDs from the core into **existing** WebUI provider
    entries.

    Constrains seeding to providers already in the WebUI catalog — does NOT add
    brand-new providers.  Adding new vendors is a maintainer curation decision.
    Respects per-provider ID conventions (e.g. nous uses @nous:-prefixed IDs).

    Safe to call multiple times; only missing entries are added.  Silently no-ops
    if hermes_cli is not importable (standalone WebUI deployments).

    Must be called AFTER ``_get_label_for_model`` is defined (module-level
    invocation is at the bottom of this module, not here).
    """
    try:
        from hermes_cli.models import _PROVIDER_MODELS as _core_pm
    except ImportError:
        return

    # Build a canonical-id → WebUI-key lookup so that providers whose canonical
    # form differs between core and WebUI (e.g. core uses "xai" but WebUI
    # indexes by "x-ai") merge into the existing entry instead of creating a
    # duplicate (#4413).
    _webui_key_by_canonical: dict[str, str] = {}
    for _wk in _PROVIDER_MODELS:
        try:
            _canon = _resolve_provider_alias(_wk)
        except Exception:
            _canon = _wk
        if _canon not in _webui_key_by_canonical:
            _webui_key_by_canonical[_canon] = _wk

    for provider_id, core_models in _core_pm.items():
        if not isinstance(core_models, list):
            continue

        # Resolve the core's provider_id to the WebUI's key for this provider.
        webui_key = provider_id
        webui_list = _PROVIDER_MODELS.get(provider_id)
        if webui_list is None:
            try:
                _canon_pid = _resolve_provider_alias(provider_id)
            except Exception:
                _canon_pid = provider_id
            webui_key = _webui_key_by_canonical.get(_canon_pid, provider_id)
            webui_list = _PROVIDER_MODELS.get(webui_key)

        if webui_list is None:
            # Provider exists in core but not in the WebUI catalog.
            # Do NOT seed — adding new vendors is a maintainer curation
            # decision, not something the seeder should do implicitly (#4413).
            continue
        if not isinstance(webui_list, list):
            continue
        # Provider exists in both — inject missing model IDs.
        # Detect per-provider ID prefix convention (e.g. nous uses @nous:).
        # The merge must respect each provider's existing ID format rather
        # than injecting the core's raw IDs (#4413).
        _existing_ids_raw: list[str] = [
            (m.get("id") if isinstance(m, dict) else str(m)) or ""
            for m in webui_list
            if isinstance(m, dict) and m.get("id")
        ]
        _prefix = ""
        if _existing_ids_raw and all(i.startswith("@") and ":" in i for i in _existing_ids_raw):
            _prefix = _existing_ids_raw[0].split(":", 1)[0] + ":"

        def _strip_prefix(mid: str, prefix: str = _prefix) -> str:
            if prefix and mid.startswith(prefix):
                return mid[len(prefix):]
            return mid

        existing_ids = {
            _strip_prefix(mid).replace("-", ".").lower()
            for mid in _existing_ids_raw
        }
        for mid in core_models:
            if not isinstance(mid, str) or not mid.strip():
                continue
            normed = mid.strip().replace("-", ".").lower()
            if normed not in existing_ids:
                inject_id = (_prefix + mid.strip()) if _prefix else mid.strip()
                webui_list.append({
                    "id": inject_id,
                    "label": _get_label_for_model(mid.strip(), []),
                })


_AMBIENT_GH_CLI_MARKERS = frozenset({"gh_cli", "gh auth token"})

# Environment variable sources that are auto-detected and should be filtered
# when the token is a classic PAT (ghp_*) that Copilot API doesn't support.
# Note: COPILOT_GITHUB_TOKEN is NOT included here - it's user-specific config.
_AMBIENT_GH_ENV_SOURCES = frozenset({"env:github_token", "env:gh_token"})


def _is_ambient_gh_cli_entry(source: str, label: str, key_source: str) -> bool:
    """True when a credential-pool entry is a seeded gh-cli token rather than
    one the user added explicitly. Filter these so Copilot doesn't appear in
    the dropdown just because `gh` is installed on the system.

    Also filters GITHUB_TOKEN and GH_TOKEN env var entries, which are
    auto-detected from the environment and should not cause Copilot to appear
    in the picker when the token is a classic PAT (ghp_*) that Copilot API
    doesn't support.

    Note: COPILOT_GITHUB_TOKEN is NOT filtered - it's user-specific config
    that should always be respected.
    """
    source_lower = source.strip().lower()
    return (
        source_lower in _AMBIENT_GH_CLI_MARKERS
        or source_lower in _AMBIENT_GH_ENV_SOURCES
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
_MODEL_PICKER_OVERFLOW_THRESHOLD = _NOUS_FEATURED_THRESHOLD
_MODEL_PICKER_VISIBLE_TARGET = _NOUS_FEATURED_TARGET
_OPENROUTER_FREE_TIER_AUGMENT_CAP = 30

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


def _strip_picker_provider_hint(model_id: str) -> str:
    mid = str(model_id or "").strip()
    if mid.startswith("@") and ":" in mid:
        return mid[mid.index(":") + 1 :]
    return mid


def _model_matches_picker_selection(
    model_id: str,
    selected_model_id: str | None,
    provider_id: str | None = None,
) -> bool:
    selected = str(selected_model_id or "").strip()
    candidate = str(model_id or "").strip()
    if not selected or not candidate:
        return False
    if candidate == selected:
        return True

    selected_bare = _strip_picker_provider_hint(selected)
    candidate_bare = _strip_picker_provider_hint(candidate)
    if selected_bare != candidate_bare:
        return False

    selected_provider = ""
    if selected.startswith("@") and ":" in selected:
        selected_provider = selected[1 : selected.index(":")].lower()
    candidate_provider = str(provider_id or "").strip().lower()
    if candidate.startswith("@") and ":" in candidate:
        candidate_provider = candidate[1 : candidate.index(":")].lower()

    return not selected_provider or not candidate_provider or selected_provider == candidate_provider


def _split_picker_overflow_models(
    ordered_models: list[dict],
    *,
    selected_model_id: str | None = None,
    provider_id: str | None = None,
    threshold: int = _MODEL_PICKER_OVERFLOW_THRESHOLD,
    target: int = _MODEL_PICKER_VISIBLE_TARGET,
) -> tuple[list[dict], list[dict]]:
    """Split an ordered picker catalog into visible rows plus an overflow tail."""
    models = [copy.deepcopy(m) for m in (ordered_models or []) if isinstance(m, dict) and m.get("id")]
    if len(models) <= threshold:
        return models, []

    visible = models[:target]
    extras = models[target:]
    if not selected_model_id:
        return visible, extras

    if any(_model_matches_picker_selection(m.get("id", ""), selected_model_id, provider_id) for m in visible):
        return visible, extras

    for idx, model in enumerate(extras):
        if not _model_matches_picker_selection(model.get("id", ""), selected_model_id, provider_id):
            continue
        displaced = visible[-1]
        visible[-1] = model
        extras[idx] = displaced
        break
    return visible, extras


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

    # Collect {model_id: [(group_idx, bucket_name, model_idx), ...]} in
    # alphabetical provider_id order so that the "first occurrence stays
    # unchanged" rule is deterministic across config edits
    # (adding/removing/reordering providers). Include ``extra_models`` too:
    # slash-command resolution and picker filtering consume the full catalog.
    sorted_group_indices = sorted(
        range(len(groups)),
        key=lambda i: groups[i].get("provider_id", ""),
    )
    id_map: dict[str, list[tuple[int, str, int]]] = {}
    for gi in sorted_group_indices:
        group = groups[gi]
        for bucket_name in ("models", "extra_models"):
            for mi, model in enumerate(group.get(bucket_name, []) or []):
                mid = str(model.get("id", "") or "").strip()
                # Skip IDs that are already provider-qualified.
                if not mid or mid.startswith("@"):
                    continue
                id_map.setdefault(mid, []).append((gi, bucket_name, mi))

    # For any ID appearing in 2+ groups, prefix all but the first occurrence.
    # This handles N>2 providers correctly: the loop iterates over all
    # occurrences after the first, prefixing each with its own provider_id.
    for original_id, locations in id_map.items():
        if len(locations) < 2:
            continue
        for gi, bucket_name, mi in locations[1:]:
            group = groups[gi]
            model = group[bucket_name][mi]
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


def _is_local_server_provider(provider_id: str) -> bool:
    """True when provider_id names a local model server.

    Named custom providers resolve to ``custom:<slug>``. Treat those as local
    when the bare slug is one of the known local-server provider names too.
    """
    provider = str(provider_id or "").strip().lower()
    if provider in _LOCAL_SERVER_PROVIDERS:
        return True
    if provider.startswith("custom:"):
        return provider.removeprefix("custom:") in _LOCAL_SERVER_PROVIDERS
    return False


def _is_first_party_model(provider_id: str, model_id: str) -> bool:
    """True when ``model_id`` is listed in ``provider_id``'s own static catalog.

    Used to tell a *redundant* first-party prefix from an *intrinsic* routing
    prefix on a bare ``custom`` endpoint. ``openai/gpt-5.4`` → gpt-5.4 is a real
    OpenAI model, so ``openai/`` is a redundant leftover and strippable (#433).
    But ``bedrock/opus-4-6`` → opus-4-6 is NOT in bedrock's first-party catalog
    (those ids look like ``global.anthropic.claude-…``), so ``bedrock/`` is a
    vendor-routing segment a proxy needs whole (#3872). Returns False on any
    unknown provider or empty model so callers preserve the id.
    """
    provider = str(provider_id or "").strip().lower()
    model = str(model_id or "").strip()
    if not provider or not model:
        return False
    catalog = _PROVIDER_MODELS.get(provider)
    if not isinstance(catalog, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("id") == model
        for entry in catalog
    )


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


def _custom_slug_rest_looks_like_host_port(rest: str) -> bool:
    """True when ``custom:<rest>`` is an endpoint-style slug ``host:port``.

    WebUI sometimes derives ``custom:10.8.71.41:8080`` from ``base_url`` authority.
    The #1776 peel must not treat that middle colon as part of an eaten model
    segment — otherwise ``@custom:10.8.71.41:8080:Qwen3`` wrongly becomes model
    ``8080:Qwen3``.
    """
    rest = str(rest or "").strip()
    if ":" not in rest:
        return False
    host, port_s = rest.rsplit(":", 1)
    if not host or ":" in host:
        return False
    if not port_s.isdigit():
        return False
    try:
        port_n = int(port_s)
    except ValueError:
        return False
    if not (1 <= port_n <= 65535):
        return False
    try:
        import ipaddress

        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    hl = host.lower()
    if hl == "localhost":
        return True
    # Typical DNS hostname used as proxy slug (contains at least one label dot).
    if "." in host:
        return True
    return False


def _get_provider_base_url(provider_id):
    """Look up the configured base_url for a provider (e.g. lmstudio).

    Checks two locations, in order:
      1. ``cfg["providers"][<provider_id>]["base_url"]`` — the explicit
         per-provider override.
      2. ``cfg["model"]["base_url"]`` — falls back here when
         ``cfg["model"]["provider"] == provider_id``. This is the historical
         shape (the model block carries both the active provider AND the
         base URL for that provider in a single record).

    Returns the URL stripped of trailing ``/`` if configured, otherwise None.
    """
    prov_cfg = _get_provider_cfg(provider_id)
    explicit = (prov_cfg.get("base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    model_cfg = cfg.get("model", {}) or {}
    if isinstance(model_cfg, dict):
        model_provider = str(model_cfg.get("provider") or "").strip().lower()
        if model_provider == str(provider_id).strip().lower():
            model_base = (model_cfg.get("base_url") or "").strip().rstrip("/")
            if model_base:
                return model_base
    return None


def _get_providers_cfg() -> dict:
    providers_cfg = cfg.get("providers")
    return providers_cfg if isinstance(providers_cfg, dict) else {}


def _get_provider_cfg(provider_id) -> dict:
    provider_cfg = _get_providers_cfg().get(provider_id, {})
    return provider_cfg if isinstance(provider_cfg, dict) else {}


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
        config_base_url = model_cfg.get("base_url")
        config_provider = _resolve_configured_provider_id(
            model_cfg.get("provider"),
            cfg,
            base_url=config_base_url,
            resolve_alias=False,
        )

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
    # However, when the active provider is an explicit non-custom provider and
    # the requested model_id is the configured default model, that active
    # provider takes precedence over overlapping custom_providers[] entries.
    # Otherwise WebUI routes to custom:<name> instead of the intended endpoint
    # and can surface a 401 from the wrong provider (#1922).
    # For all other cases, preserve custom_providers[] routing for explicitly
    # selected custom provider models.
    _is_explicit_non_custom_provider = (
        config_provider is not None
        and config_provider != 'custom'
        and not config_provider.startswith('custom:')
    )
    _default_model = model_cfg.get('default') if isinstance(model_cfg, dict) else None
    # Owns model if it appears in the static catalog for the configured provider.
    _provider_models_set: set[str] = set()
    if (
        config_provider is not None
        and config_provider in _PROVIDER_MODELS
        and isinstance(_PROVIDER_MODELS[config_provider], list)
    ):
        _provider_models_set = {
            m.get('id', '') for m in _PROVIDER_MODELS[config_provider]
            if isinstance(m, dict) and isinstance(m.get('id'), str)
        }
    _skip_custom_providers = (
        _is_explicit_non_custom_provider
        and (
            # Guard 1: model is the configured default (existing behaviour).
            (_default_model is not None and model_id == _default_model)
            # Guard 2: model is owned by the configured non-custom provider.
            or model_id in _provider_models_set
        )
    )
    custom_providers = cfg.get('custom_providers', [])
    if isinstance(custom_providers, list) and not _skip_custom_providers:
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            entry_model = (entry.get('model') or '').strip()
            entry_name = (entry.get('name') or '').strip()
            entry_base_url = (entry.get('base_url') or '').strip()
            entry_model_ids = set()
            if entry_model:
                entry_model_ids.add(entry_model)
            entry_models = entry.get('models')
            if isinstance(entry_models, dict):
                entry_model_ids.update(
                    key.strip()
                    for key in entry_models.keys()
                    if isinstance(key, str) and key.strip()
                )
            if entry_name and model_id in entry_model_ids:
                provider_hint = _custom_provider_slug_from_name(entry_name)
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
    # over-split structurally — custom hints normally carry one slug segment
    # after ``custom:``. If ``provider_hint`` has extra ``:`` tokens because the
    # model ID contained tags like ``:free``, peel one segment back (#1776).
    #
    # Exception: ``custom:<ip-or-host>:<port>`` is a single logical slug derived
    # from OpenAI ``base_url`` authority and contains no eaten model segments.
    if model_id.startswith("@") and ":" in model_id:
        inner = model_id[1:]
        provider_hint, bare_model = inner.rsplit(":", 1)
        if provider_hint.startswith("custom:") and provider_hint.count(":") >= 2:
            _slug_rest = provider_hint[len("custom:"):]
            if not _custom_slug_rest_looks_like_host_port(_slug_rest):
                provider_hint, extra = provider_hint.rsplit(":", 1)
                bare_model = f"{extra}:{bare_model}"
        elif (provider_hint not in _PROVIDER_MODELS
                and provider_hint not in _PROVIDER_DISPLAY
                and not provider_hint.startswith("custom:")):
            provider_hint, bare_model = inner.split(":", 1)
        if (
            provider_hint.startswith("custom:")
            and config_base_url
            and _is_local_server_provider(config_provider)
            and provider_hint.lower() in _custom_endpoint_slugs_for_base_url(config_base_url)
        ):
            return bare_model, config_provider, config_base_url
        return bare_model, provider_hint, _get_provider_base_url(provider_hint)

    if "/" in model_id:
        prefix, bare = model_id.split("/", 1)
        # OpenRouter always needs the full provider/model path (e.g. openrouter/free,
        # anthropic/claude-sonnet-4.6). Never strip the prefix for OpenRouter.
        if config_provider == "openrouter":
            return model_id, "openrouter", config_base_url
        # Portal providers (Nous, OpenCode, NVIDIA NIM) serve models from multiple
        # upstream namespaces — check them BEFORE the prefix-strip branch so that
        # a model id whose prefix happens to equal the config_provider (e.g.
        # nvidia/nemotron-... on NVIDIA NIM) still keeps the full namespaced path.
        # The earlier ordering ran this guard AFTER the prefix-strip, so it never
        # fired in the prefix==config_provider case, causing HTTP 404 from the
        # portal which requires the full provider/model id (#2177; sibling of
        # #854 / #894 for Nous, where this guard was originally added).
        _PORTAL_PROVIDERS = {"nous", "opencode-zen", "opencode-go", "nvidia"}
        if config_provider in _PORTAL_PROVIDERS:
            return model_id, config_provider, config_base_url
        # If prefix matches config provider exactly, strip it and use that provider directly.
        # e.g. config=anthropic, model=anthropic/claude-... → bare name to anthropic API
        if config_provider and prefix == config_provider:
            return bare, config_provider, config_base_url
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
        # Cross-provider via custom_providers: if the prefix matches a named custom
        # provider entry (e.g. "ollama-local/glm-4.7-flash:q4_k_m"), route through it
        # instead of falling back to the default config provider. MUST come BEFORE
        # the config_base_url branch because many providers have a base_url set.
        if prefix and config_provider and prefix != config_provider:
            _custom_cfg = cfg.get("custom_providers", [])
            if isinstance(_custom_cfg, list):
                for _entry in _custom_cfg:
                    if isinstance(_entry, dict) and _entry.get("name", "").strip() == prefix:
                        _slug = _custom_provider_slug_from_name(prefix)
                        _base = (_entry.get("base_url") or "").strip()
                        return model_id, _slug, _base or None

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
            if (_is_local_server_provider(config_provider)
                    or _base_url_points_at_local_server(config_base_url)):
                return model_id, config_provider, config_base_url
            # Strip the provider prefix only when it's a known provider namespace
            # AND stripping is the right call for this configured provider:
            #
            #  * A real first-party provider pointed at an OpenAI-compatible proxy
            #    (e.g. provider=openai + base_url=litellm) expects the bare id —
            #    "openai/gpt-5.4" → "gpt-5.4", "google/gemma-…" → "gemma-…". This
            #    is the #433 behaviour and applies whenever config_provider is not
            #    the bare "custom" pseudo-provider.
            #
            #  * A *bare* ``custom`` provider (or a named ``custom:<slug>``) is a
            #    vendor-routing proxy (LiteLLM, Bedrock gateway, OpenRouter-style
            #    multi-vendor endpoint). There we strip ONLY a prefix that is
            #    redundant with the model's own first-party namespace
            #    ("openai/gpt-5.4" → gpt-5.4, since gpt-5.4 is genuinely an OpenAI
            #    model — #433). An intrinsic routing prefix whose bare id is NOT a
            #    first-party model of that namespace is kept whole, because the
            #    proxy routes on the full string and truncating it 403s "model not
            #    allowed": "bedrock/opus-4-6" stays intact (opus-4-6 ∉ bedrock
            #    catalog — #3872).
            #
            # Unknown prefixes (e.g. "zai-org/GLM-5.1" on DeepInfra) are intrinsic
            # to the model ID and always preserved (#548). The redundant-prefix
            # strip that matches the *configured* provider's own family is handled
            # earlier by the ``prefix == config_provider`` branch.
            _cp_lower = (config_provider or "").strip().lower()
            _is_custom = _cp_lower == "custom" or _cp_lower.startswith("custom:")
            if prefix in _PROVIDER_MODELS and (
                not _is_custom or _is_first_party_model(prefix, bare)
            ):
                return bare, config_provider, config_base_url
            # Intrinsic / unknown prefix — pass the full model_id through unchanged.
            return model_id, config_provider, config_base_url

        # If prefix does NOT match config provider, the user picked a cross-provider model
        # from the OpenRouter dropdown (e.g. config=anthropic but picked openai/gpt-5.4-mini).
        # In this case always route through openrouter with the full provider/model string.
        # Exception (#4210): a custom provider (bare ``custom`` or named ``custom:<slug>``)
        # is a vendor-routing proxy, not a first-party provider — its model ids commonly
        # contain a known-provider prefix that the proxy uses for upstream routing, not
        # an OpenRouter dropdown selection. Keep the request on the custom provider; the
        # base_url-set sibling of this exception lives earlier in the ``config_base_url``
        # branch (#3872).
        _cp_lower_cross = (config_provider or "").strip().lower()
        _is_custom_cross = _cp_lower_cross == "custom" or _cp_lower_cross.startswith("custom:")
        if prefix in _PROVIDER_MODELS and prefix != config_provider and not _is_custom_cross:
            return model_id, "openrouter", None

    return model_id, config_provider, config_base_url


def resolve_custom_provider_connection(provider_id: str) -> tuple[str | None, str | None]:
    """Return (api_key, base_url) for a named ``custom:*`` provider.

    Supports ``custom_providers[].api_key`` as either a literal key or
    ``${ENV_VAR}``, and ``custom_providers[].key_env`` as an env-var hint.
    Returns ``(None, None)`` when no named custom provider matches.
    """
    pid = str(provider_id or "").strip().lower()
    if not pid.startswith("custom:"):
        return None, None

    def _slugify(value: str) -> str:
        s = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
        while "--" in s:
            s = s.replace("--", "-")
        return s.strip("-")

    slug = _slugify(pid.split(":", 1)[1].strip())
    if not slug:
        return None, None

    # Read the live config snapshot to avoid stale module-level cache edge
    # cases after profile switches or runtime config edits.
    cfg_data = get_config()

    def _resolve_key(raw_api_key, raw_key_env, provider_hint=None) -> str | None:
        api_key = None
        if raw_api_key is not None:
            key_text = str(raw_api_key).strip()
            if key_text.startswith("${") and key_text.endswith("}") and len(key_text) > 3:
                api_key = _thread_local_env_value(key_text[2:-1]).strip() or None
            elif key_text:
                api_key = key_text
        if not api_key:
            key_env = str(raw_key_env or "").strip()
            if key_env:
                api_key = _thread_local_env_value(key_env).strip() or None
        if not api_key and provider_hint:
            api_key = _lookup_custom_api_key_env(provider_hint)
        return api_key

    custom_providers = cfg_data.get("custom_providers", [])
    if not isinstance(custom_providers, list):
        custom_providers = []

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        entry_slug = _slugify(name)
        if entry_slug != slug:
            continue

        base_url = str(entry.get("base_url") or "").strip() or None
        api_key = _resolve_key(entry.get("api_key"), entry.get("key_env"), pid)
        return api_key, base_url

    # If exactly one custom provider is configured, use it as a pragmatic
    # fallback for mismatched slugs (e.g. punctuation differences).
    if len(custom_providers) == 1 and isinstance(custom_providers[0], dict):
        entry = custom_providers[0]
        return (
            _resolve_key(entry.get("api_key"), entry.get("key_env"), pid),
            str(entry.get("base_url") or "").strip() or None,
        )

    # Fallbacks for setups that don't use custom_providers names directly.
    providers_cfg = cfg_data.get("providers", {})
    provider_specific = providers_cfg.get(pid, {}) if isinstance(providers_cfg, dict) else {}
    provider_custom = providers_cfg.get("custom", {}) if isinstance(providers_cfg, dict) else {}

    model_cfg = cfg_data.get("model", {})
    model_provider = str(model_cfg.get("provider") or "").strip().lower() if isinstance(model_cfg, dict) else ""

    fallback_base = None
    for candidate in (provider_specific, provider_custom, model_cfg):
        if isinstance(candidate, dict):
            _base = str(candidate.get("base_url") or "").strip()
            if _base:
                fallback_base = _base
                break

    fallback_key = None
    if isinstance(provider_specific, dict):
        fallback_key = _resolve_key(provider_specific.get("api_key"), provider_specific.get("key_env"), pid)
    if not fallback_key and isinstance(provider_custom, dict):
        fallback_key = _resolve_key(provider_custom.get("api_key"), provider_custom.get("key_env"), pid)
    if not fallback_key and isinstance(model_cfg, dict) and model_provider in {"custom", pid, slug}:
        fallback_key = _resolve_key(model_cfg.get("api_key"), model_cfg.get("key_env"), pid)

    if fallback_key or fallback_base:
        return fallback_key, fallback_base or None

    return None, None


# Subprocess ACP transports (Cursor/Copilot CLI). Model IDs often contain '/'
# but must still route via explicit @provider:model so they do not fall through
# to the configured default HTTP provider (e.g. openai-codex).
_ACP_SUBPROCESS_PROVIDERS = frozenset({"cursor-acp", "copilot-acp"})


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

    # ACP subprocess providers always need the explicit hint — their slash IDs
    # are not OpenRouter paths and must not inherit config_provider routing.
    if provider in _ACP_SUBPROCESS_PROVIDERS:
        return f"@{provider}:{model}"

    # If the selected provider is already the configured provider, leaving the
    # model bare preserves provider-specific base_url/proxy settings.
    if provider == config_provider:
        return model

    # OpenRouter selections with slash IDs are explicit provider/model paths.
    if provider == "openrouter":
        return f"@{provider}:{model}"

    # Explicit providers configured in config.yaml (for example local llama.cpp,
    # Ollama, LM Studio, vLLM, or other OpenAI-compatible endpoints) must keep
    # their provider hint even when the model ID is HuggingFace-style and
    # contains '/'. Otherwise a selected local model such as
    # 'unsloth/gemma-4-12b-it-GGUF:UD-Q4_K_XL' inherits the default provider
    # (e.g. openai-codex) and is sent to the wrong backend.
    providers_cfg = cfg.get("providers") if isinstance(cfg, dict) else {}
    if isinstance(providers_cfg, dict) and provider in providers_cfg:
        return f"@{provider}:{model}"

    # For non-OpenRouter slash IDs without an explicit configured provider,
    # keep the ID intact so existing custom/proxy base_url routing and
    # portal-provider handling remain in charge.
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
# Keep this WebUI-visible set aligned with hermes-agent#29248.
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


def _strip_provider_hint_for_reasoning(model_id: str) -> str:
    """Remove WebUI routing hints before provider-specific capability lookup."""
    model = str(model_id or "").strip()
    if model.startswith("@") and ":" in model:
        return model.split(":", 1)[1]
    return model


def _reasoning_name_candidates(model_id: str) -> list[str]:
    """Return normalized model-name candidates for heuristic capability checks."""
    bare = str(model_id or "").strip().lower().rsplit("/", 1)[-1]
    if not bare:
        return []

    candidates: list[str] = []

    def _add(value: str) -> None:
        candidate = str(value or "").strip().lower()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _add(bare)

    dot_parts = [part for part in bare.split(".") if part]
    if len(dot_parts) > 1:
        # Try progressively stripping dot-separated vendor namespaces so inputs like
        # "moonshotai.kimi-k2.5" and "vendor.deepseek.v3.2" both surface the real
        # model family rather than treating every dot as part of the provider slug.
        for index in range(1, len(dot_parts)):
            suffix = ".".join(dot_parts[index:])
            if any(ch.isalpha() for ch in suffix):
                _add(suffix)

    for candidate in list(candidates):
        normalized = re.sub(r"[^a-z0-9]+", "-", candidate).strip("-")
        _add(normalized)

    return candidates


def _candidate_supports_reasoning(candidate: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(candidate or "").strip().lower()).strip("-")
    if not normalized:
        return False

    tokens = [token for token in normalized.split("-") if token]
    token_set = set(tokens)

    if "thinking" in token_set or "reasoning" in token_set:
        return True
    if "gpt" in token_set or normalized.startswith("gpt"):
        # Restrict to GPT-5+ (exclude GPT-4o/4.1/3.5 — reasoning_effort unsupported)
        m = re.search(r"gpt-(\d+)", normalized)
        if m and int(m.group(1)) >= 5:
            return True
        return False
    if normalized in {"o1", "o3", "o4"} or normalized.startswith(("o1-", "o3-", "o4-")):
        return True
    if "claude" in token_set or normalized.startswith("claude"):
        # Restrict to Claude 4+ or Claude 3.7+ (exclude Claude 3.0/3.5)
        match = re.search(r"claude.*?(\d+)(?:\D+(\d+))?", normalized)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2)) if match.group(2) else 0
            if major >= 4 or (major == 3 and minor >= 7):
                return True
        return False
    if "qwen" in token_set or normalized.startswith("qwen"):
        # Restrict to Qwen 3+ (exclude Qwen 2/2.5)
        match = re.search(r"qwen.*?(\d+)(?:\D+(\d+))?", normalized)
        if match:
            major = int(match.group(1))
            if major >= 3:
                return True
        return False
    if "kimi" in token_set or normalized.startswith("kimi"):
        return True
    if "minimax" in token_set or normalized.startswith("minimax"):
        return True
    if "mimo" in token_set or normalized.startswith("mimo"):
        return True
    if "glm" in token_set or normalized.startswith("glm"):
        return True
    if "step" in token_set or normalized.startswith("step"):
        return True
    if "deepseek" in token_set:
        # Check the token immediately after "deepseek" for a V-series or R-series
        # version marker (e.g. "v4", "r1"). This is position-independent so it
        # handles bare "deepseek-v4-flash" and @custom:name:DeepSeek-V4-Flash
        # (→ "my-provider-deepseek-v4-flash") equally, while correctly excluding
        # non-reasoning models like deepseek-chat and deepseek-coder.
        # Using tokens.index() ensures the provider slug (e.g. "vertex" in
        # "vertex-deepseek-chat") cannot falsely trigger the version guard.
        idx = tokens.index("deepseek")
        if idx + 1 < len(tokens) and tokens[idx + 1].startswith(("v", "r")):
            return True
    return False


def _nested_route_reasoning_denied(model: str) -> bool:
    """Hard deny for nested Gemini gateway routes that must never show a reasoning toggle."""
    lower = str(model or "").strip().lower()
    if not lower:
        return False
    for prefix in ("vertex/gemini-", "gemini_cli/gemini-"):
        if lower.startswith(prefix):
            tail = lower[len(prefix) :]
            if tail.startswith("embedding") or "image" in tail or "imagine" in tail:
                return True
    return False


def _nested_gateway_route_reasoning(model: str) -> bool:
    """Recognize nested ``vertex/gemini-`` and ``gemini_cli/gemini-`` routes on custom providers.

    The slash-prefix heuristic list includes ``google/gemini-2`` but not gateway-prefixed
    Gemini ids, so capable models behind custom aggregators stayed hidden.
    """
    lower = str(model or "").strip().lower()
    if not lower:
        return False
    for prefix in ("vertex/gemini-", "gemini_cli/gemini-"):
        if lower.startswith(prefix):
            tail = lower[len(prefix) :]
            if tail.startswith("embedding") or "image" in tail or "imagine" in tail:
                return False
            # Gemini thinking/reasoning controls are documented for the 2.5
            # series and 3-era models only — 1.5 (and earlier) have no thinking
            # support, so a reasoning selector on e.g. ``vertex/gemini-1.5-pro``
            # would let a user pick an effort that the route then rejects.
            # Version-gate the allow to the reasoning-capable families.
            if (
                tail == "2.5"
                or tail.startswith(("2.5-", "2.5.", "3-", "3."))
                or "thinking" in tail
                or "reasoning" in tail
            ):
                return True
            return False
    return False


def _filter_reasoning_efforts_for_provider(
    efforts: list[str],
    model_id: str,
    provider_id: str,
) -> list[str]:
    """Apply provider/model quirks to otherwise valid reasoning effort levels."""
    normalized = [
        str(eff).strip().lower()
        for eff in efforts
        if str(eff).strip().lower() in VALID_REASONING_EFFORTS
    ]
    normalized = list(dict.fromkeys(normalized))
    provider = _resolve_provider_alias(str(provider_id or "").strip().lower())
    bare = _strip_provider_hint_for_reasoning(model_id).lower().rsplit("/", 1)[-1]
    if provider == "openai-codex":
        if bare.startswith(("o1", "o3", "o4")):
            return [eff for eff in normalized if eff in {"low", "medium", "high"}]
        if bare.startswith("gpt-5"):
            return [eff for eff in normalized if eff != "max"]
    return normalized


def _heuristic_reasoning_efforts(model_id: str, provider_id: str) -> list[str]:
    """Fallback when hermes_cli is unavailable."""
    model = _strip_provider_hint_for_reasoning(model_id).lower()
    provider = _resolve_provider_alias(str(provider_id or "").strip().lower())
    if not model or provider in {"cursor-acp", "copilot-acp"}:
        return []
    bare = model.rsplit("/", 1)[-1]
    if provider == "openai-codex" and bare.startswith(("gpt-5", "o1", "o3", "o4")):
        if bare.startswith(("o1", "o3", "o4")):
            return ["low", "medium", "high"]
        return _filter_reasoning_efforts_for_provider(
            list(VALID_REASONING_EFFORTS), model, provider
        )
    if provider in {"copilot", "github-copilot"}:
        if bare.startswith(("gpt-5", "o1", "o3", "o4")):
            if bare.startswith(("o1", "o3", "o4")):
                return ["low", "medium", "high"]
            return list(VALID_REASONING_EFFORTS)
    prefixes = (
        "deepseek/",
        "anthropic/",
        "openai/",
        "x-ai/",
        "google/gemini-2",
        "google/gemma-4",
        "qwen/qwen3",
        "tencent/hy3-preview",
        "xiaomi/",
    )
    if any(model.startswith(prefix) for prefix in prefixes):
        return list(VALID_REASONING_EFFORTS)
    if _nested_gateway_route_reasoning(model):
        return list(VALID_REASONING_EFFORTS)
    # Named custom providers often rewrite model ids with dots, underscores, or
    # extra vendor namespaces. Normalize those shapes before applying family-level
    # reasoning heuristics so "deepseek.v3.2", "deepseek_v4_flash", and
    # "vendor.deepseek.v3.2" are treated consistently.
    if any(_candidate_supports_reasoning(candidate) for candidate in _reasoning_name_candidates(bare)):
        return list(VALID_REASONING_EFFORTS)
    return []


def _models_dev_reasoning_efforts(model_id: str, provider_id: str) -> list[str] | None:
    """Return reasoning efforts from Hermes Agent model metadata when known.

    ``None`` means the metadata source is unavailable or has no answer, so the
    caller should continue to compatibility fallbacks. A concrete list (including
    ``[]``) is authoritative.
    """
    model = _strip_provider_hint_for_reasoning(model_id)
    provider = str(provider_id or "").strip().lower()
    if not model or not provider:
        return None

    try:
        from agent.models_dev import get_model_capabilities
    except Exception:
        return None

    try:
        capabilities = get_model_capabilities(provider=provider, model=model)
    except Exception:
        return None
    if capabilities is None:
        return None

    supports_reasoning = getattr(capabilities, "supports_reasoning", None)
    if supports_reasoning is True:
        return _filter_reasoning_efforts_for_provider(
            list(VALID_REASONING_EFFORTS), model, provider
        )
    if supports_reasoning is False:
        return []
    return None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that refuses to follow any redirect.

    Used by the LM Studio reasoning probe so a 3xx from the probe URL can never
    forward the ``Authorization`` header (the configured LM Studio key) to a
    redirected, possibly attacker-controlled host. ``redirect_request``
    returning ``None`` makes urllib raise the original 3xx as an ``HTTPError``,
    which the probe swallows. (#3837 security review)
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_lmstudio_reasoning_probe_api_key() -> str | None:
    """Resolve the LM Studio key for reasoning probes with WebUI precedence."""
    config_data = cfg
    model_cfg = config_data.get("model") or {}
    if isinstance(model_cfg, dict):
        active_provider = str(model_cfg.get("provider") or "").strip().lower()
        model_key = str(model_cfg.get("api_key") or "").strip()
        if active_provider == "lmstudio" and model_key:
            return model_key

    providers_cfg = config_data.get("providers") or {}
    if isinstance(providers_cfg, dict):
        lmstudio_cfg = providers_cfg.get("lmstudio") or {}
        if isinstance(lmstudio_cfg, dict):
            config_key = str(lmstudio_cfg.get("api_key") or "").strip()
            if config_key:
                return config_key

    env_key = str(os.getenv("LM_API_KEY") or "").strip()
    if env_key:
        return env_key

    legacy_env_key = str(os.getenv("LMSTUDIO_API_KEY") or "").strip()
    if legacy_env_key:
        return legacy_env_key

    return None


def _lmstudio_reasoning_probe_options_fallback(
    model: str,
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Query LM Studio reasoning options without relying on hermes_cli."""
    server_root = str(base_url or "").strip().rstrip("/")
    if server_root.endswith("/v1"):
        server_root = server_root[:-3].rstrip("/")
    if not server_root or not model:
        return []

    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-webui-reasoning-probe",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        server_root + "/api/v1/models",
        headers=headers,
        method="GET",
    )
    # SECURITY: never follow redirects on this probe. urllib re-sends request
    # headers (including Authorization: Bearer <lmstudio key>) to the redirect
    # target, so a 3xx from the probe URL could exfiltrate the configured
    # LM Studio credential to an attacker-controlled host. A no-redirect opener
    # turns any 3xx into an HTTPError we swallow below. (#3837 security review)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        logger.debug(
            "LM Studio reasoning probe at %s failed with HTTP %s",
            server_root,
            exc.code,
        )
        return []
    except Exception as exc:
        logger.debug("LM Studio reasoning probe at %s failed: %s", server_root, exc)
        return []

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        logger.debug(
            "LM Studio reasoning probe at %s returned malformed payload",
            server_root,
        )
        return []

    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        if raw.get("key") != model and raw.get("id") != model:
            continue
        caps = raw.get("capabilities")
        reasoning = caps.get("reasoning") if isinstance(caps, dict) else None
        opts = reasoning.get("allowed_options") if isinstance(reasoning, dict) else None
        if isinstance(opts, list):
            return [str(opt).strip().lower() for opt in opts if isinstance(opt, str)]
        return []
    return []


def _lmstudio_model_reasoning_options(
    model: str,
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Prefer hermes_cli, but keep WebUI reasoning probes working without it.

    SECURITY: when an ``api_key`` is being sent, always use the built-in
    no-redirect fallback probe rather than ``hermes_cli``. The bundled CLI probe
    uses a plain ``urllib.request.urlopen`` that follows redirects and re-sends
    the ``Authorization`` header to the redirect target, which could leak the
    configured LM Studio credential to another host. We can only guarantee
    redirect safety for code we control, so credentialed probes go through
    ``_lmstudio_reasoning_probe_options_fallback`` (no-redirect opener). Keyless
    probes have no credential to leak, so they may use the richer CLI path.
    (#3837 security review)
    """
    if api_key:
        return _lmstudio_reasoning_probe_options_fallback(
            model,
            base_url,
            api_key=api_key,
            timeout=timeout,
        )

    try:
        from hermes_cli.models import (
            lmstudio_model_reasoning_options as _cli_lmstudio_model_reasoning_options,
        )
    except Exception:
        return _lmstudio_reasoning_probe_options_fallback(
            model,
            base_url,
            api_key=api_key,
            timeout=timeout,
        )

    try:
        return _cli_lmstudio_model_reasoning_options(
            model,
            base_url,
            api_key=api_key,
            timeout=timeout,
        )
    except (TypeError, AttributeError):
        logger.warning(
            "hermes_cli.lmstudio_model_reasoning_options has an unexpected signature; "
            "falling back to the built-in LM Studio reasoning probe",
            exc_info=True,
        )
        return _lmstudio_reasoning_probe_options_fallback(
            model,
            base_url,
            api_key=api_key,
            timeout=timeout,
        )
    except Exception:
        return _lmstudio_reasoning_probe_options_fallback(
            model,
            base_url,
            api_key=api_key,
            timeout=timeout,
        )


def resolve_model_reasoning_efforts(
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> list[str]:
    """Return supported reasoning-effort levels for *model_id*, or [] if none."""
    model = str(model_id or "").strip()
    if not model:
        return []

    provider = str(provider_id or "").strip().lower() if provider_id else ""
    resolved_base_url = str(base_url or "").strip() or None
    if not provider:
        try:
            _, provider, resolved_base_url = resolve_model_provider(model)
        except Exception:
            provider = str((cfg.get("model") or {}).get("provider") or "").strip().lower()

    provider = _resolve_provider_alias(provider)
    if provider in {"cursor-acp", "copilot-acp"}:
        return []

    hinted_model = _strip_provider_hint_for_reasoning(model)

    if _nested_route_reasoning_denied(hinted_model):
        return []

    if provider in {"copilot", "github-copilot"}:
        try:
            from hermes_cli.models import github_model_reasoning_efforts
        except Exception:
            return _heuristic_reasoning_efforts(hinted_model, provider)
        return _filter_reasoning_efforts_for_provider(
            github_model_reasoning_efforts(hinted_model), hinted_model, provider
        )

    if provider == "lmstudio":
        configured_base = _get_provider_base_url(provider)
        probe_base = resolved_base_url or configured_base
        # SECURITY: only forward the configured LM Studio credential when the
        # probe target is the configured LM Studio endpoint. /api/reasoning
        # accepts a caller-supplied base_url, so a request could otherwise point
        # the probe at an arbitrary host and harvest the stored key. When the
        # caller supplies a base_url that does not normalize to the configured
        # one, probe it WITHOUT a key. (#3837 security review)
        probe_key: str | None = None
        if not resolved_base_url or (
            configured_base
            and _normalize_base_url_for_match(probe_base)
            == _normalize_base_url_for_match(configured_base)
        ):
            probe_key = _get_lmstudio_reasoning_probe_api_key()
        opts = _lmstudio_model_reasoning_options(
            hinted_model,
            probe_base,
            api_key=probe_key,
        )
        normalized = [str(opt).strip().lower() for opt in opts if str(opt).strip()]
        if not normalized or set(normalized).issubset({"off"}):
            return []
        level_opts = [opt for opt in normalized if opt in VALID_REASONING_EFFORTS]
        if level_opts:
            return _filter_reasoning_efforts_for_provider(
                level_opts, hinted_model, provider
            )
        if set(normalized).issubset({"off", "on"}):
            return []
        return []

    # _models_dev_reasoning_efforts already applies the provider/model filter
    # internally, so it is returned as-is here (filtering again would be
    # redundant — the filter is idempotent but the double pass obscures flow).
    metadata_efforts = _models_dev_reasoning_efforts(hinted_model, provider)
    if metadata_efforts is not None:
        return metadata_efforts

    return _heuristic_reasoning_efforts(hinted_model, provider)


def coerce_reasoning_effort_for_model(
    effort: str | None,
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> str:
    """Return the closest supported effort for the target model/provider."""
    raw = str(effort or "").strip().lower()
    if not raw:
        return ""
    if raw == "none":
        return "none"
    accepts_max_as_xhigh = raw == "max"
    if accepts_max_as_xhigh:
        raw = "xhigh"
    if raw not in VALID_REASONING_EFFORTS:
        return ""
    supported = resolve_model_reasoning_efforts(
        model_id,
        provider_id=provider_id,
        base_url=base_url,
    )
    # An empty list is ambiguous: resolve_model_reasoning_efforts() returns []
    # both for models KNOWN not to support reasoning AND for models we simply
    # don't recognize (custom providers, aggregator-rewritten ids, brand-new
    # releases). Coercion exists to avoid sending a level a KNOWN-incompatible
    # model rejects (e.g. openai-codex gpt-5 'max', o1/o3/o4 above 'high') -
    # those paths return a NON-empty clamped set, so the degrade ladder below
    # still applies. When the set is empty we can't tell "unsupported" from
    # "unknown", so preserve the user's configured effort verbatim where it is
    # still valid. A stale 'max' value is no longer parser-valid on the WebUI
    # side, so degrade that unknown-model case to xhigh instead of silently
    # dropping reasoning later in parse_reasoning_effort(). (#3505 review)
    if not supported:
        return "xhigh" if accepts_max_as_xhigh else raw
    if raw in supported:
        return "xhigh" if accepts_max_as_xhigh else raw
    # Degrade to the closest *lower* supported level instead of silently
    # disabling reasoning. e.g. max -> xhigh -> high, or xhigh -> high when the
    # target model caps below the configured effort. Never escalate.
    ladder = list(VALID_REASONING_EFFORTS)  # ascending: minimal..xhigh
    try:
        raw_idx = ladder.index(raw)
    except ValueError:
        return raw
    for level in reversed(ladder[:raw_idx]):  # strictly lower, highest first
        if level in supported:
            return level
    # raw is below every supported level (shouldn't happen for a non-empty set
    # that excludes raw, but be safe): preserve the configured effort rather
    # than blank it.
    return raw


def get_reasoning_status(
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> dict:
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

    resolve_model = model_id
    resolve_provider = provider_id
    resolve_base_url = base_url
    if not resolve_model:
        model_cfg = config_data.get("model") or {}
        if isinstance(model_cfg, dict):
            resolve_model = str(model_cfg.get("default") or "").strip() or None
            if not resolve_provider and model_cfg.get("provider"):
                resolve_provider = str(model_cfg["provider"]).strip()
            if not resolve_base_url and model_cfg.get("base_url"):
                resolve_base_url = str(model_cfg["base_url"]).strip()

    supported_efforts = resolve_model_reasoning_efforts(
        resolve_model,
        provider_id=resolve_provider,
        base_url=resolve_base_url,
    )
    return {
        # Match CLI default (True if unset in config.yaml)
        "show_reasoning": bool(show_raw) if isinstance(show_raw, bool) else True,
        # Report the COERCED effort (not the raw config value) so boot/status/chip
        # read paths agree with what streaming actually sends — e.g. a stale
        # `reasoning_effort: max` surfaces as `xhigh`, not the now-unsupported `max`.
        # (Codex review of the drop-max alignment.)
        "reasoning_effort": coerce_reasoning_effort_for_model(
            str(effort_raw or "").strip().lower(),
            resolve_model,
            provider_id=resolve_provider,
            base_url=resolve_base_url,
        ),
        "supported_efforts": supported_efforts,
        "supports_reasoning_effort": bool(supported_efforts),
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


def set_reasoning_effort(
    effort: str,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> dict:
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
    return get_reasoning_status(
        model_id=model_id,
        provider_id=provider_id,
        base_url=base_url,
    )


def _public_advanced_model_options(model_cfg: dict) -> dict:
    """Return write-only-safe advanced options from a model config block."""
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    return {
        "base_url": str(model_cfg.get("base_url") or "").strip(),
        "timeout": model_cfg.get("timeout", ""),
        "download_timeout": model_cfg.get("download_timeout", ""),
        "max_concurrency": model_cfg.get("max_concurrency", ""),
        "extra_body": model_cfg.get("extra_body") if isinstance(model_cfg.get("extra_body"), dict) else {},
        "api_key_set": bool(str(model_cfg.get("api_key") or "").strip()),
    }


def _main_model_request_overrides(config_data: dict) -> dict:
    """Return supported runtime request overrides for the main chat model."""
    if not isinstance(config_data, dict):
        return {}
    model_cfg = config_data.get("model", {})
    if not isinstance(model_cfg, dict):
        return {}
    overrides = {}
    extra_body = model_cfg.get("extra_body")
    if isinstance(extra_body, dict) and extra_body:
        overrides["extra_body"] = copy.deepcopy(extra_body)
    return overrides


def _apply_advanced_model_options(model_cfg: dict, advanced: dict | None) -> None:
    """Apply supported advanced model options to a config block in-place."""
    if advanced is None:
        return
    if not isinstance(advanced, dict):
        raise ValueError("advanced model options must be an object")
    if "base_url" in advanced:
        base_url = str(advanced.get("base_url") or "").strip().rstrip("/")
        if base_url:
            model_cfg["base_url"] = base_url
        else:
            model_cfg.pop("base_url", None)
    for field in ("timeout", "download_timeout", "max_concurrency"):
        if field in advanced:
            coerced = _coerce_optional_positive_int(advanced.get(field), field)
            if coerced == "":
                model_cfg.pop(field, None)
            elif coerced is not None:
                model_cfg[field] = coerced
    if "extra_body" in advanced:
        extra_body = advanced.get("extra_body")
        if isinstance(extra_body, str):
            text = extra_body.strip()
            try:
                extra_body = json.loads(text) if text else {}
            except json.JSONDecodeError as exc:
                raise ValueError("extra_body must be valid JSON") from exc
        if extra_body in (None, ""):
            model_cfg.pop("extra_body", None)
        elif isinstance(extra_body, dict):
            if extra_body:
                model_cfg["extra_body"] = extra_body
            else:
                model_cfg.pop("extra_body", None)
        else:
            raise ValueError("extra_body must be a JSON object")
    if advanced.get("api_key_clear"):
        model_cfg.pop("api_key", None)
    api_key = str(advanced.get("api_key") or "").strip()
    if api_key:
        model_cfg["api_key"] = api_key


def set_hermes_default_model(model_id: str, provider: str | None = None, advanced: dict | None = None) -> dict:
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
        requested_provider = str(provider or "").strip()
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
        persisted_provider = str(requested_provider or resolved_provider or previous_provider or "").strip()
        provider_override_won = bool(requested_provider and requested_provider != str(resolved_provider or "").strip())
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

        if resolved_base_url and not provider_override_won:
            model_cfg["base_url"] = str(resolved_base_url).strip().rstrip("/")
        elif persisted_provider != previous_provider:
            if persisted_provider == "openai":
                model_cfg["base_url"] = "https://api.openai.com/v1"
            else:
                # Provider changed and we have no resolved URL for the new one.
                # Drop the previous provider's base_url so New Chat doesn't route
                # to the old endpoint — this MUST also cover custom:* providers
                # (a different custom provider has a different URL); leaving the
                # stale base_url sent requests to the wrong host (#4728).
                model_cfg.pop("base_url", None)

        _apply_advanced_model_options(model_cfg, advanced)

        config_data["model"] = model_cfg
        _save_yaml_config_file(config_path, config_data)
    # Reload outside the lock — reload_config() acquires _cfg_lock itself.
    reload_config()
    # Invalidate the TTL cache so the next /api/models call returns fresh data
    # with the new default model. Do NOT call get_available_models() here —
    # it triggers a live provider fetch (up to 8s) that blocks the HTTP response
    # to the browser, causing a visible freeze on every Settings save (#895).
    invalidate_models_cache()
    return {"ok": True, "model": persisted_model, "provider": persisted_provider or None}


# ── Auxiliary model configuration ──────────────────────────────────────────

# Canonical auxiliary task slots. Keep in sync with hermes_cli/config.py
# DEFAULT_CONFIG["auxiliary"] and hermes_cli/web_server.py _AUX_TASK_SLOTS.
AUX_TASK_SLOTS: tuple[str, ...] = (
 "vision",
 "web_extract",
 "compression",
 "session_search",
 "skills_hub",
 "approval",
 "mcp",
 "title_generation",
 "curator",
 "kanban_decomposer",
 "profile_describer",
 "triage_specifier",
)


def get_auxiliary_models() -> dict:
    """Return current auxiliary task assignments from config.yaml.

    Shape:
    {
        "tasks": [
            {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
            ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
    }
    """
    reload_config()
    model_cfg = cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    main_provider = str(model_cfg.get("provider") or "").strip()
    main_model = str(model_cfg.get("default") or model_cfg.get("name") or "").strip()

    aux_cfg = cfg.get("auxiliary", {})
    if not isinstance(aux_cfg, dict):
        aux_cfg = {}

    tasks = []
    for slot in AUX_TASK_SLOTS:
        entry = aux_cfg.get(slot, {})
        if not isinstance(entry, dict):
            entry = {}
        tasks.append({
            "task": slot,
            "provider": str(entry.get("provider") or "auto").strip(),
            "model": str(entry.get("model") or "").strip(),
            "base_url": str(entry.get("base_url") or "").strip(),
            "timeout": entry.get("timeout", ""),
            "download_timeout": entry.get("download_timeout", ""),
            "max_concurrency": entry.get("max_concurrency", ""),
            "extra_body": entry.get("extra_body") if isinstance(entry.get("extra_body"), dict) else {},
            "api_key_set": bool(str(entry.get("api_key") or "").strip()),
        })

    return {
        "tasks": tasks,
        "main": {
            "provider": main_provider,
            "model": main_model,
            **_public_advanced_model_options(model_cfg),
        },
    }


def _coerce_optional_positive_int(value, field: str):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return ""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number < 1:
        raise ValueError(f"{field} must be a positive integer")
    return number


def set_auxiliary_model(task: str, provider: str, model: str, advanced: dict | None = None) -> dict:
    """Persist an auxiliary model assignment in config.yaml.

    Special case: task='__reset__' clears all auxiliary slots.
    ``advanced`` may update per-slot fields surfaced behind the WebUI gear menu.
    Sensitive api_key values are write-only: get_auxiliary_models() only reports
    whether one is set.
    """
    if task != "__reset__" and task not in AUX_TASK_SLOTS:
        raise ValueError(
            f"Unknown auxiliary task slot: {task!r}. Valid: {list(AUX_TASK_SLOTS)}"
        )
    config_path = _get_config_path()
    with _cfg_lock:
        config_data = _load_yaml_config_file(config_path)

        if task == "__reset__":
            # Per-slot reset: set each slot to auto, preserving extra fields
            # (timeout, extra_body, api_key, base_url, download_timeout, etc.)
            aux_cfg = config_data.get("auxiliary", {})
            if not isinstance(aux_cfg, dict):
                aux_cfg = {}
            for slot in AUX_TASK_SLOTS:
                slot_cfg = aux_cfg.get(slot, {})
                if not isinstance(slot_cfg, dict):
                    slot_cfg = {}
                slot_cfg["provider"] = "auto"
                slot_cfg["model"] = ""
                aux_cfg[slot] = slot_cfg
            config_data["auxiliary"] = aux_cfg
        else:
            aux_cfg = config_data.get("auxiliary", {})
            if not isinstance(aux_cfg, dict):
                aux_cfg = {}
            slot_cfg = aux_cfg.get(task, {})
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = provider or "auto"
            slot_cfg["model"] = model or ""
            if provider and (provider.startswith("custom:") or provider == "custom"):
                try:
                    _, _, resolved_base_url = resolve_model_provider(model)
                    if resolved_base_url:
                        slot_cfg["base_url"] = str(resolved_base_url).strip().rstrip("/")
                except Exception:
                    pass
            if advanced is not None:
                try:
                    _apply_advanced_model_options(slot_cfg, advanced)
                except ValueError as exc:
                    msg = str(exc).replace("advanced model options", "advanced auxiliary options")
                    raise ValueError(msg) from exc
            aux_cfg[task] = slot_cfg
            config_data["auxiliary"] = aux_cfg

        _save_yaml_config_file(config_path, config_data)

    reload_config()
    return {"ok": True, "task": task, "provider": provider, "model": model}


# ── TTL cache for get_available_models() ─────────────────────────────────────
_available_models_cache: dict | None = None
_available_models_cache_ts: float = 0.0
_available_models_cache_source_fingerprint: dict | None = None
_AVAILABLE_MODELS_CACHE_TTL: float = 86400.0  # 24 hours
_SESSION_VISIT_MODELS_FRESHNESS_SECONDS: float = 300.0
_available_models_cache_lock = threading.RLock()  # must be RLock: cold path refactoring moved slow work inside this lock, requiring re-entry
_cache_build_cv = threading.Condition(_available_models_cache_lock)  # shares underlying RLock so notify_all() is safe inside with _available_models_cache_lock
_cache_build_in_progress = False  # True while a cold path is actively building

# Hard wall-clock budget for a COLD live provider-catalog rebuild when it is
# run from a foreground request path. The live rebuild does one network probe
# per detected provider (Copilot token-exchange HTTPS, OpenRouter /v1/models,
# Nous /models, ...). On a flaky / corp / WSL network any single probe can
# stall for its full per-call timeout (Copilot urllib timeout=10s) and, summed
# across N providers, block the request thread for tens of seconds. This bounds
# the time a foreground caller will wait: past the budget it returns a usable
# fallback (last-known disk cache or a network-free minimal catalog) and lets
# the rebuild finish out-of-band and populate the cache for the next call.
# Set HERMES_WEBUI_MODELS_REBUILD_BUDGET=0 to restore the legacy synchronous
# (unbounded) behaviour.
try:
    _LIVE_REBUILD_BUDGET_SECONDS: float = float(
        os.getenv("HERMES_WEBUI_MODELS_REBUILD_BUDGET", "4") or "4"
    )
except (TypeError, ValueError):
    _LIVE_REBUILD_BUDGET_SECONDS = 4.0


# ── Budget-exceeded warning rate-limit ───────────────────────────────────────
# Q-2979-A3 / Copilot discussion_r3305864400: the live-rebuild-budget-exceeded
# warning at _invoke_models_rebuild's slow-path is potentially high-volume —
# every provider catalog refresh that runs past _LIVE_REBUILD_BUDGET_SECONDS
# emits one, so a hung upstream probe (or a sustained burst of cold callers)
# could flood the log at warning level. Rate-limit per reason: the FIRST
# occurrence in a cooldown window logs at warning; subsequent occurrences in
# the same window log at info (so log signal stays useful but volume bounded).
# Override the default cooldown via HERMES_WEBUI_BUDGET_WARN_COOLDOWN (seconds).
try:
    _BUDGET_WARN_COOLDOWN_SECONDS: float = float(
        os.getenv("HERMES_WEBUI_BUDGET_WARN_COOLDOWN", "300") or "300"
    )
except (TypeError, ValueError):
    _BUDGET_WARN_COOLDOWN_SECONDS = 300.0

_BUDGET_WARN_STATE: dict[str, float] = {}
_BUDGET_WARN_LOCK = threading.Lock()


def _should_warn_budget(reason: str, cooldown_s: float | None = None) -> bool:
    """Return True iff the budget warning for ``reason`` should log at
    warning level (first hit, or last warn-level emit was more than
    ``cooldown_s`` seconds ago). Otherwise False — the caller should demote
    to info for the same payload so the signal is retained but the noise is
    capped. Thread-safe; the cooldown is shared across all live-rebuild
    callers in this process.
    """
    cooldown = (
        _BUDGET_WARN_COOLDOWN_SECONDS if cooldown_s is None else float(cooldown_s)
    )
    now = time.monotonic()
    with _BUDGET_WARN_LOCK:
        last = _BUDGET_WARN_STATE.get(reason)
        if last is None or (now - last) >= cooldown:
            _BUDGET_WARN_STATE[reason] = now
            return True
        return False


def _invoke_models_rebuild(builder):
    """Indirection seam around the cold catalog rebuild.

    Production simply calls ``builder()``. Exists so tests can simulate a
    slow / hanging provider probe without having to reach the closure that
    actually does the per-provider network calls.
    """
    return builder()


def _configured_model_badges_from_static_catalog(
    groups: list[dict],
    *,
    active_provider: str | None,
    default_model: str,
) -> dict[str, dict[str, str]]:
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

    fallback_cfg = cfg.get("fallback_providers", []) if isinstance(cfg, dict) else []
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

    option_ids = [
        m.get("id", "")
        for g in groups
        for m in g.get("models", [])
        if m.get("id")
    ]
    option_lookup = {str(opt_id): str(opt_id) for opt_id in option_ids}
    option_provider_lookup = {
        str(m.get("id")): str(g.get("provider_id") or "")
        for g in groups
        for m in g.get("models", [])
        if m.get("id")
    }

    def _norm_static_model_id(model_id: str) -> str:
        s = str(model_id or "").strip().lower()
        stripped_at_provider = False
        if s.startswith("@") and ":" in s:
            colon_idx = s.index(":", 1)
            candidate = s[colon_idx + 1:]
            stripped_at_provider = bool(candidate)
            s = candidate or s
        if "://" not in s:
            if (
                not stripped_at_provider
                and "/" in s
                and ":" in s
                and s.index(":") < s.index("/")
            ):
                s = s[s.index("/") + 1 :] or s
            if "/" in s:
                stripped = s.split("/", 1)[1]
                s = stripped or s
        return s.replace("-", ".")

    norm_lookup: dict[str, list[str]] = {}
    for opt_id in option_ids:
        norm_lookup.setdefault(_norm_static_model_id(opt_id), []).append(opt_id)

    badges: dict[str, dict[str, str]] = {}
    for entry in configured_entries:
        provider = entry["provider"]
        model = entry["model"]
        raw_candidates = []
        for candidate in (model, f"{provider}/{model}", f"@{provider}:{model}"):
            if candidate and candidate not in raw_candidates:
                raw_candidates.append(candidate)

        match_id = None
        for candidate in raw_candidates:
            if (
                candidate in option_lookup
                and option_provider_lookup.get(candidate) == provider
            ):
                match_id = option_lookup[candidate]
                break
        if match_id is None:
            for candidate in raw_candidates:
                normalized = _norm_static_model_id(candidate)
                matches = norm_lookup.get(normalized, [])
                if not matches:
                    continue
                provider_match = next(
                    (m for m in matches if option_provider_lookup.get(m) == provider),
                    None,
                )
                match_id = provider_match or matches[0]
                if match_id:
                    break

        badge_payload = {
            "role": entry["role"],
            "label": entry["label"],
            "provider": provider,
        }
        for candidate in raw_candidates:
            candidate_provider = option_provider_lookup.get(candidate)
            if candidate_provider and candidate_provider != provider:
                continue
            badges[candidate] = badge_payload
        if match_id:
            badges[match_id] = badge_payload

    return badges


def _minimal_static_models_catalog() -> dict:
    """Return the emergency one-model fallback for /api/models."""
    try:
        active_provider = None
        cfg_base_url = ""
        model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
        if isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_base_url = model_cfg.get("base_url", "") or ""
        if active_provider:
            try:
                active_provider = _resolve_configured_provider_id(
                    active_provider, cfg, base_url=cfg_base_url
                )
            except Exception:
                active_provider = str(active_provider or "").strip() or None
        if not active_provider:
            try:
                _ap = _get_auth_store_path()
                if _ap.exists():
                    _store = json.loads(_ap.read_text(encoding="utf-8"))
                    active_provider = (
                        _resolve_configured_provider_id(
                            _store.get("active_provider"), cfg, base_url=cfg_base_url
                        )
                        or None
                    )
            except Exception:
                pass
        default_model = get_effective_default_model(cfg)
        groups: list[dict] = []
        if default_model:
            try:
                label = _get_label_for_model(default_model, [])
            except Exception:
                label = default_model
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
            "configured_model_badges": {},
            "groups": groups,
            "aliases": {},
        }
    except Exception:
        logger.debug("minimal static models catalog build failed", exc_info=True)
        return {
            "active_provider": None,
            "default_model": "",
            "configured_model_badges": {},
            "groups": [],
            "aliases": {},
        }


def _static_models_catalog_without_live_probes() -> dict:
    """Return a network-free /api/models catalog from local config/auth only."""
    try:
        from api.providers import _provider_has_key

        active_provider = None
        cfg_base_url = ""
        model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
        if isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_base_url = model_cfg.get("base_url", "") or ""
        if active_provider:
            try:
                active_provider = _resolve_configured_provider_id(
                    active_provider,
                    cfg,
                    base_url=cfg_base_url,
                )
            except Exception:
                active_provider = str(active_provider or "").strip() or None

        auth_store: dict = {}
        try:
            auth_store_path = _get_auth_store_path()
            if auth_store_path.exists():
                auth_store = json.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = (
                        _resolve_configured_provider_id(
                            auth_store.get("active_provider"),
                            cfg,
                            base_url=cfg_base_url,
                        )
                        or None
                    )
        except Exception:
            logger.debug("Failed to load auth store for static models catalog", exc_info=True)

        default_model = get_effective_default_model(cfg)
        detected_providers: set[str] = set()
        configured_model_ids: dict[str, list[str]] = {}
        named_custom_groups: dict[str, dict[str, object]] = {}
        custom_group_models: list[dict] = []
        canonical_to_raw_provider_key: dict[str, str] = {}
        providers_cfg = _get_providers_cfg()

        def _append_model_id(provider_id: str | None, model_id: object) -> None:
            pid = _canonicalise_provider_id(provider_id)
            mid = str(model_id or "").strip()
            if not pid or not mid:
                return
            configured_model_ids.setdefault(pid, [])
            if mid not in configured_model_ids[pid]:
                configured_model_ids[pid].append(mid)

        if active_provider:
            detected_providers.add(active_provider)
            _append_model_id(active_provider, default_model)

        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid, _entries in _pool.items():
                    if not isinstance(_entries, list) or not _entries:
                        continue
                    if any(
                        isinstance(_entry, dict)
                        and not _is_ambient_gh_cli_entry(
                            str(_entry.get("source", "") or ""),
                            str(_entry.get("label", "") or ""),
                            str(_entry.get("key_source", "") or ""),
                        )
                        for _entry in _entries
                    ):
                        detected_providers.add(_resolve_provider_alias(str(_pid)))
        except Exception:
            logger.debug("Failed to inspect auth-store credential pool", exc_info=True)

        if isinstance(providers_cfg, dict):
            for provider_key, provider_cfg in providers_cfg.items():
                canonical = _canonicalise_provider_id(provider_key)
                if not canonical:
                    continue
                is_known_provider = (
                    canonical in _PROVIDER_MODELS
                    or canonical in _PROVIDER_DISPLAY
                    or _is_plugin_model_provider(canonical)
                )
                is_provider_config = isinstance(provider_cfg, dict)
                if not (is_known_provider or is_provider_config):
                    continue
                canonical_to_raw_provider_key.setdefault(canonical, provider_key)
                if isinstance(provider_cfg, dict):
                    has_local_signal = any(
                        str(provider_cfg.get(key) or "").strip()
                        for key in ("api_key", "key_env", "base_url")
                    )
                    provider_models = provider_cfg.get("models")
                    if isinstance(provider_models, dict):
                        for model_id in provider_models:
                            _append_model_id(canonical, model_id)
                            has_local_signal = True
                    elif isinstance(provider_models, list):
                        for item in provider_models:
                            if isinstance(item, dict):
                                _append_model_id(
                                    canonical,
                                    item.get("id") or item.get("model") or item.get("name"),
                                )
                            else:
                                _append_model_id(canonical, item)
                            has_local_signal = True
                    if has_local_signal:
                        detected_providers.add(canonical)

        for provider_id in set(_PROVIDER_MODELS) | set(_PROVIDER_DISPLAY):
            canonical = _canonicalise_provider_id(provider_id)
            if canonical and _provider_has_key(canonical):
                detected_providers.add(canonical)

        fallback_cfg = cfg.get("fallback_providers", []) if isinstance(cfg, dict) else []
        if isinstance(fallback_cfg, list):
            for entry in fallback_cfg:
                if not isinstance(entry, dict):
                    continue
                provider = _resolve_provider_alias(entry.get("provider"))
                if provider:
                    detected_providers.add(provider)
                    _append_model_id(provider, entry.get("model"))

        for entry in _custom_provider_entries(cfg):
            provider_name = str(entry.get("name") or "").strip()
            provider_slug = _custom_provider_slug_from_name(provider_name) or "custom"
            if provider_slug != "custom":
                named_custom_groups.setdefault(
                    provider_slug,
                    {"name": provider_name, "models": []},
                )
            detected_providers.add(provider_slug)

            configured_ids: list[str] = []
            model_id = str(entry.get("model") or "").strip()
            if model_id:
                configured_ids.append(model_id)
            raw_models = entry.get("models")
            if isinstance(raw_models, dict):
                for key in raw_models:
                    if isinstance(key, str) and key.strip() and key.strip() not in configured_ids:
                        configured_ids.append(key.strip())
            elif isinstance(raw_models, list):
                for item in raw_models:
                    if isinstance(item, dict):
                        candidate = str(
                            item.get("id") or item.get("model") or item.get("name") or ""
                        ).strip()
                    else:
                        candidate = str(item or "").strip()
                    if candidate and candidate not in configured_ids:
                        configured_ids.append(candidate)

            for configured_id in configured_ids:
                label = _get_label_for_model(configured_id, [])
                if provider_slug == "custom":
                    custom_group_models.append({"id": configured_id, "label": label})
                else:
                    named_custom_groups[provider_slug]["models"].append(
                        {"id": configured_id, "label": label}
                    )
                _append_model_id(provider_slug, configured_id)

        if cfg_base_url:
            detected_providers.add(
                _named_custom_provider_slug_for_base_url(cfg_base_url, cfg)
                or active_provider
                or "custom"
            )

        if detected_providers:
            detected_providers = {
                _canonicalise_provider_id(provider_id) or provider_id
                for provider_id in detected_providers
                if provider_id
            }

        groups: list[dict] = []
        for pid in sorted(detected_providers):
            if pid.startswith("custom:"):
                custom_group = named_custom_groups.get(pid, {})
                group_models = copy.deepcopy(custom_group.get("models", []))
                if group_models or pid == active_provider:
                    groups.append(
                        {
                            "provider": custom_group.get("name") or pid.replace("custom:", ""),
                            "provider_id": pid,
                            "models": _apply_provider_prefix(
                                group_models,
                                pid,
                                active_provider,
                            ),
                        }
                    )
                continue

            if pid == "custom":
                group_models = copy.deepcopy(custom_group_models)
                for model_id in configured_model_ids.get(pid, []):
                    if not any(m.get("id") == model_id for m in group_models):
                        group_models.append(
                            {"id": model_id, "label": _get_label_for_model(model_id, [])}
                        )
                if group_models or cfg_base_url or pid == active_provider:
                    groups.append(
                        {
                            "provider": _PROVIDER_DISPLAY.get(pid, "Custom"),
                            "provider_id": pid,
                            "models": _apply_provider_prefix(
                                group_models,
                                pid,
                                active_provider,
                            ),
                        }
                    )
                continue

            provider_name = _PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())
            raw_key = canonical_to_raw_provider_key.get(pid, pid)
            provider_cfg = _get_provider_cfg(raw_key)
            raw_models = []
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                cfg_models = provider_cfg["models"]
                if isinstance(cfg_models, dict):
                    raw_models = [{"id": key, "label": key} for key in cfg_models.keys()]
                elif isinstance(cfg_models, list):
                    raw_models = []
                    for item in cfg_models:
                        if isinstance(item, dict):
                            model_id = (
                                item.get("id")
                                or item.get("model")
                                or item.get("name")
                            )
                            if not model_id:
                                continue
                            raw_models.append(
                                {
                                    "id": model_id,
                                    "label": item.get("label", model_id),
                                }
                            )
                        elif item:
                            raw_models.append({"id": item, "label": item})
            if not raw_models:
                raw_models = copy.deepcopy(_PROVIDER_MODELS.get(pid, []))
            for model_id in configured_model_ids.get(pid, []):
                if model_id and not any(m.get("id") == model_id for m in raw_models):
                    raw_models.append(
                        {"id": model_id, "label": _get_label_for_model(model_id, groups)}
                    )
            if raw_models:
                groups.append(
                    {
                        "provider": provider_name,
                        "provider_id": pid,
                        "models": _apply_provider_prefix(raw_models, pid, active_provider),
                    }
                )

        if default_model:
            all_model_ids = {
                str(model.get("id") or "")
                for group in groups
                for model in group.get("models", [])
            }
            if default_model not in all_model_ids and f"@{active_provider}:{default_model}" not in all_model_ids:
                label = _get_label_for_model(default_model, groups)
                target_group = next(
                    (group for group in groups if group.get("provider_id") == active_provider),
                    None,
                )
                if target_group is not None:
                    target_group.setdefault("models", []).insert(0, {"id": default_model, "label": label})
                elif groups:
                    groups.append(
                        {
                            "provider": "Default",
                            "provider_id": active_provider or "default",
                            "models": [{"id": default_model, "label": label}],
                        }
                    )

        _deduplicate_model_ids(groups)
        groups = [
            group
            for group in groups
            if group.get("models") or str(group.get("provider_id") or "").startswith("custom:")
        ]

        providers_with_keys: set[str] = set()
        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid in _pool:
                    _canonical = _canonicalise_provider_id(_pid)
                    if _canonical:
                        providers_with_keys.add(_canonical)
        except Exception:
            pass
        try:
            for _pk, _pv in providers_cfg.items():
                if isinstance(_pv, dict) and (
                    _pv.get("api_key")
                    or _pv.get("key_env")
                    or _pv.get("base_url")
                ):
                    _canonical = _canonicalise_provider_id(_pk)
                    if _canonical:
                        providers_with_keys.add(_canonical)
        except Exception:
            pass

        def _group_sort_key(group: dict) -> tuple[int, str]:
            provider_id = str(group.get("provider_id") or "")
            if provider_id == active_provider:
                return (0, provider_id)
            if provider_id.startswith("custom:"):
                return (1, provider_id)
            if provider_id in providers_with_keys:
                return (2, provider_id)
            return (3, provider_id)

        groups.sort(key=_group_sort_key)

        model_aliases: dict[str, str] = {}
        try:
            raw_aliases = cfg.get("model", {}).get("aliases", {})
            if isinstance(raw_aliases, dict):
                model_aliases = {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_aliases.items()
                    if k and v
                }
        except Exception:
            pass

        if not groups and default_model:
            return copy.deepcopy(_minimal_static_models_catalog())

        return {
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _configured_model_badges_from_static_catalog(
                groups,
                active_provider=active_provider,
                default_model=default_model,
            ),
            "groups": groups,
            "aliases": model_aliases,
        }
    except Exception:
        logger.debug("static models catalog build failed", exc_info=True)
        return copy.deepcopy(_minimal_static_models_catalog())

# Cache for credential pool results -- calling load_pool() per-provider per-server
# session is expensive (~10s for zai due to endpoint probing).  The credential pool
# only changes when the user adds/removes credentials, which is rare; a 24h TTL
# is plenty safe and ensures get_available_models() cold paths are fast.
_CREDENTIAL_POOL_CACHE: dict[tuple[str, str], tuple[float, "CredentialPool"]] = {}  # noqa: F821  forward-ref string annotation, resolved at runtime  # (profile_tag, pid) -> (ts, pool)


def _credential_pool_profile_tag() -> str:
    """Active-profile identity for the credential-pool cache key.

    The credential pool is per-Hermes-profile (it lives in that profile's
    auth.json). Keying the process-global cache by provider id ALONE lets a
    pool loaded under profile A satisfy a lookup under profile B in the same
    server process — so a custom provider configured only in A would falsely
    report configured in B (and then 401 at request time). Scoping every
    cache key by the active profile's auth-store path keeps pools from
    crossing profile boundaries.
    """
    try:
        return str(_get_auth_store_path())
    except Exception:
        return ""


def _pool_entry_payloads(provider_id: str) -> list[dict[str, Any]]:
    """Return explicit credential-pool entry payloads for the active profile.

    Readonly profile scopes must not let ``load_pool()`` seed from process env,
    because that can materialize server-default credentials into a named
    profile's auth store. In that mode, read raw auth.json payloads only.
    """
    _pid = _resolve_provider_alias(provider_id)
    if bool(getattr(_thread_ctx, "block_process_env_fallback", False)):
        try:
            from hermes_cli.auth import read_credential_pool as _read_credential_pool

            raw_entries = _read_credential_pool(_pid)
        except ImportError:
            return []
        payloads: list[dict[str, Any]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            if _is_ambient_gh_cli_entry(
                str(entry.get("source", "") or ""),
                str(entry.get("label", "") or ""),
                str(entry.get("key_source", "") or ""),
            ):
                continue
            payloads.append(dict(entry))
        return payloads

    try:
        from agent.credential_pool import load_pool as _load_pool

        _ck = (_credential_pool_profile_tag(), _pid)
        _cached = _CREDENTIAL_POOL_CACHE.get(_ck)
        if _cached is not None:
            _cp_ts, _cp_pool = _cached
            if (time.time() - _cp_ts) < 86400.0:
                _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
            else:
                _cp_pool = _load_pool(_pid)
                _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
                _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
        else:
            _cp_pool = _load_pool(_pid)
            _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
            _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
    except ImportError:
        return []

    payloads = []
    for entry in _all_entries:
        if _is_ambient_gh_cli_entry(
            str(getattr(entry, "source", "") or ""),
            str(getattr(entry, "label", "") or ""),
            str(getattr(entry, "key_source", "") or ""),
        ):
            continue
        if hasattr(entry, "to_dict") and callable(entry.to_dict):
            payload = entry.to_dict()
        elif isinstance(entry, dict):
            payload = dict(entry)
        else:
            try:
                payload = dict(vars(entry))
            except TypeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        payload.setdefault("source", str(getattr(entry, "source", "") or ""))
        payload.setdefault("label", str(getattr(entry, "label", "") or ""))
        payload.setdefault("key_source", str(getattr(entry, "key_source", "") or ""))
        runtime_api_key = getattr(entry, "runtime_api_key", None)
        if runtime_api_key:
            payload["runtime_api_key"] = runtime_api_key
        base_url = getattr(entry, "base_url", None)
        if base_url:
            payload["base_url"] = base_url
        inference_base_url = getattr(entry, "inference_base_url", None)
        if inference_base_url:
            payload["inference_base_url"] = inference_base_url
        payloads.append(payload)
    return payloads


def _has_explicit_pool_credentials(provider_id: str) -> bool:
    """Return True when the credential pool has at least one non-ambient entry
    for *provider_id* (i.e. not a gh-cli / GITHUB_TOKEN auto-detect).

    Reuses ``_CREDENTIAL_POOL_CACHE`` so that callers on hot paths (provider
    detection, model listing, live-model fetch) don't pay the ~10s load_pool
    cost more than once per TTL window.
    """
    return bool(_pool_entry_payloads(provider_id))
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


def _get_models_cache_path() -> Path:
    """Return the /api/models disk-cache path for the *active* profile (#3957).

    WebUI profile switching is per-client/cookie scoped (issue #798), but the
    models disk cache used to be a single import-time ``STATE_DIR /
    "models_cache.json"`` shared across every profile.  The cache's
    ``_source_fingerprint`` is profile-specific (it hashes the active profile's
    config.yaml + auth.json), so a non-default profile rejected the shared
    snapshot on every read and cold-rebuilt the catalog — the serial live
    provider probes behind that cold build are what pushed ``/api/models`` (and
    the Settings → Providers panel) past the 30s frontend timeout.

    Profile-key the filename so each profile keeps its own warm cache:
      - default / root profile  → ``models_cache.json``  (unchanged path; no
        migration of the existing file)
      - named profile ``<name>`` → ``models_cache.<name>.json``

    The active profile is resolved per-request via ``get_active_profile_name()``
    (thread-local cookie context), falling back to the module-level default
    path if the profiles module is unavailable (very early boot / import cycle).

    The named-profile path is derived from ``_models_cache_path`` (the
    module-level default), not from ``STATE_DIR`` directly, so the path stays
    correct if the default is repointed (e.g. tests monkeypatch
    ``_models_cache_path`` to an isolated tmp file).
    """
    try:
        from api.profiles import get_active_profile_name, _is_root_profile

        name = (get_active_profile_name() or "").strip()
        if not name or _is_root_profile(name):
            return _models_cache_path
        # Defensive filename sanitization: the cookie-derived profile name is
        # already validated by _PROFILE_ID_RE at the request boundary, but keep
        # the on-disk filename safe regardless of how the name was resolved.
        safe = re.sub(r"[^a-z0-9_-]", "_", name.lower())[:64]
        if not safe:
            return _models_cache_path
        # Splice the profile into the default filename: models_cache.json →
        # models_cache.<safe>.json, keeping the default's parent dir + suffix.
        base = _models_cache_path
        return base.with_name(f"{base.stem}.{safe}{base.suffix}")
    except Exception:
        return _models_cache_path


def _get_auth_store_path() -> Path:
    """Return the auth.json path for the active Hermes profile."""
    try:
        from api.profiles import get_active_hermes_home as _gah

        return _gah() / "auth.json"
    except ImportError:
        return _DEFAULT_HERMES_HOME / "auth.json"


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


def _models_cache_catalog_fingerprint() -> dict:
    """Return non-secret model-catalog identity metadata for cache invalidation.

    The /api/models payload is not only a function of user config/auth files.
    It also depends on the provider/model catalog baked into this module and on
    small local catalogs such as Codex's models_cache.json. Keep this cheap and
    deterministic so a server restart after catalog changes does not keep
    serving an otherwise-valid persisted models_cache.json until the 24h TTL
    expires (#2443).
    """
    catalog_payload = {
        "provider_models": _PROVIDER_MODELS,
        "provider_display": _PROVIDER_DISPLAY,
    }
    try:
        encoded = json.dumps(
            catalog_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        provider_catalog_sha = hashlib.sha256(encoded).hexdigest()
    except Exception:
        provider_catalog_sha = "unavailable"

    codex_home = Path(os.getenv("CODEX_HOME", "").strip() or (HOME / ".codex")).expanduser()
    return {
        "provider_catalog_sha256": provider_catalog_sha,
        "codex_models_cache": _models_cache_file_fingerprint(codex_home / "models_cache.json"),
    }


# Credential-rotation fields inside auth.json that churn on a ~14-minute
# period (credential-pool / OAuth token refresh rewrites the whole file) but
# DO NOT change the set of available providers or models that /api/models
# returns. mtime/size-based fingerprinting (#1699's _models_cache_file_
# fingerprint) treats every one of these rewrites as a cache-invalidating
# change, so the 24h models cache is effectively dead — every few minutes a
# tab pays a full cold get_available_models() rebuild (see RCA t_d127953d /
# t_16551f61). We strip ONLY these known-inert fields and fingerprint the
# rest of auth.json by content, so token rotation no longer busts the cache.
#
# This is a DENY-list, not an allow-list, on purpose: a field we don't know
# about stays IN the fingerprint, so any genuine change to provider
# enablement / endpoint / api-base / model-allow (active_provider, a NEW
# credential_pool entry id, base_url, source, label, key_source, auth_type,
# priority, the providers{} block, …) still correctly invalidates the cache.
# The safety invariant is one-directional: excluding these fields can only
# ever make the fingerprint MORE stable, never make it miss a real
# provider/model-set change — because none of these fields feed
# detected_providers / the catalog in _build_available_models_uncached().
_AUTH_FINGERPRINT_VOLATILE_KEYS = frozenset({
    # Secret material — rotates on refresh, never gates the provider/model set.
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "secret",
    "client_secret",  # rotation-only on purpose; not a model-cache differentiator
    # Expiry / liveness — bumped every refresh, derived from the token above.
    "expires_at",
    "expires_at_ms",
    "expires_in",
    # Per-credential status/telemetry — churns on every request, not config.
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
    "request_count",
    # Whole-file save timestamp — rewritten on every _save_auth_store().
    "updated_at",
})


def _strip_volatile_auth_fields(obj):
    """Recursively drop credential-rotation-only keys from an auth.json tree.

    Pure structural transform; never mutates the input. Any key NOT in the
    deny-list is preserved verbatim so real provider/endpoint changes still
    show through in the fingerprint.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_volatile_auth_fields(v)
            for k, v in obj.items()
            if k not in _AUTH_FINGERPRINT_VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile_auth_fields(v) for v in obj]
    return obj


def _auth_store_semantic_fingerprint(path: Path) -> dict:
    """Return a content fingerprint of auth.json that ignores token churn.

    Unlike _models_cache_file_fingerprint() (mtime_ns + size), this hashes
    the JSON content with the credential-rotation fields stripped, so the
    ~14-min token-refresh rewrite of auth.json does NOT invalidate the 24h
    /api/models cache. A change to anything that actually affects the
    provider/model set (active_provider, a new credential_pool entry, a
    changed base_url/source/label/auth_type, the providers{} block, …)
    still changes the hash and correctly busts the cache.

    Failure modes are deliberately conservative — if the file is missing we
    record that, and if it can't be read/parsed we fall back to the old
    mtime/size fingerprint so behaviour is never *less* safe than #1699.
    """
    p = Path(path).expanduser()
    fp: dict = {"path": str(p)}
    try:
        st = p.stat()
    except OSError:
        fp["missing"] = True
        return fp
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # Unreadable / corrupt / mid-write: fall back to the stat-based
        # fingerprint. Strictly no less safe than the pre-fix behaviour
        # (every write still invalidates) for this rare path only.
        fp["mtime_ns"] = st.st_mtime_ns
        fp["size"] = st.st_size
        fp["semantic"] = "unparsed-fallback"
        return fp
    stripped = _strip_volatile_auth_fields(raw)
    try:
        encoded = json.dumps(
            stripped,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        fp["semantic_sha256"] = hashlib.sha256(encoded).hexdigest()
    except Exception:
        fp["mtime_ns"] = st.st_mtime_ns
        fp["size"] = st.st_size
        fp["semantic"] = "encode-fallback"
    return fp


def _models_cache_source_fingerprint() -> dict:
    """Return the current config/auth/catalog fingerprint for /api/models cache.

    The auth.json axis uses a *content* fingerprint that excludes pure
    credential-rotation fields (see _auth_store_semantic_fingerprint): the
    auth store is rewritten roughly every 14 minutes by token refresh, and
    a stat-based (mtime/size) fingerprint made the 24h cache churn on every
    one of those rewrites (RCA t_16551f61). config.yaml keeps the cheap
    mtime/size fingerprint because it is only rewritten on deliberate user
    edits (which can change anything) and does not churn on a timer.
    """
    return {
        "config_yaml": _models_cache_file_fingerprint(_get_config_path()),
        "auth_json": _auth_store_semantic_fingerprint(_get_auth_store_path()),
        "catalog": _models_cache_catalog_fingerprint(),
    }


def _delete_models_cache_on_disk() -> None:
    try:
        os.unlink(str(_get_models_cache_path()))
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

        cache_path = _get_models_cache_path()
        if not cache_path.exists():
            return None
        with open(cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        if not _is_loadable_disk_cache(cache):
            return None
        # Strip the disk-only metadata before returning, so the in-memory
        # cache shape stays exactly what the rest of the code expects. The
        # disk save path does not persist `aliases`, so reconstruct them from
        # current config to keep the /api/models.aliases contract intact (a
        # disk-cache hit must not silently drop `/model <alias>` resolution).
        return {
            "active_provider": cache["active_provider"],
            "default_model": cache["default_model"],
            "configured_model_badges": cache["configured_model_badges"],
            "groups": cache["groups"],
            "aliases": (
                cache["aliases"]
                if isinstance(cache.get("aliases"), dict)
                else _model_aliases_from_config()
            ),
        }
    except Exception:
        return None


def _model_aliases_from_config() -> dict[str, str]:
    """Build the normalized model-alias map from current config.

    Mirrors the alias construction used by the live and static catalog paths so
    the `/api/models.aliases` contract is consistent across every catalog source
    (live, static, and the stale-disk fallback, which can't read aliases from a
    disk cache that never persisted them).
    """
    try:
        raw_aliases = cfg.get("model", {}).get("aliases", {})
        if isinstance(raw_aliases, dict):
            return {
                str(k).strip(): str(v).strip()
                for k, v in raw_aliases.items()
                if k and v
            }
    except Exception:
        pass
    return {}


def _load_stale_models_cache_from_disk() -> dict | None:
    """Load a shape-valid stale /api/models disk cache for timeout fallback only.

    The main cache loader enforces metadata stamps for a full cold-path cache hit.
    This helper intentionally does not apply that stricter policy, so we can still
    recover a useful fallback payload when the strict loader rejected cache because
    metadata or fingerprint fields are stale. It DOES still enforce the schema
    version: a cross-schema cache can have an incompatible groups/badge shape, so
    serving it to the picker could surface a broken catalog — schema mismatch is a
    hard reject even on the fallback path.
    """
    try:
        import json as _j

        cache_path = _get_models_cache_path()
        if not cache_path.exists():
            return None
        with open(cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        if not _is_valid_models_cache(cache):
            return None
        if cache.get("_schema_version") != _MODELS_CACHE_SCHEMA_VERSION:
            return None
        aliases = cache.get("aliases")
        if not isinstance(aliases, dict):
            # The disk cache save path does not persist `aliases`, so a cache
            # read back from disk lacks them. Defaulting to {} would silently
            # break `/model <alias>` slash-command resolution (static/commands.js
            # resolves slash aliases only from /api/models.aliases) for the
            # duration of the over-budget stale fallback. Reconstruct from
            # current config, mirroring the live/static catalog alias build.
            aliases = _model_aliases_from_config()
        return {
            "active_provider": cache["active_provider"],
            "default_model": cache["default_model"],
            "configured_model_badges": cache["configured_model_badges"],
            "groups": cache["groups"],
            "aliases": aliases,
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
        cache_path = _get_models_cache_path()
        tmp = str(cache_path) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.rename(tmp, str(cache_path))
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
        # Clear the credential pool cache too (all profiles). Without this,
        # tests (and live provider key edits) see a stale CredentialPool from a
        # prior auth_store payload — the test_credential_pool_providers suite was
        # hitting this directly. A full reset is intentionally profile-wide.
        _CREDENTIAL_POOL_CACHE.clear()
    # Also delete the disk cache so the next cold build starts fresh.
    # Disk delete is outside the lock — file I/O shouldn't block other readers.
    _delete_models_cache_on_disk()
    try:
        from api.plugin_providers import invalidate_plugin_model_provider_cache

        invalidate_plugin_model_provider_cache()
    except Exception:
        pass


def invalidate_credential_pool_cache(provider_id: str):
    """Invalidate the credential pool cache for a specific provider.

    Used by the streaming layer's credential self-heal logic (#1401) to
    force a fresh credential pool load after re-reading auth.json.
    """
    global _CREDENTIAL_POOL_CACHE
    with _available_models_cache_lock:
        _cp_tag = _credential_pool_profile_tag()
        _CREDENTIAL_POOL_CACHE.pop((_cp_tag, provider_id), None)
        _CREDENTIAL_POOL_CACHE.pop((_cp_tag, _resolve_provider_alias(provider_id)), None)
    try:
        # api.providers imports from api.config; keep this lazy to avoid
        # import-cycle/module-initialization issues.
        from api.providers import invalidate_account_usage_status_cache

        invalidate_account_usage_status_cache(provider_id)
        invalidate_account_usage_status_cache(_resolve_provider_alias(provider_id))
    except Exception:
        logger.debug("Failed to invalidate account usage status cache", exc_info=True)


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
        # may be called with either, and both paths cache under their own key),
        # scoped to the active profile's cache key.
        _cp_tag = _credential_pool_profile_tag()
        _CREDENTIAL_POOL_CACHE.pop((_cp_tag, provider_id), None)
        _CREDENTIAL_POOL_CACHE.pop((_cp_tag, _resolve_provider_alias(provider_id)), None)
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

    # Check existing groups for a matching label.
    # Skip slash stripping for URI-scheme IDs (e.g. gpt://folder/model) (#3429).
    _has_scheme = lambda s: "://" in s
    _norm = lambda s: (s.split("/", 1)[-1] if ("/" in s and not _has_scheme(s)) else s).replace("-", ".").lower()
    norm_lookup = _norm(lookup_id)
    for g in existing_groups:
        for m in g.get("models", []):
            if m.get("label") and _norm(str(m.get("id", ""))) == norm_lookup:
                return m["label"]

    # Fall back: strip only the first slash-segment (provider prefix),
    # preserving vendor hierarchy for multi-slash IDs (#3360).
    # Skip for URI-scheme IDs whose slashes are path separators (#3429).
    bare = lookup_id.split("/", 1)[1] if ("/" in lookup_id and not _has_scheme(lookup_id)) else lookup_id
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


def get_available_models(*, prefer_cache: bool = False, force_refresh: bool = False) -> dict:
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

    ``prefer_cache=True`` resolves WITHOUT ever triggering a live provider
    probe: it serves the warm in-memory cache, then the last-known on-disk
    cache, and only as a last resort a network-free minimal catalog
    (config/auth derived). It NEVER does the per-provider live rebuild (the
    Copilot token-exchange HTTPS call et al.). This is the path a
    server-initiated wakeup turn (Option Z) takes so a cold catalog can never
    block the wakeup chat/start on a flaky network. A normal human request
    leaves this False and keeps the full live-discovery behaviour.

    ``force_refresh=True`` is an internal escape hatch for bounded freshness
    checks that need a real live rebuild while preserving the default cache
    contract for every existing caller.
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
            stripped_at_provider = False
            # Strip @provider: prefix (e.g., @custom:jingdong:GLM-5 -> GLM-5).
            # Defensive: if the last segment is empty (trailing colon, malformed
            # config), keep the original to avoid collapsing distinct IDs to ''.
            if s.startswith("@") and ":" in s:
                # Strip @provider: prefix, preserving remaining hierarchy including
                # colon-suffixed model IDs like provider/model:free (#3959).
                colon_idx = s.index(":", 1)
                candidate = s[colon_idx + 1:]
                stripped_at_provider = bool(candidate)
                s = candidate or s
            # Skip slash-based stripping for URI-scheme IDs (e.g.
            # gpt://folder/model/latest) whose slashes are path separators,
            # not provider delimiters (#3429).
            if "://" not in s:
                if (
                    not stripped_at_provider
                    and "/" in s
                    and ":" in s
                    and s.index(":") < s.index("/")
                ):
                    s = s[s.index("/") + 1 :] or s
                # Strip only the first slash-segment (provider prefix), preserving
                # any remaining vendor hierarchy.  Using parts[-1] here previously
                # discarded ALL segments except the last, collapsing distinct
                # multi-slash IDs like 'vendor_a/deepseek-v4-pro' and
                # 'vendor_b/deepseek/deepseek-v4-pro' to the same key (#3360).
                if "/" in s:
                    stripped = s.split("/", 1)[1]
                    s = stripped or s
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

        # Normalize active_provider to its canonical key.  Named custom
        # providers are first-class provider ids in WebUI routing; accept the
        # user-facing name from config.yaml (``provider: ollama-local``) and
        # route it through the same ``custom:<name>`` slug the picker emits.
        if active_provider:
            active_provider = _resolve_configured_provider_id(
                active_provider,
                cfg,
                base_url=cfg_base_url,
            )

        # 2. Read auth store (active_provider fallback + credential_pool inspection)
        auth_store = {}
        auth_store_path = _get_auth_store_path()
        if auth_store_path.exists():
            try:
                import json as _j

                auth_store = _j.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = _resolve_configured_provider_id(
                        auth_store.get("active_provider"),
                        cfg,
                        base_url=cfg_base_url,
                    )
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
                            # Check credential pool cache first (profile-scoped key
                            # so a pool loaded under another profile can't leak in).
                            _ck = (_credential_pool_profile_tag(), _pid)
                            _cached = _CREDENTIAL_POOL_CACHE.get(_ck)
                            if _cached is not None:
                                _cp_ts, _cp_pool = _cached
                                if (time.time() - _cp_ts) < 86400.0:
                                    _all_entries = _cp_pool.entries()
                                else:
                                    _lp_t0 = time.monotonic()
                                    _cp_pool = _load_pool(_pid)
                                    _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
                                    _all_entries = _cp_pool.entries()
                            else:
                                _lp_t0 = time.monotonic()
                                _cp_pool = _load_pool(_pid)
                                _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
                                _all_entries = _cp_pool.entries()
                            _explicit = [
                                e for e in _all_entries
                                if not _is_ambient_gh_cli_entry(
                                    str(getattr(e, "source", "") or ""),
                                    str(getattr(e, "label", "") or ""),
                                    str(getattr(e, "key_source", "") or ""),
                                )
                            ]
                            if _explicit and _is_known_model_provider(_canonical_pid):
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
                            _canonical_pid = _resolve_provider_alias(str(_pid))
                            if _is_known_model_provider(_canonical_pid):
                                detected_providers.add(_canonical_pid)
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
                hermes_env_path = _DEFAULT_HERMES_HOME / ".env"
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
                "XIAOMI_API_KEY",
                "OPENCODE_ZEN_API_KEY",
                "OPENCODE_GO_API_KEY",
                "OPENCODE_API_KEY",
                "MINIMAX_API_KEY",
                "MINIMAX_CN_API_KEY",
                "XAI_API_KEY",
                "MISTRAL_API_KEY",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
            ):
                val = _thread_local_env_value(k)
                if val:
                    all_env[k] = val
            if all_env.get("ANTHROPIC_API_KEY"):
                detected_providers.add("anthropic")
            if all_env.get("OPENAI_API_KEY"):
                # hermes-agent registers its OPENAI_API_KEY/OPENAI_BASE_URL provider
                # under the slug `openai-api` (there is no bare `openai` in the agent
                # registry — only `openai-api` and `openai-codex`). Detecting `openai`
                # here would emit `@openai:` picker entries the agent can't resolve on
                # the send path, so detect `openai-api` to match the registry (#3443).
                detected_providers.add("openai-api")
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
            if all_env.get("XIAOMI_API_KEY"):
                detected_providers.add("xiaomi")
            if all_env.get("XAI_API_KEY"):
                detected_providers.add("x-ai")
            if all_env.get("MISTRAL_API_KEY"):
                detected_providers.add("mistralai")
            if all_env.get("OPENCODE_ZEN_API_KEY") or all_env.get("OPENCODE_API_KEY"):
                detected_providers.add("opencode-zen")
            if all_env.get("OPENCODE_GO_API_KEY") or all_env.get("OPENCODE_API_KEY"):
                detected_providers.add("opencode-go")
            # AWS Bedrock uses IAM credentials rather than a single API key.
            # Detect when both access key and secret are available (#2720).
            if all_env.get("AWS_ACCESS_KEY_ID") and all_env.get("AWS_SECRET_ACCESS_KEY"):
                detected_providers.add("bedrock")
            # LM Studio: detect via LM_API_KEY + LM_BASE_URL in ~/.hermes/.env
            if all_env.get("LM_API_KEY") and all_env.get("LM_BASE_URL"):
                detected_providers.add("lmstudio")

        # Also detect providers explicitly listed in config.yaml providers section.
        # A user may configure a provider key via config.yaml providers.<name>.api_key
        # without setting the corresponding env var. (#604)
        #
        # Gating: only seed picker groups for keys whose canonical id is known
        # to ``_PROVIDER_MODELS`` / ``_PROVIDER_DISPLAY``, or whose value is a
        # dict-shaped provider config (custom/local). Scalar siblings under
        # ``providers:`` (e.g. ``providers.only_configured: true``) are config
        # flags, not providers, and must not render as phantom picker groups
        # like ``Only-Configured`` (#2399).
        #
        # Canonicalise the id slug here so a user with ``providers.opencode_go``
        # (underscore variant) doesn't see TWO provider groups in the picker —
        # one for the canonical ``opencode-go`` from active_provider detection
        # and a phantom ``Opencode_Go`` group for the config-key form (#1568).
        # The same applies to mixed-case ids like ``OpenCode-Go`` and to
        # legitimate aliases like ``z-ai`` → ``zai``.
        _cfg_providers = _get_providers_cfg()
        # Map canonical provider IDs back to raw config keys so the
        # generic-provider branch can preserve mixed-case/underscore
        # provider_cfg values (#2245).
        _canonical_to_raw_provider_key: dict[str, str] = {}
        if isinstance(_cfg_providers, dict):
            for _pid_key, _provider_cfg in _cfg_providers.items():
                _canonical = _canonicalise_provider_id(_pid_key)
                if not _canonical:
                    continue

                # See the gating comment on the block above. ``_PROVIDER_MODELS``
                # / ``_PROVIDER_DISPLAY`` membership accepts known providers and
                # aliases; ``isinstance(_provider_cfg, dict)`` accepts custom
                # entries that supply their own models/api_key/base_url. (#2399)
                _is_known_provider = (
                    _canonical in _PROVIDER_MODELS
                    or _canonical in _PROVIDER_DISPLAY
                    or _is_plugin_model_provider(_canonical)
                )
                _is_provider_config = isinstance(_provider_cfg, dict)
                if not (_is_known_provider or _is_provider_config):
                    continue

                _canonical_to_raw_provider_key.setdefault(_canonical, _pid_key)
                detected_providers.add(_canonical)

        def _configured_provider_for_base_url(base_url: object) -> str:
            target = _normalize_base_url_for_match(base_url)
            if not target:
                return ""

            if isinstance(model_cfg, dict):
                model_base_url = _normalize_base_url_for_match(model_cfg.get("base_url"))
                if model_base_url == target:
                    provider_hint = _resolve_configured_provider_id(
                        model_cfg.get("provider"),
                        cfg,
                        base_url=base_url,
                    )
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
                        return _custom_provider_slug_from_name(entry_name)
                    return "custom"

            return ""

        def _models_endpoint_for_base_url(base_url: str) -> str:
            base = str(base_url or "").strip().rstrip("/")
            if base.endswith("/v1"):
                return base + "/models"
            return base + "/v1/models"

        def _extract_model_entries_from_payload(data: object, provider: str) -> list[dict]:
            models_list = []
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    models_list = data["data"]
                elif "models" in data and isinstance(data["models"], list):
                    models_list = data["models"]
            models = []
            seen = set()
            for model in models_list:
                if not isinstance(model, dict):
                    continue
                model_id = (
                    model.get("id", "")
                    or model.get("name", "")
                    or model.get("model", "")
                )
                model_name = model.get("name", "") or model.get("model", "") or model_id
                model_id = str(model_id or "").strip()
                model_name = str(model_name or "").strip()
                if not model_id or not model_name or model_id in seen:
                    continue
                seen.add(model_id)
                label = _format_ollama_label(model_id) if provider in ("ollama", "ollama-cloud") else model_name
                models.append({"id": model_id, "label": label})
            return models

        def _custom_endpoint_error(
            provider: str,
            exc: Exception,
            *,
            code: int | None = None,
        ) -> dict:
            provider_label = str(provider or "custom").replace("custom:", "")
            status_code = code if code is not None else getattr(exc, "code", None)
            if status_code in (401, 403):
                return {
                    "kind": "auth",
                    "code": int(status_code),
                    "message": f"Models endpoint returned {status_code} — check the API key for {provider_label}.",
                }
            if isinstance(status_code, int):
                return {
                    "kind": "http",
                    "code": int(status_code),
                    "message": f"Models endpoint returned {status_code} for {provider_label}; see logs.",
                }
            return {
                "kind": "network",
                "code": None,
                "message": f"Models endpoint unreachable for {provider_label}; verify base_url.",
            }

        def _read_custom_endpoint_models(
            base_url: object,
            provider: str,
            *,
            api_key: object = "",
            trusted_base_urls: tuple[object, ...] = (),
        ) -> tuple[list[dict], dict | None]:
            base = str(base_url or "").strip()
            if not base:
                return [], None
            try:
                import ipaddress
                import urllib.error
                import urllib.request
                import socket

                endpoint_url = _models_endpoint_for_base_url(base)
                headers = {}
                key = str(api_key or "").strip()
                if key:
                    headers["Authorization"] = f"Bearer {key}"

                # User-configured custom provider endpoints are explicitly trusted,
                # but keep the same private-IP guard for non-matching targets used by
                # the legacy active model.base_url path.
                _ssrf_trusted_hosts: set[str] = set()
                for trusted in (base, *trusted_base_urls):
                    _cp_parsed = urlparse(
                        str(trusted) if "://" in str(trusted) else f"http://{trusted}"
                    )
                    if _cp_parsed.hostname:
                        _ssrf_trusted_hosts.add(_cp_parsed.hostname.lower())

                parsed_url = urlparse(endpoint_url if "://" in endpoint_url else f"http://{endpoint_url}")
                if parsed_url.scheme not in ("", "http", "https"):
                    raise ValueError(f"Invalid URL scheme: {parsed_url.scheme}")
                if parsed_url.hostname:
                    try:
                        resolved_ips = socket.getaddrinfo(parsed_url.hostname, None)
                        for _, _, _, _, addr in resolved_ips:
                            addr_obj = ipaddress.ip_address(addr[0])
                            if addr_obj.is_private or addr_obj.is_loopback or addr_obj.is_link_local:
                                host_l = (parsed_url.hostname or "").lower()
                                is_known_local = any(
                                    k in host_l
                                    for k in ("ollama", "localhost", "127.0.0.1", "lmstudio", "lm-studio")
                                ) or host_l in _ssrf_trusted_hosts
                                if not is_known_local:
                                    raise ValueError(f"SSRF: resolved hostname to private IP {addr[0]}")
                    except socket.gaierror:
                        pass

                req = urllib.request.Request(endpoint_url, method="GET")
                req.add_header("User-Agent", "OpenAI/Python 1.0")
                for k, v in headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS) as response:  # nosec B310
                    data = json.loads(response.read().decode("utf-8"))
                return _extract_model_entries_from_payload(data, provider), None
            except urllib.error.HTTPError as exc:
                error = _custom_endpoint_error(provider, exc, code=getattr(exc, "code", None))
                logger.debug("Custom endpoint models fetch failed for provider %s: %s", provider, error)
                return [], error
            except Exception as exc:
                error = _custom_endpoint_error(provider, exc)
                logger.debug("Custom endpoint unreachable or misconfigured for provider %s: %s", provider, error)
                return [], error

        # 4. Fetch models from custom endpoint if base_url is configured
        auto_detected_models = []
        auto_detected_models_by_provider: dict[str, list[dict]] = {}
        if cfg_base_url:
            base_url = cfg_base_url.strip()
            configured_provider = _configured_provider_for_base_url(base_url)
            provider = configured_provider or "custom"
            provider_from_config = bool(configured_provider)
            parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
            host = (parsed.netloc or parsed.path).lower()

            if parsed.hostname and not provider_from_config:
                try:
                    import ipaddress

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
                    api_key = (all_env.get(key) or _thread_local_env_value(key) or "").strip()
                    if api_key:
                        break

            _trusted_custom_bases: list[object] = [cfg_base_url]
            _custom_providers_for_trust = cfg.get("custom_providers", [])
            if isinstance(_custom_providers_for_trust, list):
                _trusted_custom_bases.extend(
                    _cp.get("base_url")
                    for _cp in _custom_providers_for_trust
                    if isinstance(_cp, dict) and _cp.get("base_url")
                )
            _active_endpoint_models, _active_endpoint_error = _read_custom_endpoint_models(
                base_url,
                provider,
                api_key=api_key,
                trusted_base_urls=tuple(_trusted_custom_bases),
            )
            for auto_model in _active_endpoint_models:
                auto_detected_models.append(auto_model)
                provider_key = provider.lower()
                auto_detected_models_by_provider.setdefault(provider_key, []).append(auto_model)
                detected_providers.add(provider_key)

        _custom_providers_cfg = cfg.get("custom_providers", [])
        _named_custom_groups: dict = {}
        _named_custom_errors: dict[str, dict] = {}
        if isinstance(_custom_providers_cfg, list):
            _seen_custom_ids = set()
            for _cp in _custom_providers_cfg:
                if not isinstance(_cp, dict):
                    continue
                _cp_name = (_cp.get("name") or "").strip()
                _slug = _custom_provider_slug_from_name(_cp_name) if _cp_name else None
                if _slug and _slug not in _named_custom_groups:
                    _named_custom_groups[_slug] = (_cp_name, [])

                _cp_base_url = str(_cp.get("base_url") or "").strip()
                _cp_api_key = str(_cp.get("api_key") or "").strip()
                if not _cp_api_key:
                    _cp_key_env = str(_cp.get("key_env") or "").strip()
                    if _cp_key_env:
                        _cp_api_key = _thread_local_env_value(_cp_key_env).strip()
                # Fallback: check credential pool for both api_key and base_url
                if (not _cp_api_key or not _cp_base_url) and _slug:
                    try:
                        from api.config import _has_explicit_pool_credentials
                        if _has_explicit_pool_credentials(_slug):
                            from agent.credential_pool import load_pool
                            _resolved = _resolve_provider_alias(_slug)
                            _pool = load_pool(_resolved)
                            if _pool:
                                _entry = _pool.select()
                                if _entry:
                                    if not _cp_api_key:
                                        _cp_api_key = getattr(_entry, "runtime_api_key", "") or ""
                                    if not _cp_base_url:
                                        _cp_base_url = str(getattr(_entry, "base_url", "") or "").strip()
                    except ImportError:
                        pass

                if _slug and _cp_base_url:
                    # Check if user has configured models in config.yaml —
                    # configured models take priority over live /v1/models
                    # discovery (same as hermes-agent model_switch.py Section 4
                    # patch). Without this check, ZenMux and similar aggregator
                    # gateways would show hundreds of online models instead of
                    # the user's curated list.
                    _cp_configured_models = _cp.get("models")
                    _cp_has_configured_models = (
                        isinstance(_cp_configured_models, (dict, list))
                        and len(_cp_configured_models) > 0
                    )
                    _live_models = auto_detected_models_by_provider.get(_slug)
                    _live_error = None
                    if _cp_has_configured_models:
                        # Skip the live /v1/models probe when an allowlist
                        # exists — the curated list wins and probe failures
                        # should not surface as a user-facing diagnostic in
                        # that case. Still respect any pre-warm result that
                        # ``auto_detected_models_by_provider`` already
                        # populated (cheap to keep).
                        if _live_models is None:
                            _live_models = []
                    elif _live_models is None:
                        _live_models, _live_error = _read_custom_endpoint_models(
                            _cp_base_url,
                            _slug,
                            api_key=_cp_api_key,
                            trusted_base_urls=(_cp_base_url,),
                        )
                    if _live_error:
                        _named_custom_errors[_slug] = _live_error
                        detected_providers.add(_slug)
                    for _live_model in _live_models:
                        _live_id = str(_live_model.get("id") or "").strip()
                        if not _live_id:
                            continue
                        _dedup_key = f"{_slug}:{_live_id}"
                        if _dedup_key in _seen_custom_ids:
                            continue
                        _seen_custom_ids.add(_dedup_key)
                        detected_providers.add(_slug)
                        _cp_option_id = _live_id
                        if active_provider != _slug and not _cp_option_id.startswith("@"):
                            _cp_option_id = f"@{_slug}:{_cp_option_id}"
                        _named_custom_groups[_slug][1].append(
                            {"id": _cp_option_id, "label": _live_model.get("label") or _get_label_for_model(_live_id, [])}
                        )

                # Collect configured model IDs as a fallback/sticky entry after live discovery.
                _cp_model_ids: list[str] = []
                _cp_model = _cp.get("model", "")
                if _cp_model:
                    _cp_model_ids.append(_cp_model)
                _cp_models_dict = _cp.get("models")
                if isinstance(_cp_models_dict, dict):
                    for _m_id in _cp_models_dict:
                        if isinstance(_m_id, str) and _m_id.strip() and _m_id not in _cp_model_ids:
                            _cp_model_ids.append(_m_id.strip())
                elif isinstance(_cp_models_dict, list):
                    for _item in _cp_models_dict:
                        if isinstance(_item, str):
                            _mid = _item.strip()
                            if _mid and _mid not in _cp_model_ids:
                                _cp_model_ids.append(_mid)
                        elif isinstance(_item, dict):
                            _mid = str(_item.get("id") or _item.get("model") or _item.get("name") or "").strip()
                            if _mid and _mid not in _cp_model_ids:
                                _cp_model_ids.append(_mid)

                for _cp_model in _cp_model_ids:
                    _dedup_key = f"{_slug}:{_cp_model}" if _slug else _cp_model
                    if _cp_model and _dedup_key not in _seen_custom_ids:
                        _cp_label = _get_label_for_model(_cp_model, [])
                        _seen_custom_ids.add(_dedup_key)
                        if _slug:
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

        _named_custom_slugs = _named_custom_provider_slugs(cfg)
        _base_matched_named_slug = _named_custom_provider_slug_for_base_url(cfg_base_url, cfg)
        if _base_matched_named_slug and _named_custom_slugs:
            for _pid in list(detected_providers):
                _pid_norm = str(_pid or "").strip().lower()
                if _pid_norm.startswith("custom:") and _pid_norm not in _named_custom_slugs:
                    detected_providers.discard(_pid)

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
            _picker_selected_model_id = (
                (model_cfg.get("model") if isinstance(model_cfg, dict) else None)
                or default_model
                or None
            )

            def _append_picker_group(
                provider_label: str,
                provider_id: str,
                raw_models: list[dict] | None,
                *,
                models_endpoint_error: dict | None = None,
                apply_prefix: bool = True,
                decorate_overflow_label: bool = False,
                allow_empty: bool = False,
            ) -> None:
                picker_models = copy.deepcopy(raw_models or [])
                if apply_prefix:
                    picker_models = _apply_provider_prefix(picker_models, provider_id, active_provider)
                visible_models, extra_models = _split_picker_overflow_models(
                    picker_models,
                    selected_model_id=_picker_selected_model_id,
                    provider_id=provider_id,
                )
                if not (visible_models or extra_models or models_endpoint_error or allow_empty):
                    return
                group_entry = {
                    "provider": provider_label,
                    "provider_id": provider_id,
                    "models": visible_models,
                }
                if decorate_overflow_label and extra_models:
                    group_entry["provider"] = (
                        f"{provider_label} ({len(visible_models)} of {len(visible_models) + len(extra_models)})"
                    )
                if extra_models:
                    group_entry["extra_models"] = extra_models
                if models_endpoint_error:
                    group_entry["models_endpoint_error"] = models_endpoint_error
                groups.append(group_entry)

            for pid in sorted(detected_providers):
                # Custom-provider PIDs are populated above via the
                # _named_custom_groups branch (or skipped intentionally).
                # They MUST NOT fall through to the auto_detected_models
                # fallback below, otherwise the active provider's models
                # get copied into a phantom Custom group with mismatched
                # provider prefixes (#1881).
                if pid.startswith("custom:"):
                    if pid in _named_custom_groups:
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
                        if _nc_models or pid in _named_custom_errors:
                            _append_picker_group(
                                _nc_display,
                                pid,
                                _nc_models,
                                models_endpoint_error=_named_custom_errors.get(pid),
                                apply_prefix=False,
                            )
                    continue
                provider_name = _effective_provider_display_name(pid, _PROVIDER_DISPLAY)
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
                        free_tier_models = []
                        selected_free_tier_model = None
                        with _urlreq.urlopen(_req, timeout=8.0) as _resp:
                            _payload = json.loads(_resp.read().decode())
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
                            _entry = {"id": _mid, "label": _label}
                            free_tier_models.append(_entry)
                            if _model_matches_picker_selection(
                                _mid,
                                _picker_selected_model_id,
                                "openrouter",
                            ):
                                selected_free_tier_model = _entry
                        if len(free_tier_models) > _OPENROUTER_FREE_TIER_AUGMENT_CAP:
                            free_tier_models = free_tier_models[:_OPENROUTER_FREE_TIER_AUGMENT_CAP]
                            if (
                                selected_free_tier_model
                                and not any(
                                    m.get("id") == selected_free_tier_model.get("id")
                                    for m in free_tier_models
                                )
                            ):
                                free_tier_models[-1] = selected_free_tier_model
                        for _entry in free_tier_models:
                            seen_ids.add(_entry["id"])
                            raw_models.append(_entry)
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

                    _append_picker_group("OpenRouter", "openrouter", raw_models)
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
                        _append_picker_group(provider_name, pid, raw_models)
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
                        _append_picker_group(provider_name, pid, raw_models)
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
                    live_fetch_failed = False
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        live_ids = _provider_model_ids("nous") or []
                    except Exception:
                        logger.warning("Failed to load Nous Portal models from hermes_cli")
                        live_ids = []
                        live_fetch_failed = True

                    if live_ids:
                        featured_ids, extras_ids = _build_nous_featured_set(
                            live_ids,
                            selected_model_id=_picker_selected_model_id,
                        )
                        ordered_ids = featured_ids + extras_ids
                        raw_models = [
                            {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                            for mid in ordered_ids
                        ]
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
                        _append_picker_group(
                            provider_name,
                            pid,
                            raw_models,
                            apply_prefix=False,
                            decorate_overflow_label=True,
                        )
                elif pid == "lmstudio":
                    # LM Studio is a local server — fetch live loaded models via
                    # the OpenAI-compatible /v1/models endpoint (#WebUI).
                    #
                    # Two-tier lookup, each in its own try so a failure in one
                    # does not abort the other (the bug pattern that broke
                    # tests/test_issue1527_lmstudio_base_url_classification on
                    # CI environments where hermes_cli isn't importable —
                    # ImportError in the cli tier was hijacking the whole
                    # branch and silently skipping the urlopen fallback).
                    raw_models = []
                    lm_ids: list[str] = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids
                        lm_ids = _provider_model_ids("lmstudio") or []
                    except Exception:
                        logger.debug("hermes_cli LM Studio lookup unavailable; using urlopen fallback")

                    if lm_ids:
                        raw_models = [{"id": mid, "label": mid} for mid in lm_ids]
                    else:
                        # Fallback: fetch /models directly from the configured
                        # base URL. Looks for the URL in either
                        # `cfg["providers"]["lmstudio"]["base_url"]` or
                        # `cfg["model"]["base_url"]` (via _get_provider_base_url),
                        # so the historical model-block config shape still works.
                        lm_cfg = _get_provider_cfg("lmstudio")
                        lm_base_url = _get_provider_base_url("lmstudio") or ""
                        lm_api_key = str(lm_cfg.get("api_key") or "").strip()
                        if lm_base_url:
                            headers = {"User-Agent": "OpenAI/Python 1.0"}
                            if lm_api_key:
                                headers["Authorization"] = f"Bearer {lm_api_key}"
                            endpoint = (lm_base_url + "/models").rstrip("/")
                            try:
                                import urllib.request as _urlreq
                                req = _urlreq.Request(endpoint, method="GET", headers=headers)
                                with _urlreq.urlopen(req, timeout=5) as resp:
                                    lm_data = json.loads(resp.read().decode())
                                for m in (lm_data.get("data") or []):
                                    if isinstance(m, dict):
                                        mid = str(m.get("id") or "").strip()
                                        if mid and {"id": mid, "label": mid} not in raw_models:
                                            raw_models.append({"id": mid, "label": mid})
                            except Exception:
                                logger.debug("LM Studio /models fetch failed at %s", endpoint)

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif (
                    pid in _PROVIDER_MODELS
                    or pid in _canonical_to_raw_provider_key
                    or _is_plugin_model_provider(pid)
                ):
                    # Look up provider_cfg using the original raw key from
                    # config.yaml so that mixed-case / underscore keys like
                    # ``CLIPpoxy`` or ``snake_case_provider`` still resolve
                    # (#2245).  Fall back to the canonical pid for providers
                    # that appear in _PROVIDER_MODELS but not in cfg.
                    _raw_key = _canonical_to_raw_provider_key.get(pid, pid)
                    provider_cfg = _get_provider_cfg(_raw_key)
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
                            raw_models = [{"id": k["id"] if isinstance(k, dict) else k,
                                            "label": k.get("label", k["id"]) if isinstance(k, dict) else k}
                                           for k in cfg_models]

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
                    _append_picker_group(provider_name, pid, raw_models)
                else:
                    detected_models = auto_detected_models_by_provider.get(pid)
                    if detected_models:
                        models_for_group = copy.deepcopy(detected_models)
                    elif auto_detected_models and (pid == "custom" or _is_known_model_provider(pid)):
                        # Don't fall back to the global auto_detected_models
                        # list for the bare "custom" PID when the active
                        # provider is something concrete (e.g. ai-gateway,
                        # openrouter). Those auto-detected entries already
                        # belong to the active provider's group — copying
                        # them into a Custom group too produces phantom
                        # duplicates with mismatched prefixes (#1881).
                        if pid == "custom" and active_provider and active_provider != "custom":
                            models_for_group = []
                        else:
                            models_for_group = copy.deepcopy(auto_detected_models)
                    else:
                        # An unrecognized provider id with no catalog of its
                        # own must NOT be painted with the global
                        # auto_detected_models list. Otherwise a non-model
                        # credential_pool key (the Photon plugin's
                        # photon/photon_project/photon_user entries, #4324) or
                        # any future unknown id renders as a phantom provider
                        # carrying the active endpoint's entire model catalog.
                        # Such ids are dropped upstream by
                        # _is_known_model_provider() in the pool-detection
                        # loop; this omission is belt-and-braces matching the
                        # #1572/#7372 "omit rather than misattribute" posture.
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
                        _append_picker_group(
                            provider_name,
                            pid,
                            models_for_group,
                            apply_prefix=False,
                        )
                    elif pid == "custom" and cfg_base_url:
                        # Anonymous custom endpoint: /v1/models probe may have
                        # failed (e.g. llama-server, lightweight relay), but the
                        # chat endpoint itself may still work. Add the group
                        # with an empty model list so the user can type a model
                        # ID manually rather than being blocked by a silent
                        # probe failure (#2542).
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": [],
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
                all_ids_norm = {
                    _norm_model_id(m["id"])
                    for g in groups
                    for bucket_name in ("models", "extra_models")
                    for m in g.get(bucket_name, [])
                }
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

        # Sort groups: active provider first, then custom:* providers,
        # then providers with configured keys, then the rest alphabetically.
        _providers_with_keys: set[str] = set()
        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid in _pool:
                    _providers_with_keys.add(_resolve_provider_alias(str(_pid)))
        except Exception:
            pass
        try:
            _cfg_providers = cfg.get("providers", {})
            if isinstance(_cfg_providers, dict):
                for _pk, _pv in _cfg_providers.items():
                    if isinstance(_pv, dict) and (_pv.get("api_key") or _pv.get("key_env")):
                        _providers_with_keys.add(_resolve_provider_alias(str(_pk)))
        except Exception:
            pass

        def _group_sort_key(g):
            pid = g.get("provider_id") or ""
            if pid == active_provider:
                return (0, pid)
            if pid.startswith("custom:"):
                return (1, pid)
            if pid in _providers_with_keys:
                return (2, pid)
            return (3, pid)
        groups.sort(key=_group_sort_key)

        # 12. Include model aliases so the WebUI frontend can resolve them.
        model_aliases: dict[str, str] = {}
        try:
            raw_aliases = cfg.get("model", {}).get("aliases", {})
            if isinstance(raw_aliases, dict):
                model_aliases = {str(k).strip(): str(v).strip() for k, v in raw_aliases.items() if k and v}
        except Exception:
            pass

        return {
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _build_configured_model_badges(),
            "groups": groups,
            "aliases": model_aliases,
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
    stale_disk_groups = None
    if _available_models_cache is None and not force_refresh:
        disk_groups = _load_models_cache_from_disk()
        if disk_groups is None:
            stale_disk_groups = _load_stale_models_cache_from_disk()
    elif force_refresh:
        stale_disk_groups = _load_stale_models_cache_from_disk()

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
            stale_disk_groups = None

        # Serve from memory cache if fresh
        now = time.monotonic()
        if not force_refresh:
            cached = _get_fresh_memory_models_cache(now)
            if cached is not None:
                return cached

        # Cold path: disk cache hit — use it (fast, no lock contention)
        if disk_groups is not None and not force_refresh:
            _available_models_cache = disk_groups
            _available_models_cache_ts = now
            _available_models_cache_source_fingerprint = _models_cache_source_fingerprint()
            return copy.deepcopy(disk_groups)

        # ── prefer_cache: NEVER run the live provider rebuild ────────────────
        # Server-initiated wakeup turns (Option Z) reach here with a cold
        # cache (the drain thread fires while idle; the catalog warmed by a
        # human's /api/models has expired or was never built). The live
        # rebuild does a Copilot token-exchange HTTPS call per the proven
        # thread-stack; on this WSL/corp network it stalls the wakeup
        # chat/start indefinitely. A wakeup turn does NOT need the full live
        # catalog — _resolve_compatible_session_model_state only needs
        # default_model/active_provider and trusts the persisted session
        # model. Serve a network-free minimal catalog instead and let a later
        # human request do the real live rebuild.
        if prefer_cache:
            # NOTE (Greptile P1): do NOT touch _cache_build_in_progress here.
            # This branch never set the flag (only the cold path below does),
            # and `should_wait` is sampled outside the lock (line ~4964). A
            # concurrent cold-path caller can flip the flag to True after our
            # sample but before we acquire the lock; clearing it here would
            # prematurely release that rebuild's serialization, waking waiters
            # to an empty cache and triggering a second live rebuild. Just
            # serve the network-free minimal catalog and leave the flag alone.
            return copy.deepcopy(_minimal_static_models_catalog())

        # Cold path: full rebuild — only one thread reaches here at a time
        with _cache_build_cv:
            _cache_build_in_progress = True

        # Capture the active per-request profile (#3957). The live provider
        # probe inside the rebuild resolves credentials from os.environ /
        # HERMES_HOME and the disk-cache path/fingerprint from the profile TLS;
        # the detached worker thread below inherits NEITHER, so it must be
        # captured here (on the request thread, where the TLS is valid) and
        # re-bound on the worker. Empty / default for single-profile installs.
        from contextlib import nullcontext as _nullcontext

        _active_profile_name = ""
        _prof_env_request = None
        _prof_scope_worker = None
        try:
            from api.profiles import (
                get_active_profile_name as _gapn,
                profile_env_for_active_request as _prof_env_request,
                profile_scope_for_detached_worker as _prof_scope_worker,
            )
            _active_profile_name = (_gapn() or "").strip()
        except Exception:
            _prof_env_request = None
            _prof_scope_worker = None

        # Legacy synchronous (unbounded) rebuild — opt-in via budget<=0.
        if _LIVE_REBUILD_BUDGET_SECONDS <= 0:
            try:
                # Foreground thread already carries the request-profile TLS;
                # apply the mirrored profile env (no-op for default) for the
                # live probe because provider_model_ids() still has raw
                # os.getenv()/HERMES_HOME readers on this synchronous path.
                _sync_scope = (
                    _prof_env_request("models rebuild (sync)")
                    if _prof_env_request is not None
                    else _nullcontext()
                )
                with _sync_scope:
                    result = _invoke_models_rebuild(_build_available_models_uncached)
            except BaseException:
                # Always reset the flag so waiting threads don't block for 60s
                with _cache_build_cv:
                    _cache_build_in_progress = False
                    _cache_build_cv.notify_all()
                raise
            with _cache_build_cv:
                _available_models_cache = result
                _available_models_cache_ts = time.monotonic()
                _available_models_cache_source_fingerprint = _models_cache_source_fingerprint()
            try:
                _save_models_cache_to_disk(result)
            finally:
                with _cache_build_cv:
                    _cache_build_in_progress = False
                    _cache_build_cv.notify_all()
            return copy.deepcopy(result)

        # ── Bounded rebuild (defense-in-depth) ───────────────────────────────
        # The live rebuild does a network probe per provider (Copilot token
        # exchange over HTTPS, OpenRouter/Nous /models, ...). On a flaky / corp
        # / WSL network any single probe can stall for its full per-call
        # timeout and, summed across providers, pin a foreground request
        # thread for tens of seconds (the wakeup-turn / chat-start hang).
        #
        # Run the rebuild on a daemon worker; the foreground waits at most
        # _LIVE_REBUILD_BUDGET_SECONDS.
        #
        # WITHIN budget (the normal fast case): the FOREGROUND publishes the
        # result synchronously and only then returns — preserving the exact
        # pre-existing contract (cache + on-disk file populated by the time
        # get_available_models() returns). The worker stays hands-off.
        #
        # OVER budget (a provider probe is slow/hung): the foreground returns
        # the best fallback immediately and the still-running worker publishes
        # its result out-of-band when it finally finishes, so the next caller
        # gets a warm cache instead of paying the cold rebuild again.
        #
        # ``_publish_models_result`` / ``box["published"]`` ensure exactly one
        # publisher even at the budget boundary (no double write, no lost
        # refresh). The worker only touches _cache_build_cv after the
        # foreground releases the RLock by returning, so no lock inversion.
        build_done = threading.Event()
        budget_exceeded = threading.Event()
        publish_lock = threading.Lock()
        box: dict = {}

        def _publish_models_result(result):
            global _cache_build_in_progress, _available_models_cache
            global _available_models_cache_ts, _available_models_cache_source_fingerprint
            with _cache_build_cv:
                _available_models_cache = result
                _available_models_cache_ts = time.monotonic()
                _available_models_cache_source_fingerprint = (
                    _models_cache_source_fingerprint()
                )
            try:
                _save_models_cache_to_disk(result)
            except Exception:
                logger.debug("models cache disk save failed", exc_info=True)
            finally:
                with _cache_build_cv:
                    _cache_build_in_progress = False
                    _cache_build_cv.notify_all()

        def _clear_build_in_progress():
            global _cache_build_in_progress
            with _cache_build_cv:
                _cache_build_in_progress = False
                _cache_build_cv.notify_all()

        def _claim_publish() -> bool:
            """Return True iff the caller won the right to publish."""
            with publish_lock:
                if box.get("published"):
                    return False
                box["published"] = True
                return True

        def _rebuild_worker():
            # Re-bind the captured per-request profile on THIS worker thread
            # (#3957): the daemon inherits neither the request-profile TLS nor
            # os.environ, so without this it would probe the default profile's
            # credentials and, over budget, publish the rebuilt catalog to the
            # DEFAULT profile's disk cache. No-op for the default profile.
            _worker_scope = (
                _prof_scope_worker(_active_profile_name, "models rebuild (worker)")
                if _prof_scope_worker is not None
                else _nullcontext()
            )
            with _worker_scope:
                try:
                    box["result"] = _invoke_models_rebuild(_build_available_models_uncached)
                except Exception as exc:  # noqa: BLE001 — propagated to caller
                    box["error"] = exc
                finally:
                    build_done.set()
                    # Only publish out-of-band if the foreground already gave up
                    # (over budget). Within budget the foreground publishes
                    # synchronously, so the worker must NOT touch the cache.
                    # NOTE: the publish (and its disk write + fingerprint) runs
                    # INSIDE this profile scope so the over-budget path writes
                    # the correct profile's cache file.
                    if budget_exceeded.is_set() and _claim_publish():
                        if "result" in box:
                            _publish_models_result(box["result"])
                        else:
                            _clear_build_in_progress()

        _worker = threading.Thread(
            target=_rebuild_worker,
            name="models-catalog-rebuild",
            daemon=True,
        )
        _worker.start()

        if build_done.wait(timeout=_LIVE_REBUILD_BUDGET_SECONDS):
            # Build finished within budget — foreground publishes
            # synchronously, exactly like the legacy path.
            if "error" in box:
                _clear_build_in_progress()
                raise box["error"]
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Budget elapsed. Mark it so the worker knows it owns out-of-band
        # publication. Handle the tiny race where the build completed between
        # wait() returning False and here: if so, still publish synchronously
        # so this caller honours the cache contract.
        budget_exceeded.set()
        if build_done.is_set() and "error" not in box and "result" in box:
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Genuinely slow/hung probe: serve the best fallback now; the worker
        # keeps going and refreshes the cache for the next caller.
        # Rate-limit the warning per Q-2979-A3 — see _should_warn_budget; a
        # sustained budget breach demotes to info after the first emit in
        # each cooldown window so log volume stays bounded.
        _budget_log_msg = (
            "live provider-catalog rebuild exceeded %.1fs budget — serving "
            "fallback, refreshing catalog out-of-band"
        )
        if _should_warn_budget("live_rebuild_budget_exceeded"):
            logger.warning(_budget_log_msg, _LIVE_REBUILD_BUDGET_SECONDS)
        else:
            logger.info(_budget_log_msg, _LIVE_REBUILD_BUDGET_SECONDS)
        # ``stale_disk_groups`` is shape-valid but failed the strict metadata
        # checks required for authoritative cold-path use. It was read before
        # acquiring _available_models_cache_lock so this over-budget fallback
        # does not extend the lock hold while the worker is ready to publish.
        if stale_disk_groups is not None:
            return copy.deepcopy(stale_disk_groups)
        return copy.deepcopy(_static_models_catalog_without_live_probes())


def _models_cache_file_age_seconds(cache_path: Path, now: float) -> float | None:
    try:
        return max(0.0, now - cache_path.stat().st_mtime)
    except OSError:
        return None


def get_available_models_for_session_visit() -> dict:
    """Return /api/models with a short session-visit freshness horizon."""
    global _available_models_cache, _available_models_cache_ts, _available_models_cache_source_fingerprint
    cache_path = _get_models_cache_path()
    cache_age = _models_cache_file_age_seconds(cache_path, time.time())
    disk_cached = None
    if cache_age is not None and cache_age < _SESSION_VISIT_MODELS_FRESHNESS_SECONDS:
        now_mono = time.monotonic()
        with _available_models_cache_lock:
            cached = _get_fresh_memory_models_cache(now_mono)
            if cached is not None:
                return cached
        disk_cached = _load_models_cache_from_disk()
        if disk_cached is not None:
            with _available_models_cache_lock:
                cached = _get_fresh_memory_models_cache(time.monotonic())
                if cached is not None:
                    return cached
                _available_models_cache = copy.deepcopy(disk_cached)
                _available_models_cache_ts = time.monotonic()
                _available_models_cache_source_fingerprint = _models_cache_source_fingerprint()
            return copy.deepcopy(disk_cached)

    stale_cached = disk_cached or _load_stale_models_cache_from_disk()
    try:
        return get_available_models(force_refresh=True)
    except Exception:
        logger.debug("session-visit models refresh failed", exc_info=True)
        if stale_cached is not None:
            return copy.deepcopy(stale_cached)
        return get_available_models(prefer_cache=True)


# ── Static file path ─────────────────────────────────────────────────────────
_INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"

# ── Thread synchronisation ───────────────────────────────────────────────────
LOCK = threading.Lock()
# Max compact Session objects held in the in-memory LRU (issue #3506). Lighter
# than the agent cache (no live agent runtime), but still bounded and operator-
# tunable via HERMES_WEBUI_SESSIONS_MAX for installs with hundreds of sessions.
SESSIONS_MAX = _env_int("HERMES_WEBUI_SESSIONS_MAX", 100)
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
        self._last_event_id: str | None = None

    def subscribe(self) -> queue.Queue:
        q, _snapshot = self.subscribe_with_snapshot()
        return q

    def subscribe_with_snapshot(self) -> tuple[queue.Queue, dict[str, object]]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            # Replay buffered events to the new subscriber INSIDE the lock so a
            # concurrent put_nowait() can't broadcast a newer event before we
            # finish replaying the older buffered tail. queue.Queue.put_nowait
            # is non-blocking on an unbounded queue, so holding the lock here
            # is safe. Per Opus advisor on stage-292.
            for item in self._offline_buffer:
                q.put_nowait(item)
            snapshot = {
                "offline_buffered_events": len(self._offline_buffer),
                "last_event_id": self._last_event_id,
            }
            self._subscribers.append(q)
        return q, snapshot

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def note_last_event_id(self, event_id: str | None) -> None:
        """Record the latest journal event id without changing the queue shape."""
        if not event_id:
            return
        with self._lock:
            self._last_event_id = event_id

    def put_nowait(self, item: tuple[str, object] | tuple[str, object, str | None]) -> None:
        event_id = item[2] if len(item) >= 3 else None
        with self._lock:
            if event_id:
                self._last_event_id = event_id
            subscribers = list(self._subscribers)
            if not subscribers:
                self._offline_buffer.append(item)
                return
            self._offline_buffer.clear()
        for q in subscribers:
            q.put_nowait(item)

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return non-sensitive stream observation counters for health checks."""
        with self._lock:
            return {
                "subscriber_count": len(self._subscribers),
                "offline_buffered_events": len(self._offline_buffer),
            }


def create_stream_channel() -> StreamChannel:
    return StreamChannel()


STREAMS: dict = {}
STREAMS_LOCK = threading.Lock()
CANCEL_FLAGS: dict = {}
AGENT_INSTANCES: dict = {}  # stream_id -> AIAgent instance for interrupt propagation
STREAM_PARTIAL_TEXT: dict = {}  # stream_id -> partial assistant text accumulated during streaming
STREAM_REASONING_TEXT: dict = {}  # stream_id -> reasoning trace accumulated during streaming (#1361 §A)
STREAM_LIVE_TOOL_CALLS: dict = {}  # stream_id -> live tool calls accumulated during streaming (#1361 §B)
STREAM_GOAL_RELATED: dict = {}  # stream_id -> bool: only evaluate goal for goal-related turns (#1932)
STREAM_LAST_EVENT_ID: dict = {}  # stream_id -> latest journal event_id for `id:` field on live SSE frames (stage-364)
PENDING_GOAL_CONTINUATION: set = set()  # session_ids awaiting a goal continuation turn (#1932)

# ── Gateway capability cache ─────────────────────────────────────────────────
# Probes /v1/capabilities once per base_url/api-key pair and caches the result
# for 60 s so guarded-turn routing decisions do not add latency on every chat
# turn.
_GATEWAY_CAPS_CACHE: dict[tuple[str, str], dict] = {}
_GATEWAY_CAPS_LOCK = threading.Lock()
_GATEWAY_CAPS_TTL_S: float = 60.0


def get_gateway_caps(base_url: str, api_key: str = "") -> dict:
    """Return cached gateway capability flags, probing /v1/capabilities if stale."""
    base_url = str(base_url or "").rstrip("/")
    cache_key = (base_url, str(api_key or ""))
    now = time.time()
    probe_started_at = now
    with _GATEWAY_CAPS_LOCK:
        cached = _GATEWAY_CAPS_CACHE.get(cache_key)
        if cached and now - cached.get("fetched_at", 0) < _GATEWAY_CAPS_TTL_S:
            return cached
    caps = {"approval_events": False, "run_approval_response": False, "fetched_at": 0.0}
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(f"{base_url}/v1/capabilities", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read(65536))
        features = body.get("features") if isinstance(body, dict) else {}
        if not isinstance(features, dict):
            features = {}
        caps["approval_events"] = bool(features.get("approval_events"))
        caps["run_approval_response"] = bool(features.get("run_approval_response"))
    except Exception:
        pass
    with _GATEWAY_CAPS_LOCK:
        current = _GATEWAY_CAPS_CACHE.get(cache_key)
        if current and current.get("fetched_at", 0) > probe_started_at:
            return current
        caps["fetched_at"] = time.time()
        _GATEWAY_CAPS_CACHE[cache_key] = caps
    return caps


def gateway_supports_approval(base_url: str, api_key: str = "") -> bool:
    """True only when the gateway advertises both approval_events and run_approval_response."""
    caps = get_gateway_caps(base_url, api_key)
    return bool(caps.get("approval_events") and caps.get("run_approval_response"))


def invalidate_gateway_caps(base_url: str | None = None) -> None:
    """Evict capability cache for base_url, or all entries when base_url is None."""
    with _GATEWAY_CAPS_LOCK:
        if base_url is None:
            _GATEWAY_CAPS_CACHE.clear()
        else:
            normalized = str(base_url or "").rstrip("/")
            for cache_key in [key for key in _GATEWAY_CAPS_CACHE if key[0] == normalized]:
                _GATEWAY_CAPS_CACHE.pop(cache_key, None)


# ── notify_on_complete agent-wakeup wiring ─────────────────────────────────
# When terminal(notify_on_complete=true, background=true) fires, the process
# registry pushes a completion event onto tools.process_registry.completion_queue.
# A drain task spawned at WebUI startup (api/background_process.py) reads that
# queue and emits an SSE `process_complete` event to the matching session.
# PROCESS_SESSION_INDEX maps the per-process "session_key" (set in the spawned
# subprocess via HERMES_SESSION_KEY) back to the WebUI session_id that owns it,
# so the drain task can route the event to the right SSE channel.
# PENDING_BG_TASK_COMPLETIONS mirrors PENDING_GOAL_CONTINUATION: server-side
# marker discarded atomically by routes.py when the frontend re-POSTs the
# wakeup_prompt as the next user turn. (process_complete event, agent wakeup fix)
PROCESS_SESSION_INDEX: dict = {}  # process_registry session_key -> WebUI session_id
PROCESS_SESSION_INDEX_LOCK = threading.Lock()
PENDING_BG_TASK_COMPLETIONS: set = set()  # session_ids awaiting a process_complete wakeup turn
BG_TASK_COMPLETE_EVENTS_SEEN: dict = {}  # session_id -> set[process_id] for idempotency
BG_TASK_COMPLETE_EVENTS_SEEN_LOCK = threading.Lock()

# Defer-path fix (fast-bg-task wakeup race): when a completion arrives while a
# turn is active, Option Z's drain branch CANNOT start a turn (would 409). The
# pre-existing PENDING_BG_TASK_COMPLETIONS marker was a bare session_id flag —
# the wakeup_prompt was DISCARDED, and the only consumer (PR #2279 next-turn
# drain) reads completion_queue, which the Option Z drain thread already
# emptied. So for an autonomous agent (no next user turn) the deferred wakeup
# was lost forever. DEFERRED_PROCESS_WAKEUPS persists the actual prompt(s) so a
# turn-teardown idle-hook (api/streaming) can redeliver them once the session
# goes idle — symmetric with the idle branch (idle now → fire now; busy now →
# fire at turn-end). Atomic claim (pop under lock) guarantees single delivery:
# whoever claims first (teardown hook OR next-turn drain) fires; the other
# finds nothing → no double-fire, no wakeup loop.
DEFERRED_PROCESS_WAKEUPS: dict = {}  # session_id -> list[{"process_id", "wakeup_prompt"}]
DEFERRED_PROCESS_WAKEUPS_LOCK = threading.Lock()

# ── Persistent per-session SSE channel (Option X) ──────────────────────────
# A long-lived SSE channel scoped to a WebUI session_id rather than a single
# agent turn (stream_id). Subscribed to by the frontend on session mount,
# torn down on session unmount, and refcounted across tabs. Used to deliver
# events (currently process_complete) that fire while no agent turn is
# active — bridging the gap that PR #2242 + #2279 left when STREAMS has
# already been torn down. The registry lives in api.background_process; this
# constant is the idle-cap before the reaper collects an unsubscribed
# channel. 4h is a defensive ceiling against zombie connections; the
# subscribers-empty grace path (60s) handles ordinary tab-close traffic.
SESSION_CHANNEL_IDLE_TTL_SECS: int = 14400  # 4 hours
SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS: int = 60  # subscribers-empty grace

# Active agent-run registry. This intentionally tracks worker lifecycle rather
# than SSE lifecycle: cancel/reconnect may remove STREAMS while the worker is
# still unwinding, blocked in a provider call, or waiting for delegated work.
ACTIVE_RUNS: dict = {}
ACTIVE_RUNS_LOCK = threading.Lock()
LAST_RUN_FINISHED_AT: float | None = None
SERVER_START_TIME = time.time()


def register_active_run(stream_id: str, **metadata) -> None:
    """Mark a WebUI agent worker as alive until its outer finally exits."""
    if not stream_id:
        return
    now = time.time()
    entry = dict(metadata or {})
    entry.setdefault("stream_id", stream_id)
    entry.setdefault("started_at", now)
    entry.setdefault("phase", "running")
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS[stream_id] = entry


def update_active_run(stream_id: str, **metadata) -> None:
    """Update active-run metadata without creating a new run implicitly."""
    if not stream_id:
        return
    with ACTIVE_RUNS_LOCK:
        entry = ACTIVE_RUNS.get(stream_id)
        if entry is not None:
            entry.update(metadata)


def unregister_active_run(stream_id: str) -> None:
    """Remove a worker from the active-run registry and record idle start."""
    if not stream_id:
        return
    global LAST_RUN_FINISHED_AT
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS.pop(stream_id, None)
        LAST_RUN_FINISHED_AT = time.time()

# Agent cache: reuse AIAgent across messages in the same WebUI session so that
# _user_turn_count survives between turns.  This mirrors the gateway's
# _agent_cache pattern and is required for injectionFrequency: "first-turn".
# LRU cache with size limit to prevent memory bloat.
# All cache operations (get, set, move_to_end, popitem) are protected by
# SESSION_AGENT_CACHE_LOCK for thread safety in multi-threaded ASGI servers.
import collections
SESSION_AGENT_CACHE: collections.OrderedDict = collections.OrderedDict()  # LRU cache
# Each cached agent pins a full conversation transcript in RAM, so this cap is
# the dominant lever on WebUI resident memory (issue #3506). The default is kept
# deliberately modest -- large/long sessions can each weigh tens of MB, so 50
# live agents could pin >1 GB on a heavily multiplexed install. Operators can
# tune it via HERMES_WEBUI_AGENT_CACHE_MAX without editing source.
SESSION_AGENT_CACHE_MAX = _env_int("HERMES_WEBUI_AGENT_CACHE_MAX", 25)
SESSION_AGENT_CACHE_LOCK = threading.Lock()


def _evict_session_agent(session_id: str) -> None:
    """Remove a cached agent for a session (on delete, clear, or model switch).

    Attempts a lifecycle commit before dropping the agent handle so that
    batch-extraction memory providers can extract any pending work.  If the
    commit fails or there is uncommitted work with no successful commit, the
    lifecycle entry is preserved (not unregistered) so a future commit can
    retry.
    """
    agent = None
    with SESSION_AGENT_CACHE_LOCK:
        entry = SESSION_AGENT_CACHE.pop(session_id, None)
        if entry is not None:
            agent = entry[0] if isinstance(entry, tuple) else None
    if agent is None:
        return
    should_close = True
    try:
        from api.session_lifecycle import commit_session_memory, discard_session, has_uncommitted_work, unregister_agent
        if has_uncommitted_work(session_id):
            commit_session_memory(session_id, agent=agent, wait=True)
        if not has_uncommitted_work(session_id):
            unregister_agent(session_id)
            # Bound the lifecycle dict: drop the entry now that the session has
            # no uncommitted work and the agent handle is gone (issue #3506).
            discard_session(session_id)
        else:
            should_close = False
    except Exception:
        should_close = False
        logger.debug("Lifecycle commit on eviction failed for %s", session_id, exc_info=True)
    if should_close and getattr(agent, '_session_db', None) is not None:
        try:
            agent._session_db.close()
        except Exception:
            logger.debug("Failed to close _session_db on eviction for %s", session_id, exc_info=True)

# ── Thread-local env context ─────────────────────────────────────────────────
# (_thread_ctx + _thread_local_env_value are defined near the top of this module,
# above the config-file section, so _expand_env_vars can reference them at the
# import-time reload_config() without a forward-reference NameError.)


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
    "show_quota_chip": False,  # show ambient provider quota chip in composer footer (default off; wide desktop only when enabled, see style.css @media)
    "show_conversation_outline": False,  # show opt-in desktop jump-to-question outline panel
    "hide_empty_state_suggestions": False,  # hide the default new-chat suggestion buttons
    "virtualize_transcript": False,  # #4343: virtualize long (>80 msg) transcripts. EXPERIMENTAL, opt-IN (default OFF). Was opt-out/default-on in #4325 but caused scroll-up flicker on long sessions with tall tool-call rows (variable-height anchor oscillation) — flipped off for everyone in #4343; re-enabling requires an explicit opt-in (see virtualize_transcript_optin migration in load_settings).
    "virtualize_transcript_optin": False,  # #4343 migration marker: True only once the user explicitly enables virtualize_transcript AFTER the default-off flip. A stored virtualize_transcript=True WITHOUT this marker is a stale pre-flip value and is reset to False on load (force-off-for-everyone migration).
    "show_tps": False,  # show tokens-per-second chip in assistant message headers
    "fade_text_effect": False,  # animate newly streamed words with a lightweight fade-in effect
    "show_cli_sessions": True,  # merge CLI/TUI/messaging sessions from state.db into the sidebar by default (#3988); established installs are grandfathered OFF by the load_settings backfill
    "show_cron_sessions": False,  # surface cron sessions in the sidebar (subordinate to show_cli_sessions)
    "show_previous_messaging_sessions": False,  # show older Telegram/Discord/etc. reset segments
    "sync_to_insights": False,  # mirror WebUI token usage to state.db for /insights
    "check_for_updates": True,  # check if webui/agent repos are behind upstream
    "ignore_agent_updates": False,  # keep WebUI update notices but suppress Agent update checks
    "whats_new_summary_enabled": False,  # show an LLM-written What's New summary before diff links
    "theme": "dark",  # light | dark | system
    "skin": "default",  # accent color skin: default | ares | mono | graphite | slate | poseidon | sisyphus | charizard | sienna | catppuccin | nous
    "font_size": "default",  # small | default | large | xlarge
    "session_jump_buttons": False,  # show Start/End transcript jump pills
    "render_user_markdown": False,  # opt-in: render full markdown in user messages (#3870)
    "structured_code_default_view": "auto",  # JSON/YAML fenced-block default render: auto | on | off (#484 follow-up). auto => Tree when line count >= structured_code_auto_tree_lines, else Raw.
    "structured_code_auto_tree_lines": 10,  # in 'auto' mode, minimum line count to default a JSON/YAML block to Tree view (preserves the original hardcoded >=10 behavior)
    "session_endless_scroll": False,  # auto-load older transcript pages while scrolling upward
    "chat_activity_display_mode": "compact_worklog",  # compact_worklog | transparent_stream
    "auto_scroll_follow": True,  # follow new output to the bottom while streaming (Codex/Claude-Code-style sticky bottom); the user scrolling up unpins and is respected
    "worklog_details_expanded_default": False,  # opt-in: expand Worklog details by default; default remains folded
    "hide_composer_attach": False,  # hide attach button in composer footer
    "hide_composer_saved_prompts": False,  # hide saved prompts button in composer footer
    "hide_composer_mic": False,  # hide dictation mic button in composer footer
    "show_titlebar_profile": False,  # show profile switcher in app titlebar (opt-in)
    "hide_composer_voice_mode": False,  # hide hands-free voice-mode button in composer footer
    "hide_composer_yolo": False,  # hide YOLO chip in composer footer
    "hide_composer_profile": False,  # hide profile chip in composer footer
    "hide_composer_workspace": False,  # hide workspace controls in composer footer/mobile config panel
    "hide_composer_mobile_config": False,  # hide mobile composer config button
    "hide_composer_model": False,  # hide model chip in composer footer/mobile config panel
    "hide_composer_quota_chip": False,  # hide provider quota chip in composer footer
    "hide_composer_reasoning": False,  # hide reasoning chip in composer footer/mobile config panel
    "hide_composer_toolsets": False,  # hide toolsets chip in composer footer
    "hide_composer_status": False,  # hide status text in composer footer
    "hide_composer_context": False,  # hide context indicator in composer footer/mobile config panel
    "hide_composer_bg_badge": False,  # hide background-jobs badge in composer footer
    "pinned_sessions_limit": 3,  # maximum active pinned sessions shown in the sidebar
    "inflight_state_max_sessions": 8,  # max active-stream recovery snapshots kept in browser localStorage
    "inflight_state_max_messages": 24,  # max recent messages kept per recovery snapshot
    "inflight_state_max_tool_calls": 48,  # max recent tool-call records kept per recovery snapshot
    "inflight_state_max_string_chars": 60000,  # max string length kept inside a recovery snapshot field
    "inflight_state_max_json_chars": 1500000,  # max serialized recovery snapshot payload before pruning
    "hidden_tabs": [],  # sidebar tab panel names hidden by user (e.g. ["tasks","kanban"]); chat and settings are always visible
    "tab_order": [],  # user-defined sidebar/rail tab order for reorderable tabs; chat/settings stay fixed
    "language": "en",  # UI locale code; must match a key in static/i18n.js LOCALES
    "bot_name": os.getenv(
        "HERMES_WEBUI_BOT_NAME", "Hermes"
    ),  # display name for the assistant
    "sound_enabled": False,  # play notification sound when assistant finishes
    "rtl": False,  # right-to-left chat layout (chat messages + composer only)
    "notifications_enabled": False,  # browser notification when tab is in background
    "show_thinking": True,  # show/hide thinking/reasoning blocks in chat view
    "simplified_tool_calling": True,  # legacy compatibility; Worklog renderer remains enabled
    "terminal_auto_expand_on_output": False,  # auto-expand terminal panel when output arrives while collapsed
    "workspace_todos_tab": False,  # show a Todos tab in the workspace panel (right side)
    "api_redact_enabled": True,  # redact sensitive data (API keys, secrets) from API responses
    "dashboard_plugins": {},  # plugin_name -> bool, opt-in per plugin (default off per PF-10b)
    "sidebar_density": "compact",  # compact | detailed
    "auto_title_refresh_every": "0",  # adaptive title refresh: 0=off, 5/10/20=every N exchanges
    "busy_input_mode": "queue",  # behavior when sending while agent is running: queue | interrupt | steer
    "password_hash": None,  # PBKDF2-HMAC-SHA256 hash; None = auth disabled
    "auth_disabled_acknowledged": False,  # user acknowledged unauthenticated risk
}
_SETTINGS_LEGACY_DROP_KEYS = {
    "assistant_language",
    "bubble_layout",
    "default_model",
    "activity_feed_expanded_default",
    "simplified_tool_calling",
}
_SETTINGS_THEME_VALUES = {"light", "dark", "system"}
_SETTINGS_SKIN_VALUES = {
    "default",
    "ares",
    "mono",
    "graphite",
    "slate",
    "poseidon",
    "sisyphus",
    "charizard",
    "sienna",
    "catppuccin",
    "nous",
    "geist-contrast",
    "zeus",
    "verdigris",
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
                if (
                    "worklog_details_expanded_default" not in stored
                    and "activity_feed_expanded_default" in stored
                ):
                    settings["worklog_details_expanded_default"] = bool(
                        stored.get("activity_feed_expanded_default")
                    )
                settings.update(
                    {
                        k: v
                        for k, v in stored.items()
                        if k not in _SETTINGS_LEGACY_DROP_KEYS
                    }
                )
                # Grandfather established installs OFF for show_cli_sessions (#3988).
                # The default flipped True so NEW users see CLI/TUI/messaging
                # sessions without hunting for the toggle — but an existing user
                # who never opted in should not have their sidebar silently change.
                # Treat the install as established (and pin the old False default)
                # when show_cli_sessions is absent AND the file already carries
                # real user state — either onboarding was completed, or some
                # setting OTHER than a not-yet-completed onboarding flag has been
                # persisted. Keying on "has saved user state" (not just
                # onboarding_completed) also covers a CLI-configured user who
                # tweaked a WebUI setting before running the wizard. A genuinely
                # new / still-mid-onboarding file falls through to the True default.
                _established_keys = [
                    k for k in stored
                    if k not in ("show_cli_sessions", "onboarding_completed")
                ]
                if "show_cli_sessions" not in stored and (
                    bool(stored.get("onboarding_completed")) or _established_keys
                ):
                    settings["show_cli_sessions"] = False
                # Force-off-for-everyone migration for virtualize_transcript (#4343).
                # The feature shipped opt-OUT/default-on in #4325, then proved to
                # cause scroll-up flicker on long sessions (variable-height anchor
                # oscillation). It is now EXPERIMENTAL/opt-IN (default off). Any
                # stored virtualize_transcript=True from the #4325 window is a stale
                # pre-flip value and must be reset to off, so 100% of existing users
                # land on off — re-enabling requires an explicit opt-in made AFTER
                # the flip, which writes virtualize_transcript_optin=True alongside.
                # Honor a stored True only when that marker is present.
                if not bool(stored.get("virtualize_transcript_optin")):
                    settings["virtualize_transcript"] = False

        except Exception:
            logger.debug("Failed to load settings from %s", SETTINGS_FILE)
    settings["theme"], settings["skin"] = _normalize_appearance(
        stored.get("theme") if isinstance(stored, dict) else settings.get("theme"),
        stored.get("skin") if isinstance(stored, dict) else settings.get("skin"),
    )
    settings["default_model"] = get_effective_default_model()
    try:
        model_cfg = get_config().get("model", {})
        if isinstance(model_cfg, dict) and model_cfg.get("provider"):
            settings["default_model_provider"] = str(model_cfg.get("provider"))
    except Exception:
        logger.debug("Failed to resolve default model provider for settings")
    return settings


_SETTINGS_ALLOWED_KEYS = set(_SETTINGS_DEFAULTS.keys()) - {
    "password_hash",
    "default_model",
    "simplified_tool_calling",
}
_SETTINGS_ENUM_VALUES = {
    "send_key": {"enter", "ctrl+enter"},
    "sidebar_density": {"compact", "detailed"},
    "font_size": {"small", "default", "large", "xlarge"},
    "auto_title_refresh_every": {"0", "5", "10", "20"},
    "busy_input_mode": {"queue", "interrupt", "steer"},
    "chat_activity_display_mode": {"compact_worklog", "transparent_stream"},
    "structured_code_default_view": {"auto", "on", "off"},
}
_SETTINGS_INT_RANGES = {
    "pinned_sessions_limit": (1, 99),
    "inflight_state_max_sessions": (1, 25),
    "inflight_state_max_messages": (1, 100),
    "inflight_state_max_tool_calls": (1, 200),
    "inflight_state_max_string_chars": (1000, 500000),
    "inflight_state_max_json_chars": (100000, 4000000),
    "structured_code_auto_tree_lines": (1, 1000),
}
_SETTINGS_BOOL_KEYS = {
    "onboarding_completed",
    "show_token_usage",
    "show_quota_chip",
    "show_conversation_outline",
    "hide_empty_state_suggestions",
    "virtualize_transcript",
    "virtualize_transcript_optin",
    "show_tps",
    "fade_text_effect",
    "show_cli_sessions",
    "show_cron_sessions",
    "show_previous_messaging_sessions",
    "sync_to_insights",
    "check_for_updates",
    "ignore_agent_updates",
    "whats_new_summary_enabled",
    "sound_enabled",
    "rtl",
    "notifications_enabled",
    "show_thinking",
    "terminal_auto_expand_on_output",
    "workspace_todos_tab",
    "api_redact_enabled",
    "session_jump_buttons",
    "render_user_markdown",
    "session_endless_scroll",
    "auto_scroll_follow",
    "worklog_details_expanded_default",
    "auth_disabled_acknowledged",
    "hide_composer_attach",
    "hide_composer_saved_prompts",
    "hide_composer_mic",
    "show_titlebar_profile",
    "hide_composer_voice_mode",
    "hide_composer_yolo",
    "hide_composer_profile",
    "hide_composer_workspace",
    "hide_composer_mobile_config",
    "hide_composer_model",
    "hide_composer_quota_chip",
    "hide_composer_reasoning",
    "hide_composer_toolsets",
    "hide_composer_status",
    "hide_composer_context",
    "hide_composer_bg_badge",
}
# Language codes are validated as short alphanumeric BCP-47-like tags (e.g. 'en', 'zh', 'fr')
_SETTINGS_LANG_RE = __import__("re").compile(r"^[a-zA-Z]{2,10}(-[a-zA-Z0-9]{2,8})?$")

_SETTINGS_WRITE_VERSION = 0
_SETTINGS_WRITE_LOCK = __import__("threading").Lock()


def save_settings(settings: dict) -> dict:
    """Save settings to disk. Returns the merged settings. Ignores unknown keys."""
    current = load_settings()
    if (
        "worklog_details_expanded_default" not in settings
        and "activity_feed_expanded_default" in settings
    ):
        settings["worklog_details_expanded_default"] = settings.get(
            "activity_feed_expanded_default"
        )
    settings.pop("activity_feed_expanded_default", None)
    settings.pop("simplified_tool_calling", None)
    pending_theme = current.get("theme")
    pending_skin = current.get("skin")
    theme_was_explicit = False
    skin_was_explicit = False
    # Handle _set_password: hash and store as password_hash
    _password_changed = False
    raw_pw = settings.pop("_set_password", None)
    if raw_pw and isinstance(raw_pw, str) and raw_pw.strip():
        # Use PBKDF2 from auth module (600k iterations) -- never raw SHA-256
        from api.auth import _hash_password

        current["password_hash"] = _hash_password(raw_pw.strip())
        _password_changed = True
    # Handle _clear_password: explicitly disable auth
    if settings.pop("_clear_password", False):
        current["password_hash"] = None
        _password_changed = True
    # Deep-merge dashboard_plugins dict (plugin_name -> bool)
    _dashboard_plugins = settings.get("dashboard_plugins")
    if isinstance(_dashboard_plugins, dict):
        current_dash = current.get("dashboard_plugins", {})
        if isinstance(current_dash, dict):
            # Coerce values to bool + keep only str keys so settings.json can't be
            # polluted with non-bool/non-str junk from a crafted POST.
            current_dash.update({k: bool(v) for k, v in _dashboard_plugins.items() if isinstance(k, str)})
            current["dashboard_plugins"] = current_dash
    for k, v in settings.items():
        # dashboard_plugins is deep-merged above (not a flat allowlisted scalar).
        if k == "dashboard_plugins":
            continue
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
            # Validate bounded integer settings.
            if k in _SETTINGS_INT_RANGES:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                min_value, max_value = _SETTINGS_INT_RANGES[k]
                if v < min_value or v > max_value:
                    continue
            # Validate language codes (BCP-47-like: 'en', 'zh', 'fr', 'zh-CN')
            if k == "language" and (
                not isinstance(v, str) or not _SETTINGS_LANG_RE.match(v)
            ):
                continue
            # Validate list-valued sidebar tab settings. Chat/settings stay fixed
            # even if a tampered POST tries to persist them, and duplicates are
            # collapsed while preserving the first requested order.
            if k in {"hidden_tabs", "tab_order"}:
                if not isinstance(v, list):
                    continue
                seen = set()
                cleaned = []
                for s in v:
                    if not isinstance(s, str):
                        continue
                    s = s.strip()
                    if not s or s in {"chat", "settings"} or s in seen:
                        continue
                    seen.add(s)
                    cleaned.append(s)
                v = cleaned
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
    global _SETTINGS_WRITE_VERSION
    with _SETTINGS_WRITE_LOCK:
        _SETTINGS_WRITE_VERSION += 1
    # Invalidate the in-memory password hash cache so the next call to
    # get_password_hash() picks up the new value from disk immediately.
    if _password_changed:
        from api.auth import _invalidate_password_hash_cache

        _invalidate_password_hash_cache()
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


# Run the provider-model seeder once at import time. Must be at the END of the
# module because _seed_provider_models_from_core() calls _get_label_for_model,
# which is defined ~3000 lines above. Placing the invocation earlier (e.g. right
# after the seeder's def) caused a NameError that the bare except silently
# swallowed — exactly when the seeder had real work to do (#4413).
try:
    _seed_provider_models_from_core()
except ImportError:
    pass  # hermes_cli not available (standalone deployment)
except Exception:
    logger.warning("provider-model seeder failed", exc_info=True)
