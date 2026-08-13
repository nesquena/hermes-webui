"""Read-only session lineage report endpoint tests."""

import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import api.agent_sessions as agent_sessions
import api.models as models
import api.routes as routes


def _ensure_state_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    return conn


def _insert_state_row(conn, sid, *, parent=None, ended_at=None, end_reason=None, started_at=None, source="webui", session_source=None):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
        VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?)
        """,
        (sid, source, session_source, sid.replace("_", " "), started_at or time.time(), parent, ended_at, end_reason),
    )
    conn.commit()


def _insert_message(conn, sid, *, timestamp=None, role="user"):
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'hello', ?)",
        (f"msg_{sid}_{role}", sid, role, timestamp or time.time()),
    )
    conn.commit()


def _ensure_production_state_db(path):
    """Create the current Agent lineage shape (not the synthetic fork schema)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            model_config TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL
        );
        """
    )
    return conn


def _insert_production_row(
    conn,
    sid,
    *,
    parent=None,
    started_at,
    ended_at=None,
    end_reason=None,
    model_config=None,
    source="webui",
    title=None,
):
    if isinstance(model_config, dict):
        model_config = json.dumps(model_config)
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, title, model, model_config, started_at, message_count,
         parent_session_id, ended_at, end_reason)
        VALUES (?, ?, ?, 'openai/gpt-5', ?, ?, 1, ?, ?, ?)
        """,
        (sid, source, title or sid, model_config, started_at, parent, ended_at, end_reason),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        (sid, f"message:{sid}", started_at),
    )
    conn.commit()


def test_lineage_report_returns_bounded_read_only_tip_and_hidden_segments(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_mid", parent="lineage_report_root", started_at=t0 + 6, ended_at=t0 + 12, end_reason="cli_close")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_mid", started_at=t0 + 13)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip")

        assert report["mutation"] is False
        assert report["session_id"] == "lineage_report_tip"
        assert report["lineage_key"] == "lineage_report_root"
        assert report["tip_session_id"] == "lineage_report_tip"
        assert report["total_segments"] == 3
        assert report["materialized_segments"] == 3
        assert [s["session_id"] for s in report["segments"]] == [
            "lineage_report_tip",
            "lineage_report_mid",
            "lineage_report_root",
        ]
        assert [s["role"] for s in report["segments"]] == ["tip", "hidden_segment", "hidden_segment"]
        assert report["children"] == []
        assert report["manual_review"] is False
        assert "archive_candidates" not in report
        assert "delete_candidates" not in report
    finally:
        conn.close()


def test_compression_persist_race_still_collapses_to_one_lineage(tmp_path):
    """A child may be inserted just before its compression parent is closed."""
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(
            conn,
            "compression_race_root",
            started_at=t0,
            ended_at=t0 + 5.020,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "compression_race_tip",
            parent="compression_race_root",
            started_at=t0 + 5.000,
        )
        _insert_message(conn, "compression_race_tip", timestamp=t0 + 6)

        report = agent_sessions.read_session_lineage_report(
            tmp_path / "state.db", "compression_race_tip"
        )
        rows = agent_sessions.read_importable_agent_session_rows(
            tmp_path / "state.db", exclude_sources=()
        )

        assert report["lineage_key"] == "compression_race_root"
        assert report["tip_session_id"] == "compression_race_tip"
        assert [segment["session_id"] for segment in report["segments"]] == [
            "compression_race_tip",
            "compression_race_root",
        ]
        assert [row["id"] for row in rows] == ["compression_race_tip"]
        assert rows[0]["_lineage_root_id"] == "compression_race_root"
        assert rows[0]["_compression_segment_count"] == 2
    finally:
        conn.close()


