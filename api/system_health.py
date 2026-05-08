"""Safe aggregate host resource metrics for the WebUI VPS panel (#693).

The browser only needs coarse CPU/RAM/disk usage. Keep this module intentionally
small and dependency-free: no process lists, command strings, user identities,
environment variables, or filesystem topology leave the server.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROC_STAT = Path("/proc/stat")
_PROC_MEMINFO = Path("/proc/meminfo")
_CPU_SAMPLE_SECONDS = 0.05


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
    """Sample aggregate CPU usage without psutil.

    A short local sample avoids storing cross-request state and returns a stable
    percentage on the first poll. Unsupported platforms raise a safe error code.
    """
    start = _read_proc_stat_cpu()
    time.sleep(_CPU_SAMPLE_SECONDS)
    end = _read_proc_stat_cpu()
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
    meminfo = _read_meminfo_kib()
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

    available = any(metrics[name] is not None for name in metrics)
    status = "ok" if available and not errors else "partial" if available else "unavailable"
    return {
        "status": status,
        "available": available,
        "checked_at": _checked_at(),
        "cpu": metrics["cpu"],
        "memory": metrics["memory"],
        "disk": metrics["disk"],
        "errors": errors,
    }
