"""Regression coverage for delegated subagent cleanup on CLI-session delete."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def _seed_session(
    conn, sid, *, parent_id=None, model_config=None, source=None,
    end_reason=None, ended_at=None, started_at="2026-07-11T00:00:00Z"
):
    conn.execute(
        """
        INSERT INTO sessions (
            id, title, started_at, source, parent_session_id, model_config,
            end_reason, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            sid,
            started_at,
            source or ("subagent" if (model_config or {}).get("_delegate_from") else "cli"),
            parent_id,
            json.dumps(model_config) if model_config else None,
            end_reason,
            ended_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES (?, 'user', ?, '2026-07-11T00:00:01Z')
        """,
        (sid, f"message for {sid}"),
    )


def _seed_transcript_artifacts(hermes_home, session_ids):
    sessions_dir = hermes_home / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    for sid in session_ids:
        (sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
        (sessions_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        (sessions_dir / f"request_dump_{sid}_1.json").write_text("{}", encoding="utf-8")
    return sessions_dir


def _remaining_artifact_session_ids(sessions_dir):
    names = {path.name for path in sessions_dir.iterdir()}
    ids = set()
    for name in names:
        if name.startswith("request_dump_"):
            ids.add(name.removeprefix("request_dump_").rsplit("_", 1)[0])
        else:
            ids.add(name.rsplit(".", 1)[0])
    return ids


def _assert_all_artifacts_exist(sessions_dir, session_ids):
    """Verify each preserved session keeps every supported artifact kind."""
    for sid in session_ids:
        assert (sessions_dir / f"{sid}.json").exists()
        assert (sessions_dir / f"{sid}.jsonl").exists()
        assert (sessions_dir / f"request_dump_{sid}_1.json").exists()


@pytest.mark.requires_agent_modules
def test_delete_cli_session_cascades_delegates_but_preserves_branch(tmp_path, monkeypatch):
    """Current Hermes removes delegates while preserving all other child kinds."""
    hermes_state = pytest.importorskip("hermes_state")
    SessionDB = hermes_state.SessionDB

    state_db = tmp_path / "state.db"
    db = SessionDB(db_path=state_db)
    db.close()

    conn = sqlite3.connect(state_db)
    try:
        _seed_session(conn, "parent")
        _seed_session(
            conn,
            "delegate-child",
            parent_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        _seed_session(
            conn,
            "delegate-grandchild",
            parent_id="delegate-child",
            model_config={"_delegate_from": "delegate-child"},
        )
        _seed_session(
            conn,
            "ordinary-branch",
            parent_id="parent",
            model_config={"_branched_from": "parent"},
            source="subagent",
        )
        _seed_session(conn, "generic-child", parent_id="parent")
        _seed_session(conn, "compression-parent", end_reason="compression")
        _seed_session(
            conn, "compression-child", parent_id="compression-parent",
            source="subagent",
            model_config={"_delegate_from": "compression-parent"},
        )
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(
        tmp_path,
        {
            "parent", "delegate-child", "delegate-grandchild", "ordinary-branch",
            "generic-child", "compression-parent", "compression-child",
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    assert delete_cli_session("parent") is True
    assert delete_cli_session("compression-parent") is True

    conn = sqlite3.connect(state_db)
    try:
        remaining_sessions = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, parent_session_id FROM sessions "
                "WHERE id IN (?, ?, ?, ?, ?, ?, ?)",
                (
                    "parent", "delegate-child", "delegate-grandchild",
                    "ordinary-branch", "generic-child", "compression-parent",
                    "compression-child",
                ),
            )
        }
        remaining_messages = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT session_id FROM messages "
                "WHERE session_id IN (?, ?, ?, ?, ?, ?, ?)",
                (
                    "parent", "delegate-child", "delegate-grandchild",
                    "ordinary-branch", "generic-child", "compression-parent",
                    "compression-child",
                ),
            )
        }
    finally:
        conn.close()

    assert remaining_sessions == {
        "ordinary-branch": None,
        "compression-child": None,
        "generic-child": None,
    }
    assert remaining_messages == set(remaining_sessions)
    assert _remaining_artifact_session_ids(sessions_dir) == set(remaining_sessions)
    assert delete_cli_session("parent") is True


def test_delete_cli_session_cascades_marked_and_legacy_subagents(
    tmp_path, monkeypatch
):
    """The scoped transaction keeps the delegated cascade invariant."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                started_at TEXT,
                source TEXT,
                parent_session_id TEXT,
                model_config TEXT,
                end_reason TEXT,
                ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "parent")
        _seed_session(
            conn, "marked-child", parent_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        _seed_session(conn, "legacy-child", parent_id="parent", source="subagent")
        _seed_session(
            conn, "delegate-grandchild", parent_id="legacy-child",
            model_config={"_delegate_from": "legacy-child"},
        )
        _seed_session(
            conn, "branch-child", parent_id="parent",
            model_config={"_branched_from": "parent"},
            source="subagent",
        )
        _seed_session(conn, "generic-child", parent_id="parent")
        _seed_session(conn, "compression-parent", end_reason="compression")
        _seed_session(
            conn, "compression-child", parent_id="compression-parent",
            source="subagent",
            model_config={"_delegate_from": "compression-parent"},
        )
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(
        tmp_path,
        {
            "parent", "marked-child", "legacy-child", "delegate-grandchild",
            "branch-child", "generic-child", "compression-parent",
            "compression-child",
        },
    )
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    assert delete_cli_session("parent") is True
    assert delete_cli_session("compression-parent") is True

    conn = sqlite3.connect(state_db)
    try:
        remaining = dict(
            conn.execute("SELECT id, parent_session_id FROM sessions ORDER BY id")
        )
        remaining_messages = {
            row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")
        }
    finally:
        conn.close()

    assert remaining == {
        "branch-child": None,
        "compression-child": None,
        "generic-child": None,
    }
    assert remaining_messages == set(remaining)
    assert _remaining_artifact_session_ids(sessions_dir) == set(remaining)
    assert delete_cli_session("parent") is True


def test_delete_cli_session_preserves_ambiguous_and_inherited_marker_branches(
    tmp_path, monkeypatch
):
    """Destructive classification must fail closed for branch evidence."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                started_at TEXT,
                source TEXT,
                parent_session_id TEXT,
                model_config TEXT,
                end_reason TEXT,
                ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "delegate-parent")
        _seed_session(
            conn,
            "both-marker-branch",
            parent_id="delegate-parent",
            source="subagent",
            model_config={
                "_delegate_from": "delegate-parent",
                "_branched_from": "delegate-parent",
            },
        )
        _seed_session(
            conn,
            "nested-branch-child",
            parent_id="both-marker-branch",
            source="subagent",
            model_config={"_delegate_from": "delegate-parent"},
        )
        _seed_session(conn, "other-lineage")
        _seed_session(
            conn,
            "mismatched-lineage-child",
            parent_id="delegate-parent",
            source="subagent",
            model_config={"_delegate_from": "other-lineage"},
        )
        _seed_session(
            conn, "corrupt-config-child", parent_id="delegate-parent", source="subagent"
        )
        conn.execute(
            "UPDATE sessions SET model_config = ? WHERE id = ?",
            ('{"_delegate_from":"other-lineage"', "corrupt-config-child"),
        )
        _seed_session(
            conn, "scalar-config-child", parent_id="delegate-parent", source="subagent"
        )
        conn.execute(
            "UPDATE sessions SET model_config = ? WHERE id = ?",
            ('"not-a-dict"', "scalar-config-child"),
        )
        _seed_session(
            conn,
            "missing-time-parent",
            end_reason="branched",
            ended_at=None,
        )
        _seed_session(
            conn,
            "missing-time-branch",
            parent_id="missing-time-parent",
            source="subagent",
            started_at=None,
        )
        _seed_session(
            conn,
            "malformed-time-parent",
            end_reason="branched",
            ended_at="not-a-timestamp",
        )
        _seed_session(
            conn,
            "malformed-time-branch",
            parent_id="malformed-time-parent",
            source="subagent",
            started_at="also-not-a-timestamp",
        )
        _seed_session(
            conn,
            "nonfinite-time-parent",
            end_reason="branched",
            ended_at="NaN",
        )
        _seed_session(
            conn,
            "nonfinite-time-branch",
            parent_id="nonfinite-time-parent",
            source="subagent",
            started_at="1783728001.0",
        )
        _seed_session(
            conn,
            "nonfinite-child-parent",
            end_reason="branched",
            ended_at="1783728000.0",
        )
        for suffix, started_at in (
            ("nan", "NaN"),
            ("positive-infinity", "Infinity"),
            ("negative-infinity", "-Infinity"),
        ):
            _seed_session(
                conn,
                f"nonfinite-{suffix}-branch",
                parent_id="nonfinite-child-parent",
                source="subagent",
                started_at=started_at,
            )
        _seed_session(
            conn,
            "naive-time-parent",
            end_reason="branched",
            ended_at="2026-07-11T00:00:00",
        )
        _seed_session(
            conn,
            "naive-time-branch",
            parent_id="naive-time-parent",
            source="subagent",
            started_at="2026-07-10T23:59:59Z",
        )
        _seed_session(
            conn,
            "parsed-time-parent",
            end_reason="branched",
            ended_at="1783728000.0",
        )
        _seed_session(
            conn,
            "parsed-time-branch",
            parent_id="parsed-time-parent",
            source="subagent",
            started_at="2026-07-11T00:00:01Z",
        )
        _seed_session(
            conn,
            "parsed-time-delegate",
            parent_id="parsed-time-parent",
            source="subagent",
            started_at="1783727999.0",
        )
        conn.commit()
    finally:
        conn.close()

    preserved = {
        "both-marker-branch",
        "nested-branch-child",
        "other-lineage",
        "mismatched-lineage-child",
        "corrupt-config-child",
        "scalar-config-child",
        "missing-time-branch",
        "malformed-time-branch",
        "nonfinite-time-branch",
        "nonfinite-nan-branch",
        "nonfinite-positive-infinity-branch",
        "nonfinite-negative-infinity-branch",
        "naive-time-branch",
        "parsed-time-branch",
    }
    sessions_dir = _seed_transcript_artifacts(
        tmp_path,
        preserved
        | {
            "delegate-parent",
            "missing-time-parent",
            "malformed-time-parent",
            "nonfinite-time-parent",
            "nonfinite-child-parent",
            "naive-time-parent",
            "parsed-time-parent",
            "parsed-time-delegate",
        },
    )
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    assert delete_cli_session("delegate-parent") is True
    assert delete_cli_session("missing-time-parent") is True
    assert delete_cli_session("malformed-time-parent") is True
    assert delete_cli_session("nonfinite-time-parent") is True
    assert delete_cli_session("nonfinite-child-parent") is True
    assert delete_cli_session("naive-time-parent") is True
    assert delete_cli_session("parsed-time-parent") is True

    conn = sqlite3.connect(state_db)
    try:
        remaining = dict(
            conn.execute("SELECT id, parent_session_id FROM sessions ORDER BY id")
        )
        remaining_messages = {
            row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")
        }
    finally:
        conn.close()

    assert remaining["nested-branch-child"] == "both-marker-branch"
    assert {
        sid: parent_id
        for sid, parent_id in remaining.items()
        if sid != "nested-branch-child"
    } == {sid: None for sid in preserved - {"nested-branch-child"}}
    assert remaining_messages == preserved
    assert _remaining_artifact_session_ids(sessions_dir) == preserved
    _assert_all_artifacts_exist(sessions_dir, preserved)


def test_delete_cli_session_profile_resolution_failure_does_not_guess_fallback(
    tmp_path, monkeypatch
):
    fallback_home = tmp_path / "fallback-profile"
    fallback_home.mkdir()
    state_db = fallback_home / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            CREATE TABLE messages (session_id TEXT);
            INSERT INTO sessions (id) VALUES ('victim');
            INSERT INTO messages (session_id) VALUES ('victim');
            """
        )
        conn.commit()
    finally:
        conn.close()

    import api.profiles

    monkeypatch.setenv("HERMES_HOME", str(fallback_home))

    def _resolution_failure():
        raise RuntimeError("active profile unavailable")

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", _resolution_failure)

    from api.models import delete_cli_session

    assert delete_cli_session("victim") is False
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT id FROM sessions").fetchall() == [("victim",)]
        assert conn.execute("SELECT session_id FROM messages").fetchall() == [
            ("victim",)
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("sid", ["victim", "_victim", "-victim"])
def test_delete_cli_session_reports_artifact_cleanup_failure(
    tmp_path, monkeypatch, sid
):
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                started_at TEXT,
                source TEXT,
                parent_session_id TEXT,
                model_config TEXT,
                end_reason TEXT,
                ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, sid)
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {sid})
    locked_artifact = sessions_dir / f"{sid}.json"
    locked_content = locked_artifact.read_text(encoding="utf-8")
    original_unlink = Path.unlink

    def _locked_unlink(path, *args, **kwargs):
        if path == locked_artifact:
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _locked_unlink)
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    assert delete_cli_session(sid) is False
    assert locked_artifact.read_text(encoding="utf-8") == locked_content

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT id FROM sessions").fetchall() == []
        assert conn.execute("SELECT session_id FROM messages").fetchall() == []
    finally:
        conn.close()


