"""Read-only bridge to Hermes Agent's per-profile projects database."""

from __future__ import annotations

import importlib
import inspect
import logging
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from api import profiles


logger = logging.getLogger(__name__)

_SQLITE_TIMEOUT_SECONDS = 1.0


def _requested_profile(profile_name: str | None) -> str | None:
    name = profiles.get_active_profile_name() if profile_name is None else profile_name
    if not isinstance(name, str) or not profiles._PROFILE_ID_RE.fullmatch(name):
        return None
    if profiles._is_isolated_profile_mode() and name != profiles._isolated_profile_name():
        return None
    return name


def _profile_home(profile_name: str) -> Path | None:
    try:
        home = Path(profiles.get_hermes_home_for_profile(profile_name)).expanduser()
    except (OSError, ValueError):
        return None

    if profiles._is_root_profile(profile_name) or profiles._is_isolated_profile_mode():
        try:
            return home if home.is_dir() else None
        except (OSError, ValueError):
            return None

    try:
        profiles_root = profiles._profiles_root()
        lexical_home = profiles_root / profile_name
        resolved_home = home.resolve(strict=True)
        if (
            lexical_home.resolve(strict=True) != resolved_home
            or not resolved_home.is_relative_to(profiles_root)
            or not resolved_home.is_dir()
        ):
            return None
        return resolved_home
    except (OSError, ValueError):
        return None


def _validated_db_path(home: Path) -> Path | None:
    """Return an existing database path contained by its profile home.

    The local boundary is crafted profile paths, not same-UID filesystem races:
    reject a symlinked leaf and require its resolved target to remain in the
    resolved profile home before opening it.
    """
    db_path = home / "projects.db"
    try:
        if db_path.is_symlink():
            return None
        resolved_home = home.resolve(strict=True)
        resolved_db = db_path.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not resolved_db.is_file() or not resolved_db.is_relative_to(resolved_home):
        return None
    return resolved_db


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=_SQLITE_TIMEOUT_SECONDS,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
    except Exception:
        conn.close()
        raise
    return conn


def _supports_include_archived(function: Any) -> bool:
    parameters = inspect.signature(function).parameters
    include_archived = parameters.get("include_archived")
    supports_keyword = include_archived is not None and include_archived.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return supports_keyword or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _folder_dict(folder: Any) -> dict:
    to_dict = getattr(folder, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if not isinstance(value, dict):
            raise TypeError("project folder to_dict() must return a dict")
        return dict(value)
    if isinstance(folder, dict):
        return dict(folder)
    raise TypeError("incompatible project folder DTO")


def _project_dict(project: Any, profile_name: str) -> dict:
    project_id = getattr(project, "id", None)
    slug = getattr(project, "slug", None)
    name = getattr(project, "name", None)
    if not project_id or not slug or not name:
        raise TypeError("native project is missing required identity fields")
    return {
        "project_id": project_id,
        "native_project_id": project_id,
        "slug": slug,
        "name": name,
        "description": getattr(project, "description", None),
        "icon": getattr(project, "icon", None),
        "color": getattr(project, "color", None),
        "board_slug": getattr(project, "board_slug", None),
        "primary_path": getattr(project, "primary_path", None),
        "folders": [_folder_dict(folder) for folder in getattr(project, "folders", [])],
        "profile": profile_name,
        "created_at": getattr(project, "created_at", None),
        "archived": bool(getattr(project, "archived", False)),
        "project_source": "hermes-agent",
        "read_only": True,
    }


def load_native_projects(profile_name: str | None = None) -> list[dict] | None:
    """Return active native projects without creating or migrating their store."""
    try:
        resolved_profile = _requested_profile(profile_name)
        if resolved_profile is None:
            return None
        home = _profile_home(resolved_profile)
        if home is None:
            return None
        db_path = _validated_db_path(home)
        if db_path is None:
            return None
        projects_db = importlib.import_module("hermes_cli.projects_db")
        with closing(_open_read_only(db_path)) as conn:
            if _supports_include_archived(projects_db.list_projects):
                projects = projects_db.list_projects(conn, include_archived=False)
            else:
                projects = projects_db.list_projects(conn)
            return [_project_dict(project, resolved_profile) for project in projects]
    except (ImportError, sqlite3.Error, OSError) as exc:
        logger.debug(
            "Native projects backend unavailable (%s)",
            type(exc).__name__,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Failed to load native projects (%s)",
            type(exc).__name__,
        )
        return None


def native_project_ids_for_paths(
    paths: Iterable[str | None], profile_name: str | None = None
) -> dict[str, str] | None:
    """Resolve paths to native project ids through the upstream matcher."""
    try:
        resolved_profile = _requested_profile(profile_name)
        if resolved_profile is None:
            return None
        home = _profile_home(resolved_profile)
        if home is None:
            return None
        db_path = _validated_db_path(home)
        if db_path is None:
            return None
        projects_db = importlib.import_module("hermes_cli.projects_db")
        distinct_paths = list(
            dict.fromkeys(path for path in paths if isinstance(path, str) and path.strip())
        )
        result: dict[str, str] = {}
        project_for_path = projects_db.project_for_path
        supports_include_archived = _supports_include_archived(project_for_path)
        with closing(_open_read_only(db_path)) as conn:
            for path in distinct_paths:
                if supports_include_archived:
                    project = project_for_path(conn, path, include_archived=False)
                else:
                    project = project_for_path(conn, path)
                if project is not None:
                    project_id = getattr(project, "id", None)
                    if not project_id:
                        raise TypeError("native project is missing its id")
                    result[path] = project_id
        return result
    except (ImportError, sqlite3.Error, OSError) as exc:
        logger.debug(
            "Native project path backend unavailable (%s)",
            type(exc).__name__,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Failed to resolve native project paths (%s)",
            type(exc).__name__,
        )
        return None
