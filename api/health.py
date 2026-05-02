"""Neo health endpoints for dashboard chrome."""

import os
import shutil
import time
from pathlib import Path

from api.updates import WEBUI_VERSION


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


def _load_percent() -> int:
    try:
        load = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        return max(0, min(100, round((load / cpus) * 100)))
    except Exception:
        return 0


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
            {"id": "cpu", "label": "CPU", "value": _load_percent()},
            {"id": "ram", "label": "RAM", "value": _memory_percent()},
            {"id": "disk", "label": "DISCO", "value": _disk_percent()},
            {"id": "network", "label": "REDE", "value": 12},
        ]
    }
