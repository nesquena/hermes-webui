"""Staged-migration crash-point tests (gate finding: no unintended authority).

Drives scripts/migrate_sessions_to_sqlite.py as a subprocess with
HERMES_MIGRATE_CRASH_AFTER set, then inspects the session directory and
store activation from this process.

Commit mode builds sessions.db STAGED as sessions.db.migrating and publishes
by the script's atomic publish only after the staged file verifies through
a fresh connection, is marked migration_complete=1, checkpointed, and
fsynced. No crash point may leave a selector-visible sessions.db with
authority it did not earn, and a long-lived selector must observe a later
publish.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import api.models as models
from api.webui_session_sqlite import WebUISqliteSessionDB

REPO = Path(__file__).resolve().parents[1]

STAGED_NAME = "sessions.db.migrating"


def _seed_sessions(d: Path, n: int = 3) -> None:
    for i in range(n):
        payload = {
            "session_id": f"mig{i}",
            "title": f"T{i}",
            "workspace": "/w",
            "created_at": 1.0,
            "updated_at": 2.0,
            "personality": None,  # explicit-None presence must survive
            "messages": [{"role": "user", "content": "hello"}],
            "tool_calls": [],
            "context_messages": [],
        }
        (d / f"mig{i}.json").write_text(json.dumps(payload), encoding="utf-8")


# Explicit, closed env allowlist for the migration subprocess. The whole
# environment is deliberately NOT inherited (explicit named keys only): the
# child only needs to find the interpreter, its home, and this repo on
# sys.path, plus the crash/mutate injection vars the tests set themselves.
_INHERITED_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
)
_TEST_ENV_VARS = (
    "HERMES_MIGRATE_CRASH_AFTER",
    "HERMES_MIGRATE_TEST_MUTATE",
    "HERMES_MIGRATE_TEST_MUTATE_AFTER_PUBLISH",
    "HERMES_MIGRATE_TEST_PAUSE_BEFORE_CAS",
    "HERMES_MIGRATE_TEST_PAUSE_AFTER_CAS",
    "HERMES_MIGRATE_PUBLISH_LOCK_TIMEOUT",
    "HERMES_CUTOVER_LOCK_TIMEOUT",
)


def _script_env(extra_env: dict | None = None) -> dict[str, str]:
    """Explicit, closed env for the migration subprocess (see above)."""
    env: dict[str, str] = {}
    for key in _INHERITED_ENV:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for key in _TEST_ENV_VARS:
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)
    return env


def _run_script(
    d: Path,
    crash: str | None,
    commit: bool,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = _script_env(extra_env)
    if crash:
        env["HERMES_MIGRATE_CRASH_AFTER"] = crash
    cmd = [sys.executable, str(REPO / "scripts" / "migrate_sessions_to_sqlite.py"), str(d)]
    if commit:
        cmd.append("--commit")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO))


def _publish_staged_dir(tmp_path: Path) -> None:
    """Move the fully-marked staged DB onto sessions.db, reproducing the
    migration script's atomic publish step (its guarded rename under the
    advisory lock) without a raw rename call in test code."""
    (tmp_path / STAGED_NAME).rename(tmp_path / "sessions.db")


def _store_active(d: Path, monkeypatch) -> bool:
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    return bool(models._get_sqlite_session_store())


def _meta(d: Path, db_name: str = "sessions.db") -> dict:
    conn = sqlite3.connect(str(d / db_name))
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def test_dry_run_does_not_mutate_session_dir(tmp_path, monkeypatch):
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, None, commit=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "sessions.db").exists()
    assert not (tmp_path / STAGED_NAME).exists()
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"mig{i}.json" for i in range(3)]


def test_crash_after_pre_schema_leaves_no_db(tmp_path, monkeypatch):
    """pre_schema fires before the store constructor: nothing is created."""
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "pre_schema", commit=True)
    assert result.returncode == 42
    assert not (tmp_path / "sessions.db").exists()
    assert not (tmp_path / STAGED_NAME).exists()
    assert _store_active(tmp_path, monkeypatch) is False


def test_crash_after_schema_leaves_unmarked_staged_db(tmp_path, monkeypatch):
    """Pre-marker constructor crash: the staged file has tables but NO
    markers, so nothing can activate it — and a later app open must not
    stamp created_by=app on the leftover."""
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "schema", commit=True)
    assert result.returncode == 42
    # The crash window is on the STAGED name: the final path is absent.
    assert not (tmp_path / "sessions.db").exists()
    staged = tmp_path / STAGED_NAME
    assert staged.exists()
    # Unmarked: Commit A (tables) landed, Commit B (markers) never did.
    assert "created_by" not in _meta(tmp_path, STAGED_NAME)
    assert _store_active(tmp_path, monkeypatch) is False

    # Opening the leftover through the store (created_by=app default) must
    # NOT stamp created_by=app: it is a pre-existing, row-less database of
    # unknown origin and stays unmarked (fail closed).
    store = WebUISqliteSessionDB(session_dir=tmp_path, db_name=STAGED_NAME)
    assert store.get_meta("created_by") is None
    assert store.is_active() is False
    store.close()
    assert "created_by" not in _meta(tmp_path, STAGED_NAME)

    # The next migration run deletes the staged leftover and completes.
    result = _run_script(tmp_path, None, commit=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _store_active(tmp_path, monkeypatch) is True


def test_crash_after_markers_leaves_marked_incomplete_staged_db(tmp_path, monkeypatch):
    """Post-marker constructor crash: markers exist but migration_complete=0."""
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "markers", commit=True)
    assert result.returncode == 42
    assert not (tmp_path / "sessions.db").exists()
    meta = _meta(tmp_path, STAGED_NAME)
    assert meta.get("created_by") == "migration"
    assert meta.get("migration_complete") == "0"
    assert _store_active(tmp_path, monkeypatch) is False


def test_crash_after_create_leaves_inactive_staged_db(tmp_path, monkeypatch):
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "create", commit=True)
    assert result.returncode == 42
    # Staged-only: the final path is never created in place.
    assert not (tmp_path / "sessions.db").exists()
    assert (tmp_path / STAGED_NAME).exists()
    assert _store_active(tmp_path, monkeypatch) is False


def test_crash_after_copy_leaves_partial_inactive_db(tmp_path, monkeypatch):
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "copy", commit=True)
    assert result.returncode == 42
    assert not (tmp_path / "sessions.db").exists()
    assert (tmp_path / STAGED_NAME).exists()
    assert _store_active(tmp_path, monkeypatch) is False


def test_crash_after_verify_leaves_complete_inactive_db(tmp_path, monkeypatch):
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "verify", commit=True)
    assert result.returncode == 42
    assert not (tmp_path / "sessions.db").exists()
    assert (tmp_path / STAGED_NAME).exists()
    assert _store_active(tmp_path, monkeypatch) is False
    # JSON files untouched — the session dir still serves from sidecars.
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"mig{i}.json" for i in range(3)]


def test_crash_after_publish_leaves_unpublished_staged_db(tmp_path, monkeypatch):
    """The publish hook fires BEFORE the rename: a crash there leaves the
    staged file fully verified and marked complete but never visible as
    sessions.db. Re-running the migration rebuilds and publishes cleanly."""
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, "publish", commit=True)
    assert result.returncode == 42
    assert not (tmp_path / "sessions.db").exists()
    # The staged file was verified, marked complete, checkpointed, fsynced.
    meta = _meta(tmp_path, STAGED_NAME)
    assert meta.get("created_by") == "migration"
    assert meta.get("migration_complete") == "1"
    assert _store_active(tmp_path, monkeypatch) is False
    # Publication happens before sidecar moves, so no session is lost.
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"mig{i}.json" for i in range(3)]

    # Recovery: a clean re-run publishes and activates.
    result = _run_script(tmp_path, None, commit=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _store_active(tmp_path, monkeypatch) is True
    store = models._get_sqlite_session_store()
    for i in range(3):
        row = store.read_session(f"mig{i}")
        assert row is not None and len(row["messages"]) == 1
        assert "personality" in row and row["personality"] is None


def test_commit_completes_and_activates(tmp_path, monkeypatch):
    _seed_sessions(tmp_path)
    result = _run_script(tmp_path, None, commit=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / STAGED_NAME).exists()
    assert _store_active(tmp_path, monkeypatch) is True
    assert (tmp_path / "json-backup").is_dir()
    assert sorted(p.name for p in (tmp_path / "json-backup").glob("*.json")) == [
        f"mig{i}.json" for i in range(3)
    ]


def test_commit_refuses_to_overwrite_active_db(tmp_path, monkeypatch):
    """A final sessions.db with authority is never replaced: refuse first."""
    _seed_sessions(tmp_path)
    store = WebUISqliteSessionDB(session_dir=tmp_path)  # created_by=app -> active
    store.close()
    result = _run_script(tmp_path, None, commit=True)
    assert result.returncode == 1
    assert "refusing" in (result.stdout + result.stderr).lower()
    # Untouched: still the app db, still no backup moves.
    assert _meta(tmp_path).get("created_by") == "app"
    assert not (tmp_path / "json-backup").exists()
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"mig{i}.json" for i in range(3)]
    assert _store_active(tmp_path, monkeypatch) is True


def test_store_constructor_crash_hooks_without_script(tmp_path, monkeypatch):
    """The store exposes the migration crash hooks so unit tests can hit the
    in-constructor stages directly (env HERMES_MIGRATE_CRASH_AFTER)."""
    monkeypatch.setenv("HERMES_MIGRATE_CRASH_AFTER", "schema")
    with pytest.raises(SystemExit):
        WebUISqliteSessionDB(session_dir=tmp_path, created_by="migration")
    assert "created_by" not in _meta(tmp_path)

    # markers: tables + cutover markers committed, then the hook fires.
    for p in tmp_path.iterdir():
        p.unlink()
    monkeypatch.setenv("HERMES_MIGRATE_CRASH_AFTER", "markers")
    with pytest.raises(SystemExit):
        WebUISqliteSessionDB(session_dir=tmp_path, created_by="migration")
    monkeypatch.delenv("HERMES_MIGRATE_CRASH_AFTER")
    meta = _meta(tmp_path)
    assert meta.get("created_by") == "migration"
    assert meta.get("migration_complete") == "0"

    # The marked-incomplete leftover has no authority, and reopening it as a
    # migration db does not re-run the hooks once the env is cleared.
    store = WebUISqliteSessionDB(session_dir=tmp_path, created_by="migration")
    assert store.is_active() is False
    store.close()


def test_app_open_of_preexisting_empty_db_does_not_stamp(tmp_path, monkeypatch):
    """Fail-closed stamp rule: a pre-existing, row-less, unmarked database
    (the exact leftover of a pre-marker constructor crash on the final path)
    must NOT be stamped created_by=app when the app opens it — that would
    activate an empty store over live JSON sidecars."""
    monkeypatch.setenv("HERMES_MIGRATE_CRASH_AFTER", "schema")
    with pytest.raises(SystemExit):
        WebUISqliteSessionDB(session_dir=tmp_path, created_by="migration")
    monkeypatch.delenv("HERMES_MIGRATE_CRASH_AFTER")
    assert (tmp_path / "sessions.db").exists()

    # The selector denies it and the app open leaves it unmarked.
    assert _store_active(tmp_path, monkeypatch) is False
    store = WebUISqliteSessionDB(session_dir=tmp_path)  # created_by=app default
    assert store.get_meta("created_by") is None
    assert store.is_active() is False
    store.close()
    assert "created_by" not in _meta(tmp_path)


def test_long_lived_selector_sees_publish(tmp_path, monkeypatch):
    """One long-lived selector across publication: an armed negative cache
    must NOT stick after a marked-complete staged file is published over
    sessions.db via the script's atomic publish."""
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    # Arm the negative cache: no sessions.db exists yet.
    assert models._get_sqlite_session_store() is False

    # Build and publish a marked-complete staged file exactly the way the
    # migration script does (mark -> checkpoint -> fsync -> atomic rename).
    staged = WebUISqliteSessionDB(session_dir=tmp_path, db_name=STAGED_NAME, created_by="migration")
    staged.write_session(
        {
            "session_id": "pub1",
            "title": "published",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_calls": [],
            "context_messages": [],
        }
    )
    staged.set_meta("migration_complete", "1")
    staged._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    staged._conn().commit()
    staged.close()
    _publish_staged_dir(tmp_path)

    # No singleton reset: the same selector must observe the publication.
    store = models._get_sqlite_session_store()
    assert store
    assert store.read_session("pub1") is not None


