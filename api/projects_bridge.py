"""Bridge between the WebUI workspace picker and the Hermes Agent Projects
store (projects.db).

Implements issue #5763: the WebUI workspace list surfaces the profile's
authoritative ``projects.db`` (the same SQLite store backing Hermes Desktop /
CLI ``hermes project list``) instead of requiring a manually duplicated
picker list, and the write slice (create / archive / rename) propagates
WebUI project operations into ``projects.db`` so Desktop/CLI see them too.

Design:
- Reads are fail-safe by contract: a missing DB, missing tables, lock
  contention, or any other error yields an empty result with ``read_ok``
  telling callers whether the answer is authoritative — the workspace picker
  then behaves exactly as it did before this bridge existed
  (``workspaces.json`` fallback).
- Writes go through the upstream ``hermes_cli.projects_db`` module when
  importable (same code path as ``hermes project create``), with a
  schema-compatible subprocess fallback. The ``HERMES_WEBUI_PROJECTS_DB_SYNC``
  kill switch disables reads AND writes.
- Read results are cached per DB path and invalidated on DB/WAL mtime+size
  change, so a project created via Desktop/CLI appears on the next
  ``/api/workspaces`` poll without a WebUI restart.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import sys
import threading
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

# path -> ((db stat, wal stat), active_entries, archived_by_path)
_cache: dict[
    str,
    tuple[tuple[tuple[float, int] | None, tuple[float, int] | None], list[tuple[str, str]], dict[str, str]],
] = {}
_cache_lock = threading.Lock()

# Env kill-switch: set HERMES_WEBUI_PROJECTS_DB_SYNC=0 to disable the bridge.
_DISABLE_ENV = "HERMES_WEBUI_PROJECTS_DB_SYNC"


def _sync_enabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


def sync_enabled() -> bool:
    """Public kill-switch probe: True when the shared store may be read/written.

    Route handlers call this BEFORE any preflight that could touch the DB
    (open/initialize) or the filesystem (mkdir), so `HERMES_WEBUI_PROJECTS_DB_SYNC=0`
    means the shared store is genuinely never touched (re-gate should-fix 4).
    """
    return _sync_enabled()


def _is_remote_workspace_path(path: str, profile: str | Path | None = None) -> bool:
    """True when ``path`` is a target-side remote terminal path (SSH/Docker).

    Remote paths live on the target host: resolving them against the WebUI
    host filesystem (``realpath``) is wrong — a host symlink with the same
    spelling silently collapses a remote workspace onto a local directory
    (re-gate must-fix 2). Detection reuses the same profile-aware candidate
    check the workspace validators use, so the answer is consistent with
    how the row was stored in the first place.
    """
    try:
        from api.workspace import _remote_terminal_workspace_candidate
        return _remote_terminal_workspace_candidate(path, profile=profile) is not None
    except Exception:
        return False


def is_remote_workspace_path(path: str, profile: str | Path | None = None) -> bool:
    """Public probe: True when ``path`` is a target-side remote workspace for
    ``profile``. Route handlers use this to keep remote literals out of
    projects.db OWNERSHIP decisions — the shared store is host-local, and a
    remote path whose spelling coincides with a host directory (host symlink
    at the same spelling) must never acquire native ownership through that
    coincidence (re-gate must-fix: remote-to-host identity crossing).
    Provenance (``project_mirror``) is the only thing that can make a
    remote-spelled row a genuine native entry."""
    return profile is not None and _is_remote_workspace_path(str(path), profile=profile)


def row_db_key(path: str, profile: str | Path | None = None, *, native_provenance: bool = False) -> str:
    """Comparison key for a workspace ROW against projects.db.

    projects.db is host-local, so genuine local/native entries key the HOST
    form (realpath folds symlink spellings of a host directory — deep-audit
    2a). But a REMOTE literal (target-side SSH/Docker path) keeps its lexical
    identity: a host symlink at the same spelling must not fold a remote row
    onto an unrelated host project (re-gate must-fix: the merged projection
    replacing a remote alias label with a host project name, and rename/
    remove mutating the host project through the realpath coincidence).
    ``native_provenance`` (the row's persisted ``project_mirror`` flag) is the
    only override: a mirror row was created FROM the shared store, so it is a
    native entry even when its path string sits under a remote terminal cwd
    (deep-audit 2b: host-owned rows under a remote cwd must stay shared-backed).
    """
    if (
        not native_provenance
        and profile is not None
        and _is_remote_workspace_path(str(path), profile=profile)
    ):
        return path_key(str(path), profile=profile)
    return path_key(str(path))


def _lexical_key(s: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(s))).rstrip("/\\")


def path_key(path: str | Path, profile: str | Path | None = None) -> str:
    """Canonical comparison key for a workspace/project path.

    Both sides of every bridge comparison go through this. Local
    ``workspaces.json`` rows are stored symlink-resolved
    (``validate_workspace_to_add``), while ``projects.db`` rows are stored as
    Desktop/CLI wrote them — possibly a symlinked or differently-cased
    spelling of the same directory. Comparing raw strings lets one directory
    look like two paths: a shared-backed workspace misclassified as
    local-only silently reverts on the next poll (deep-audit P1).

    ``realpath`` folds symlinks (and, on case-insensitive filesystems, the
    on-disk casing); ``normcase`` covers Windows case-folding. Trailing
    separators are stripped so ``/x`` and ``/x/`` compare equal.

    ``profile`` makes the key profile-aware: paths that are target-side
    remote terminal workspaces for that profile are keyed lexically (no
    host ``realpath``), so a host symlink cannot collapse a remote path
    onto a local directory (re-gate must-fix 2). DB paths are always
    host-local and keep the symlink-folding key regardless.
    """
    s = str(path).strip()
    if profile is not None and _is_remote_workspace_path(s, profile=profile):
        # Remote target-side path: keep it as written (lexical normalization
        # only — abspath never touches the filesystem).
        try:
            return _lexical_key(s)
        except (OSError, ValueError):
            return os.path.normcase(s).rstrip("/\\")
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(s))).rstrip("/\\")
    except (OSError, ValueError):
        # Embedded NUL or pathological input: fall back to the lexical form.
        return _lexical_key(s)


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


def _query_projects(db: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return ``(active_entries, archived_by_path)`` for the profile's projects.

    A project's workspace path is its primary folder when set, otherwise any
    attached folder (lowest ``added_at`` for determinism). Projects with no
    folders at all are skipped: a workspace entry needs a real directory.

    Archived projects are returned as ``path_key -> {names}`` so the merge can
    hide local MIRROR rows whose shared project was archived from Desktop/CLI
    — while still showing a workspace the user deliberately re-added under its
    own name after the archive (see merge_hermes_projects).

    Raises sqlite.Error on any read failure (missing tables, lock, corrupt
    file): callers must distinguish "no projects" from "could not tell" —
    a failed read is never authoritative and must never be cached.
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
            SELECT p.name AS name, p.archived AS archived,
                   COALESCE(
                       (SELECT pf.path FROM project_folders pf
                         WHERE pf.project_id = p.id AND pf.is_primary = 1
                         LIMIT 1),
                       (SELECT pf.path FROM project_folders pf
                         WHERE pf.project_id = p.id
                         ORDER BY pf.added_at ASC LIMIT 1)
                   ) AS path
            FROM projects p
            ORDER BY p.created_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    out: list[tuple[str, str]] = []
    archived: dict[str, set[str]] = {}
    seen: set[str] = set()
    for r in rows:
        path = (r["path"] or "").strip()
        name = (r["name"] or "").strip()
        if not path:
            continue
        if r["archived"]:
            # A set of names: two archived projects can share a path (archive
            # one, re-create at the same dir, archive again) and every mirror
            # name must be hidden, not just the oldest one's (deep-audit P3).
            archived.setdefault(path_key(path), set()).add(name)
            continue
        if not name or path in seen:
            continue
        seen.add(path)
        out.append((path, name))
    return out, archived


def load_project_state(profile_home: Path | None = None) -> tuple[list[dict], dict[str, set[str]], bool]:
    """Return ``(active_entries, archived_by_path, read_ok)`` from projects.db.

    ``read_ok`` is False when the DB exists but could not be read (lock,
    corrupt file, older schema). Callers MUST treat a failed read as
    "unknown", never as "no projects": an empty result from a failed read is
    not authoritative and must not hide shared projects, drop local rows, or
    prove a path is local-only. Failed reads are never cached — the next poll
    re-queries, so a transient error self-heals without waiting for a
    timestamp change.

    ``archived_by_path`` maps ``path_key`` to the SET of archived project
    names at that path — always a dict, on every path, including the
    kill-switch and no-DB shortcuts (callers may rely on the mapping shape).

    The cache key tracks the DB and WAL (mtime, size) SEPARATELY: a max() of
    the two lets a newer main-DB timestamp mask a WAL commit (future-dated
    restore, coarse-timestamp filesystems), and mtime alone misses a commit
    that lands inside the filesystem's timestamp granularity of the stat
    whose result got cached — size catches every row insert/delete.
    """
    if not _sync_enabled():
        return [], {}, True
    try:
        db = _projects_db_path(profile_home)
        if db is None:
            # No shared store at all: trivially "read fine, nothing there".
            return [], {}, True
        key = str(db)

        def _stamp(p: Path) -> tuple[float, int] | None:
            try:
                st = p.stat()
                return (st.st_mtime, st.st_size)
            except OSError:
                return None

        db_stamp = _stamp(db)
        if db_stamp is None:
            return [], {}, True
        wal_stamp = _stamp(db.with_name(db.name + "-wal"))
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None and cached[0] == (db_stamp, wal_stamp):
                entries, archived = cached[1], cached[2]
            else:
                try:
                    entries, archived = _query_projects(db)
                except sqlite3.Error as e:
                    # Failed read: never cache, report unknown.
                    logger.debug("projects.db read failed (not cached): %s", e)
                    return [], {}, False
                _cache[key] = ((db_stamp, wal_stamp), entries, archived)
        return (
            [{"path": p, "name": n, "source": "hermes_project"} for p, n in entries],
            {k: set(v) for k, v in archived.items()},
            True,
        )
    except Exception:
        logger.debug("projects.db bridge failed; falling back to local workspaces", exc_info=True)
        return [], {}, False


def load_hermes_project_workspaces(profile_home: Path | None = None) -> list[dict]:
    """Return workspace-shaped entries ``{'path','name','source'}`` from projects.db.

    Fail-safe convenience wrapper: any failure returns ``[]`` so callers that
    only list projects fall back to the WebUI-local workspace list unchanged.
    Callers that MUTATE based on ownership must use ``load_project_state``
    instead and honor ``read_ok``.
    """
    entries, _archived, ok = load_project_state(profile_home=profile_home)
    return entries if ok else []


def merge_hermes_projects(workspaces: list[dict], profile_home: Path | None = None, profile: str | Path | None = None) -> list[dict]:
    """Merge projects.db entries into a WebUI workspace list.

    - projects.db is authoritative for a path it owns: a local entry with the
      same path takes the project's display name.
    - A local row explicitly marked as a project MIRROR (``project_mirror``
      provenance, persisted in workspaces.json when the row was created from
      a projects.db registration) whose shared project was ARCHIVED
      (from Desktop/CLI) is hidden. Provenance is the ONLY hiding trigger:
      a plain local workspace re-added at an archived project's path — even
      under the same name — must stay visible (re-gate must-fix 1: name-based
      inference made a legitimate re-add an invisible row that the add route
      then refused as a duplicate).
    - Local-only entries keep their order and names (WebUI additions still work).
    - DB projects not present locally are appended.
    - A FAILED DB read is not authoritative: local entries pass through
      untouched (no archive-hiding, no renames).
    ``profile`` makes row<->DB keying provenance-aware (re-gate must-fix): a
    REMOTE literal without ``project_mirror`` provenance keeps its lexical
    identity and can never fold onto a host project through a host-symlink
    realpath coincidence; genuine local/native entries (plain local rows and
    mirror rows) keep the host-canonicalized key because projects.db is
    host-local (deep-audit 2a/2b).
    Never mutates the input list.
    """
    db_entries, archived, read_ok = load_project_state(profile_home=profile_home)
    if not read_ok:
        return list(workspaces)
    if not db_entries and not archived:
        return list(workspaces)
    # First row wins for a shared path key: the append loop below dedupes to
    # the FIRST db_entry, so the name-override side must agree — otherwise a
    # reorder (which materializes a local row) would flip the displayed name
    # from the first project's to the last project's without any rename.
    db_by_path: dict[str, dict] = {}
    for e in db_entries:
        db_by_path.setdefault(path_key(e["path"]), e)
    merged: list[dict] = []
    used: set[str] = set()
    for w in workspaces:
        entry = dict(w)
        path = entry.get("path", "")
        # Row<->DB keying (re-gate must-fix, supersedes the blanket host-key
        # walk-back of 7c990429): projects.db is host-local, so genuine
        # local/native rows key the HOST form — a local row spelled under a
        # remote terminal.cwd that is actually host-owned (or a persisted
        # mirror created FROM the shared store) matches its DB project by
        # realpath (deep-audit 2a/2b). But a REMOTE literal without mirror
        # provenance keys lexically: a host symlink at the same spelling must
        # not replace the remote alias label with an unrelated host project's
        # name nor drop the native row from the projection.
        key = row_db_key(path, profile=profile,
                         native_provenance=bool(entry.get("project_mirror")))
        if entry.get("project_mirror") and archived.get(key) is not None and db_by_path.get(key) is None:
            # Persisted mirror of a retired shared project: hide it. Rows
            # without the provenance flag are ordinary local workspaces and
            # always survive an archived-name match at their path.
            continue
        hit = db_by_path.get(key)
        if hit is not None:
            entry["name"] = hit["name"]
            entry["source"] = "hermes_project"
            used.add(key)
        merged.append(entry)
    for e in db_entries:
        key = path_key(e["path"])
        if key not in used:
            # Dedupe by key: two DB rows for one directory (e.g. registered
            # through different symlink spellings) must not both appear.
            used.add(key)
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
        raise RuntimeError(f"project create subprocess failed: {e}") from e
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


def projects_db_openable(profile_home: Path | None = None) -> bool:
    """True when the profile's projects.db can be opened (or freshly created).

    Callers use this to fail *before* side effects (mkdir) when the DB exists
    but is corrupt/unopenable — a failed opt-in registration must not leave a
    newly created empty folder behind. The probe is a plain sqlite3 read, so
    it works whether or not the native manager is importable in this process.

    The probe is READ-ONLY: a missing DB returns True without opening
    anything (the native manager creates it during the real write). Opening
    with the initializing connection here would create + schema-init
    ``projects.db`` even when a later validation rejects the path (re-gate
    low: preflight must not write).
    """
    try:
        db = _projects_db_path(profile_home)
        if db is None:
            # No DB file yet — nothing to probe; creation belongs to the write.
            return True
        # Plain sqlite3 read: no native manager needed for a read-only probe,
        # so corrupt-DB detection works even when hermes_cli is unreachable.
        conn = sqlite3.connect(db)
        try:
            # Probe a REAL table read, not `SELECT 1`: a valid header with a
            # corrupt/malformed body passes SELECT 1 but fails here — the
            # route must reject before mkdir instead of leaving an orphan
            # folder when registration later fails (greptile P2). A valid
            # DB without the schema yet is fine: the native manager
            # initializes it on the real write.
            try:
                # SELECT * (not SELECT 1): forces SQLite to read the table
                # b-tree pages — a header-valid DB with a malformed table
                # page passes a constant projection (index-only) but raises
                # here.
                conn.execute("SELECT * FROM projects LIMIT 1").fetchone()
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower():
                    raise
        finally:
            conn.close()
        return True
    except Exception:
        return False


def create_hermes_project(path: str, name: str, profile_home: Path | None = None) -> dict:
    """Create a Hermes Project for ``path`` in the profile's projects.db.

    Honors the ``HERMES_WEBUI_PROJECTS_DB_SYNC`` kill switch: with the bridge
    disabled, writes are refused too — the documented "disable the bridge
    entirely" must mean the shared store is never touched, reads or writes.

    Returns ``{'id', 'slug', 'name', 'path', 'created': True}``.
    Raises ValueError with a user-facing message when the path already belongs
    to another project or the name is empty; RuntimeError when the native
    Projects manager is unreachable. A missing projects.db is initialized
    through the native manager (``pdb.connect`` runs the idempotent schema
    init), so a fresh profile that has never run Desktop/CLI can still opt in
    to project registration.
    """
    if not _sync_enabled():
        raise RuntimeError("projects bridge disabled (HERMES_WEBUI_PROJECTS_DB_SYNC=0) — shared store untouched")
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
import json, os, sys
db_path, path, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, agent_dir)
def _key(p):
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(str(p).strip()))).rstrip("/\\\\")
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(p).strip()))).rstrip("/\\\\")
try:
    from hermes_cli import projects_db as pdb
    from pathlib import Path
    conn = pdb.connect(db_path=Path(db_path))
    try:
        # BEGIN IMMEDIATE spans lookup + ambiguity check + mutation so a
        # concurrent writer cannot swap the owner between our read and write
        # (re-gate must-fix 3; mirrors the in-process implementation).
        conn.execute("BEGIN IMMEDIATE")
        key = _key(path)
        matches = []
        for p_ in pdb.list_projects(conn, include_archived=False):
            primary = p_.primary_path or next(
                (f.path for f in p_.folders if f.is_primary), p_.folders[0].path if p_.folders else None)
            if primary and _key(primary) == key:
                matches.append(p_)
        if not matches:
            conn.execute("ROLLBACK")
            print(json.dumps({"ok": True, "archived": False, "reason": "not-found"}))
        elif len(matches) > 1:
            conn.execute("ROLLBACK")
            print(json.dumps({"ok": True, "archived": False, "reason": "ambiguous-path", "count": len(matches)}))
        else:
            ok = conn.execute("UPDATE projects SET archived = 1 WHERE id = ?", (matches[0].id,)).rowcount
            conn.execute("COMMIT")
            print(json.dumps({"ok": True, "archived": bool(ok), "id": matches[0].id}))
    finally:
        conn.close()
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
"""


def _find_projects_by_path(pdb, conn, path: str):
    """All non-archived projects whose primary path canonicalizes to ``path``.

    Upstream ``find_by_primary_path`` matched lexically (normcase/abspath), so
    a DB row stored under a symlinked spelling of the directory the WebUI
    resolved would not be found and the archive/rename would silently no-op
    while the route already classified the path as shared-backed (deep-audit
    P1). Same traversal as upstream, canonical keys on both sides.

    Returns a LIST because Desktop/CLI can register two projects over the same
    directory through different spellings (real path + symlink). Callers must
    treat 0 matches as not-found and >1 as ambiguous (greptile P1: acting on
    the first match could mutate a different project than the one the picker
    shows, since the merge collapses same-key rows into one entry).
    """
    key = path_key(path)
    matches = []
    for proj in pdb.list_projects(conn, include_archived=False):
        primary = proj.primary_path or next(
            (f.path for f in proj.folders if f.is_primary),
            proj.folders[0].path if proj.folders else None,
        )
        if primary and path_key(primary) == key:
            matches.append(proj)
    return matches


def archive_hermes_project(path: str, profile_home: Path | None = None) -> dict:
    """Archive the projects.db project owning ``path`` (if any).

    Fail-safe by contract, mirroring the read bridge: any error (no DB, no
    hermes_cli, subprocess failure) yields ``{"archived": False, ...}`` and
    never raises — the local workspaces.json removal has already succeeded and
    must not be rolled back by a DB-side problem.
    """
    if not _sync_enabled():
        # Kill switch: the shared store must stay untouched, writes included.
        return {"archived": False, "reason": "disabled"}
    resolved = path_key(path)
    db = _projects_db_path(profile_home)
    if db is None:
        return {"archived": False, "reason": "no-db"}
    pdb = _projects_db_module()
    if pdb is not None:
        try:
            conn = pdb.connect(db_path=db)
            try:
                # One BEGIN IMMEDIATE spans owner lookup, ambiguity check and
                # the mutation (re-gate must-fix 3): a concurrent native
                # connection cannot move the matched project's folder and
                # register a replacement at this path between our read and
                # our write. The UPDATE is inlined (same statement as
                # pdb.archive_project) because write_txn cannot nest.
                with pdb.write_txn(conn):
                    matches = _find_projects_by_path(pdb, conn, resolved)
                    if not matches:
                        return {"archived": False, "reason": "not-found"}
                    if len(matches) > 1:
                        # Ambiguous owner: the picker shows one collapsed entry
                        # for this path, but the DB has several projects behind
                        # it. Refuse rather than archive a project the user did
                        # not target (greptile P1).
                        return {"archived": False, "reason": "ambiguous-path",
                                "count": len(matches)}
                    proj = matches[0]
                    n = conn.execute(
                        "UPDATE projects SET archived = 1 WHERE id = ?", (proj.id,)
                    ).rowcount
                _invalidate_cache(db)
                return {"archived": bool(n), "id": proj.id}
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
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
import json, os, sys
db_path, path, name, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, agent_dir)
def _key(p):
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(str(p).strip()))).rstrip("/\\\\")
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(p).strip()))).rstrip("/\\\\")
try:
    from hermes_cli import projects_db as pdb
    from pathlib import Path
    conn = pdb.connect(db_path=Path(db_path))
    try:
        # BEGIN IMMEDIATE spans lookup + ambiguity check + mutation
        # (re-gate must-fix 3; mirrors the in-process implementation).
        conn.execute("BEGIN IMMEDIATE")
        key = _key(path)
        matches = []
        for p_ in pdb.list_projects(conn, include_archived=False):
            primary = p_.primary_path or next(
                (f.path for f in p_.folders if f.is_primary), p_.folders[0].path if p_.folders else None)
            if primary and _key(primary) == key:
                matches.append(p_)
        if not matches:
            conn.execute("ROLLBACK")
            print(json.dumps({"ok": True, "renamed": False, "reason": "not-found"}))
        elif len(matches) > 1:
            conn.execute("ROLLBACK")
            print(json.dumps({"ok": True, "renamed": False, "reason": "ambiguous-path", "count": len(matches)}))
        else:
            ok = conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name, matches[0].id)).rowcount
            conn.execute("COMMIT")
            print(json.dumps({"ok": True, "renamed": bool(ok), "id": matches[0].id}))
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
    if not _sync_enabled():
        # Kill switch: the shared store must stay untouched, writes included.
        return {"renamed": False, "reason": "disabled"}
    name = (name or "").strip()
    if not name:
        return {"renamed": False, "reason": "empty-name"}
    resolved = path_key(path)
    db = _projects_db_path(profile_home)
    if db is None:
        return {"renamed": False, "reason": "no-db"}
    pdb = _projects_db_module()
    if pdb is not None:
        try:
            conn = pdb.connect(db_path=db)
            try:
                # Transactional lookup+mutation — see archive_hermes_project
                # (re-gate must-fix 3). The UPDATE mirrors pdb.update_project's
                # name branch because write_txn cannot nest.
                with pdb.write_txn(conn):
                    matches = _find_projects_by_path(pdb, conn, resolved)
                    if not matches:
                        return {"renamed": False, "reason": "not-found"}
                    if len(matches) > 1:
                        # Ambiguous owner — see archive_hermes_project.
                        return {"renamed": False, "reason": "ambiguous-path",
                                "count": len(matches)}
                    proj = matches[0]
                    n = conn.execute(
                        "UPDATE projects SET name = ? WHERE id = ?", (name, proj.id)
                    ).rowcount
                _invalidate_cache(db)
                return {"renamed": bool(n), "id": proj.id}
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
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
