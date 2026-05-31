"""Read-only operator truth payloads for the composer proof strip.

This module is intentionally conservative: every chip is backed by a source read
performed during the request, and source failures degrade to ``unknown`` or
``stale`` instead of pretending the operator view is healthy.
"""

from __future__ import annotations

import importlib
import os
import re
import time
from pathlib import Path
from typing import Any

STATE_ORDER = {"live": 0, "unknown": 1, "stale": 2}
_OPERATOR_TTL_SECONDS = 30
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_INCIDENT_WORKSPACE = "/mnt/c/Users/malac/.openclaw/workspace/main"


def build_operator_truth_payload(
    *,
    session_id: str | None = None,
    ui_board_hint: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the versioned Operator Truth Strip payload.

    The function performs only cheap, read-only probes. A broken source affects
    its own chip and the overall status; it should not make the whole endpoint
    return 500.
    """

    checked_at = float(time.time() if now is None else now)
    sources: list[dict[str, Any]] = []
    issues: list[str] = []

    workspace_chip, workspace_info, workspace_sources = _workspace_chip(session_id, checked_at)
    sources.extend(workspace_sources)

    profile_chip, profile_sources = _profile_chip(checked_at)
    sources.extend(profile_sources)

    state_chip, state_sources = _webui_state_chip(checked_at)
    sources.extend(state_sources)

    kanban_chip, board_info, kanban_sources = _kanban_board_chip(ui_board_hint, checked_at)
    sources.extend(kanban_sources)

    scratch_chip, scratch_sources = _scratch_safety_chip(board_info, workspace_info, checked_at)
    sources.extend(scratch_sources)

    chips = [workspace_chip, profile_chip, state_chip, kanban_chip, scratch_chip]
    source_chip = _source_truth_files_chip(sources, checked_at)
    chips.append(source_chip)

    for chip in chips:
        issues.extend(str(issue) for issue in chip.get("issues", []) if issue)

    status = _worst_state(chips)
    return {
        "version": 1,
        "verified_at": checked_at,
        "status": status,
        "ttl_seconds": _OPERATOR_TTL_SECONDS,
        "summary": _summary_for_status(status),
        "chips": chips,
        "sources": sources,
        "issues": issues,
    }


def _chip(
    chip_id: str,
    label: str,
    state: str,
    *,
    value: str = "",
    display_path: str = "",
    source: dict[str, Any] | None = None,
    checked_at: float,
    issues: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": chip_id,
        "label": label,
        "state": state if state in STATE_ORDER else "unknown",
        "value": value,
        "source": source or {"kind": "unknown"},
        "checked_at": checked_at,
        "issues": list(issues or []),
    }
    if display_path:
        payload["display_path"] = display_path
    payload.update(extra)
    return payload


def _source_stat(
    source_id: str,
    path: Any,
    *,
    kind: str,
    api: str | None = None,
    symbol: str | None = None,
    required: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": source_id,
        "kind": kind,
        "path": _safe_display_path(path),
        "exists": False,
        "required": bool(required),
    }
    if api:
        item["api"] = api
    if symbol:
        item["symbol"] = symbol
    if not path:
        item["issue"] = "path unavailable"
        return item
    try:
        p = Path(path)
        item["exists"] = p.exists()
        if item["exists"]:
            item["mtime"] = p.stat().st_mtime
        elif required:
            item["issue"] = "missing"
    except Exception as exc:  # pragma: no cover - platform/path edge cases
        item["issue"] = f"unreadable: {_short_error(exc)}"
    return item


def _workspace_chip(session_id: str | None, checked_at: float) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    source: dict[str, Any] = {"kind": "unknown"}
    workspace_path: str | None = None
    issues: list[str] = []
    state = "unknown"

    if session_id:
        if not _valid_session_id(session_id):
            issues.append("invalid session_id")
            source = {"kind": "session", "api": "/api/session"}
        else:
            try:
                models = importlib.import_module("api.models")
                try:
                    session = models.get_session(session_id, metadata_only=True)
                except TypeError:
                    session = models.get_session(session_id)
                workspace_path = _field(session, "workspace")
                session_path = _field(session, "path") or _session_json_path(session_id)
                sources.append(_source_stat("session", session_path, kind="session", api="/api/session"))
                source = {"kind": "session", "api": "/api/session", "path": _safe_display_path(session_path)}
            except Exception as exc:
                issues.append(f"session source unreadable: {_short_error(exc)}")
                source = {"kind": "session", "api": "/api/session"}

    if not workspace_path and not issues:
        try:
            workspace = importlib.import_module("api.workspace")
            workspace_path = workspace.get_last_workspace()
            last_workspace_file = getattr(workspace, "_last_workspace_file", lambda: None)()
            workspaces_file = getattr(workspace, "_workspaces_file", lambda: None)()
            if last_workspace_file:
                sources.append(_source_stat("last_workspace", last_workspace_file, kind="webui_state"))
            if workspaces_file:
                sources.append(_source_stat("workspaces", workspaces_file, kind="webui_state"))
            source = {"kind": "last_workspace", "path": _safe_display_path(last_workspace_file)}
        except Exception as exc:
            issues.append(f"workspace source unreadable: {_short_error(exc)}")

    if workspace_path:
        path = Path(str(workspace_path)).expanduser()
        display_path = _safe_display_path(path)
        value = path.name or display_path or "workspace"
        if path.is_dir():
            state = "live"
            if _same_path(path, _INCIDENT_WORKSPACE):
                issues.append("workspace/main is a recovered partial workspace; do not use it as scratch")
                state = "stale"
        else:
            state = "stale"
            issues.append("workspace path missing or inaccessible")
        info = {"path": str(path), "display_path": display_path, "state": state}
        return (
            _chip(
                "workspace",
                "Workspace",
                state,
                value=value,
                display_path=display_path,
                source=source,
                checked_at=checked_at,
                issues=issues,
            ),
            info,
            sources,
        )

    info = {"path": None, "display_path": "", "state": "unknown"}
    return (
        _chip("workspace", "Workspace", "unknown", value="unknown", source=source, checked_at=checked_at, issues=issues or ["workspace unknown"]),
        info,
        sources,
    )


def _profile_chip(checked_at: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        profiles = importlib.import_module("api.profiles")
        name = profiles.get_active_profile_name() or "default"
        hermes_home = profiles.get_active_hermes_home()
        source = {"kind": "active_profile", "api": "/api/profile/active"}
        home_exists = bool(hermes_home and Path(hermes_home).exists())
        state = "live" if name and home_exists else "unknown"
        issues = [] if state == "live" else ["Hermes home path missing"]
        return (
            _chip(
                "profile",
                "Profile",
                state,
                value=str(name) if name else "unknown",
                display_path=_safe_display_path(hermes_home),
                source=source,
                checked_at=checked_at,
                issues=issues,
            ),
            [_source_stat("hermes_home", hermes_home, kind="active_profile", api="/api/profile/active")],
        )
    except Exception as exc:
        return (
            _chip(
                "profile",
                "Profile",
                "unknown",
                value="unknown",
                source={"kind": "active_profile", "api": "/api/profile/active"},
                checked_at=checked_at,
                issues=[f"profile source unreadable: {_short_error(exc)}"],
            ),
            [],
        )


def _webui_state_chip(checked_at: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    try:
        config = importlib.import_module("api.config")
        state_dir = Path(config.STATE_DIR)
        session_dir = Path(getattr(config, "SESSION_DIR", state_dir / "sessions"))
        sources.append(_source_stat("webui_state", state_dir, kind="config", symbol="api.config.STATE_DIR"))
        sources.append(_source_stat("sessions", session_dir, kind="config", symbol="api.config.SESSION_DIR"))
        try:
            workspace = importlib.import_module("api.workspace")
            workspaces_file = getattr(workspace, "_workspaces_file", lambda: state_dir / "workspaces.json")()
            last_workspace_file = getattr(workspace, "_last_workspace_file", lambda: state_dir / "last_workspace.txt")()
        except Exception:
            workspaces_file = state_dir / "workspaces.json"
            last_workspace_file = state_dir / "last_workspace.txt"
        for source_id, path, required in (
            ("workspaces", workspaces_file, True),
            ("last_workspace", last_workspace_file, True),
            ("settings", state_dir / "settings.json", False),
            ("projects", state_dir / "projects.json", False),
        ):
            sources.append(_source_stat(source_id, path, kind="webui_state", required=required))
        state = "live" if state_dir.exists() else "unknown"
        issues = [] if state == "live" else ["WebUI state directory missing"]
        return (
            _chip(
                "webui_state",
                "WebUI state",
                state,
                value=state_dir.name or "webui",
                display_path=_safe_display_path(state_dir),
                source={"kind": "config", "symbol": "api.config.STATE_DIR"},
                checked_at=checked_at,
                issues=issues,
            ),
            sources,
        )
    except Exception as exc:
        return (
            _chip(
                "webui_state",
                "WebUI state",
                "unknown",
                value="unknown",
                source={"kind": "config", "symbol": "api.config.STATE_DIR"},
                checked_at=checked_at,
                issues=[f"WebUI state source unreadable: {_short_error(exc)}"],
            ),
            sources,
        )


def _kanban_board_chip(ui_board_hint: str | None, checked_at: float) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    try:
        kb = importlib.import_module("hermes_cli.kanban_db")
    except Exception as exc:
        issue = _short_error(exc)
        return (
            _chip(
                "kanban_board",
                "Board",
                "unknown",
                value="unknown",
                source={"kind": "kanban_db"},
                checked_at=checked_at,
                issues=[issue],
            ),
            {"available": False, "error": issue},
            sources,
        )

    current_path = _call_path(kb, "current_board_path")
    if current_path:
        sources.append(_source_stat("kanban_current", current_path, kind="kanban_current", required=False))

    board = "default"
    source_kind = "default"
    state = "live"
    issues: list[str] = []

    env_board = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if env_board:
        board = env_board
        source_kind = "env:HERMES_KANBAN_BOARD"
        if not _safe_board_exists(kb, board):
            state = "stale"
            issues.append("HERMES_KANBAN_BOARD points to a missing board")
    elif current_path and Path(current_path).exists():
        try:
            raw = Path(current_path).read_text(encoding="utf-8").strip()
        except Exception as exc:
            raw = ""
            state = "unknown"
            issues.append(f"current board file unreadable: {_short_error(exc)}")
        if raw:
            board = raw
            source_kind = "kanban_current"
            if not _safe_board_exists(kb, board):
                state = "stale"
                issues.append(f"current board {board!r} does not exist")
        elif state == "live":
            board = "default"
            source_kind = "default"
    else:
        board = "default"
        source_kind = "default"
        if not _safe_board_exists(kb, board):
            state = "unknown"
            issues.append("default board could not be proven")

    if ui_board_hint and ui_board_hint != board:
        state = "stale"
        issues.append("browser board hint differs from backend current")

    metadata_path = _call_path(kb, "board_metadata_path", board)
    db_path = _call_path(kb, "kanban_db_path", board)
    workspace_root = _call_path(kb, "workspaces_root", board)
    if metadata_path:
        sources.append(_source_stat("kanban_board_metadata", metadata_path, kind="kanban_board_metadata"))
    if db_path:
        sources.append(_source_stat("kanban_db", db_path, kind="kanban_db"))

    metadata: dict[str, Any] | None = None
    if state != "stale" or _safe_board_exists(kb, board):
        try:
            metadata = kb.read_board_metadata(board)
        except Exception as exc:
            metadata = None
            if state == "live":
                state = "unknown"
            issues.append(f"board metadata unreadable: {_short_error(exc)}")

    source = {
        "kind": source_kind,
        "path": _safe_display_path(current_path),
        "metadata_path": _safe_display_path(metadata_path),
        "db_path": _safe_display_path(db_path),
    }
    info = {
        "available": True,
        "kb": kb,
        "board": board,
        "state": state,
        "metadata": metadata,
        "metadata_path": metadata_path,
        "db_path": db_path,
        "workspace_root": workspace_root,
        "allowed_root": Path(current_path).parent if current_path else None,
    }
    return (
        _chip(
            "kanban_board",
            "Board",
            state,
            value=board,
            source=source,
            checked_at=checked_at,
            issues=issues,
        ),
        info,
        sources,
    )


def _scratch_safety_chip(
    board_info: dict[str, Any],
    workspace_info: dict[str, Any],
    checked_at: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    if not board_info.get("available"):
        return (
            _chip(
                "scratch_safety",
                "Scratch",
                "unknown",
                value="unknown",
                source={"kind": "kanban_board_metadata"},
                checked_at=checked_at,
                issues=[board_info.get("error") or "kanban source unavailable"],
            ),
            sources,
        )

    metadata = board_info.get("metadata") or {}
    default_workdir = metadata.get("default_workdir") or board_info.get("workspace_root")
    metadata_path = board_info.get("metadata_path")
    if metadata_path:
        sources.append(_source_stat("scratch_metadata", metadata_path, kind="kanban_board_metadata"))
    if not default_workdir:
        return (
            _chip(
                "scratch_safety",
                "Scratch",
                "unknown",
                value="unknown",
                source={"kind": "kanban_board_metadata", "path": _safe_display_path(metadata_path)},
                checked_at=checked_at,
                issues=["scratch default workdir unknown"],
            ),
            sources,
        )

    target = _safe_resolve(default_workdir)
    allowed_root = _safe_resolve(board_info.get("allowed_root"))
    active_workspace = workspace_info.get("path")
    source = {
        "kind": "kanban_board_metadata",
        "default_workdir": _safe_display_path(target),
        "allowed_root": _safe_display_path(allowed_root),
        "path": _safe_display_path(metadata_path),
    }

    issues: list[str] = []
    state = "live"
    value = "safe"
    if active_workspace and _path_contains(active_workspace, target):
        state = "stale"
        value = "risky"
        issues.append("scratch default_workdir points under the active workspace")
    elif _same_path(target, _INCIDENT_WORKSPACE):
        state = "stale"
        value = "risky"
        issues.append("scratch default_workdir points at recovered workspace/main")
    elif allowed_root and not _path_contains(allowed_root, target):
        state = "stale"
        value = "risky"
        issues.append("scratch default_workdir is outside Hermes-owned Kanban storage")
    elif not allowed_root:
        state = "unknown"
        value = "unknown"
        issues.append("Hermes Kanban storage root unknown")

    return (
        _chip(
            "scratch_safety",
            "Scratch",
            state,
            value=value,
            display_path=_safe_display_path(target),
            source=source,
            checked_at=checked_at,
            issues=issues,
        ),
        sources,
    )


def _source_truth_files_chip(sources: list[dict[str, Any]], checked_at: float) -> dict[str, Any]:
    checked = sum(1 for source in sources if source.get("exists"))
    unreadable = [source for source in sources if source.get("issue")]
    state = "live" if sources and not unreadable else "unknown"
    return _chip(
        "source_truth_files",
        "Sources",
        state,
        value=f"{checked} checked",
        source={"kind": "aggregate"},
        checked_at=checked_at,
        issues=[f"{len(unreadable)} source(s) unreadable"] if unreadable else [],
    )


def _worst_state(chips: list[dict[str, Any]]) -> str:
    worst = "live"
    for chip in chips:
        state = chip.get("state", "unknown")
        if STATE_ORDER.get(state, 1) > STATE_ORDER[worst]:
            worst = state
    return worst


def _summary_for_status(status: str) -> str:
    if status == "live":
        return "Truth live"
    if status == "stale":
        return "Truth stale"
    return "Truth unknown"


def _valid_session_id(session_id: str) -> bool:
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return False
    return bool(_SESSION_ID_RE.match(session_id))


def _session_json_path(session_id: str) -> Path | None:
    try:
        config = importlib.import_module("api.config")
        return Path(getattr(config, "SESSION_DIR")) / f"{session_id}.json"
    except Exception:
        return None


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_board_exists(kb: Any, board: str) -> bool:
    try:
        return bool(kb.board_exists(board))
    except Exception:
        return False


def _call_path(obj: Any, name: str, *args: Any) -> Path | None:
    try:
        value = getattr(obj, name)(*args)
        return Path(value) if value is not None else None
    except Exception:
        return None


def _safe_resolve(path: Any) -> Path | None:
    if path is None:
        return None
    try:
        return Path(path).expanduser().resolve(strict=False)
    except Exception:
        try:
            return Path(str(path)).expanduser()
        except Exception:
            return None


def _path_contains(root: Any, candidate: Any) -> bool:
    root_path = _safe_resolve(root)
    candidate_path = _safe_resolve(candidate)
    if not root_path or not candidate_path:
        return False
    try:
        candidate_path.relative_to(root_path)
        return True
    except ValueError:
        return False


def _same_path(left: Any, right: Any) -> bool:
    left_path = _safe_resolve(left)
    right_path = _safe_resolve(right)
    return bool(left_path and right_path and left_path == right_path)


def _safe_display_path(path: Any) -> str:
    if not path:
        return ""
    text = str(path)
    try:
        home = str(Path.home())
        if text == home:
            text = "~"
        elif text.startswith(home + os.sep):
            text = "~" + text[len(home) :]
    except Exception:
        pass
    if len(text) <= 88:
        return text
    parts = Path(text).parts
    if len(parts) >= 3:
        return "…/" + "/".join(parts[-3:])
    return "…" + text[-85:]


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__
