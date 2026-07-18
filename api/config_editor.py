"""Raw config.yaml viewer/editor for the WebUI Settings > System panel.

Scope (kept deliberately small, mirrors the read-only viewer proposed
upstream in nesquena/hermes-webui#5088, but adds a gated write path):

  * GET  /api/config/raw — returns the active profile's config.yaml as raw
    text (comments preserved), with credential-shaped values replaced by a
    placeholder and the list of redacted key paths.
  * PUT  /api/config/raw — writes a new config.yaml, but only when an
    operator has opted in via ``HERMES_WEBUI_ALLOW_CONFIG_RAW_WRITE`` AND the
    submitted YAML does not touch security-critical keys (auth/security
    sections, trusted proxies, allowlists). This keeps a compromised WebUI
    session from using the raw editor to disable its own gates.

Both endpoints operate on the *raw text* of config.yaml rather than
round-tripping through ``yaml.safe_dump`` — that is what preserves comments
and key ordering across a save.
"""

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WRITE_GATE_ENV = "HERMES_WEBUI_ALLOW_CONFIG_RAW_WRITE"
_REDACTED_PLACEHOLDER = "•••REDACTED•••"
_BACKUP_SUFFIX = ".webui-editor-bak"

# Key names whose value is credential-shaped. Matched as a substring of the
# key name after stripping underscores/hyphens and lowercasing, so
# "api_key", "apiKey", "API-KEY", "access_token", "client_secret", and
# "password_hash" all match — case-insensitive substring, as specified.
_SENSITIVE_SUBSTRINGS = ("apikey", "token", "secret", "password")

_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")


class ConfigEditorError(Exception):
    """Raised for any GET/PUT config-editor failure; carries an HTTP status
    and optional extra JSON fields (blocked_paths, line/column, ...)."""

    def __init__(self, message: str, *, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


# ── Path resolution ───────────────────────────────────────────────────────
#
# Mirrors api.routes._active_profile_config_path(): follow the active
# profile's config.yaml, but defer to api.config._get_config_path() when a
# test has monkeypatched that resolver (module name check), so config-editor
# tests can redirect I/O without needing api.profiles/a real profile tree.


def _active_profile_config_path() -> Path:
    from api.config import _get_config_path

    test_override_module = getattr(_get_config_path, "__module__", "")
    if test_override_module != "api.config":
        return _get_config_path()
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()) / "config.yaml"
    except Exception:
        return _get_config_path()


