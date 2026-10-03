"""Read-only bridge from the Hermes Agent Projects store (projects.db).

Implements the read-path slice of issue #5763: the WebUI workspace list
surfaces the profile's authoritative ``projects.db`` (the same SQLite store
backing Hermes Desktop / CLI ``hermes project list``) instead of requiring a
manually duplicated picker list.

Design (per the maintainer's recommended first slice):
- In-process read only. No writes ever touch ``projects.db``; the WebUI keeps
  owning ``workspaces.json`` for its own additions.
- Fail-safe by contract: a missing DB, missing tables, lock contention, or any
  other error yields an empty list — the workspace picker then behaves exactly
  as it did before this bridge existed (``workspaces.json`` fallback).
- Results are cached per DB path and invalidated on file mtime change, so a
  project created via Desktop/CLI appears on the next ``/api/workspaces``
  poll without a WebUI restart.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

# path -> (db_mtime, [(path, name), ...])
_cache: dict[str, tuple[float | None, list[tuple[str, str]]]] = {}
_cache_lock = threading.Lock()

# Env kill-switch: set HERMES_WEBUI_PROJECTS_DB_SYNC=0 to disable the bridge.
_DISABLE_ENV = "HERMES_WEBUI_PROJECTS_DB_SYNC"


def _sync_enabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


def _projects_db_path(profile_home: Path | None) -> Path | None:
    """Resolve ``projects.db`` for a profile home (None = ambient active profile)."""
    try:
        if profile_home is not None:
            home = Path(profile_home)
        else:
            from api.profiles import get_active_hermes_home
            home = get_active_hermes_home()
    except Exception:
        return None
    db = home / "projects.db"
    return db if db.is_file() else None


def _query_projects(db: Path) -> list[tuple[str, str]]:
    """Return (path, name) for each non-archived project with a usable folder.

    A project's workspace path is its primary folder when set, otherwise any
    attached folder (lowest ``added_at`` for determinism). Projects with no
    folders at all are skipped: a workspace entry needs a real directory.
    """
    # uri=ro + busy_timeout: never create a DB, never block on a writer for long.
    # quote() the path: '#' would become a URI fragment and '?' would split
    # params early, silently disabling the bridge for such profile homes.
    uri = "file:" + urllib.parse.quote(db.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.name AS name,
                   COALESCE(
                       (SELECT pf.path FROM project_folders pf
                         WHERE pf.project_id = p.id AND pf.is_primary = 1
                         LIMIT 1),
                       (SELECT pf.path FROM project_folders pf
                         WHERE pf.project_id = p.id
                         ORDER BY pf.added_at ASC LIMIT 1)
                   ) AS path
            FROM projects p
            WHERE COALESCE(p.archived, 0) = 0
            ORDER BY p.created_at ASC
            """
        ).fetchall()
    except sqlite3.Error:
        # Missing tables / older schema — treat as "no opinion".
        return []
    finally:
        conn.close()

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        path = (r["path"] or "").strip()
        name = (r["name"] or "").strip()
        if not path or not name or path in seen:
            continue
        seen.add(path)
        out.append((path, name))
    return out


def load_hermes_project_workspaces(profile_home: Path | None = None) -> list[dict]:
    """Return workspace-shaped entries ``{'path','name','source'}`` from projects.db.

    Never raises: any failure returns ``[]`` so callers fall back to the
    WebUI-local workspace list unchanged.
    """
    if not _sync_enabled():
        return []
    try:
        db = _projects_db_path(profile_home)
        if db is None:
            return []
        key = str(db)
        try:
            mtime = db.stat().st_mtime
            # Production DBs are WAL (upstream open_db sets journal_mode=wal):
            # while a long-lived external writer holds the DB open, commits land
            # in the -wal sidecar and the main file's mtime stays stale until a
            # checkpoint. Fold the WAL mtime into the cache key so external
            # creates/archives invalidate the cache promptly.
            wal = db.with_name(db.name + "-wal")
            try:
                mtime = max(mtime, wal.stat().st_mtime)
            except OSError:
                pass
        except OSError:
            return []
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None and cached[0] == mtime:
                entries = cached[1]
            else:
                entries = _query_projects(db)
                _cache[key] = (mtime, entries)
        return [{"path": p, "name": n, "source": "hermes_project"} for p, n in entries]
    except Exception:
        logger.debug("projects.db bridge failed; falling back to local workspaces", exc_info=True)
        return []