def test_delete_cli_session_deletes_explicit_migrated_delegate_without_source_tag(
    tmp_path, monkeypatch
):
    """Explicit lineage is authoritative; source gates only legacy inference."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                started_at TEXT,
                source TEXT,
                parent_session_id TEXT,
                model_config TEXT,
                end_reason TEXT,
                ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "parent")
        # Agent-migrated delegates can carry the authoritative marker without
        # retaining the historical source='subagent' tag.
        _seed_session(
            conn, "migrated-delegate", parent_id="parent",
            source="cli",
            model_config={"_delegate_from": "parent"},
        )
        # Marker-less legacy inference still requires source='subagent'.
        _seed_session(conn, "legacy-delegate", parent_id="parent", source="subagent")
        _seed_session(conn, "webui-child", parent_id="parent", source="webui")
        _seed_session(conn, "cli-child", parent_id="parent", source="cli")
        # Branch evidence continues to take precedence over an explicit marker.
        _seed_session(
            conn, "migrated-branch", parent_id="parent", source="cli",
            model_config={
                "_delegate_from": "parent",
                "_branched_from": "parent",
            },
        )
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(
        tmp_path,
        {
            "parent", "migrated-delegate", "legacy-delegate",
            "webui-child", "cli-child", "migrated-branch",
        },
    )
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    assert delete_cli_session("parent") is True

    conn = sqlite3.connect(state_db)
    try:
        remaining = dict(
            conn.execute("SELECT id, parent_session_id FROM sessions ORDER BY id")
        )
        remaining_messages = {
            row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")
        }
    finally:
        conn.close()

    preserved = {"webui-child", "cli-child", "migrated-branch"}
    assert remaining == {sid: None for sid in preserved}
    assert remaining_messages == preserved
    assert _remaining_artifact_session_ids(sessions_dir) == preserved
    _assert_all_artifacts_exist(sessions_dir, preserved)


def test_delete_cli_session_serializes_manifest_lifecycle_across_processes(
    tmp_path
):
    """Two workers must serialize both early recovery and late artifact cleanup."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "trigger-a")
        _seed_session(conn, "trigger-b")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(
        tmp_path, {"trigger-a", "trigger-b", "orphan-a", "orphan-b"}
    )
    (sessions_dir / ".cleanup_manifest_orphan-a.json").write_text(
        json.dumps(["orphan-a"]), encoding="utf-8"
    )
    (sessions_dir / ".cleanup_manifest_orphan-b.json").write_text(
        json.dumps(["orphan-b"]), encoding="utf-8"
    )

    go = tmp_path / "go"
    active = tmp_path / "manifest-recovery-active"
    violation = tmp_path / "manifest-recovery-overlap"
    artifact_active = tmp_path / "artifact-cleanup-active"
    artifact_violation = tmp_path / "artifact-cleanup-overlap"
    script = r"""
import os
import sys
import time
from pathlib import Path

from api import models
from api import profiles

home = Path(sys.argv[1])
sid = sys.argv[2]
ready = Path(sys.argv[3])
go = Path(sys.argv[4])
active = Path(sys.argv[5])
violation = Path(sys.argv[6])
artifact_active = Path(sys.argv[7])
artifact_violation = Path(sys.argv[8])
artifact_seen = Path(sys.argv[9])
original = models._process_stale_cleanup_manifests
original_unlink = Path.unlink
profiles.get_active_hermes_home = lambda: home

def observed_recovery(hermes_home):
    owns_marker = False
    try:
        fd = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        violation.write_text("overlap", encoding="utf-8")
    else:
        os.close(fd)
        owns_marker = True
    try:
        time.sleep(0.5)
        return original(hermes_home)
    finally:
        if owns_marker:
            active.unlink(missing_ok=True)

models._process_stale_cleanup_manifests = observed_recovery

artifact_phase_entered = False

def observed_unlink(path, *args, **kwargs):
    global artifact_phase_entered
    is_current_artifact = (
        path.parent == home / "sessions"
        and path.name in {f"{sid}.json", f"{sid}.jsonl"}
    )
    if not is_current_artifact or artifact_phase_entered:
        return original_unlink(path, *args, **kwargs)

    artifact_phase_entered = True
    artifact_seen.write_text("seen", encoding="utf-8")
    owns_marker = False
    try:
        fd = os.open(artifact_active, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        artifact_violation.write_text("overlap", encoding="utf-8")
    else:
        os.close(fd)
        owns_marker = True
    try:
        # Keep the late phase open longer than the serialized recovery probe
        # above. If a future refactor releases the process lock after recovery,
        # the second worker must reach this window and trip the overlap marker.
        time.sleep(2.0)
        return original_unlink(path, *args, **kwargs)
    finally:
        if owns_marker:
            original_unlink(artifact_active, missing_ok=True)

Path.unlink = observed_unlink
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
while not go.exists():
    if time.monotonic() >= deadline:
        raise SystemExit("start barrier timeout")
    time.sleep(0.01)
print(f"RESULT={models.delete_cli_session(sid)}", flush=True)
"""
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    env["HERMES_WEBUI_ISOLATED_PROFILE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), env.get("PYTHONPATH", "")) if part
    )
    procs = []
    ready_paths = []
    artifact_seen_paths = []
    for suffix in ("a", "b"):
        ready = tmp_path / f"ready-{suffix}"
        ready_paths.append(ready)
        artifact_seen = tmp_path / f"artifact-seen-{suffix}"
        artifact_seen_paths.append(artifact_seen)
        procs.append(
            subprocess.Popen(
                [
                    sys.executable, "-c", script, str(tmp_path),
                    f"trigger-{suffix}", str(ready), str(go), str(active),
                    str(violation), str(artifact_active), str(artifact_violation),
                    str(artifact_seen),
                ],
                cwd=repo_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

    deadline = time.monotonic() + 15
    while not all(path.exists() for path in ready_paths):
        if time.monotonic() >= deadline:
            for proc in procs:
                proc.kill()
            raise AssertionError("worker readiness timeout")
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")

    outputs = [proc.communicate(timeout=20) for proc in procs]
    for proc, (stdout, stderr) in zip(procs, outputs, strict=False):
        assert proc.returncode == 0, (stdout, stderr)
        assert "RESULT=True" in stdout, (stdout, stderr)
    assert not violation.exists(), "stale-manifest recovery overlapped across processes"
    assert all(path.exists() for path in artifact_seen_paths), (
        "both workers must reach post-commit artifact cleanup"
    )
    assert not artifact_violation.exists(), (
        "post-commit artifact cleanup overlapped across processes"
    )
    assert not list(sessions_dir.glob(".cleanup_manifest_*.json"))
    for sid in ("trigger-a", "trigger-b", "orphan-a", "orphan-b"):
        assert not (sessions_dir / f"{sid}.json").exists()
        assert not (sessions_dir / f"{sid}.jsonl").exists()


def test_cleanup_manifest_process_lock_uses_windows_byte_lock(
    tmp_path, monkeypatch
):
    """The native-Windows fallback locks and unlocks one persistent byte."""
    import api.models as models

    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, nbytes):
            calls.append((fd, mode, nbytes))

    monkeypatch.setattr(models, "_fcntl", None)
    monkeypatch.setattr(models, "_msvcrt", FakeMsvcrt)

    with pytest.raises(RuntimeError, match="probe"):
        with models._cleanup_manifest_process_lock(tmp_path):
            assert [call[1:] for call in calls] == [(FakeMsvcrt.LK_LOCK, 1)]
            raise RuntimeError("probe")

    assert [call[1:] for call in calls] == [
        (FakeMsvcrt.LK_LOCK, 1),
        (FakeMsvcrt.LK_UNLCK, 1),
    ]
    lock_path = tmp_path / ".session_cleanup.lock"
    assert lock_path.read_bytes() == b"\0"


def test_delete_cli_session_thread_lock_does_not_block_different_profiles(
    tmp_path, monkeypatch
):
    """A stalled cleanup in one profile must not globally block another."""
    homes = {
        "profile-a": tmp_path / "profile-a",
        "profile-b": tmp_path / "profile-b",
    }
    for name, home in homes.items():
        home.mkdir()
        conn = sqlite3.connect(home / "state.db")
        try:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                    source TEXT, parent_session_id TEXT, model_config TEXT,
                    end_reason TEXT, ended_at TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
                );
                """
            )
            _seed_session(conn, name)
            conn.commit()
        finally:
            conn.close()
        _seed_transcript_artifacts(home, {name})

    import api.models as models
    import api.profiles as profiles

    monkeypatch.setattr(
        profiles,
        "get_active_hermes_home",
        lambda: homes[threading.current_thread().name],
    )
    original_recovery = models._process_stale_cleanup_manifests
    profile_a_entered = threading.Event()
    profile_b_entered = threading.Event()
    release_profile_a = threading.Event()

    def observed_recovery(hermes_home):
        if Path(hermes_home) == homes["profile-a"]:
            profile_a_entered.set()
            if not release_profile_a.wait(timeout=10):
                raise TimeoutError("profile-a release timeout")
        else:
            profile_b_entered.set()
        return original_recovery(hermes_home)

    monkeypatch.setattr(models, "_process_stale_cleanup_manifests", observed_recovery)
    results = {}

    def delete_for_profile(name):
        results[name] = models.delete_cli_session(name)

    thread_a = threading.Thread(target=delete_for_profile, args=("profile-a",), name="profile-a")
    thread_b = threading.Thread(target=delete_for_profile, args=("profile-b",), name="profile-b")
    thread_a.start()
    assert profile_a_entered.wait(timeout=10)
    thread_b.start()
    try:
        assert profile_b_entered.wait(timeout=3), (
            "profile-b was globally blocked by profile-a's in-process lock"
        )
        thread_b.join(timeout=10)
        assert not thread_b.is_alive()
        assert results.get("profile-b") is True
    finally:
        release_profile_a.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert results == {"profile-a": True, "profile-b": True}


def test_delete_cli_session_cleans_referential_tables(tmp_path, monkeypatch):
    """Deleting a session must also remove rows from session_model_usage,
    compression_locks, and telegram_dm_topic_bindings — not just
    sessions and messages.
    """
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                started_at TEXT,
                source TEXT,
                parent_session_id TEXT,
                model_config TEXT,
                end_reason TEXT,
                ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session_id, model)
            );
            CREATE TABLE compression_locks (
                session_id TEXT NOT NULL,
                holder TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE telegram_dm_topic_bindings (
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                linked_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            );
            """
        )
        _seed_session(conn, "parent")
        _seed_session(
            conn, "delegate", parent_id="parent",
            source="subagent",
            model_config={"_delegate_from": "parent"},
        )
        # Populate referential tables for both parent and delegate.
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens) "
            "VALUES ('parent', 'test-model', 100, 50)"
        )
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens) "
            "VALUES ('delegate', 'test-model', 200, 75)"
        )
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) "
            "VALUES ('parent', 'compressor', 1000.0, 2000.0)"
        )
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) "
            "VALUES ('delegate', 'compressor', 1000.0, 2000.0)"
        )
        conn.execute(
            "INSERT INTO telegram_dm_topic_bindings "
            "(chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at) "
            "VALUES ('chat1', 'thread1', 'user1', 'key1', 'parent', 1000.0, 1000.0)"
        )
        conn.execute(
            "INSERT INTO telegram_dm_topic_bindings "
            "(chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at) "
            "VALUES ('chat2', 'thread2', 'user1', 'key2', 'delegate', 1000.0, 1000.0)"
        )
        # An unrelated session's rows must survive.
        _seed_session(conn, "bystander")
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens) "
            "VALUES ('bystander', 'test-model', 10, 5)"
        )
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) "
            "VALUES ('bystander', 'compressor', 1000.0, 2000.0)"
        )
        conn.execute(
            "INSERT INTO telegram_dm_topic_bindings "
            "(chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at) "
            "VALUES ('chat3', 'thread3', 'user1', 'key3', 'bystander', 1000.0, 1000.0)"
        )
        conn.commit()
    finally:
        conn.close()

    _seed_transcript_artifacts(tmp_path, {"parent", "delegate", "bystander"})
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    assert delete_cli_session("parent") is True

    conn = sqlite3.connect(state_db)
    try:
        # session_model_usage: only bystander survives.
        smu = [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM session_model_usage ORDER BY session_id"
            )
        ]
        assert smu == ["bystander"]

        # compression_locks: only bystander survives.
        cl = [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM compression_locks ORDER BY session_id"
            )
        ]
        assert cl == ["bystander"]

        # telegram_dm_topic_bindings: only bystander survives.
        tdtb = [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM telegram_dm_topic_bindings ORDER BY session_id"
            )
        ]
        assert tdtb == ["bystander"]
    finally:
        conn.close()