def _write_enabled() -> bool:
    return os.getenv(_WRITE_GATE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ── Redaction (GET) ──────────────────────────────────────────────────────


def _segment_is_sensitive(segment: str) -> bool:
    norm = re.sub(r"[_\-]", "", str(segment).strip().strip("'\"")).lower()
    return any(term in norm for term in _SENSITIVE_SUBSTRINGS)


def _path_is_sensitive(path_segments: list) -> bool:
    return any(_segment_is_sensitive(s) for s in path_segments)


def _try_parse_key_line(line: str):
    """Return (indent_len, dash_len, key, rest) for a `key: value` /
    `key:` / `- key: value` mapping line, else None.

    Requires a real YAML key/value separator (colon followed by whitespace
    or end-of-line) so colon-bearing scalars (URLs, timestamps) are never
    mistaken for a key.
    """
    indent_len = len(line) - len(line.lstrip(" \t"))
    body = line[indent_len:]
    dash_len = 0
    if body.startswith("- ") or body.startswith("-\t"):
        dash_len = 2
        body = body[2:]
    elif body == "-":
        return None
    if not body or body.startswith("#"):
        return None
    sep_idx = None
    n = len(body)
    i = 0
    while i < n:
        ch = body[i]
        if ch == "#" and (i == 0 or body[i - 1] == " "):
            break
        if ch == ":" and (i + 1 == n or body[i + 1] in (" ", "\t")):
            sep_idx = i
            break
        i += 1
    if sep_idx is None:
        return None
    key = body[:sep_idx].strip().strip("'\"")
    if not key:
        return None
    value_part = body[sep_idx + 1:]
    rest = value_part[1:] if value_part[:1] in (" ", "\t") else value_part
    return indent_len, dash_len, key, rest


def _try_parse_bare_list_item(line: str):
    """Return (indent_len, value) for a scalar (non key:value) sequence
    item line (`- value`), else None."""
    indent_len = len(line) - len(line.lstrip(" \t"))
    body = line[indent_len:]
    if body == "-":
        return indent_len, ""
    if not (body.startswith("- ") or body.startswith("-\t")):
        return None
    val = body[1:].lstrip(" \t")
    if val.startswith("#"):
        return None
    return indent_len, val


def _redact_yaml_text(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, redacted_paths). Preserves formatting/comments
    for everything that is not itself a credential-shaped value."""
    lines = text.split("\n")
    out_lines: list[str] = []
    redacted_paths: list[str] = []
    seen_paths: set[str] = set()
    stack: list[tuple[int, str]] = []  # (column, key)
    n = len(lines)
    i = 0

    def _record(path_segments: list) -> None:
        path_str = ".".join(str(s) for s in path_segments)
        if path_str not in seen_paths:
            seen_paths.add(path_str)
            redacted_paths.append(path_str)

    while i < n:
        line = lines[i]
        parsed_key = _try_parse_key_line(line)
        if parsed_key is not None:
            indent_len, dash_len, key, rest = parsed_key
            col = indent_len + dash_len
            while stack and stack[-1][0] >= col:
                stack.pop()
            path_segments = [k for _, k in stack] + [key]
            sensitive = _path_is_sensitive(path_segments)
            rest_stripped = rest.strip()
            prefix = line[:indent_len] + ("- " if dash_len else "")
            if sensitive and rest_stripped and _BLOCK_SCALAR_RE.match(rest_stripped):
                # Multiline block scalar (| or >): redact the key line and
                # swallow every following more-indented line into it.
                out_lines.append(f"{prefix}{key}: {_REDACTED_PLACEHOLDER}")
                _record(path_segments)
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    if nxt.strip() == "":
                        j += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip(" \t"))
                    if nxt_indent <= col:
                        break
                    j += 1
                i = j
                stack.append((col, key))
                continue
            if sensitive and rest_stripped and not rest_stripped.startswith("#"):
                out_lines.append(f"{prefix}{key}: {_REDACTED_PLACEHOLDER}")
                _record(path_segments)
            else:
                out_lines.append(line)
            stack.append((col, key))
            i += 1
            continue

        bare = _try_parse_bare_list_item(line)
        if bare is not None:
            indent_len, val = bare
            while stack and stack[-1][0] >= indent_len:
                stack.pop()
            enclosing_path = [k for _, k in stack]
            if val.strip() and enclosing_path and _path_is_sensitive(enclosing_path):
                out_lines.append(f"{line[:indent_len]}- {_REDACTED_PLACEHOLDER}")
                _record(enclosing_path)
                i += 1
                continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines), redacted_paths


# ── Denylist (PUT) ────────────────────────────────────────────────────────
#
# Guards security-critical keys against being changed through the raw
# editor, so a compromised/careless WebUI session cannot use it to disable
# auth, widen trust, or open an allowlist. A path is "denylisted" if any
# segment starts with "allow" (allowed_hosts, allowlist, allow_users, ...),
# if it is/starts with a top-level auth/security section, if it starts with
# webui.auth*/webui.security* (nested form), if its top-level key starts
# with one of the sensitive flat webui_* prefixes below, or if any segment
# equals "trusted_proxies". Once a path is denylisted the whole subtree from
# that point is compared as one unit (deep equality) rather than recursed
# into further.
#
# config.yaml does NOT nest WebUI settings under a `webui:` mapping — the
# codebase uses flat `webui_<name>` top-level keys throughout (see
# api/auth_oidc.py, api/streaming.py, api/gateway_chat.py). An earlier
# version of this denylist only matched the nested `webui.auth*` shape via
# `path[0] == "webui"`, which never matches a flat `webui_oidc` key — a
# proven bypass letting the raw editor rewrite auth/execution/routing
# settings the denylist was meant to protect. Matched as a prefix on the
# WHOLE top-level key name (path[0]), same as the auth/security rule above.
_SENSITIVE_WEBUI_FLAT_KEY_PREFIXES = (
    "webui_oidc",  # OIDC issuer/client_id — hijacking these is a full auth bypass (api/auth_oidc.py)
    "webui_auth",
    "webui_security",
    "webui_trusted",
    "webui_passkey",  # passkey auth enable/disable toggle (api/auth.py)
    "webui_prefill_messages_script",  # shell command run via shlex+subprocess on every session prefill — RCE (api/streaming.py)
    "webui_gateway",  # gateway proxy base URL / routing — SSRF + credential exfiltration (api/gateway_chat.py)
    "webui_chat_backend",  # switches chat traffic into gateway-proxied mode (api/gateway_chat.py)
)

_MISSING = object()


def _is_denylisted_path(path: tuple) -> bool:
    if not path:
        return False
    lowered = [str(s).lower() for s in path]
    if any(seg.startswith("allow") for seg in lowered):
        return True
    if "trusted_proxies" in lowered:
        return True
    if lowered[0] in ("auth", "security"):
        return True
    if lowered[0] == "webui" and len(lowered) >= 2 and (
        lowered[1].startswith("auth") or lowered[1].startswith("security")
    ):
        return True
    if any(lowered[0].startswith(prefix) for prefix in _SENSITIVE_WEBUI_FLAT_KEY_PREFIXES):
        return True
    return False


def _find_denylist_violations(old: dict, new: dict) -> list[str]:
    violations: list[str] = []

    def walk(old_node: Any, new_node: Any, path: tuple) -> None:
        if path and _is_denylisted_path(path):
            if old_node != new_node:
                violations.append(".".join(path))
            return
        if isinstance(old_node, dict) or isinstance(new_node, dict):
            old_d = old_node if isinstance(old_node, dict) else {}
            new_d = new_node if isinstance(new_node, dict) else {}
            for key in sorted({str(k) for k in old_d} | {str(k) for k in new_d}):
                walk(old_d.get(key, _MISSING), new_d.get(key, _MISSING), path + (key,))
        # Non-dict, non-denylisted differences are allowed edits — no-op.

    walk(old, new, ())
    return violations


# ── YAML error location ─────────────────────────────────────────────────


def _yaml_error_location(exc) -> tuple:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is None:
        return None, None
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    return (
        line + 1 if isinstance(line, int) else None,
        column + 1 if isinstance(column, int) else None,
    )


# ── Atomic write ─────────────────────────────────────────────────────────


def _write_config_atomic(config_path: Path, text: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = None
    if config_path.exists():
        backup_path = config_path.with_name(config_path.name + _BACKUP_SUFFIX)
        try:
            shutil.copy2(config_path, backup_path)
        except OSError:
            logger.warning("Failed to write config editor backup at %s", backup_path, exc_info=True)
        try:
            original_mode = stat.S_IMODE(config_path.stat().st_mode)
        except OSError:
            original_mode = None
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".config-editor.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp() creates the tempfile 0600 regardless of the target's real
        # mode. Without re-applying the original mode, os.replace() silently
        # downgrades config.yaml to 0600 on every save — a behavior change
        # from whatever permissions (e.g. group-readable) the deployment had
        # intentionally set.
        if original_mode is not None:
            try:
                os.chmod(tmp, original_mode)
            except OSError:
                logger.warning("Failed to preserve config.yaml file mode on save", exc_info=True)
        os.replace(tmp, config_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Public API ────────────────────────────────────────────────────────────


def _read_config_bytes(config_path: Path) -> bytes:
    try:
        return config_path.read_bytes() if config_path.exists() else b""
    except OSError:
        logger.debug("Failed to read config.yaml at %s", config_path, exc_info=True)
        return b""


def _etag_for(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def get_config_raw() -> dict:
    config_path = _active_profile_config_path()
    raw_bytes = _read_config_bytes(config_path)
    text = raw_bytes.decode("utf-8", errors="replace")
    redacted_text, redacted_paths = _redact_yaml_text(text)
    return {
        "yaml": redacted_text,
        "redacted": redacted_paths,
        "allowed": _write_enabled(),
        "write_gate_env": _WRITE_GATE_ENV,
        "etag": _etag_for(raw_bytes),
    }


def put_config_raw(yaml_text: Any, *, etag: str | None = None) -> dict:
    if not _write_enabled():
        raise ConfigEditorError(
            f"Raw config editing is disabled. Set {_WRITE_GATE_ENV}=1 to enable it.",
            status=403,
            extra={"write_gate_env": _WRITE_GATE_ENV},
        )
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise ConfigEditorError("yaml is required", status=400)
    if _REDACTED_PLACEHOLDER in yaml_text:
        raise ConfigEditorError(
            "Submitted YAML still contains redacted placeholder values. "
            "Re-fetch and edit without redacted values.",
            status=400,
        )

    try:
        import yaml as _yaml
    except ImportError as exc:
        raise ConfigEditorError("PyYAML is required to save config.yaml", status=500) from exc

    try:
        parsed = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError as exc:
        line, column = _yaml_error_location(exc)
        raise ConfigEditorError(
            f"Invalid YAML: {exc}",
            status=400,
            extra={"line": line, "column": column},
        ) from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ConfigEditorError("Config must be a YAML mapping at the top level", status=400)

    config_path = _active_profile_config_path()

    from api.config import _cfg_lock, reload_config

    # The freshness check, the denylist comparison, and the write must all
    # observe the SAME on-disk snapshot, so they run together under
    # _cfg_lock rather than read-then-lock-then-write. Reading "current"
    # state outside the lock (the original implementation) left a window
    # between that read and the write where a concurrent save could land:
    # the denylist check would then be validating against an already-stale
    # snapshot, and the write would silently clobber the intervening change
    # without ever having checked it.
    with _cfg_lock:
        current_bytes = _read_config_bytes(config_path)

        if etag is not None:
            current_etag = _etag_for(current_bytes)
            if current_etag != etag:
                raise ConfigEditorError(
                    "config.yaml changed on disk since it was loaded. Re-fetch and retry.",
                    status=409,
                    extra={"etag": current_etag},
                )

        current_text = current_bytes.decode("utf-8", errors="replace")
        try:
            current_parsed = _yaml.safe_load(current_text) or {}
        except _yaml.YAMLError:
            current_parsed = {}
        if not isinstance(current_parsed, dict):
            current_parsed = {}

        blocked = _find_denylist_violations(current_parsed, parsed)
        if blocked:
            raise ConfigEditorError(
                "Refusing to change security-critical config keys via the raw editor: "
                + ", ".join(blocked),
                status=400,
                extra={"blocked_paths": blocked},
            )

        _write_config_atomic(config_path, yaml_text)
        new_etag = _etag_for(yaml_text.encode("utf-8"))
    reload_config()
    return {"ok": True, "etag": new_etag}
