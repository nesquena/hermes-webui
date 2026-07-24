"""Authoritative artifact descriptors for Final Answer file links."""

from __future__ import annotations

import json
from pathlib import Path


CANONICAL_MUTATION_TOOLS = frozenset({"write_file", "patch"})


def normalize_tool_name(value) -> str:
    return str(value or "").strip().removeprefix("functions.")


def parse_tool_result(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def tool_result_is_error(value) -> bool:
    result = parse_tool_result(value)
    return bool(result and (result.get("error") or result.get("success") is False))


def _workspace_relative_path(candidate, workspace_root: str) -> str | None:
    if not isinstance(candidate, str) or not candidate.strip() or not workspace_root:
        return None
    try:
        root = Path(workspace_root).expanduser().resolve()
        raw = Path(candidate).expanduser()
        # Replay has no persisted descriptor to bind a relative result to the
        # workspace that actually received the write. Require the tool's
        # absolute landed path instead of rebinding it to the current root.
        if not raw.is_absolute():
            return None
        target = raw.resolve()
        return target.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def landed_artifact_descriptors(
    tool_name,
    result_value,
    *,
    workspace_root: str,
    tool_call_id,
    session_id=None,
) -> list[dict]:
    """Return fail-closed descriptors only for canonical successful mutations."""
    name = normalize_tool_name(tool_name)
    result = parse_tool_result(result_value)
    call_id = str(tool_call_id or "").strip()
    root = str(workspace_root or "").strip()
    if name not in CANONICAL_MUTATION_TOOLS or not result or not call_id or not root:
        return []
    if result.get("error") or result.get("success") is False:
        return []

    candidates = []
    if name == "write_file":
        if "bytes_written" not in result:
            return []
        if type(result["bytes_written"]) is not int or result["bytes_written"] < 0:
            return []
        candidates = [result.get("resolved_path")]
    elif name == "patch":
        if result.get("success") is not True:
            return []
        modified = result.get("files_modified")
        created = result.get("files_created")
        candidates = (modified if isinstance(modified, list) else []) + (
            created if isinstance(created, list) else []
        )

    descriptors = []
    seen = set()
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("path")
        path = _workspace_relative_path(candidate, root)
        if not path or path in seen:
            continue
        seen.add(path)
        descriptor = {
            "path": path,
            "workspace_root": str(Path(root).expanduser().resolve()),
            "tool_call_id": call_id,
            "tool_name": name,
        }
        if str(session_id or "").strip():
            descriptor["session_id"] = str(session_id).strip()
        descriptors.append(descriptor)
    return descriptors