def test_delete_cli_session_artifact_cleanup_is_idempotent(tmp_path, monkeypatch):
    """When artifact cleanup partially fails, the manifest retains the
    pending ID so a second call retries it.  A second call that succeeds
    must return True and leave no manifest behind.
    """
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                started_at TEXT,
                source TEXT,
                parent_session_id TEXT,
                model_config TEXT,
                end_reason TEXT,
                ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "victim")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"victim"})
    locked_artifact = sessions_dir / "victim.json"
    locked_content = locked_artifact.read_text(encoding="utf-8")
    original_unlink = Path.unlink

    call_count = {"n": 0}

    def _locked_once_unlink(path, *args, **kwargs):
        if path == locked_artifact:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PermissionError("locked on first attempt")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _locked_once_unlink)
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    # First call: DB row is deleted but artifact cleanup fails → False.
    assert delete_cli_session("victim") is False
    assert locked_artifact.read_text(encoding="utf-8") == locked_content

    # Manifest must still exist with "victim" pending.
    manifests = sorted(sessions_dir.glob(".cleanup_manifest_*.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8")) == ["victim"]

    # DB row is already gone; second call returns True (idempotent —
    # absence is success) and retries the artifact.
    assert delete_cli_session("victim") is True
    assert not locked_artifact.exists()
    assert not list(sessions_dir.glob(".cleanup_manifest_*.json"))


