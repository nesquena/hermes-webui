"""Read-only adapter from hermes_cli.projects_db into WebUI project dicts."""
from __future__ import annotations

import importlib
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _active_profile_name(profile_name: str | None = None) -> str:
    if profile_name:
        return str(profile_name).strip() or "default"
    try:
        from api.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _project_to_webui_dict(project, profile_name: str) -> dict:
    row = {
        "project_id": project.slug,
        "name": project.name,
        "color": project.color,
        "profile": profile_name,
    }
    created_at = getattr(project, "created_at", None)
    if created_at is not None:
        row["created_at"] = created_at
    primary_path = getattr(project, "primary_path", None)
    if primary_path is not None:
        row["primary_path"] = primary_path
    folders = getattr(project, "folders", None)
    if folders is not None:
        row["folders"] = [
            folder.to_dict() if hasattr(folder, "to_dict") else folder
            for folder in folders
        ]
    return row


def load_projects_from_db(*, profile_name: str | None = None) -> list[dict] | None:
    try:
        projects_db = importlib.import_module("hermes_cli.projects_db")
    except Exception:
        return None

    try:
        from api.profiles import get_hermes_home_for_profile

        profile = _active_profile_name(profile_name)
        db_path = Path(get_hermes_home_for_profile(profile)) / "projects.db"
    except Exception:
        return None

    if not db_path or not Path(db_path).exists():
        return None

    resolved_db_path = Path(db_path).resolve()
    wal_path = resolved_db_path.with_name(f"{resolved_db_path.name}-wal")
    shm_path = resolved_db_path.with_name(f"{resolved_db_path.name}-shm")

    def _read_projects(database_path: Path, *, immutable: bool) -> list[dict]:
        conn = None
        query = "mode=ro"
        if immutable:
            query += "&immutable=1"
        db_uri = f"{database_path.as_uri()}?{query}"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = []
            for project in projects_db.list_projects(conn):
                if getattr(project, "archived", False):
                    continue
                rows.append(_project_to_webui_dict(project, profile))
            return rows
        finally:
            try:
                conn.close()
            except Exception:
                logger.debug("Failed to close projects_db connection", exc_info=True)

    def _sidecar_state() -> tuple[bool, bool]:
        return wal_path.exists(), shm_path.exists()

    def _read_partial_snapshot() -> list[dict] | None:
        wal_exists, shm_exists = _sidecar_state()
        if not wal_exists and shm_exists:
            return None
        with tempfile.TemporaryDirectory(prefix="hermes-projects-db-") as temp_dir:
            snapshot_db = Path(temp_dir) / resolved_db_path.name
            shutil.copy2(resolved_db_path, snapshot_db)
            if wal_exists:
                shutil.copy2(wal_path, snapshot_db.with_name(f"{snapshot_db.name}-wal"))
            if shm_exists:
                shutil.copy2(shm_path, snapshot_db.with_name(f"{snapshot_db.name}-shm"))
            return _read_projects(snapshot_db, immutable=False)

    def _read_matrix() -> list[dict] | None:
        wal_exists, shm_exists = _sidecar_state()
        if wal_exists and shm_exists:
            return _read_projects(resolved_db_path, immutable=False)
        if wal_exists:
            return _read_partial_snapshot()
        if shm_exists:
            return None
        return _read_projects(resolved_db_path, immutable=True)

    initial_state = _sidecar_state()
    try:
        return _read_matrix()
    except Exception:
        if _sidecar_state() != initial_state:
            try:
                return _read_matrix()
            except Exception:
                pass
        return None