def test_production_identity_metadata_keeps_branches_out_of_raced_compression_lineage(
    tmp_path, monkeypatch
):
    """A slow production handoff stitches only after identity/source guards pass."""
    db_path = tmp_path / "state.db"
    conn = _ensure_production_state_db(db_path)
    root_id = "67731d41a751"
    middle_id = "20260809_092619_1df4e5"
    middle2_id = "20260809_153027_ecd2d0"
    tip_id = "20260811_163454_515676"
    branch_id = "production_branch"
    delegate_id = "production_delegate"
    malformed_id = "malformed_identity"
    ambiguous_id = "ambiguous_identity"
    foreign_identity_id = "foreign_identity"
    inherited_identity = {"_branched_from": "pre_compression_origin"}
    try:
        _insert_production_row(
            conn,
            root_id,
            started_at=1000.0,
            ended_at=1100.041825,
            end_reason="compression",
            model_config=inherited_identity,
        )
        _insert_production_row(
            conn,
            middle_id,
            parent=root_id,
            started_at=1100.0,
            ended_at=1200.031181,
            end_reason="compression",
            model_config=inherited_identity,
        )
        _insert_production_row(
            conn,
            middle2_id,
            parent=middle_id,
            started_at=1200.0,
            ended_at=1300.042504,
            end_reason="compression",
            model_config=inherited_identity,
        )
        _insert_production_row(
            conn,
            tip_id,
            parent=middle2_id,
            # Publishing a large handoff can take longer than one second before
            # the parent closure is stamped. The Agent contract has no elapsed
            # wall-clock cutoff once the physical and identity guards validate.
            started_at=1290.0,
            model_config=inherited_identity,
        )
        _insert_production_row(
            conn,
            branch_id,
            parent=middle2_id,
            started_at=1300.02,
            model_config={"_branched_from": middle2_id},
        )
        _insert_production_row(
            conn,
            delegate_id,
            parent=middle2_id,
            started_at=1300.02,
            model_config={"_delegate_from": middle2_id},
        )
        _insert_production_row(
            conn,
            malformed_id,
            parent=middle2_id,
            started_at=1300.02,
            model_config='{"_branched_from":',
        )
        _insert_production_row(
            conn,
            ambiguous_id,
            parent=middle2_id,
            started_at=1300.02,
            model_config={
                "_branched_from": middle2_id,
                "_delegate_from": middle2_id,
            },
        )
        _insert_production_row(
            conn,
            foreign_identity_id,
            parent=middle2_id,
            started_at=1300.02,
            model_config={"_branched_from": "unrelated_lineage"},
        )
        rows = agent_sessions.read_importable_agent_session_rows(
            db_path, limit=None, exclude_sources=()
        )
        rows_by_id = {row["id"]: row for row in rows}
        assert set(rows_by_id) == {
            tip_id,
            branch_id,
            delegate_id,
            malformed_id,
            ambiguous_id,
            foreign_identity_id,
        }
        assert rows_by_id[tip_id]["_lineage_root_id"] == root_id
        assert rows_by_id[tip_id]["_lineage_tip_id"] == tip_id
        assert rows_by_id[tip_id]["_compression_segment_count"] == 4
        for independent_id in (
            branch_id,
            delegate_id,
            malformed_id,
            ambiguous_id,
            foreign_identity_id,
        ):
            assert rows_by_id[independent_id]["relationship_type"] == "child_session"
            assert "_lineage_root_id" not in rows_by_id[independent_id]
            assert "model_config" not in rows_by_id[independent_id]
            assert "_lineage_model_config" not in rows_by_id[independent_id]

        tip_report = agent_sessions.read_session_lineage_report(db_path, tip_id)
        assert tip_report["lineage_key"] == root_id
        assert tip_report["tip_session_id"] == tip_id
        assert tip_report["total_segments"] == 4
        assert [segment["session_id"] for segment in tip_report["segments"]] == [
            tip_id,
            middle2_id,
            middle_id,
            root_id,
        ]
        for independent_id in (
            branch_id,
            delegate_id,
            malformed_id,
            ambiguous_id,
            foreign_identity_id,
        ):
            report = agent_sessions.read_session_lineage_report(db_path, independent_id)
            assert report["lineage_key"] == independent_id
            assert report["total_segments"] == 1

        metadata = agent_sessions.read_session_lineage_metadata(
            db_path,
            {
                root_id,
                middle_id,
                middle2_id,
                tip_id,
                branch_id,
                delegate_id,
                malformed_id,
                ambiguous_id,
                foreign_identity_id,
            },
        )
        assert metadata[tip_id]["_lineage_root_id"] == root_id
        assert metadata[tip_id]["_lineage_tip_id"] == tip_id
        for stale_id in (root_id, middle_id, middle2_id):
            assert metadata[stale_id]["_lineage_tip_id"] == tip_id
        for independent_id in (
            branch_id,
            delegate_id,
            malformed_id,
            ambiguous_id,
            foreign_identity_id,
        ):
            assert metadata[independent_id]["relationship_type"] == "child_session"
            assert "_lineage_root_id" not in metadata[independent_id]

        monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
        assert [
            message["content"]
            for message in models.get_state_db_session_messages(
                tip_id, stitch_continuations=True
            )
        ] == [
            f"message:{root_id}",
            f"message:{middle_id}",
            f"message:{middle2_id}",
            f"message:{tip_id}",
        ]
        for independent_id in (
            branch_id,
            delegate_id,
            malformed_id,
            ambiguous_id,
            foreign_identity_id,
        ):
            assert [
                message["content"]
                for message in models.get_state_db_session_messages(
                    independent_id, stitch_continuations=True
                )
            ] == [f"message:{independent_id}"]
    finally:
        conn.close()


