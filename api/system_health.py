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


_PROC_NET_DEV = Path("/proc/net/dev")
# Cross-request window for throughput deltas. Kept tiny and module-local so the
# endpoint stays stateless per request but can still report a bytes/sec rate.
_NET_SAMPLE: dict[str, Any] = {"rx": None, "tx": None, "ts": None}


def _read_proc_net_dev() -> tuple[int, int]:
    """Sum (rx_bytes, tx_bytes) over physical interfaces, excluding loopback."""
    rx_total = 0
    tx_total = 0
    try:
        with _PROC_NET_DEV.open("r", encoding="utf-8") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                name, _, rest = line.partition(":")
                if name.strip() == "lo":
                    continue
                parts = rest.split()
                if len(parts) < 9:
                    continue
                try:
                    rx_total += int(parts[0])
                    tx_total += int(parts[8])
                except ValueError:
                    continue
    except OSError:
        return 0, 0
    return rx_total, tx_total


def _network_throughput() -> dict[str, float]:
    """Sample aggregate network throughput (bytes/sec).

    Stateless over requests: keeps the previous total byte count + timestamp in
    a small module cache and computes a delta. The first call returns zeros to
    avoid a bogus spike. No per-interface breakdown or peer data leaves the server.
    """
    rx_now, tx_now = _read_proc_net_dev()
    now = time.monotonic()
    prev = _NET_SAMPLE
    if prev["ts"] is None:
        prev["rx"], prev["tx"], prev["ts"] = rx_now, tx_now, now
        return {
            "rx_bytes_per_s": 0.0,
            "tx_bytes_per_s": 0.0,
            "rx_total_bytes": rx_now,
            "tx_total_bytes": tx_now,
        }
    elapsed = now - prev["ts"]
    if elapsed <= 0:
        return {
            "rx_bytes_per_s": 0.0,
            "tx_bytes_per_s": 0.0,
            "rx_total_bytes": rx_now,
            "tx_total_bytes": tx_now,
        }
    rx_rate = max(0.0, (rx_now - prev["rx"]) / elapsed)
    tx_rate = max(0.0, (tx_now - prev["tx"]) / elapsed)
    prev["rx"], prev["tx"], prev["ts"] = rx_now, tx_now, now
    return {
        "rx_bytes_per_s": round(rx_rate, 1),
        "tx_bytes_per_s": round(tx_rate, 1),
        "rx_total_bytes": rx_now,
        "tx_total_bytes": tx_now,
    }


def build_system_health_payload() -> dict[str, Any]:
    metrics: dict[str, Any] = {"cpu": None, "memory": None, "disk": None, "network": None}
    errors: list[dict[str, str]] = []

    collectors = {
        "cpu": _cpu_percent,
        "memory": _memory_usage,
        "disk": _disk_usage,
        "network": _network_throughput,
    }
    for name, collect in collectors.items():
        try:
            value = collect()
            if name == "cpu":
                metrics[name] = {"percent": _clamp_percent(value)}
            elif name == "network":
                metrics[name] = {
                    "rx_bytes_per_s": float(value["rx_bytes_per_s"]),
                    "tx_bytes_per_s": float(value["tx_bytes_per_s"]),
                    "rx_total_bytes": int(value["rx_total_bytes"]),
                    "tx_total_bytes": int(value["tx_total_bytes"]),
                }
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
        "network": metrics["network"],
        "errors": errors,
    }