def test_delete_cli_session_stale_manifest_preserves_live_session(tmp_path, monkeypatch):
    """A stale manifest from a failed commit must not unlink artifacts
    for a session that still exists in the DB.
    """
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "live-one")
        _seed_session(conn, "live-two")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"live-one", "live-two"})

    # Write a stale manifest claiming "live-one" artifacts should be
    # removed, simulating a previous delete_cli_session whose commit
    # failed after the manifest was written.
    stale_manifest = sessions_dir / ".cleanup_manifest_stale-sim.json"
    stale_manifest.write_text(
        json.dumps(["live-one"]), encoding="utf-8"
    )

    import api.profiles
    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    from api.models import delete_cli_session

    # Delete a session NOT in the stale manifest.
    assert delete_cli_session("live-two") is True

    # _process_stale_cleanup_manifests ran at entry and should have
    # skipped "live-one" because it's still alive.
    assert (sessions_dir / "live-one.json").exists()
    assert (sessions_dir / "live-one.jsonl").exists()
    assert (sessions_dir / "request_dump_live-one_1.json").exists()

    # "live-two" artifacts should be gone.
    assert not (sessions_dir / "live-two.json").exists()
    assert not (sessions_dir / "live-two.jsonl").exists()

    # The stale manifest was processed and dropped (live-one is alive,
    # so the manifest was identified as stale from a failed commit and
    # silently removed).  No manifests should linger.
    assert not list(sessions_dir.glob(".cleanup_manifest_*.json"))