def test_state_db_transcript_stitch_keeps_overlapping_synthetic_fork_independent(
    tmp_path, monkeypatch
):
    """Synthetic fork identity must reach the shared continuation predicate."""
    db_path = tmp_path / "state.db"
    conn = _ensure_state_db(db_path)
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN model_config TEXT")
        _insert_state_row(
            conn,
            "parent",
            started_at=100.0,
            ended_at=200.0,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "fork",
            parent="parent",
            started_at=199.5,
            session_source="fork",
        )
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', ?, ?)",
            [
                ("parent-message", "parent", "PARENT_MUST_NOT_STITCH", 150.0),
                ("fork-message", "fork", "FORK_ONLY", 201.0),
            ],
        )
        conn.commit()

        monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

        assert [
            message["content"]
            for message in models.get_state_db_session_messages(
                "fork", stitch_continuations=True
            )
        ] == ["FORK_ONLY"]
    finally:
        conn.close()


def test_live_title_families_collapse_and_read_back_from_every_stale_segment(
    tmp_path, monkeypatch
):
    """Sanitized 2026-08-11 lineages keep one row, one tip, and full history."""
    db_path = tmp_path / "state.db"
    conn = _ensure_production_state_db(db_path)
    families = [
        (
            "Autopilote horaire des conversations Hermes",
            [
                ("67731d41a751", 1786199393.152380, 1786260379.268876),
                ("20260809_092619_1df4e5", 1786260379.227051, 1786282227.910058),
                ("20260809_153027_ecd2d0", 1786282227.878877, 1786458894.936033),
                ("20260811_163454_515676", 1786458894.893529, 1786462423.932897),
                ("20260811_173343_35b01a", 1786462423.579563, 1786466746.646338),
                ("20260811_184546_6a9d43", 1786466746.605288, None),
            ],
        ),
        (
            "Continuing Session Compaction Analysis",
            [
                ("5614899f20c1", 1785149596.191732, 1785151212.680752),
                ("20260727_132012_cfd871", 1785151212.659361, 1785153940.767830),
                ("20260727_140540_d53386", 1785153940.623974, 1785155431.781549),
                ("20260727_143031_ef4b12", 1785155431.697044, 1785156223.920522),
                ("20260727_144343_257512", 1785156223.857336, 1785160956.350932),
                ("20260727_160236_15a54c", 1785160956.085989, 1785161310.475170),
                ("20260727_160830_ac2f3c", 1785161310.350187, 1785162060.158157),
                ("20260727_162059_4f40a6", 1785162059.967021, 1785167813.575161),
                ("20260727_175653_f3e1b1", 1785167813.496525, 1785187411.139954),
                ("20260727_232331_c5c5d0", 1785187411.110537, 1785190793.448982),
                ("20260728_001953_9256d5", 1785190793.410722, 1785232651.865177),
                ("20260728_115731_6ac7d1", 1785232651.368463, 1785666018.332939),
                ("20260802_122018_e27695", 1785666018.125275, 1786262452.129295),
                ("20260809_100052_0e8b4c", 1786262452.047742, 1786459582.710381),
                ("20260811_164622_304865", 1786459582.685995, 1786466779.658785),
                ("20260811_184619_9bf3d0", 1786466779.633715, None),
            ],
        ),
        (
            "Résolution des conflits et validation PR (fork)",
            [
                ("06fc4aac7a3d", 1785144260.375660, 1785144379.690550),
                ("20260727_112619_043c7e", 1785144379.661809, 1785151726.380666),
                ("20260727_132846_261925", 1785151726.347790, 1785153397.592452),
                ("20260727_135637_389b2a", 1785153397.417235, 1785154116.375054),
                ("20260727_140836_a1ad57", 1785154116.293680, 1785162081.853102),
                ("20260727_162121_bfe2e2", 1785162081.782784, 1785166760.640480),
                ("20260727_173920_c3c7e9", 1785166760.623645, 1785234994.808266),
                ("20260728_123634_02f377", 1785234994.789763, 1785235430.425519),
                ("20260728_124350_f27aaf", 1785235430.346803, 1785316501.566468),
                ("20260729_111501_4bfbc4", 1785316501.487571, 1785428575.902096),
                ("20260730_182255_cb8af6", 1785428575.842911, 1785693684.900629),
                ("20260802_200124_2ae2ef", 1785693684.862480, 1786221839.421037),
                ("20260808_224359_22465b", 1786221839.363932, 1786266062.115920),
                ("20260809_110101_cc59c5", 1786266061.908550, 1786459562.448499),
                ("20260811_164602_0577ed", 1786459562.407618, None),
            ],
        ),
    ]
    try:
        for title, segments in families:
            parent = None
            for index, (sid, started_at, ended_at) in enumerate(segments):
                _insert_production_row(
                    conn,
                    sid,
                    parent=parent,
                    started_at=started_at,
                    ended_at=ended_at,
                    end_reason="compression" if ended_at is not None else None,
                    title=title if index == 0 else None,
                )
                parent = sid

        rows = agent_sessions.read_importable_agent_session_rows(
            db_path, limit=None, exclude_sources=()
        )
        rows_by_id = {row["id"]: row for row in rows}
        assert set(rows_by_id) == {segments[-1][0] for _, segments in families}

        all_ids = {sid for _, segments in families for sid, _, _ in segments}
        metadata = agent_sessions.read_session_lineage_metadata(db_path, all_ids)
        monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

        for title, segments in families:
            root_id = segments[0][0]
            tip_id = segments[-1][0]
            tip_row = rows_by_id[tip_id]
            assert tip_row["title"] == title
            assert tip_row["_lineage_root_id"] == root_id
            assert tip_row["_lineage_tip_id"] == tip_id
            assert tip_row["_compression_segment_count"] == len(segments)
            assert tip_row.get("relationship_type") != "child_session"

            report = agent_sessions.read_session_lineage_report(db_path, tip_id)
            assert report["lineage_key"] == root_id
            assert report["tip_session_id"] == tip_id
            assert report["total_segments"] == len(segments)
            assert report["children"] == []

            for sid, _, _ in segments:
                assert metadata[sid]["_lineage_root_id"] == root_id
                assert metadata[sid]["_lineage_tip_id"] == tip_id

            assert [
                message["content"]
                for message in models.get_state_db_session_messages(
                    tip_id, stitch_continuations=True
                )
            ] == [f"message:{sid}" for sid, _, _ in segments]
    finally:
        conn.close()