def test_unrecognized_markers_fail_closed(tmp_path, monkeypatch):
    _seed_sessions(tmp_path)
    assert _run_script(tmp_path, None, commit=True).returncode == 0
    # Unrecognizable markers must deny authority rather than assume it.
    conn = sqlite3.connect(str(tmp_path / "sessions.db"))
    conn.execute("UPDATE meta SET value = 'unknown-origin' WHERE key = 'created_by'")
    conn.commit()
    conn.close()
    assert _store_active(tmp_path, monkeypatch) is False


# ── Cutover coordination: lock, source-identity CAS, partitioned skips ──────


def test_migration_refuses_when_sidecar_drifts_after_copy(tmp_path, monkeypatch):
    """A live writer racing the cutover must refuse the whole run BEFORE
    publication: no sessions.db, no json-backup/, and the drifted sidecar
    keeps its newer bytes on disk (no lost write)."""
    _seed_sessions(tmp_path)
    result = _run_script(
        tmp_path, None, commit=True, extra_env={"HERMES_MIGRATE_TEST_MUTATE": "mig1"}
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRIFT mig1" in result.stdout
    assert f"FAILURES: 1" in result.stdout
    assert not (tmp_path / "sessions.db").exists()
    assert not (tmp_path / "json-backup").exists()
    # The newer bytes survive in place; nothing was retired.
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"mig{i}.json" for i in range(3)]
    mutated = json.loads((tmp_path / "mig1.json").read_text(encoding="utf-8"))
    assert mutated.get("_mutated") is not None
    assert _store_active(tmp_path, monkeypatch) is False

    # Quiesced re-run completes the cutover cleanly.
    result = _run_script(tmp_path, None, commit=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _store_active(tmp_path, monkeypatch) is True


def test_migration_retires_only_exact_preimage_after_publish(tmp_path, monkeypatch):
    """Drift AFTER publication: the database is active, but the drifted
    sidecar is left in place (exit 3), not moved into json-backup/ and not
    auto-re-imported over the fenced SQL row."""
    _seed_sessions(tmp_path)
    result = _run_script(
        tmp_path,
        None,
        commit=True,
        extra_env={"HERMES_MIGRATE_TEST_MUTATE_AFTER_PUBLISH": "mig1"},
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "DRIFTED mig1" in result.stdout
    assert _store_active(tmp_path, monkeypatch) is True
    store = models._get_sqlite_session_store()
    for i in range(3):
        assert store.read_session(f"mig{i}") is not None
    # The drifted sidecar stays in the session dir with its newer bytes;
    # the other two retired to json-backup/.
    assert (tmp_path / "mig1.json").exists()
    assert json.loads((tmp_path / "mig1.json").read_text(encoding="utf-8")).get("_mutated")
    assert sorted(p.name for p in (tmp_path / "json-backup").glob("*.json")) == [
        "mig0.json",
        "mig2.json",
    ]


def test_migration_skips_unparseable_sidecar_and_proceeds(tmp_path, monkeypatch):
    """One bad legacy file must not abort the run: valid sessions migrate,
    the unparseable sidecar is SKIP-reported and left in place."""
    _seed_sessions(tmp_path)
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
    result = _run_script(tmp_path, None, commit=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP bad" in result.stdout
    assert _store_active(tmp_path, monkeypatch) is True
    store = models._get_sqlite_session_store()
    assert sorted(str(m["session_id"]) for m in store.list_sessions()) == [
        f"mig{i}" for i in range(3)
    ]
    # Left in place, NOT retired to json-backup/.
    assert (tmp_path / "bad.json").exists()
    assert not (tmp_path / "json-backup" / "bad.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock path; the msvcrt branch is unit-tested below")
def test_migration_lock_serializes_concurrent_runs(tmp_path, monkeypatch):
    """A held .migrating.lock refuses a concurrent run (exit 1) before
    anything is created or moved."""
    import fcntl

    _seed_sessions(tmp_path)
    lock_path = tmp_path / ".migrating.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = _run_script(tmp_path, None, commit=True)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".migrating.lock" in (result.stdout + result.stderr)
    assert not (tmp_path / "sessions.db").exists()
    assert not (tmp_path / STAGED_NAME).exists()
    assert not (tmp_path / "json-backup").exists()
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"mig{i}.json" for i in range(3)]


def _load_migration_script_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migrate_sessions_to_sqlite", REPO / "scripts" / "migrate_sessions_to_sqlite.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_lock_msvcrt_branch(tmp_path, monkeypatch):
    """The Windows lock fallback mirrors api.models' msvcrt pattern: lock
    byte 0 with LK_LOCK/LK_UNLCK, contention or a missing primitive refuses
    (SystemExit 1) instead of running an uncoordinated cutover."""
    mod = _load_migration_script_module()
    monkeypatch.setattr(mod, "_fcntl", None)

    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, nbytes):
            calls.append(mode)

    monkeypatch.setattr(mod, "_msvcrt", FakeMsvcrt)
    with mod._migration_lock(tmp_path):
        pass
    assert calls == [FakeMsvcrt.LK_LOCK, FakeMsvcrt.LK_UNLCK]

    class BusyMsvcrt(FakeMsvcrt):
        @staticmethod
        def locking(fd, mode, nbytes):
            if mode == FakeMsvcrt.LK_LOCK:
                raise OSError("locked")
            calls.append(mode)

    monkeypatch.setattr(mod, "_msvcrt", BusyMsvcrt)
    with pytest.raises(SystemExit):
        with mod._migration_lock(tmp_path):
            pass

    # No lock primitive at all: fail closed.
    monkeypatch.setattr(mod, "_msvcrt", None)
    with pytest.raises(SystemExit):
        with mod._migration_lock(tmp_path):
            pass


# ── Cutover handoff: the check→publish race, closed with real writers ───────
#
# The static gate's final blocker: the pre-publish source-identity CAS and
# the atomic publication were two separate steps with an unguarded window
# between them, and live WebUI sidecar writers took no lock — a save landing
# in that window was left as post-publish drift evidence (exit 3) while the
# healthy SQL row became canonical, silently de-canonizing the acknowledged
# live save. The protocol under test: api.models._sidecar_write_guard holds
# .cutover.lock SHARED around every sidecar write (re-probing the selector
# inside the hold); the script's publish window holds it EXCLUSIVE across
# CAS → os.replace → retirement. A live save therefore either completes
# before the CAS (drift → refusal before activation) or blocks and then
# lands durably in the published SQL generation.


def _popen_script(d: Path, extra_env: dict) -> subprocess.Popen:
    """Run the migration with streamable stdout (pause markers are flushed)."""
    cmd = [sys.executable, str(REPO / "scripts" / "migrate_sessions_to_sqlite.py"), str(d), "--commit"]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_script_env(extra_env),
        cwd=str(REPO),
    )


