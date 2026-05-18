"""Metadata-only Capy structured progress event status.

This module exposes a bounded local taxonomy for future progress/event streams.
It intentionally returns only aggregate/status metadata: no raw prompts, command
bodies, generated widget bodies, renderer/source/html/script fields, API-auth
fields, or secret-looking values are read or echoed.
"""
from __future__ import annotations

from typing import Any

_EVENT_FAMILIES = [
    "run",
    "tool",
    "subagent",
    "taskboard",
    "memory.ingest",
    "space.visual_qa",
]

_SUPPORTED_EVENT_TYPES = [
    "run.started",
    "run.completed",
    "run.failed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "subagent.started",
    "subagent.completed",
    "subagent.failed",
    "taskboard.updated",
    "memory.ingest.started",
    "memory.ingest.completed",
    "memory.ingest.failed",
    "space.visual_qa.started",
    "space.visual_qa.completed",
    "space.visual_qa.failed",
]


def progress_status() -> dict[str, Any]:
    """Return local-only progress event capability/status metadata."""
    return {
        "available": True,
        "local_only": True,
        "metadata_only": True,
        "status": "ready",
        "active_run_count": 0,
        "recent_event_count": 0,
        "event_families": list(_EVENT_FAMILIES),
        "supported_event_types": list(_SUPPORTED_EVENT_TYPES),
        "redaction_status": "metadata_only",
    }