def test_live_cli_mislabeled_tip_stays_outside_webui_lineage_until_repaired(
    tmp_path, monkeypatch
):
    """Do not weaken the cross-source guard to absorb an autopilot source defect."""
    db_path = tmp_path / "state.db"
    conn = _ensure_production_state_db(db_path)
    try:
        _insert_production_row(
            conn,
            "20260811_164622_304865",
            started_at=1786459582.685995,
            ended_at=1786466779.658785,
            end_reason="compression",
        )
        _insert_production_row(
            conn,
            "20260811_184619_9bf3d0",
            parent="20260811_164622_304865",
            started_at=1786466779.633715,
            source="cli",
        )

        rows = agent_sessions.read_importable_agent_session_rows(
            db_path, limit=None, exclude_sources=()
        )
        assert {row["id"] for row in rows} == {
            "20260811_164622_304865",
            "20260811_184619_9bf3d0",
        }

        monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
        assert [
            message["content"]
            for message in models.get_state_db_session_messages(
                "20260811_184619_9bf3d0", stitch_continuations=True
            )
        ] == ["message:20260811_184619_9bf3d0"]
    finally:
        conn.close()


def test_jouvence_compression_collapse_preserves_three_real_delegate_children(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    conn = _ensure_production_state_db(db_path)
    root_id = "20260802_122018_e27695"
    tip_id = "20260809_100052_0e8b4c"
    delegate_ids = [f"jouvence_delegate_{index}" for index in range(3)]
    try:
        _insert_production_row(
            conn,
            root_id,
            started_at=1000.0,
            ended_at=2000.081553,
            end_reason="compression",
        )
        _insert_production_row(conn, tip_id, parent=root_id, started_at=2000.0)
        for index, delegate_id in enumerate(delegate_ids):
            _insert_production_row(
                conn,
                delegate_id,
                parent=root_id,
                started_at=1500.0 + index,
                source="subagent",
                model_config={"_delegate_from": root_id},
            )

        rows = agent_sessions.read_importable_agent_session_rows(
            db_path, limit=None, exclude_sources=()
        )
        assert [row["id"] for row in rows if row.get("relationship_type") != "child_session"] == [
            tip_id
        ]
        tip_row = next(row for row in rows if row["id"] == tip_id)
        assert tip_row["_lineage_root_id"] == root_id
        assert tip_row["_lineage_tip_id"] == tip_id
        assert tip_row["_compression_segment_count"] == 2
        child_rows = [row for row in rows if row.get("relationship_type") == "child_session"]
        assert {row["id"] for row in child_rows} == set(delegate_ids)
        assert all(row["parent_session_id"] == root_id for row in child_rows)

        report = agent_sessions.read_session_lineage_report(db_path, tip_id)
        assert report["lineage_key"] == root_id
        assert report["tip_session_id"] == tip_id
        assert report["total_segments"] == 2
        assert {child["session_id"] for child in report["children"]} == set(delegate_ids)

        metadata = agent_sessions.read_session_lineage_metadata(
            db_path, {root_id, tip_id, *delegate_ids}
        )
        assert metadata[root_id]["_lineage_tip_id"] == tip_id
        assert metadata[tip_id]["_lineage_tip_id"] == tip_id
        assert all(
            metadata[delegate_id]["relationship_type"] == "child_session"
            for delegate_id in delegate_ids
        )

        monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
        assert [
            message["content"]
            for message in models.get_state_db_session_messages(
                tip_id, stitch_continuations=True
            )
        ] == [f"message:{root_id}", f"message:{tip_id}"]
    finally:
        conn.close()


def test_cli_close_overlap_remains_a_child_session(tmp_path):
    """Only automatic compression may overlap the parent's close timestamp."""
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(
            conn,
            "cli_overlap_parent",
            started_at=t0,
            ended_at=t0 + 5.020,
            end_reason="cli_close",
        )
        _insert_state_row(
            conn,
            "cli_overlap_child",
            parent="cli_overlap_parent",
            started_at=t0 + 5.000,
        )

        child_report = agent_sessions.read_session_lineage_report(
            tmp_path / "state.db", "cli_overlap_child"
        )
        parent_report = agent_sessions.read_session_lineage_report(
            tmp_path / "state.db", "cli_overlap_parent"
        )

        assert child_report["lineage_key"] == "cli_overlap_child"
        assert [segment["session_id"] for segment in child_report["segments"]] == [
            "cli_overlap_child"
        ]
        assert [child["session_id"] for child in parent_report["children"]] == [
            "cli_overlap_child"
        ]
    finally:
        conn.close()


def test_guarded_compression_handoff_has_no_wall_clock_cutoff():
    parent = {
        "id": "compression_parent",
        "source": "webui",
        "ended_at": 200.0,
        "end_reason": "compression",
    }

    assert agent_sessions._is_continuation_session(
        parent,
        {
            "id": "slow_handoff",
            "source": "webui",
            "parent_session_id": "compression_parent",
            "started_at": 150.0,
        },
    )


def test_lineage_report_keeps_cross_surface_parent_out_of_hidden_segments(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(
            conn,
            "lineage_report_telegram_parent",
            source="telegram",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_report_webui_tip",
            source="webui",
            parent="lineage_report_telegram_parent",
            started_at=t0 + 6,
        )

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_webui_tip")

        assert report["lineage_key"] == "lineage_report_webui_tip"
        assert report["total_segments"] == 1
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_webui_tip"]
        assert report["segments"][0]["role"] == "tip"
        assert report["children"] == []
    finally:
        conn.close()


def test_lineage_report_keeps_explicit_forks_out_of_hidden_segments(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(
            conn,
            "lineage_report_fork",
            parent="lineage_report_root",
            started_at=t0 + 6,
            session_source="fork",
        )

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_fork")

        assert report["lineage_key"] == "lineage_report_fork"
        assert report["tip_session_id"] == "lineage_report_fork"
        assert report["total_segments"] == 1
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_fork"]
        assert report["segments"][0]["role"] == "tip"
        assert report["children"] == []
        assert report["manual_review"] is False
    finally:
        conn.close()


def test_importable_agent_projection_keeps_explicit_forks_out_of_compression_lineage(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(
            conn,
            "lineage_report_fork",
            parent="lineage_report_root",
            started_at=t0 + 6,
            session_source="fork",
        )
        _insert_message(conn, "lineage_report_fork", timestamp=t0 + 7)

        rows = agent_sessions.read_importable_agent_session_rows(tmp_path / "state.db", exclude_sources=())

        assert [row["id"] for row in rows] == ["lineage_report_fork"]
        fork = rows[0]
        assert fork.get("relationship_type") == "child_session"
        assert fork.get("parent_session_id") == "lineage_report_root"
        assert fork.get("_parent_lineage_root_id") == "lineage_report_root"
        assert "_lineage_root_id" not in fork
        assert "_compression_segment_count" not in fork
    finally:
        conn.close()


def test_lineage_report_surfaces_non_continuation_children_without_mutation(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_root", started_at=t0 + 6, ended_at=t0 + 15, end_reason="user_stop")
        _insert_state_row(conn, "lineage_report_child", parent="lineage_report_tip", started_at=t0 + 8)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip")

        assert report["lineage_key"] == "lineage_report_root"
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_tip", "lineage_report_root"]
        assert report["children"] == [
            {
                "session_id": "lineage_report_child",
                "role": "child_session",
                "title": "lineage report child",
                "source": "webui",
                "started_at": t0 + 8,
                "updated_at": t0 + 8,
                "end_reason": None,
                "active": True,
                "archived": False,
            }
        ]
        assert report["mutation"] is False
    finally:
        conn.close()


def test_lineage_report_marks_bounded_parent_walk_for_manual_review(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_mid", parent="lineage_report_root", started_at=t0 + 6, ended_at=t0 + 12, end_reason="compression")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_mid", started_at=t0 + 13)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip", max_hops=1)

        assert report["mutation"] is False
        assert report["manual_review"] is True
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_tip", "lineage_report_mid"]
        assert report["total_segments"] == 2
    finally:
        conn.close()


def test_lineage_report_endpoint_is_read_only_and_uses_active_state_db(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_root", started_at=t0 + 6)
        captured = {}

        def fake_j(handler, data, status=200, **_kwargs):
            captured["status"] = status
            captured["data"] = data
            return data

        handler = SimpleNamespace()
        parsed = urlparse("/api/session/lineage/report?session_id=lineage_report_tip")
        with patch.object(routes, "_active_state_db_path", return_value=tmp_path / "state.db"), patch.object(routes, "j", side_effect=fake_j):
            routes.handle_get(handler, parsed)

        assert captured["status"] == 200
        assert captured["data"]["mutation"] is False
        assert captured["data"]["lineage_key"] == "lineage_report_root"
        assert captured["data"]["total_segments"] == 2
    finally:
        conn.close()


def test_lineage_report_endpoint_returns_404_for_unknown_session(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    conn.close()
    captured = {}

    def fake_bad(handler, message, status=400):
        captured["status"] = status
        captured["message"] = message
        return {"error": message}

    handler = SimpleNamespace()
    parsed = urlparse("/api/session/lineage/report?session_id=missing_lineage_report_session")
    with patch.object(routes, "_active_state_db_path", return_value=tmp_path / "state.db"), patch.object(routes, "bad", side_effect=fake_bad):
        routes.handle_get(handler, parsed)

    assert captured == {"status": 404, "message": "Session not found"}


def test_lineage_report_preserves_child_order_for_each_segment_parent(tmp_path):
    """Behavioural coverage for batched child fetch: started_at DESC per parent."""
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 200
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(
            conn,
            "lineage_report_tip",
            parent="lineage_report_root",
            started_at=t0 + 6,
            ended_at=t0 + 15,
            end_reason="user_stop",
        )
        _insert_state_row(conn, "lineage_report_child_old", parent="lineage_report_tip", started_at=t0 + 8)
        _insert_state_row(conn, "lineage_report_child_new", parent="lineage_report_tip", started_at=t0 + 20)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip")

        assert [child["session_id"] for child in report["children"]] == [
            "lineage_report_child_new",
            "lineage_report_child_old",
        ]
    finally:
        conn.close()