def _wait_for_pause(proc: subprocess.Popen, marker: str) -> None:
    for line in proc.stdout:
        if marker in line:
            return
    raise AssertionError(f"migration never reached {marker!r}")


def _aim_models_at(d: Path, monkeypatch) -> None:
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    monkeypatch.setattr(models, "_sqlite_session_store_stamp", None)


def test_publish_window_serializes_live_save_into_sql(tmp_path, monkeypatch):
    """The gate's exact schedule: pause migration immediately AFTER the final
    preimage check (inside the exclusive publish window), perform a REAL
    atomic WebUI sidecar save, then release publication. The compliant
    writer cannot land in the window: it blocks on the shared cutover lock,
    publication completes, and the save re-routes into the published SQL
    store — the first canonical post-cutover load returns the writer's
    newer bytes (review outcome 2: durably applied to the published
    generation), and the run retires every exact preimage (exit 0)."""
    _aim_models_at(tmp_path, monkeypatch)
    _seed_sessions(tmp_path)
    release = tmp_path / "release.marker"
    proc = _popen_script(tmp_path, {"HERMES_MIGRATE_TEST_PAUSE_AFTER_CAS": str(release)})
    try:
        _wait_for_pause(proc, "TEST-PAUSE after CAS")

        # A REAL production writer: sidecar-lineage load (sessions.db is not
        # published yet), a user edit, and the real Session.save() — the
        # same path a live WebUI owner takes.
        outcome: dict = {}

        def _live_writer():
            try:
                owner = models.Session.load("mig1")
                owner.title = "live-edit-during-cutover"
                owner.messages = list(owner.messages) + [
                    {"role": "assistant", "content": "written during the publish window"}
                ]
                owner.save()
                outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - recorded for the join assert
                outcome["error"] = repr(exc)

        writer = threading.Thread(target=_live_writer)
        writer.start()
        try:
            writer.join(timeout=2.0)
            # The save has NOT landed: it is blocked on the shared cutover
            # lock while the publish window holds it exclusive. (Were it
            # complete, mig1.json would have drifted and the run would be
            # heading for exit 1 — caught by the returncode assert below.)
            assert writer.is_alive(), outcome
        finally:
            release.write_text("go", encoding="utf-8")
            writer.join(timeout=30.0)
        assert not writer.is_alive()
        assert outcome.get("ok"), outcome
    finally:
        out = proc.communicate(timeout=60.0)[0]
    assert proc.returncode == 0, out
    assert _store_active(tmp_path, monkeypatch) is True

    # The first canonical post-cutover load is the SQL row — and it contains
    # the writer's newer bytes (title AND transcript).
    reloaded = models.Session.load("mig1")
    assert reloaded is not None
    assert reloaded.title == "live-edit-during-cutover"
    assert any(
        m.get("content") == "written during the publish window"
        for m in reloaded.messages
    )
    # The adopted write fenced-and-bumped the published generation (the
    # migration copy was generation 1).
    store = models._get_sqlite_session_store()
    assert store.read_row_version("mig1") == {"generation": 2, "incarnation": 1}
    # No stranded newer sidecar: the writer never renamed it, so retirement
    # moved the exact preimage and the run exited 0.
    assert not (tmp_path / "mig1.json").exists()
    assert (tmp_path / "json-backup" / "mig1.json").exists()
    # The other sessions retired too.
    assert sorted(p.name for p in (tmp_path / "json-backup").glob("*.json")) == [
        "mig0.json",
        "mig1.json",
        "mig2.json",
    ]