def test_delete_cli_session_concurrent_unique_manifests(tmp_path, monkeypatch):
    """Each delete call writes its own unique manifest file so concurrent
    deletes never clobber each other's retry records.
    """
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "parent-a")
        _seed_session(
            conn, "child-a", parent_id="parent-a", source="subagent",
            model_config={"_delegate_from": "parent-a"},
        )
        _seed_session(conn, "parent-b")
        _seed_session(
            conn, "child-b", parent_id="parent-b", source="subagent",
            model_config={"_delegate_from": "parent-b"},
        )
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(
        tmp_path, {"parent-a", "child-a", "parent-b", "child-b"}
    )

    import api.profiles
    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    from api.models import delete_cli_session

    # True concurrent delete: two threads synchronised by a Barrier so
    # both enter the manifest critical section in parallel.  The lock
    # serialises access; both must still complete successfully.
    import threading as _thr

    barrier = _thr.Barrier(2)
    results = []

    def _del_a():
        barrier.wait()
        results.append(delete_cli_session("parent-a"))

    def _del_b():
        barrier.wait()
        results.append(delete_cli_session("parent-b"))

    t_a = _thr.Thread(target=_del_a)
    t_b = _thr.Thread(target=_del_b)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert results == [True, True], f"Both deletes must succeed: {results}"

    # No manifests linger; all artifacts cleaned for every deleted session.
    assert not list(sessions_dir.glob(".cleanup_manifest_*.json"))
    for sid in ("parent-a", "child-a", "parent-b", "child-b"):
        assert not (sessions_dir / f"{sid}.json").exists()
        assert not (sessions_dir / f"{sid}.jsonl").exists()