def merge_hermes_projects(workspaces: list[dict], profile_home: Path | None = None) -> list[dict]:
    """Merge projects.db entries into a WebUI workspace list.

    - projects.db is authoritative for a path it owns: a local entry with the
      same path takes the project's display name.
    - Local-only entries keep their order and names (WebUI additions still work).
    - DB projects not present locally are appended.
    Never mutates the input list.
    """
    db_entries = load_hermes_project_workspaces(profile_home=profile_home)
    if not db_entries:
        return list(workspaces)
    db_by_path = {e["path"]: e for e in db_entries}
    merged: list[dict] = []
    used: set[str] = set()
    for w in workspaces:
        entry = dict(w)
        hit = db_by_path.get(entry.get("path", ""))
        if hit is not None:
            entry["name"] = hit["name"]
            entry["source"] = "hermes_project"
            used.add(hit["path"])
        merged.append(entry)
    for e in db_entries:
        if e["path"] not in used:
            merged.append(dict(e))
    return merged


# ── Write path: register a WebUI workspace as a Hermes Project ─────────────
#
# #5763 Phase-1 slice, write side: creating a project from the WebUI registers
# it in the profile's authoritative projects.db so Desktop/CLI see it too.
# Preferred implementation is the upstream hermes_cli.projects_db module itself
# (same code path as `hermes project create`); a schema-compatible direct
# insert is the fallback when hermes_cli is not importable in this process.


def _projects_db_module():
    try:
        import hermes_cli.projects_db as pdb
        return pdb
    except Exception:
        return None


def _invalidate_cache(db: Path) -> None:
    with _cache_lock:
        _cache.pop(str(db), None)


def _agent_dir() -> Path | None:
    """Locate the hermes-agent checkout (same discovery order as api.config)."""
    try:
        from api.config import _discover_agent_dir
        d = _discover_agent_dir()
        return d if d and (d / "hermes_cli").is_dir() else None
    except Exception:
        return None


# Subprocess program: runs upstream create_project verbatim in a fresh
# interpreter that has the agent checkout on sys.path. No logic is
# reimplemented here — validation, slug uniqueness and txn semantics all
# belong to hermes_cli.projects_db.
_CREATE_PROJECT_PROG = """
import json, sys
db_path, name, path, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, agent_dir)
try:
    from hermes_cli import projects_db as pdb
    from pathlib import Path
    conn = pdb.connect(db_path=Path(db_path))
    try:
        pid = pdb.create_project(conn, name=name, primary_path=path)
        row = conn.execute("SELECT slug FROM projects WHERE id = ?", (pid,)).fetchone()
        conn.commit()
        print(json.dumps({"ok": True, "id": pid, "slug": row["slug"] if row else None}))
    finally:
        conn.close()
except ValueError as e:
    print(json.dumps({"ok": False, "kind": "value", "error": str(e)}))
except Exception as e:
    print(json.dumps({"ok": False, "kind": "error", "error": str(e)}))
"""