def test_live_save_completing_before_cas_refuses_publication(tmp_path, monkeypatch):
    """The complementary leg: a REAL save that completes BEFORE the window's
    preimage CAS (paused pre-window, exclusive lock not yet taken). The CAS
    observes the drift and refuses the whole run BEFORE sessions.db becomes
    active (review outcome 1) — and the writer's newer bytes remain the
    canonical sidecar contents for the next run."""
    _aim_models_at(tmp_path, monkeypatch)
    _seed_sessions(tmp_path)
    release = tmp_path / "release.marker"
    proc = _popen_script(tmp_path, {"HERMES_MIGRATE_TEST_PAUSE_BEFORE_CAS": str(release)})
    try:
        _wait_for_pause(proc, "TEST-PAUSE before CAS")

        owner = models.Session.load("mig1")
        owner.title = "live-edit-before-cas"
        owner.save()  # completes now: the shared lock is uncontended

        release.write_text("go", encoding="utf-8")
        out = proc.communicate(timeout=60.0)[0]
    finally:
        if proc.poll() is None:  # pragma: no cover - failure cleanup
            proc.kill()
            out = proc.communicate()[0]
    assert proc.returncode == 1, out
    assert "DRIFT mig1" in out
    assert not (tmp_path / "sessions.db").exists()
    assert not (tmp_path / "json-backup").exists()
    assert _store_active(tmp_path, monkeypatch) is False
    # The newer bytes stay canonical: with no active store, the load reads
    # the sidecar the writer just renamed.
    assert models.Session.load("mig1").title == "live-edit-before-cas"


