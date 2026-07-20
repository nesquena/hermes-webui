"""Safe aggregate host resource metrics for the WebUI system panel (#693).

The browser only needs coarse CPU/RAM/disk usage. Linux uses procfs first;
platforms without procfs (for example macOS) fall back to psutil for aggregate
CPU/RAM metrics. Keep the payload intentionally small: no process lists,
command strings, user identities, environment variables, or filesystem topology
leave the server.
"""

from __future__ import annotations

import shutil
import time
from importlib import import_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROC_STAT = Path("/proc/stat")
_PROC_MEMINFO = Path("/proc/meminfo")
_CPU_SAMPLE_SECONDS = 0.05


def _load_optional_psutil():
    try:
        return import_module("psutil")
    except ImportError:
        raise RuntimeError("psutil_unavailable") from None


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_percent(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric < 0:
        numeric = 0.0
    if numeric > 100:
        numeric = 100.0
    return round(numeric, 1)


def _read_proc_stat_cpu() -> tuple[int, int]:
    """Return (idle_ticks, total_ticks) from Linux /proc/stat."""
    with _PROC_STAT.open("r", encoding="utf-8") as handle:
        first = handle.readline().strip().split()
    if not first or first[0] != "cpu":
        raise RuntimeError("proc_stat_unavailable")
    values = [int(part) for part in first[1:]]
    if len(values) < 4:
        raise RuntimeError("proc_stat_unavailable")
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    if total <= 0:
        raise RuntimeError("proc_stat_unavailable")
    return idle, total


def _cpu_delta_percent(start: tuple[int, int], end: tuple[int, int]) -> float:
    idle_delta = end[0] - start[0]
    total_delta = end[1] - start[1]
    if total_delta <= 0:
        return 0.0
    busy_delta = max(0, total_delta - max(0, idle_delta))
    return _clamp_percent((busy_delta / total_delta) * 100.0)


def _cpu_percent() -> float:
    """Sample aggregate CPU usage.

    A short local sample avoids storing cross-request state and returns a stable
    percentage on the first poll. Linux uses procfs without extra dependencies;
    platforms without procfs fall back to psutil when it is already available.
    Unsupported platforms raise a safe error code.
    """
    try:
        start = _read_proc_stat_cpu()
    except OSError:
        psutil = _load_optional_psutil()
        return _clamp_percent(psutil.cpu_percent(interval=_CPU_SAMPLE_SECONDS))
    time.sleep(_CPU_SAMPLE_SECONDS)
    try:
        end = _read_proc_stat_cpu()
    except OSError:
        psutil = _load_optional_psutil()
        return _clamp_percent(psutil.cpu_percent(interval=0.0))
    return _cpu_delta_percent(start, end)


def _read_meminfo_kib() -> dict[str, int]:
    data: dict[str, int] = {}
    with _PROC_MEMINFO.open("r", encoding="utf-8") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if not key or not rest:
                continue
            parts = rest.strip().split()
            if not parts:
                continue
            try:
                data[key] = int(parts[0])
            except ValueError:
                continue
    return data


def _memory_usage() -> dict[str, int | float]:
    try:
        meminfo = _read_meminfo_kib()
    except OSError:
        vm = _load_optional_psutil().virtual_memory()
        total = int(getattr(vm, "total", 0) or 0)
        if total <= 0:
            raise RuntimeError("memory_unavailable") from None
        available = max(0, int(getattr(vm, "available", 0) or 0))
    else:
        total = int(meminfo.get("MemTotal") or 0) * 1024
        if total <= 0:
            raise RuntimeError("meminfo_unavailable")
        available_kib = meminfo.get("MemAvailable")
        if available_kib is None:
            available_kib = (
                meminfo.get("MemFree", 0)
                + meminfo.get("Buffers", 0)
                + meminfo.get("Cached", 0)
                + meminfo.get("SReclaimable", 0)
                - meminfo.get("Shmem", 0)
            )
        available = max(0, int(available_kib) * 1024)
    used = max(0, min(total, total - available))
    return {
        "used_bytes": used,
        "total_bytes": total,
        "percent": _clamp_percent((used / total) * 100.0),
    }


def _disk_usage() -> dict[str, int | float]:
    usage = shutil.disk_usage("/")
    total = int(usage.total)
    if total <= 0:
        raise RuntimeError("disk_unavailable")
    used = int(usage.used)
    return {
        "used_bytes": used,
        "total_bytes": total,
        "percent": _clamp_percent((used / total) * 100.0),
    }


def _safe_error(metric: str, exc: Exception) -> dict[str, str]:
    # Keep this intentionally coarse. Exception messages can contain local paths
    # on unusual platforms; the browser only needs a safe unavailable reason.
    return {"metric": metric, "code": type(exc).__name__}


def _zero_webui_runtime_payload() -> dict[str, Any]:
    return {
        "sessions": {"resident_count": 0, "effective_cap": 0},
        "session_list_cache": {"entries": 0, "entry_cap": 0, "inflight_rebuilds": 0},
        "streams": {
            "active_streams": 0,
            "total_subscribers": 0,
            "total_offline_buffered_events": 0,
            "total_offline_dropped_events": 0,
            "per_stream_offline_buffer_cap": 0,
        },
        "models_cache": {
            "loaded": False,
            "provider_groups": 0,
            "total_models": 0,
            "age_seconds": None,
        },
    }


def _webui_runtime_sources() -> dict[str, Any]:
    from api import config as _config
    from api import route_session_list_cache as _route_session_list_cache

    return {
        "sessions": _config.SESSIONS,
        "sessions_lock": _config.LOCK,
        "get_sessions_cache_max": _config.get_sessions_cache_max,
        "streams": _config.STREAMS,
        "streams_lock": _config.STREAMS_LOCK,
        "stream_buffer_cap": _config.StreamChannel._OFFLINE_BUFFER_MAXLEN,
        "session_list_cache": _route_session_list_cache._SESSIONS_CACHE,
        "session_list_cache_inflight": _route_session_list_cache._SESSIONS_CACHE_INFLIGHT,
        "session_list_cache_lock": _route_session_list_cache._SESSIONS_CACHE_LOCK,
        "session_list_cache_cap": _route_session_list_cache._SESSIONS_CACHE_MAX_ENTRIES,
        "models_cache_lock": _config._available_models_cache_lock,
        "models_cache_snapshot": lambda: (
            _config._available_models_cache,
            _config._available_models_cache_ts,
        ),
        "is_valid_models_cache": _config._is_valid_models_cache,
    }


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _models_cache_stats(snapshot: Any, cache_ts: Any, validator) -> dict[str, Any]:
    stats = {"loaded": False, "provider_groups": 0, "total_models": 0, "age_seconds": None}
    loaded = bool(callable(validator) and validator(snapshot))
    if not loaded or not isinstance(snapshot, dict):
        return stats
    groups = snapshot.get("groups")
    if not isinstance(groups, list):
        return stats
    stats["loaded"] = True
    stats["provider_groups"] = len(groups)
    total_models = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in ("models", "extra_models"):
            models = group.get(bucket)
            if isinstance(models, list):
                total_models += len(models)
    stats["total_models"] = total_models
    try:
        ts = float(cache_ts)
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 0:
        stats["age_seconds"] = round(max(0.0, time.monotonic() - ts), 1)
    return stats


def _webui_runtime_payload() -> dict[str, Any]:
    payload = _zero_webui_runtime_payload()
    sources = _webui_runtime_sources()

    sessions = sources.get("sessions")
    sessions_lock = sources.get("sessions_lock")
    with sessions_lock:
        payload["sessions"]["resident_count"] = len(sessions)
    get_sessions_cache_max = sources.get("get_sessions_cache_max")
    if callable(get_sessions_cache_max):
        payload["sessions"]["effective_cap"] = _safe_int(get_sessions_cache_max())

    session_list_cache = sources.get("session_list_cache")
    session_list_cache_lock = sources.get("session_list_cache_lock")
    session_list_cache_inflight = sources.get("session_list_cache_inflight")
    with session_list_cache_lock:
        payload["session_list_cache"]["entries"] = len(session_list_cache)
        payload["session_list_cache"]["inflight_rebuilds"] = len(session_list_cache_inflight)
        payload["session_list_cache"]["entry_cap"] = _safe_int(sources.get("session_list_cache_cap"))

    streams = sources.get("streams")
    streams_lock = sources.get("streams_lock")
    stream_buffer_cap = _safe_int(sources.get("stream_buffer_cap"))
    payload["streams"]["per_stream_offline_buffer_cap"] = stream_buffer_cap
    total_subscribers = 0
    total_offline_buffered_events = 0
    total_offline_dropped_events = 0
    stream_items: list[Any] = []
    with streams_lock:
        stream_items = list(streams.values())
    payload["streams"]["active_streams"] = len(stream_items)
    for channel in stream_items:
        snapshot = channel.diagnostic_snapshot()
        if not isinstance(snapshot, dict):
            continue
        total_subscribers += _safe_int(snapshot.get("subscriber_count"))
        total_offline_buffered_events += _safe_int(snapshot.get("offline_buffered_events"))
        total_offline_dropped_events += _safe_int(snapshot.get("offline_dropped_events"))
    payload["streams"]["total_subscribers"] = total_subscribers
    payload["streams"]["total_offline_buffered_events"] = total_offline_buffered_events
    payload["streams"]["total_offline_dropped_events"] = total_offline_dropped_events

    models_cache_lock = sources.get("models_cache_lock")
    with models_cache_lock:
        snapshot, cache_ts = sources["models_cache_snapshot"]()
    payload["models_cache"] = _models_cache_stats(snapshot, cache_ts, sources["is_valid_models_cache"])
    return payload


def build_system_health_payload() -> dict[str, Any]:
    metrics: dict[str, Any] = {"cpu": None, "memory": None, "disk": None}
    errors: list[dict[str, str]] = []

    collectors = {
        "cpu": _cpu_percent,
        "memory": _memory_usage,
        "disk": _disk_usage,
    }
    for name, collect in collectors.items():
        try:
            value = collect()
            if name == "cpu":
                metrics[name] = {"percent": _clamp_percent(value)}
            else:
                metrics[name] = {
                    "used_bytes": max(0, int(value["used_bytes"])),
                    "total_bytes": max(0, int(value["total_bytes"])),
                    "percent": _clamp_percent(value["percent"]),
                }
        except Exception as exc:
            errors.append(_safe_error(name, exc))

    try:
        runtime = _webui_runtime_payload()
    except Exception as exc:
        runtime = _zero_webui_runtime_payload()
        errors.append(_safe_error("webui_runtime", exc))

    available = any(metrics[name] is not None for name in metrics)
    status = "ok" if available and not errors else "partial" if available else "unavailable"
    return {
        "status": status,
        "available": available,
        "checked_at": _checked_at(),
        "cpu": metrics["cpu"],
        "memory": metrics["memory"],
        "disk": metrics["disk"],
        "webui_runtime": runtime,
        "errors": errors,
    }