def test_delete_cli_session_releases_manifest_lock_after_unlink_error(
    tmp_path, monkeypatch
):
    """A stale-manifest exception must not deadlock every later delete."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "victim")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"victim"})
    empty_manifest = sessions_dir / ".cleanup_manifest_empty.json"
    empty_manifest.write_text("[]", encoding="utf-8")

    import api.profiles
    import api.models as models

    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    original_unlink = Path.unlink

    def _raise_for_manifest(path, *args, **kwargs):
        if path == empty_manifest:
            raise PermissionError("manifest is locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _raise_for_manifest)
    assert models.delete_cli_session("victim") is False

    # The context manager must release this profile's lock even though stale
    # manifest cleanup raised before the DB transaction began.
    manifest_lock = models._cleanup_manifest_thread_lock(tmp_path)
    assert manifest_lock.acquire(blocking=False) is True
    manifest_lock.release()

    # Restore unlink behavior and prove a complete later delete can enter the
    # same critical section and finish, rather than merely acquiring the lock.
    monkeypatch.setattr(Path, "unlink", original_unlink)
    empty_manifest.unlink()
    assert models.delete_cli_session("victim") is True
    assert not (sessions_dir / "victim.json").exists()


def test_delete_cli_session_missing_db_preserves_manifested_live_artifacts(
    tmp_path, monkeypatch
):
    """A temporarily missing state.db is unknown, never proof of deletion."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "live-session")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"live-session"})
    manifest = sessions_dir / ".cleanup_manifest_missing-db.json"
    manifest.write_text(json.dumps(["live-session"]), encoding="utf-8")
    hidden_db = tmp_path / "state.db.temporarily-hidden"
    state_db.rename(hidden_db)

    import api.profiles
    from api.models import delete_cli_session

    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    assert delete_cli_session("unrelated") is False

    # Every artifact and the retry record survive while liveness is unknown.
    assert (sessions_dir / "live-session.json").exists()
    assert (sessions_dir / "live-session.jsonl").exists()
    assert (sessions_dir / "request_dump_live-session_1.json").exists()
    assert manifest.exists()

    hidden_db.rename(state_db)
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", ("live-session",)
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_delete_cli_session_unqueryable_db_preserves_manifested_live_artifacts(
    tmp_path, monkeypatch
):
    """A failed liveness query preserves every artifact and reports failure."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "live-session")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"live-session"})
    manifest = sessions_dir / ".cleanup_manifest_unqueryable-db.json"
    manifest.write_text(json.dumps(["live-session"]), encoding="utf-8")

    import api.profiles
    import api.models as models

    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    original_connect = sqlite3.connect

    def _fail_readonly_connect(database, *args, **kwargs):
        if kwargs.get("uri") is True and "mode=ro" in str(database):
            raise sqlite3.OperationalError("state.db is temporarily unreadable")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _fail_readonly_connect)
    assert models.delete_cli_session("unrelated") is False

    assert manifest.exists()
    _assert_all_artifacts_exist(sessions_dir, {"live-session"})
    conn = original_connect(state_db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", ("live-session",)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ?", ("live-session",)
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_delete_cli_session_uses_delegate_lineage_parent_for_compression(
    tmp_path, monkeypatch
):
    """Authoritative delegate lineage preserves a compression continuation."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "compression-parent", end_reason="compression")
        _seed_session(conn, "physical-parent")
        _seed_session(
            conn,
            "continuation",
            parent_id="physical-parent",
            source="subagent",
            model_config={"_delegate_from": "compression-parent"},
        )
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(
        tmp_path, {"compression-parent", "physical-parent", "continuation"}
    )

    import api.profiles
    from api.models import delete_cli_session

    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    assert delete_cli_session("compression-parent") is True

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            ("continuation",),
        ).fetchone() == ("physical-parent",)
        assert conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ?", ("continuation",)
        ).fetchone() == (1,)
    finally:
        conn.close()
    _assert_all_artifacts_exist(sessions_dir, {"continuation"})


