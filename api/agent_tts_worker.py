"""One-shot child process for Agent-owned TTS operations."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

SCHEMA = 1
REQUEST_MAX_BYTES = 64 * 1024
STATUS_MAX_BYTES = 64 * 1024


def _status(ok: bool, code: str, **fields: Any) -> dict[str, Any]:
    payload = {"schema": SCHEMA, "ok": ok, "code": code}
    payload.update(fields)
    return payload


def write_status_file(path: Path, payload: dict[str, Any]) -> None:
    """Create exactly one private, bounded status file."""
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if not encoded or len(encoded) > STATUS_MAX_BYTES:
        raise ValueError("status exceeds protocol limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _request_paths(request: dict[str, Any]) -> tuple[Path, Path] | None:
    request_dir_value = request.get("request_dir")
    status_path_value = request.get("status_path")
    if not isinstance(request_dir_value, str) or not isinstance(status_path_value, str):
        return None
    request_dir = Path(request_dir_value).expanduser()
    status_path = Path(status_path_value).expanduser()
    try:
        resolved_dir = request_dir.resolve(strict=True)
        resolved_status = status_path.resolve(strict=False)
        resolved_status.relative_to(resolved_dir)
        if resolved_status.parent != resolved_dir:
            return None
        mode = os.lstat(resolved_dir).st_mode
    except (OSError, ValueError):
        return None
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        return None
    return resolved_dir, resolved_status


_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EDGE_VOICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class WorkerOperationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        diagnostic: str | None = None,
        provider: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic
        self.provider = provider


_ACTIVE_CONFIG_BINDING: ContextVar[
    tuple[Path, Path, tuple[int, int] | None] | None
] = ContextVar("agent_tts_config_binding", default=None)


def _classify_provider_failure(value: Any) -> str:
    """Reduce arbitrary provider failures to a bounded, non-secret category."""
    try:
        text = str(value or "").lower()[:2048]
    except Exception:
        text = ""
    if any(marker in text for marker in ("timed out", "timeout", "deadline")):
        return "provider_timeout"
    if any(marker in text for marker in ("rate limit", "quota", "too many requests", "429")):
        return "provider_rate_limit"
    if any(
        marker in text
        for marker in ("unauthorized", "forbidden", "api key", "authentication", "401", "403")
    ):
        return "provider_auth"
    if any(marker in text for marker in ("bad request", "invalid argument", "400")):
        return "provider_request"
    if any(marker in text for marker in ("module", "dependency", "not installed")):
        return "provider_dependency"
    if any(marker in text for marker in ("connection", "network", "dns", "ssl")):
        return "provider_network"
    return "provider_error"


_BUILTIN_LABEL_KEYS = {
    provider_id: f"tts_provider_{provider_id}"
    for provider_id in (
        "edge",
        "openai",
        "xai",
        "elevenlabs",
        "mistral",
        "gemini",
        "kittentts",
        "piper",
        "deepinfra",
        "neutts",
    )
}


def _import_agent_modules():
    from hermes_cli import config as config_module
    from hermes_cli import tools_config
    from tools import tts_tool

    return tts_tool, tools_config, config_module


def _callable_accepts(function: Any, **arguments: Any) -> bool:
    if not callable(function):
        return False
    try:
        inspect.signature(function).bind_partial(**arguments)
    except (TypeError, ValueError):
        return False
    return True


def _safe_label(value: Any, fallback: str = "") -> str:
    text = " ".join(str(value or fallback).split())
    return text[:160]


def _config_fingerprint(config: dict[str, Any]) -> str:
    canonical = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _with_tts_snapshot(tts_tool: Any, tts_config: dict[str, Any], callback):
    original = tts_tool._load_tts_config
    tts_tool._load_tts_config = lambda: copy.deepcopy(tts_config)
    try:
        return callback()
    finally:
        tts_tool._load_tts_config = original


def _candidate_config(tools_config: Any, row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(config)
    apply_selection = getattr(tools_config, "apply_provider_selection", None)
    if callable(apply_selection):
        apply_selection("tts", str(row.get("name") or ""), candidate)
    else:
        section = candidate.get("tts")
        if not isinstance(section, dict):
            section = {}
        section["provider"] = str(row.get("tts_provider") or "").strip()
        candidate["tts"] = section
    return candidate


def _requirements_for_config(tts_tool: Any, config: dict[str, Any]) -> bool:
    section = config.get("tts")
    snapshot = copy.deepcopy(section) if isinstance(section, dict) else {}
    return bool(
        _with_tts_snapshot(tts_tool, snapshot, tts_tool.check_tts_requirements)
    )


def _unsupported_capability() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": "agent",
        "supported": False,
        "synthesis_supported": False,
        "provider_write_supported": False,
        "code": "agent_contract_unavailable",
        "providers": [],
    }


def _resolve_text_limits(
    tts_tool: Any,
    provider: str,
    tts_snapshot: dict[str, Any],
) -> tuple[int, int, str]:
    limit_resolver = getattr(tts_tool, "_resolve_max_text_length", None)
    if callable(limit_resolver):
        provider_limit = int(limit_resolver(provider, copy.deepcopy(tts_snapshot)))
        if provider_limit <= 0:
            raise ValueError("non-positive limit")
        limit_source = "agent"
    else:
        provider_limit = 2000
        limit_source = "compatibility_fallback"
    try:
        transport_limit = int(os.environ.get("HERMES_WEBUI_TTS_REQUEST_MAX_CHARS", "4000"))
    except ValueError:
        transport_limit = 4000
    transport_limit = max(256, min(transport_limit, 10000))
    return provider_limit, min(provider_limit, transport_limit), limit_source


def _resolve_runtime_provider(tts_tool: Any, provider: str, available: bool) -> str:
    resolved = provider
    if provider != "edge" or not available:
        return resolved
    check_edge = getattr(tts_tool, "_check_edge_available", None)
    check_neutts = getattr(tts_tool, "_check_neutts_available", None)
    try:
        if callable(check_edge) and not check_edge() and callable(check_neutts) and check_neutts():
            resolved = "neutts"
    except Exception:
        pass
    return resolved


def _provider_readiness(
    tools_config: Any,
    row: dict[str, Any],
    config: dict[str, Any],
    *,
    is_active: bool,
) -> bool | None:
    readiness = getattr(tools_config, "provider_readiness_status", None)
    if not callable(readiness):
        return None
    try:
        return readiness(
            row, copy.deepcopy(config), is_active=is_active
        ) == "ready"
    except Exception:
        return False


def build_capability_payload() -> dict[str, Any]:
    """Mirror Agent/Desktop selection semantics without exposing setup secrets."""
    try:
        tts_tool, tools_config, config_module = _import_agent_modules()
    except (ImportError, ModuleNotFoundError):
        return _unsupported_capability()

    synthesis = getattr(tts_tool, "text_to_speech_tool", None)
    required_tts = (
        getattr(tts_tool, "_load_tts_config", None),
        getattr(tts_tool, "_get_provider", None),
        getattr(tts_tool, "check_tts_requirements", None),
    )
    required_catalog = (
        getattr(tools_config, "_visible_providers", None),
        getattr(tools_config, "_is_provider_active", None),
    )
    load_config = getattr(config_module, "load_config", None)
    if (
        not _callable_accepts(synthesis, text="x", output_path="/tmp/audio.mp3")
        or not all(callable(value) for value in required_tts + required_catalog)
        or not callable(load_config)
    ):
        return _unsupported_capability()

    try:
        config = load_config()
        if not isinstance(config, dict):
            config = {}
        config = copy.deepcopy(config)
        section = config.get("tts")
        tts_snapshot = copy.deepcopy(section) if isinstance(section, dict) else {}
        active_provider = str(tts_tool._get_provider(tts_snapshot) or "edge").strip().lower()
        category = getattr(tools_config, "TOOL_CATEGORIES", {}).get("tts")
        if not isinstance(category, dict):
            return _unsupported_capability()
        try:
            visible_rows = tools_config._visible_providers(
                category, copy.deepcopy(config), force_fresh=True
            )
        except TypeError:
            visible_rows = tools_config._visible_providers(category, copy.deepcopy(config))
        if not isinstance(visible_rows, list):
            visible_rows = []
    except Exception:
        return _unsupported_capability()

    active_available = _requirements_for_config(tts_tool, config)
    rows: list[dict[str, Any]] = []
    active_name = active_provider
    for raw_row in visible_rows:
        if not isinstance(raw_row, dict):
            continue
        provider_id = str(raw_row.get("tts_provider") or "").strip().lower()
        name = _safe_label(raw_row.get("name"), provider_id)
        if not _PROVIDER_ID_RE.fullmatch(provider_id) or not name:
            continue
        try:
            active = bool(
                tools_config._is_provider_active(
                    raw_row, copy.deepcopy(config), force_fresh=True
                )
            )
        except TypeError:
            active = bool(tools_config._is_provider_active(raw_row, copy.deepcopy(config)))
        except Exception:
            active = False
        # Readiness is the managed-provider entitlement boundary. An exception
        # is unknown, never permission to use an unrelated direct credential.
        configured = _provider_readiness(
            tools_config, raw_row, config, is_active=active
        )
        # Requirements checks import provider SDKs in the one-shot worker. The
        # Agent readiness contract already proves an inactive row with missing
        # setup/credentials cannot run, so avoid importing its SDK merely to
        # rediscover the same unavailable result. Active and ready candidates
        # still receive the exact Agent requirements check.
        if configured is not False:
            try:
                candidate = _candidate_config(tools_config, raw_row, config)
                available = _requirements_for_config(tts_tool, candidate)
            except Exception:
                available = False
        else:
            available = False
        if configured is None:
            configured = available
        if active:
            active_name = name
            active_available = bool(available)
        rows.append(
            {
                "name": name,
                "provider_id": provider_id,
                "label_key": (
                    None
                    if raw_row.get("managed_nous_feature")
                    else _BUILTIN_LABEL_KEYS.get(provider_id)
                ),
                "badge": _safe_label(raw_row.get("badge")),
                "tag": _safe_label(raw_row.get("tag")),
                "configured": bool(configured),
                "available": bool(available),
                "active": active,
                "selectable": bool(configured and available),
            }
        )

    resolved_provider = _resolve_runtime_provider(
        tts_tool, active_provider, active_available
    )
    configured_providers = tts_snapshot.get("providers")
    configured_active = (
        configured_providers.get(active_provider)
        if isinstance(configured_providers, dict)
        else None
    )
    if (
        not any(row["active"] for row in rows)
        and isinstance(configured_active, dict)
        and _PROVIDER_ID_RE.fullmatch(active_provider)
    ):
        # Agent command providers are valid runtime authorities even when the
        # Desktop catalog has no selectable row for them. Surface only the safe
        # config key; never project the command, environment, or plugin payload.
        rows.append(
            {
                "name": active_provider,
                "provider_id": active_provider,
                "label_key": _BUILTIN_LABEL_KEYS.get(active_provider),
                "badge": "",
                "tag": "",
                "configured": True,
                "available": bool(active_available),
                "active": True,
                "selectable": False,
            }
        )
        active_name = active_provider

    try:
        provider_limit, request_limit, limit_source = _resolve_text_limits(
            tts_tool, active_provider, tts_snapshot
        )
    except Exception:
        return _unsupported_capability()

    provider_write_supported = all(
        callable(value)
        for value in (
            getattr(tools_config, "apply_provider_selection", None),
            getattr(config_module, "load_config", None),
            getattr(config_module, "save_config", None),
            getattr(config_module, "get_config_path", None),
        )
    )
    return {
        "schema_version": 1,
        "engine": "agent",
        "supported": True,
        "synthesis_supported": True,
        "provider_write_supported": provider_write_supported,
        "active_provider": active_provider,
        "active_provider_name": active_name,
        "active_provider_available": active_available,
        "resolved_provider": resolved_provider,
        "provider_max_text_length": provider_limit,
        "request_max_text_length": request_limit,
        "limit_source": limit_source,
        "config_fingerprint": _config_fingerprint(config),
        "providers": rows,
    }


def _handle_capability(request: dict[str, Any]) -> dict[str, Any]:
    payload = build_capability_payload()
    if payload.get("supported") is not True:
        return _status(False, "agent_contract_unavailable")
    return _status(True, "ok", **payload)


def _visible_tts_rows(tools_config: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    category = getattr(tools_config, "TOOL_CATEGORIES", {}).get("tts")
    if not isinstance(category, dict):
        raise WorkerOperationError("agent_contract_unavailable")
    try:
        rows = tools_config._visible_providers(
            category, copy.deepcopy(config), force_fresh=True
        )
    except TypeError:
        rows = tools_config._visible_providers(category, copy.deepcopy(config))
    if not isinstance(rows, list):
        raise WorkerOperationError("agent_contract_unavailable")
    return [row for row in rows if isinstance(row, dict)]


def _capture_config_binding(config_module: Any):
    get_path = getattr(config_module, "get_config_path", None)
    if not callable(get_path):
        raise WorkerOperationError("agent_contract_unavailable")
    declared = Path(get_path()).expanduser()
    try:
        link_stat = os.lstat(declared)
    except FileNotFoundError:
        link_identity = None
    else:
        link_identity = (
            (link_stat.st_dev, link_stat.st_ino) if declared.is_symlink() else None
        )
    target = declared.resolve(strict=False) if link_identity is not None else declared
    return declared, target, link_identity, get_path


@contextmanager
def _config_process_lock(config_module: Any):
    """Serialize an Agent config transaction with ambient WebUI YAML writers."""
    declared, target, link_identity, original_get_path = _capture_config_binding(
        config_module
    )
    lock_path = target.with_name(f".{target.name}.hermes-webui.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        if os.name == "nt":  # pragma: no cover - Windows CI is unavailable
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        _verify_config_binding(declared, target, link_identity)
        config_module.get_config_path = lambda: target
        token = _ACTIVE_CONFIG_BINDING.set((declared, target, link_identity))
        try:
            yield
        finally:
            _ACTIVE_CONFIG_BINDING.reset(token)
            config_module.get_config_path = original_get_path
    finally:
        try:
            if os.name == "nt":  # pragma: no cover - Windows CI is unavailable
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _serialized_provider_write(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            _tts_tool, _tools_config, config_module = _import_agent_modules()
        except Exception as exc:
            raise WorkerOperationError("agent_contract_unavailable") from exc
        with _config_process_lock(config_module):
            return function(*args, **kwargs)

    return wrapped


def _verify_config_binding(
    declared: Path, target: Path, link_identity: tuple[int, int] | None
) -> None:
    if link_identity is None:
        if declared.is_symlink():
            raise WorkerOperationError("config_write_failed")
        return
    try:
        current = os.lstat(declared)
    except OSError as exc:
        raise WorkerOperationError("config_write_failed") from exc
    if (
        not declared.is_symlink()
        or (current.st_dev, current.st_ino) != link_identity
        or declared.resolve(strict=False) != target
    ):
        raise WorkerOperationError("config_write_failed")


def _save_agent_config(config_module: Any, config: dict[str, Any]) -> None:
    save_config = getattr(config_module, "save_config", None)
    if not callable(save_config):
        raise WorkerOperationError("agent_contract_unavailable")
    active_binding = _ACTIVE_CONFIG_BINDING.get()
    if active_binding is not None:
        declared, target, link_identity = active_binding
        _verify_config_binding(declared, target, link_identity)
        try:
            save_config(config)
        except Exception as exc:
            raise WorkerOperationError("config_write_failed") from exc
        _verify_config_binding(declared, target, link_identity)
        return
    declared, target, link_identity, original_get_path = _capture_config_binding(config_module)
    _verify_config_binding(declared, target, link_identity)
    config_module.get_config_path = lambda: target
    try:
        save_config(config)
    except Exception as exc:
        raise WorkerOperationError("config_write_failed") from exc
    finally:
        config_module.get_config_path = original_get_path
    _verify_config_binding(declared, target, link_identity)


def _active_exact_row(
    tools_config: Any,
    row: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    try:
        return bool(
            tools_config._is_provider_active(
                row, copy.deepcopy(config), force_fresh=True
            )
        )
    except TypeError:
        return bool(tools_config._is_provider_active(row, copy.deepcopy(config)))


@_serialized_provider_write
def select_provider_payload(
    provider_name: str,
    provider_id: str,
    expected_fingerprint: str,
    *,
    legacy_edge_voice: str | None = None,
) -> dict[str, Any]:
    """Apply one exact visible Desktop row and verify the authoritative result."""
    provider_name = str(provider_name or "")
    provider_id = str(provider_id or "").strip().lower()
    if (
        not isinstance(provider_name, str)
        or not provider_name.strip()
        or len(provider_name) > 160
        or not isinstance(expected_fingerprint, str)
    ):
        raise WorkerOperationError("invalid_provider")
    if legacy_edge_voice is not None and legacy_edge_voice:
        if not _EDGE_VOICE_RE.fullmatch(str(legacy_edge_voice)):
            raise WorkerOperationError("invalid_request")

    try:
        tts_tool, tools_config, config_module = _import_agent_modules()
        load_config = getattr(config_module, "load_config", None)
        apply_selection = getattr(tools_config, "apply_provider_selection", None)
        if not callable(load_config) or not callable(apply_selection):
            raise WorkerOperationError("agent_contract_unavailable")
        current = load_config()
        if not isinstance(current, dict):
            current = {}
        current = copy.deepcopy(current)
    except WorkerOperationError:
        raise
    except Exception as exc:
        raise WorkerOperationError("agent_contract_unavailable") from exc

    if _config_fingerprint(current) != expected_fingerprint:
        raise WorkerOperationError("config_conflict")
    rows = _visible_tts_rows(tools_config, current)
    matches = [
        row
        for row in rows
        if row.get("name") == provider_name
        and (
            not provider_id
            or str(row.get("tts_provider") or "").strip().lower() == provider_id
        )
    ]
    if len(matches) != 1:
        raise WorkerOperationError("invalid_provider")
    exact_row = matches[0]
    provider_id = str(exact_row.get("tts_provider") or "").strip().lower()
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise WorkerOperationError("invalid_provider")

    candidate = copy.deepcopy(current)
    try:
        apply_selection("tts", provider_name, candidate)
    except Exception as exc:
        raise WorkerOperationError("invalid_provider") from exc
    configured = _provider_readiness(
        tools_config, exact_row, candidate, is_active=True
    )
    if configured is False:
        raise WorkerOperationError("provider_unavailable")
    if not _requirements_for_config(tts_tool, candidate):
        raise WorkerOperationError("provider_unavailable")
    candidate_tts = candidate.get("tts")
    if not isinstance(candidate_tts, dict):
        candidate_tts = {}
    try:
        provider_limit, request_limit, limit_source = _resolve_text_limits(
            tts_tool, provider_id, candidate_tts
        )
    except Exception as exc:
        raise WorkerOperationError("agent_contract_unavailable") from exc

    previous_tts_present = "tts" in current
    previous_tts = copy.deepcopy(current.get("tts")) if previous_tts_present else None
    if provider_id == "edge" and legacy_edge_voice:
        tts_section = candidate.setdefault("tts", {})
        if not isinstance(tts_section, dict):
            tts_section = {}
            candidate["tts"] = tts_section
        edge_section = tts_section.setdefault("edge", {})
        if not isinstance(edge_section, dict):
            edge_section = {}
            tts_section["edge"] = edge_section
        edge_section["voice"] = legacy_edge_voice

    _save_agent_config(config_module, candidate)
    try:
        authoritative = load_config()
        if not isinstance(authoritative, dict):
            authoritative = {}
        authoritative = copy.deepcopy(authoritative)
    except Exception as exc:
        raise WorkerOperationError("config_write_failed") from exc
    try:
        is_active = _active_exact_row(tools_config, exact_row, authoritative)
        is_available = _requirements_for_config(tts_tool, authoritative)
    except Exception as exc:
        raise WorkerOperationError("config_write_failed") from exc
    if not is_active or not is_available:
        raise WorkerOperationError("config_write_failed")
    authoritative_tts = authoritative.get("tts")
    if not isinstance(authoritative_tts, dict):
        authoritative_tts = {}
    try:
        provider_limit, request_limit, limit_source = _resolve_text_limits(
            tts_tool, provider_id, authoritative_tts
        )
    except Exception as exc:
        raise WorkerOperationError("config_write_failed") from exc
    resolved_provider = _resolve_runtime_provider(
        tts_tool, provider_id, bool(is_available)
    )

    return {
        "active_provider": provider_id,
        "active_provider_name": provider_name,
        "active_provider_available": True,
        "resolved_provider": resolved_provider,
        "configured": True,
        "synthesis_supported": True,
        "provider_max_text_length": provider_limit,
        "request_max_text_length": request_limit,
        "limit_source": limit_source,
        "config_fingerprint": _config_fingerprint(authoritative),
        "previous_tts_present": previous_tts_present,
        "previous_tts": previous_tts,
    }


@_serialized_provider_write
def restore_tts_payload(
    previous_tts: Any,
    previous_tts_present: bool,
    expected_post_fingerprint: str,
) -> dict[str, Any]:
    """Conditionally compensate only the prior server-held TTS subtree."""
    if previous_tts_present and not isinstance(previous_tts, dict):
        raise WorkerOperationError("invalid_request")
    try:
        tts_tool, tools_config, config_module = _import_agent_modules()
        load_config = getattr(config_module, "load_config", None)
        if not callable(load_config):
            raise WorkerOperationError("agent_contract_unavailable")
        current = load_config()
        if not isinstance(current, dict):
            current = {}
        current = copy.deepcopy(current)
    except WorkerOperationError:
        raise
    except Exception as exc:
        raise WorkerOperationError("agent_contract_unavailable") from exc
    if _config_fingerprint(current) != expected_post_fingerprint:
        raise WorkerOperationError("config_conflict")
    if previous_tts_present:
        current["tts"] = copy.deepcopy(previous_tts)
    else:
        current.pop("tts", None)
    _save_agent_config(config_module, current)
    authoritative = load_config()
    if not isinstance(authoritative, dict):
        authoritative = {}
    if ("tts" in authoritative) != previous_tts_present or (
        previous_tts_present and authoritative.get("tts") != previous_tts
    ):
        raise WorkerOperationError("config_write_failed")
    section = authoritative.get("tts")
    snapshot = copy.deepcopy(section) if isinstance(section, dict) else {}
    active_provider = str(tts_tool._get_provider(snapshot) or "edge").strip().lower()
    return {
        "active_provider": active_provider,
        "config_fingerprint": _config_fingerprint(authoritative),
    }


def _handle_select_provider(request: dict[str, Any]) -> dict[str, Any]:
    result = select_provider_payload(
        request.get("provider_name"),
        request.get("provider_id"),
        request.get("expected_fingerprint"),
        legacy_edge_voice=request.get("legacy_edge_voice"),
    )
    return _status(True, "ok", **result)


def _path_inside_root(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return False
    return True


def synthesize_payload(
    text: str,
    output_path: Path,
    request_root: Path,
) -> dict[str, Any]:
    """Call the exact Agent tool against one frozen configuration snapshot."""
    if not isinstance(text, str) or not text.strip():
        raise WorkerOperationError("invalid_request")
    output_path = Path(output_path)
    request_root = Path(request_root)
    if not _path_inside_root(output_path, request_root):
        raise WorkerOperationError("tts_artifact_invalid")
    try:
        tts_tool, _tools_config, config_module = _import_agent_modules()
        load_config = getattr(config_module, "load_config", None)
        synthesis = getattr(tts_tool, "text_to_speech_tool", None)
        if (
            not callable(load_config)
            or not _callable_accepts(
                synthesis, text="x", output_path=str(output_path)
            )
        ):
            raise WorkerOperationError("agent_contract_unavailable")
        config = load_config()
        if not isinstance(config, dict):
            config = {}
        config = copy.deepcopy(config)
        section = config.get("tts")
        snapshot = copy.deepcopy(section) if isinstance(section, dict) else {}
        configured_provider = str(
            tts_tool._get_provider(copy.deepcopy(snapshot)) or "edge"
        ).strip().lower()
    except WorkerOperationError:
        raise
    except Exception as exc:
        raise WorkerOperationError("agent_contract_unavailable") from exc

    if not bool(
        _with_tts_snapshot(
            tts_tool, snapshot, tts_tool.check_tts_requirements
        )
    ):
        raise WorkerOperationError("provider_unavailable")
    limit_resolver = getattr(tts_tool, "_resolve_max_text_length", None)
    if callable(limit_resolver):
        try:
            provider_limit = int(
                limit_resolver(configured_provider, copy.deepcopy(snapshot))
            )
        except Exception as exc:
            raise WorkerOperationError("agent_contract_unavailable") from exc
    else:
        provider_limit = 2000
    try:
        request_limit = int(
            os.environ.get("HERMES_WEBUI_TTS_REQUEST_MAX_CHARS", "4000")
        )
    except ValueError:
        request_limit = 4000
    request_limit = max(256, min(request_limit, 10000))
    effective_limit = min(provider_limit, request_limit)
    if provider_limit <= 0 or len(text) > effective_limit:
        raise WorkerOperationError("text_too_long")

    try:
        raw_result = _with_tts_snapshot(
            tts_tool,
            snapshot,
            lambda: synthesis(text=text, output_path=str(output_path)),
        )
    except Exception as exc:
        raise WorkerOperationError(
            "synthesis_failed",
            diagnostic=_classify_provider_failure(exc),
            provider=(
                configured_provider
                if _PROVIDER_ID_RE.fullmatch(configured_provider)
                else None
            ),
        ) from exc
    if not isinstance(raw_result, str) or len(raw_result.encode("utf-8")) > STATUS_MAX_BYTES:
        raise WorkerOperationError("synthesis_failed")
    try:
        result = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise WorkerOperationError("synthesis_failed") from exc
    if not isinstance(result, dict):
        raise WorkerOperationError("synthesis_failed")
    if result.get("success") is not True:
        raise WorkerOperationError(
            "synthesis_failed",
            diagnostic=_classify_provider_failure(result.get("error")),
            provider=(
                configured_provider
                if _PROVIDER_ID_RE.fullmatch(configured_provider)
                else None
            ),
        )
    artifact_value = result.get("file_path")
    actual_provider = str(result.get("provider") or "").strip().lower()
    if not isinstance(artifact_value, str) or not artifact_value:
        raise WorkerOperationError("tts_artifact_invalid")
    artifact_path = Path(artifact_value).expanduser()
    if not artifact_path.is_absolute():
        artifact_path = Path.cwd() / artifact_path
    if not _path_inside_root(artifact_path, request_root):
        raise WorkerOperationError("tts_artifact_invalid")
    if actual_provider != configured_provider and not (
        configured_provider == "edge" and actual_provider == "neutts"
    ):
        raise WorkerOperationError("provider_mismatch")
    return {
        "artifact_path": str(artifact_path),
        "configured_provider": configured_provider,
        "provider": actual_provider,
        "provider_max_text_length": provider_limit,
        "request_max_text_length": effective_limit,
    }


def _handle_synthesize(request: dict[str, Any]) -> dict[str, Any]:
    output_value = request.get("output_path")
    request_root_value = request.get("request_dir")
    if not isinstance(output_value, str) or not isinstance(request_root_value, str):
        raise WorkerOperationError("invalid_request")
    result = synthesize_payload(
        request.get("text"), Path(output_value), Path(request_root_value)
    )
    return _status(True, "ok", **result)


def _handle_restore_tts(request: dict[str, Any]) -> dict[str, Any]:
    result = restore_tts_payload(
        request.get("previous_tts"),
        request.get("previous_tts_present") is True,
        request.get("expected_post_fingerprint"),
    )
    return _status(True, "ok", **result)


def dispatch_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema") != SCHEMA:
        return _status(False, "invalid_request")
    operation = request.get("op")
    handlers = {
        "capability": _handle_capability,
        "select_provider": _handle_select_provider,
        "synthesize": _handle_synthesize,
        "restore_tts": _handle_restore_tts,
    }
    handler = handlers.get(operation)
    if handler is None:
        return _status(False, "invalid_request")
    try:
        return handler(request)
    except WorkerOperationError as exc:
        fields = {}
        if exc.diagnostic:
            fields["diagnostic"] = exc.diagnostic
        if exc.provider:
            fields["provider"] = exc.provider
        return _status(False, exc.code, **fields)
    except Exception:
        return _status(False, "agent_contract_unavailable")


def main() -> int:
    raw = sys.stdin.buffer.read(REQUEST_MAX_BYTES + 1)
    if not raw or len(raw) > REQUEST_MAX_BYTES:
        return 2
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 2
    if not isinstance(request, dict):
        return 2
    paths = _request_paths(request)
    if paths is None:
        return 2
    _request_dir, status_path = paths
    response = dispatch_request(request)
    try:
        write_status_file(status_path, response)
    except Exception:
        return 2
    return 0 if response.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