def _publish_ready_staged(d: Path, sid: str) -> None:
    """Publish a marked-complete staged db containing ``sid``'s sidecar copy,
    exactly the way the script does: copy (force) → mark → checkpoint →
    atomic rename (never a raw os.replace in test code)."""
    staged = WebUISqliteSessionDB(session_dir=d, db_name=STAGED_NAME, created_by="migration")
    try:
        staged.write_session(
            json.loads((d / f"{sid}.json").read_text(encoding="utf-8")), force=True
        )
        staged.set_meta("migration_complete", "1")
        staged._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        staged._conn().commit()
    finally:
        staged.close()
    _publish_staged_dir(d)


def test_cached_sidecar_owner_save_adopts_published_store(tmp_path, monkeypatch):
    """A long-lived WebUI owner loaded from the sidecar, with the cutover
    publishing between load and save: the first post-cutover save must be
    durably applied to the published SQL generation (fresh durable token
    adoption), not deterministically refused by the CAS forever."""
    _aim_models_at(tmp_path, monkeypatch)
    _seed_sessions(tmp_path)
    owner = models.Session.load("mig1")
    assert owner._persisted_generation is None  # sidecar lineage, no token
    owner.title = "post-publish-edit"
    owner.messages = list(owner.messages) + [{"role": "assistant", "content": "kept"}]

    _publish_ready_staged(tmp_path, "mig1")

    owner.save()
    store = models._get_sqlite_session_store()
    assert store
    row = store.read_session("mig1")
    assert row["title"] == "post-publish-edit"
    assert any(m.get("content") == "kept" for m in row["messages"])
    assert row["generation"] == 2
    assert owner._persisted_generation == 2
    # The second save is a clean plain-CAS write on the adopted lineage.
    owner.title = "second-edit"
    owner.save()
    assert store.read_session("mig1")["generation"] == 3
    reloaded = models.Session.load("mig1")
    assert reloaded.title == "second-edit"