def test_delete_cli_session_manifest_publish_failure_rolls_back(
    tmp_path, monkeypatch
):
    """A missing retry record must prevent the DB delete from committing."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "victim")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"victim"})

    import api.profiles
    from api.models import delete_cli_session

    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    original_write_text = Path.write_text

    def _deny_manifest_publish(path, *args, **kwargs):
        if path.name.startswith(".cleanup_manifest_") and path.suffix == ".tmp":
            raise PermissionError("manifest directory is read-only")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _deny_manifest_publish)
    assert delete_cli_session("victim") is False

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", ("victim",)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ?", ("victim",)
        ).fetchone() == (1,)
    finally:
        conn.close()
    _assert_all_artifacts_exist(sessions_dir, {"victim"})
    assert not list(sessions_dir.glob(".cleanup_manifest_*.json"))
    assert not list(sessions_dir.glob(".cleanup_manifest_*.tmp"))


@pytest.mark.parametrize("payload", ['["orphan"', '{"orphan": true}', '[123]'])
def test_delete_cli_session_preserves_malformed_manifest_and_reports_failure(
    tmp_path, monkeypatch, payload
):
    """Unknown pending IDs remain recoverable and make cleanup incomplete."""
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, started_at TEXT,
                source TEXT, parent_session_id TEXT, model_config TEXT,
                end_reason TEXT, ended_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, timestamp TEXT NOT NULL
            );
            """
        )
        _seed_session(conn, "victim")
        conn.commit()
    finally:
        conn.close()

    sessions_dir = _seed_transcript_artifacts(tmp_path, {"victim", "orphan"})
    malformed = sessions_dir / ".cleanup_manifest_truncated.json"
    malformed.write_text(payload, encoding="utf-8")

    import api.profiles
    from api.models import delete_cli_session

    monkeypatch.setattr(
        api.profiles, "get_active_hermes_home", lambda: str(tmp_path)
    )
    assert delete_cli_session("victim") is False

    assert malformed.exists()
    assert malformed.read_text(encoding="utf-8") == payload
    _assert_all_artifacts_exist(sessions_dir, {"orphan"})
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", ("victim",)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ?", ("victim",)
        ).fetchone() is None
    finally:
        conn.close()