def _create_via_subprocess(db: Path, *, name: str, resolved: str) -> dict:
    """Run upstream projects_db.create_project in a subprocess with the agent
    checkout on sys.path. Raises RuntimeError if no agent checkout is found."""
    agent_dir = _agent_dir()
    if agent_dir is None:
        raise RuntimeError("hermes_cli not importable and no agent checkout found — cannot register project")
    import subprocess
    try:
        proc = subprocess.run(
            # -I keeps the agent checkout's imports isolated from this venv's
            # site-packages noise; the agent dir goes in via argv + sys.path
            # because -I also ignores PYTHONPATH.
            [sys.executable, "-I", "-c", _CREATE_PROJECT_PROG, str(db), name, resolved, str(agent_dir)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"project create subprocess failed: {e}")
    out = (proc.stdout or "").strip().splitlines()
    payload = None
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if payload is None:
        raise RuntimeError(f"project create produced no result (exit {proc.returncode}): {(proc.stderr or '').strip()[:200]}")
    return payload


def projects_write_supported(profile_home: Path | None = None) -> bool:
    """True when ``create_hermes_project`` can register a project at all.

    Write support needs the native Projects manager (``hermes_cli.projects_db``
    importable here, or the agent checkout reachable for the subprocess
    fallback). Callers use this to fail *before* side effects (mkdir, local
    workspace save) instead of mid-way through registration.
    """
    if _projects_db_module() is not None:
        return True
    return _agent_dir() is not None


def _profile_home_for_write(profile_home: Path | None) -> Path:
    """Resolve the profile home for a write, even when projects.db is absent."""
    if profile_home is not None:
        return Path(profile_home)
    from api.profiles import get_active_hermes_home
    return get_active_hermes_home()


def create_hermes_project(path: str, name: str, profile_home: Path | None = None) -> dict:
    """Create a Hermes Project for ``path`` in the profile's projects.db.

    Returns ``{'id', 'slug', 'name', 'path', 'created': True}``.
    Raises ValueError with a user-facing message when the path already belongs
    to another project or the name is empty; RuntimeError when the native
    Projects manager is unreachable. A missing projects.db is initialized
    through the native manager (``pdb.connect`` runs the idempotent schema
    init), so a fresh profile that has never run Desktop/CLI can still opt in
    to project registration.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("project name must not be empty")
    db = _projects_db_path(profile_home)
    if db is None:
        # Fresh profile: the native manager creates the DB + schema on connect.
        db = _profile_home_for_write(profile_home) / "projects.db"
    resolved = os.path.abspath(os.path.expanduser(str(path).strip())).rstrip("/\\")
    pdb = _projects_db_module()
    if pdb is not None:
        try:
            conn = pdb.connect(db_path=db)
            try:
                pid = pdb.create_project(conn, name=name, primary_path=resolved)
                row = conn.execute("SELECT slug FROM projects WHERE id = ?", (pid,)).fetchone()
                slug = row["slug"] if row else None
                conn.commit()
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
        except (sqlite3.Error, OSError) as e:
            # Lock contention ("database is locked") and other driver errors
            # must surface as RuntimeError per this function's contract —
            # the route maps ValueError/RuntimeError to clean 4xx/5xx bodies
            # instead of an unhandled-exception 500. OSError covers DB-init
            # failures (e.g. profile home path is not a directory).
            raise RuntimeError(f"projects.db write failed: {e}") from e
    else:
        # hermes_cli is not importable in this process: run the SAME upstream
        # function in a subprocess against the agent checkout. No local
        # reimplementation of create_project semantics.
        payload = _create_via_subprocess(db, name=name, resolved=resolved)
        if not payload.get("ok"):
            if payload.get("kind") == "value":
                raise ValueError(payload.get("error") or "project creation rejected")
            raise RuntimeError(payload.get("error") or "project creation failed")
        pid, slug = payload["id"], payload.get("slug")
    _invalidate_cache(db)
    return {"id": pid, "slug": slug, "name": name, "path": resolved, "created": True}


# ── Write path: archive a Hermes Project when its workspace is removed ──────
#
# #5763 read bridge made projects.db authoritative for the picker list:
# removing a workspace from workspaces.json alone no longer hides the entry —
# merge_hermes_projects re-appends it from the DB on the next poll, so the
# delete "doesn't work". Removing the workspace must therefore also archive the
# owning project in projects.db (soft delete: restore via Desktop/CLI stays
# possible; _query_projects only surfaces non-archived rows).

_ARCHIVE_PROJECT_PROG = """
import json, sys
db_path, path, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, agent_dir)
try:
    from hermes_cli import projects_db as pdb
    from pathlib import Path
    conn = pdb.connect(db_path=Path(db_path))
    try:
        proj = pdb.find_by_primary_path(conn, path)
        if proj is None:
            print(json.dumps({"ok": True, "archived": False, "reason": "not-found"}))
        else:
            ok = pdb.archive_project(conn, proj.id)
            conn.commit()
            print(json.dumps({"ok": True, "archived": bool(ok), "id": proj.id}))
    finally:
        conn.close()
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
"""


def archive_hermes_project(path: str, profile_home: Path | None = None) -> dict:
    """Archive the projects.db project owning ``path`` (if any).

    Fail-safe by contract, mirroring the read bridge: any error (no DB, no
    hermes_cli, subprocess failure) yields ``{"archived": False, ...}`` and
    never raises — the local workspaces.json removal has already succeeded and
    must not be rolled back by a DB-side problem.
    """
    resolved = os.path.abspath(os.path.expanduser(str(path).strip())).rstrip("/\\")
    db = _projects_db_path(profile_home)
    if db is None:
        return {"archived": False, "reason": "no-db"}
    pdb = _projects_db_module()
    if pdb is not None:
        try:
            conn = pdb.connect(db_path=db)
            try:
                proj = pdb.find_by_primary_path(conn, resolved)
                if proj is None:
                    return {"archived": False, "reason": "not-found"}
                ok = pdb.archive_project(conn, proj.id)
                conn.commit()
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            _invalidate_cache(db)
            return {"archived": bool(ok), "id": proj.id}
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("archive_hermes_project failed: %s", e)
            return {"archived": False, "reason": "error"}
    # hermes_cli not importable here: same upstream functions via subprocess,
    # matching the create path's fallback style.
    agent_dir = _agent_dir()
    if agent_dir is None:
        return {"archived": False, "reason": "no-hermes-cli"}
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _ARCHIVE_PROJECT_PROG, str(db), resolved, str(agent_dir)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("archive_hermes_project subprocess failed: %s", e)
        return {"archived": False, "reason": "error"}
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not payload or not payload.get("ok"):
        logger.debug("archive_hermes_project subprocess result: %s", payload)
        return {"archived": False, "reason": "error"}
    _invalidate_cache(db)
    return payload


# ── Write path: rename a Hermes Project when its workspace is renamed ────────
#
# merge_hermes_projects makes projects.db authoritative for a path it owns,
# so renaming only workspaces.json reverts on the next poll. Propagate the
# new name to the DB (fail-safe, same contract as archive).

_RENAME_PROJECT_PROG = """
import json, sys
db_path, path, name, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, agent_dir)
try:
    from hermes_cli import projects_db as pdb
    from pathlib import Path
    conn = pdb.connect(db_path=Path(db_path))
    try:
        proj = pdb.find_by_primary_path(conn, path)
        if proj is None:
            print(json.dumps({"ok": True, "renamed": False, "reason": "not-found"}))
        else:
            ok = pdb.update_project(conn, proj.id, name=name)
            conn.commit()
            print(json.dumps({"ok": True, "renamed": bool(ok), "id": proj.id}))
    finally:
        conn.close()
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
"""


def rename_hermes_project(path: str, name: str, profile_home: Path | None = None) -> dict:
    """Rename the projects.db project owning ``path`` (if any).

    Fail-safe by contract, mirroring archive: any error yields
    ``{"renamed": False, ...}`` and never raises — the local rename has
    already succeeded and must not be rolled back by a DB-side problem.
    """
    name = (name or "").strip()
    if not name:
        return {"renamed": False, "reason": "empty-name"}
    resolved = os.path.abspath(os.path.expanduser(str(path).strip())).rstrip("/\\")
    db = _projects_db_path(profile_home)
    if db is None:
        return {"renamed": False, "reason": "no-db"}
    pdb = _projects_db_module()
    if pdb is not None:
        try:
            conn = pdb.connect(db_path=db)
            try:
                proj = pdb.find_by_primary_path(conn, resolved)
                if proj is None:
                    return {"renamed": False, "reason": "not-found"}
                ok = pdb.update_project(conn, proj.id, name=name)
                conn.commit()
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            _invalidate_cache(db)
            return {"renamed": bool(ok), "id": proj.id}
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("rename_hermes_project failed: %s", e)
            return {"renamed": False, "reason": "error"}
    agent_dir = _agent_dir()
    if agent_dir is None:
        return {"renamed": False, "reason": "no-hermes-cli"}
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _RENAME_PROJECT_PROG, str(db), resolved, name, str(agent_dir)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("rename_hermes_project subprocess failed: %s", e)
        return {"renamed": False, "reason": "error"}
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not payload or not payload.get("ok"):
        logger.debug("rename_hermes_project subprocess result: %s", payload)
        return {"renamed": False, "reason": "error"}
    _invalidate_cache(db)
    return payload