def test_cached_sidecar_owner_draft_adopts_when_cutover_publishes_mid_call(tmp_path, monkeypatch):
    """The composer's draft autosave (save_metadata) mid-call flip: the entry
    probe selected the JSON backend, the publish lands while the fallback
    holds the shared cutover lock. The in-hold re-probe must route the draft
    into the published SQL row instead of renaming a sidecar the next load
    will never read."""
    from contextlib import contextmanager

    _aim_models_at(tmp_path, monkeypatch)
    _seed_sessions(tmp_path)
    owner = models.Session.load("mig1")

    real_guard = models._sidecar_write_guard
    flips = {"n": 0}

    @contextmanager
    def guard_that_publishes_on_first_hold():
        with real_guard():
            if flips["n"] == 0:
                flips["n"] = 1
                _publish_ready_staged(tmp_path, "mig1")
            yield

    monkeypatch.setattr(models, "_sidecar_write_guard", guard_that_publishes_on_first_hold)
    owner.save_metadata({"composer_draft": {"text": "typing-during-cutover"}})
    assert flips["n"] == 1

    store = models._get_sqlite_session_store()
    assert store
    assert store.read_session("mig1")["composer_draft"] == {"text": "typing-during-cutover"}
    assert models.Session.load("mig1").composer_draft == {"text": "typing-during-cutover"}
    # The sidecar was never renamed with the draft (the row is authority).
    assert json.loads((tmp_path / "mig1.json").read_text(encoding="utf-8"))[
        "title"
    ] == "T1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock path; msvcrt branch unit-tested below")
