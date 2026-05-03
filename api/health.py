"""Neo health endpoints for dashboard chrome."""

import os
import shutil
import time
from pathlib import Path

from api.updates import WEBUI_VERSION

_CPU_SAMPLE: tuple[int, int] | None = None
_NET_SAMPLE: tuple[float, int] | None = None


def _uptime_seconds() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return int(time.monotonic())


def _format_uptime(seconds: int) -> str:
    days, rem = divmod(max(0, int(seconds)), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}h {minutes:02d}m"


def _memory_percent() -> int:
    try:
        data = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, val = line.split(":", 1)
            data[key] = int(val.strip().split()[0])
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", 0)
        if total > 0:
            return round((total - available) * 100 / total)
    except Exception:
        pass
    return 0


def _disk_percent() -> int:
    try:
        usage = shutil.disk_usage(Path.home())
        return round(usage.used * 100 / usage.total)
    except Exception:
        return 0


def _read_cpu_totals() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()
        values = [int(v) for v in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return idle, total
    except Exception:
        return None


def _cpu_percent() -> int:
    global _CPU_SAMPLE
    sample = _read_cpu_totals()
    if sample:
        previous = _CPU_SAMPLE
        _CPU_SAMPLE = sample
        if previous:
            idle_delta = sample[0] - previous[0]
            total_delta = sample[1] - previous[1]
            if total_delta > 0:
                return max(0, min(100, round((1 - idle_delta / total_delta) * 100)))
    try:
        load = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        return max(0, min(100, round((load / cpus) * 100)))
    except Exception:
        return 0


def _network_total_bytes() -> int | None:
    try:
        total = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            parts = data.split()
            total += int(parts[0]) + int(parts[8])
        return total
    except Exception:
        return None


def _network_percent() -> int:
    global _NET_SAMPLE
    total = _network_total_bytes()
    now = time.monotonic()
    if total is None:
        return 0
    previous = _NET_SAMPLE
    _NET_SAMPLE = (now, total)
    if not previous:
        return 0
    elapsed = max(0.001, now - previous[0])
    bytes_per_second = max(0, total - previous[1]) / elapsed
    # Normalize against 10 MiB/s as a conservative VPS activity scale.
    return max(0, min(100, round(bytes_per_second * 100 / (10 * 1024 * 1024))))


def _metric(metric_id: str, label: str, value: int, source: str) -> dict:
    return {
        "id": metric_id,
        "label": label,
        "value": max(0, min(100, int(value))),
        "source": source,
    }


def build_system_health() -> dict:
    region = os.environ.get("HERMES_WEBUI_REGION") or os.environ.get("NEO_REGION") or "São Paulo / BR"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
    uptime = _uptime_seconds()
    return {
        "status": "online",
        "status_label": "ONLINE",
        "user": user.title() if user else "Root",
        "uptime": _format_uptime(uptime),
        "uptime_seconds": uptime,
        "region": region,
        "version": os.environ.get("NEO_WEBUI_VERSION") or f"{WEBUI_VERSION}-neo",
    }


def build_vps_health() -> dict:
    return {
        "metrics": [
            _metric("cpu", "CPU", _cpu_percent(), "procfs"),
            _metric("ram", "RAM", _memory_percent(), "procfs"),
            _metric("disk", "DISCO", _disk_percent(), "filesystem"),
            _metric("network", "REDE", _network_percent(), "procfs"),
        ]
    }
