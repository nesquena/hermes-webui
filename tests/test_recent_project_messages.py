"""Contract tests for bounded, profile-scoped project message reads."""

import json
import sqlite3

import pytest

from api.project_context import recent_project_messages


PROJECT_ID = "project00001"
PROFILE = "research"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sidecar(path, sid, *, project_id=PROJECT_ID, profile=PROFILE, archived=False, title=None, workspace=None):
    # Keep the transcript deliberately after the metadata fields. The read path
    # must stop before it rather than scanning full session transcripts.
    _write_json(
        path / f"{sid}.json",
        {
            "session_id": sid,
            "title": title or f"Title {sid}",
            "workspace": workspace or f"/work/{sid}",
            "created_at": 1.0,
            "updated_at": 2.0,
            "archived": archived,
            "project_id": project_id,
            "profile": profile,
            "messages": [{"role": "user", "content": "must not be read"}] * 100,
        },
    )


@pytest.fixture
def project_store(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    projects_file = tmp_path / "projects.json"
    index_file = session_dir / "_index.json"
    state_db = tmp_path / "state.db"
    _write_json(
        projects_file,
        [{"project_id": PROJECT_ID, "name": "Example", "profile": PROFILE}],
    )
    _write_json(index_file, [])
    with sqlite3.connect(state_db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                started_at REAL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL
            );
            CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
            """
        )
    return {
        "projects_file": projects_file,
        "session_index_file": index_file,
        "session_dir": session_dir,
        "state_db_path": state_db,
    }


def _seed(store, sessions, messages):
    index = []
    with sqlite3.connect(store["state_db_path"]) as conn:
        for session in sessions:
            sid = session["session_id"]
            index.append(
                {
                    "session_id": sid,
                    "title": session.get("title", f"Title {sid}"),
                    "workspace": session.get("workspace", f"/work/{sid}"),
                    "project_id": session.get("project_id", PROJECT_ID),
                    "profile": session.get("profile", PROFILE),
                    "archived": session.get("archived", False),
                }
            )
            _sidecar(
                store["session_dir"],
                sid,
                project_id=session.get("sidecar_project_id", session.get("project_id", PROJECT_ID)),
                profile=session.get("sidecar_profile", session.get("profile", PROFILE)),
                archived=session.get("archived", False),
                title=session.get("title"),
                workspace=session.get("workspace"),
            )
            conn.execute(
                "INSERT INTO sessions (id, source, title, started_at) VALUES (?, ?, ?, ?)",
                (sid, session.get("source", "webui"), session.get("title", f"Title {sid}"), 1.0),
            )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            messages,
        )
    _write_json(store["session_index_file"], index)


def _read(store, **kwargs):
    return recent_project_messages(
        project_id=PROJECT_ID,
        profile=PROFILE,
        **store,
        **kwargs,
    )


def test_latest_messages_are_global_across_three_sessions_with_stable_ties(project_store):
    _seed(
        project_store,
        [{"session_id": "session_a"}, {"session_id": "session_b"}, {"session_id": "session_c"}],
        [
            ("session_a", "user", "a-old", 10.0),
            ("session_b", "user", "b-new", 30.0),
            ("session_a", "user", "a-new", 20.0),
            ("session_c", "user", "c-tie", 30.0),
            ("session_c", "user", "c-old", 15.0),
        ],
    )

    result = _read(project_store, limit=5)

    assert [row["content"] for row in result["messages"]] == [
        "c-tie",
        "b-new",
        "a-new",
        "c-old",
        "a-old",
    ]
    assert all(
        set(row) == {
            "timestamp",
            "role",
            "content",
            "session_id",
            "session_title",
            "workspace",
            "profile",
            "project_id",
        }
        for row in result["messages"]
    )
    assert result["order"] == "timestamp_desc_session_id_desc_message_id_desc"


def test_roles_default_to_user_and_reject_privileged_roles(project_store):
    _seed(
        project_store,
        [{"session_id": "session_a"}],
        [
            ("session_a", "user", "question", 1.0),
            ("session_a", "assistant", "answer", 2.0),
            ("session_a", "tool", "raw tool payload", 3.0),
            ("session_a", "system", "system prompt", 4.0),
        ],
    )

    assert [m["content"] for m in _read(project_store)["messages"]] == ["question"]
    assert [m["content"] for m in _read(project_store, roles=["user", "assistant"])["messages"]] == [
        "answer",
        "question",
    ]
    with pytest.raises(ValueError, match="roles"):
        _read(project_store, roles=["tool"])


def test_message_content_and_title_are_always_credential_redacted(project_store, monkeypatch):
    _seed(
        project_store,
        [{"session_id": "session_a", "title": "secret title"}],
        [("session_a", "user", "secret content", 1.0)],
    )
    calls = []

    def fake_redact(value, *, _enabled=None):
        calls.append((value, _enabled))
        return value.replace("secret", "[REDACTED]")

    monkeypatch.setattr("api.project_context._redact_text", fake_redact)

    result = _read(project_store)

    assert result["messages"][0]["content"] == "[REDACTED] content"
    assert result["messages"][0]["session_title"] == "[REDACTED] title"
    assert calls == [("secret content", True), ("secret title", True)]


def test_profile_and_project_ownership_fail_closed(project_store):
    _seed(
        project_store,
        [
            {"session_id": "owned"},
            {"session_id": "foreign", "profile": "other", "sidecar_profile": "other"},
            {"session_id": "unassigned", "project_id": None, "sidecar_project_id": None},
        ],
        [
            ("owned", "user", "owned text", 1.0),
            ("foreign", "user", "foreign text", 3.0),
            ("unassigned", "user", "unassigned text", 2.0),
        ],
    )

    result = _read(project_store)
    assert [m["content"] for m in result["messages"]] == ["owned text"]

    with pytest.raises(LookupError, match="Project not found"):
        recent_project_messages(
            project_id=PROJECT_ID,
            profile="other",
            **project_store,
        )


def test_missing_or_disagreeing_sidecars_are_excluded_with_count_only_diagnostics(project_store):
    _seed(
        project_store,
        [
            {"session_id": "valid"},
            {"session_id": "missing"},
            {"session_id": "mismatch", "sidecar_project_id": "different0001"},
        ],
        [
            ("valid", "user", "safe", 1.0),
            ("missing", "user", "must not leak missing", 3.0),
            ("mismatch", "user", "must not leak mismatch", 2.0),
        ],
    )
    (project_store["session_dir"] / "missing.json").unlink()

    result = _read(project_store)

    assert [m["content"] for m in result["messages"]] == ["safe"]
    assert result["partial"] is True
    assert result["diagnostics"]["missing_sidecars"] == 1
    assert result["diagnostics"]["membership_mismatches"] == 1
    assert "missing" not in result["diagnostics"].values()
    assert "mismatch" not in result["diagnostics"].values()


def test_state_db_disagreement_is_excluded_without_exposing_session_identity(project_store):
    _seed(
        project_store,
        [{"session_id": "valid"}, {"session_id": "db_missing"}],
        [("valid", "user", "safe", 1.0)],
    )
    with sqlite3.connect(project_store["state_db_path"]) as conn:
        conn.execute("DELETE FROM sessions WHERE id = 'db_missing'")

    result = _read(project_store)

    assert [m["content"] for m in result["messages"]] == ["safe"]
    assert result["partial"] is True
    assert result["diagnostics"]["missing_state_db_sessions"] == 1
    assert "db_missing" not in json.dumps(result["diagnostics"])


def test_explicit_classifier_excludes_cron_delegation_compaction_and_system_notices(project_store):
    _seed(
        project_store,
        [
            {"session_id": "regular"},
            {"session_id": "cron_session", "source": "cron"},
            {"session_id": "delegated", "source": "subagent"},
        ],
        [
            ("regular", "user", "genuine", 1.0),
            ("regular", "user", "[IMPORTANT: Background process done]", 9.0),
            ("regular", "user", "[ASYNC DELEGATION BATCH COMPLETE — x]", 8.0),
            ("regular", "user", "[context compaction: summary]", 7.0),
            ("regular", "user", "[Your active task list was preserved across context compression]", 6.0),
            ("regular", "user", "[Session arc summary: prior work]", 5.0),
            ("regular", "user", "[System: verify before finishing]", 4.0),
            ("regular", "user", "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.", 3.5),
            ("cron_session", "user", "cron prompt", 3.0),
            ("delegated", "user", "delegation goal", 2.0),
        ],
    )

    result = _read(project_store, limit=20)

    assert [m["content"] for m in result["messages"]] == ["genuine"]
    assert result["classifier"] == "project_context_v1"


def test_classifier_does_not_drop_plain_user_discussion_of_context_compaction(project_store):
    _seed(
        project_store,
        [{"session_id": "regular"}],
        [("regular", "user", "context compaction strategies are worth discussing", 1.0)],
    )

    assert [m["content"] for m in _read(project_store)["messages"]] == [
        "context compaction strategies are worth discussing"
    ]


def test_limit_and_opaque_before_cursor_page_without_overlap(project_store):
    _seed(
        project_store,
        [{"session_id": "session_a"}, {"session_id": "session_b"}],
        [
            ("session_a", "user", "a3", 3.0),
            ("session_a", "user", "a2", 2.0),
            ("session_a", "user", "a1", 1.0),
            ("session_b", "user", "b3", 3.0),
            ("session_b", "user", "b2", 2.0),
            ("session_b", "user", "b1", 1.0),
        ],
    )

    first = _read(project_store, limit=3)
    second = _read(project_store, limit=3, before=first["next_before"])

    assert [m["content"] for m in first["messages"]] == ["b3", "a3", "b2"]
    assert [m["content"] for m in second["messages"]] == ["a2", "b1", "a1"]
    assert set(m["content"] for m in first["messages"]).isdisjoint(
        m["content"] for m in second["messages"]
    )
    with pytest.raises(ValueError, match="before"):
        _read(project_store, before="not-a-valid-cursor")
    with pytest.raises(ValueError, match="before"):
        _read(project_store, roles=["assistant"], before=first["next_before"])


def test_archived_sessions_are_opt_in(project_store):
    _seed(
        project_store,
        [{"session_id": "active"}, {"session_id": "archived", "archived": True}],
        [
            ("active", "user", "active text", 1.0),
            ("archived", "user", "archived text", 2.0),
        ],
    )

    assert [m["content"] for m in _read(project_store)["messages"]] == ["active text"]
    assert [m["content"] for m in _read(project_store, include_archived=True)["messages"]] == [
        "archived text",
        "active text",
    ]


def test_large_corpus_reads_only_a_bounded_tail_per_eligible_session(project_store):
    sessions = [{"session_id": f"session_{i}"} for i in range(3)]
    messages = []
    for i in range(3):
        sid = f"session_{i}"
        messages.extend((sid, "user", f"{sid}-{n}", float(n)) for n in range(1000))
    _seed(project_store, sessions, messages)

    result = _read(project_store, limit=5)

    assert len(result["messages"]) == 5
    assert result["diagnostics"]["candidate_rows_read"] <= 26 * len(sessions)
    assert result["diagnostics"]["eligible_sessions"] == len(sessions)


def test_synthetic_tail_saturation_fails_bounded_and_reports_partial(project_store):
    _seed(
        project_store,
        [{"session_id": "session_a"}],
        [
            ("session_a", "user", "genuine but beyond the bounded window", 1.0),
            *[
                ("session_a", "user", f"[System: synthetic {i}]", float(i + 2))
                for i in range(25)
            ],
        ],
    )

    result = _read(project_store)

    assert result["messages"] == []
    assert result["partial"] is True
    assert result["diagnostics"]["classifier_scan_saturated_sessions"] == 1
    assert result["diagnostics"]["candidate_rows_read"] == 26


@pytest.mark.parametrize("index_value", [None, {"not": "a list"}])
def test_missing_or_malformed_index_reports_partial(project_store, index_value):
    if index_value is None:
        project_store["session_index_file"].unlink()
    else:
        _write_json(project_store["session_index_file"], index_value)

    result = _read(project_store)

    assert result["messages"] == []
    assert result["partial"] is True
    assert result["diagnostics"]["session_index_unavailable"] == 1


def test_malformed_and_non_finite_timestamps_fail_closed_per_row(project_store):
    _seed(
        project_store,
        [{"session_id": "session_a"}],
        [
            ("session_a", "user", "valid", 1.0),
            ("session_a", "user", "malformed", "not-a-timestamp"),
            ("session_a", "user", "non-finite", "NaN"),
        ],
    )

    result = _read(project_store)

    assert [m["content"] for m in result["messages"]] == ["valid"]
    assert result["partial"] is True
    assert result["diagnostics"]["invalid_timestamp_rows"] == 2


def test_limit_contract_is_default_five_and_max_twenty(project_store):
    _seed(
        project_store,
        [{"session_id": "session_a"}],
        [("session_a", "user", str(i), float(i)) for i in range(30)],
    )

    assert len(_read(project_store)["messages"]) == 5
    assert len(_read(project_store, limit=999)["messages"]) == 20
    with pytest.raises(ValueError, match="limit"):
        _read(project_store, limit=0)