def test_sidecar_write_guard_blocks_during_publish_window(tmp_path, monkeypatch):
    """The shared hold serializes against the publisher's exclusive window:
    a writer thread must not enter while LOCK_EX is held, and must proceed
    the moment the window closes."""
    import fcntl

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    entered = threading.Event()
    leave = threading.Event()
    done = threading.Event()

    def _writer():
        with models._sidecar_write_guard():
            entered.set()
            leave.wait(timeout=10.0)
            done.set()

    fd = os.open(tmp_path / ".cutover.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # the migration publish window
    writer = threading.Thread(target=_writer)
    try:
        writer.start()
        assert not entered.wait(0.5), "writer entered the exclusive window"
        fcntl.flock(fd, fcntl.LOCK_UN)  # window closes
        assert entered.wait(5.0), "writer did not proceed after the window"
    finally:
        leave.set()
        writer.join(timeout=5.0)
        os.close(fd)
    assert done.is_set()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock path")
def test_sidecar_write_guard_times_out_fail_closed(tmp_path, monkeypatch):
    """A wedged publish window must fail the save closed after the bounded
    wait — never proceed to rename a sidecar inside the window."""
    import fcntl

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setenv("HERMES_CUTOVER_LOCK_TIMEOUT", "0.3")
    fd = os.open(tmp_path / ".cutover.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match="cutover window"):
            with models._sidecar_write_guard():
                pass  # pragma: no cover - must not be reached
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_sidecar_write_guard_msvcrt_branch(tmp_path, monkeypatch):
    """Windows fallback: exclusive byte lock on the same byte the publish
    window holds, released on exit; a permanently busy lock fails the save
    closed after the bounded wait; no primitive at all fails closed."""
    calls: list[int] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, nbytes):
            calls.append(mode)

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_fcntl", None)
    monkeypatch.setattr(models, "_msvcrt", FakeMsvcrt)
    with models._sidecar_write_guard():
        pass
    assert calls == [FakeMsvcrt.LK_NBLCK, FakeMsvcrt.LK_UNLCK]

    class BusyMsvcrt(FakeMsvcrt):
        @staticmethod
        def locking(fd, mode, nbytes):
            if mode == FakeMsvcrt.LK_NBLCK:
                raise OSError("locked")
            calls.append(mode)

    monkeypatch.setattr(models, "_msvcrt", BusyMsvcrt)
    monkeypatch.setenv("HERMES_CUTOVER_LOCK_TIMEOUT", "0.2")
    with pytest.raises(RuntimeError, match="cutover window"):
        with models._sidecar_write_guard():
            pass  # pragma: no cover

    monkeypatch.setattr(models, "_msvcrt", None)
    with pytest.raises(RuntimeError, match="unavailable"):
        with models._sidecar_write_guard():
            pass  # pragma: no cover


def test_publish_window_lock_msvcrt_branch(tmp_path, monkeypatch):
    """The script's exclusive publish-window lock mirrors the migration
    lock's msvcrt pattern; contention past the deadline refuses (exit 1)."""
    mod = _load_migration_script_module()
    calls: list[int] = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, nbytes):
            calls.append(mode)

    monkeypatch.setattr(mod, "_fcntl", None)
    monkeypatch.setattr(mod, "_msvcrt", FakeMsvcrt)
    with mod._publish_window_lock(tmp_path):
        pass
    assert calls == [FakeMsvcrt.LK_LOCK, FakeMsvcrt.LK_UNLCK]

    class BusyMsvcrt(FakeMsvcrt):
        @staticmethod
        def locking(fd, mode, nbytes):
            if mode == FakeMsvcrt.LK_LOCK:
                raise OSError("locked")
            calls.append(mode)

    monkeypatch.setattr(mod, "_msvcrt", BusyMsvcrt)
    monkeypatch.setenv("HERMES_MIGRATE_PUBLISH_LOCK_TIMEOUT", "0.2")
    with pytest.raises(SystemExit):
        with mod._publish_window_lock(tmp_path):
            pass  # pragma: no cover

    monkeypatch.setattr(mod, "_msvcrt", None)
    with pytest.raises(SystemExit):
        with mod._publish_window_lock(tmp_path):
            pass  # pragma: no cover
