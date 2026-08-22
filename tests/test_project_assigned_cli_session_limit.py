"""Regression: project-assigned CLI sessions must survive the recent-session cap.

Two independent 20-session caps used to hide older project-assigned CLI/TUI
sessions while their project chip still claimed them. Recovering them must not
swing to the opposite failure, so these tests pin BOTH edges of the contract:

* an assigned conversation older than the recent window is still reachable
  (the original bug), including past the 200-conversation mark when the
  assignments are spread over several projects;
* one busy project cannot starve the others: a per-project budget applied to a
  single global recency window is not a per-project bound, so a saturated window
  is followed by project-scoped, per-project-budgeted queries (greptile P1);
* the follow-up is COMPLETE — every starved project is served, with no fixed
  per-build project cap that would permanently skip the rest (greptile P1);
* the recovered set is BOUNDED — per project, counting logical conversations —
  including assigned rows that arrive as imported WebUI sidecars and therefore
  never pass through any state.db cap;
* assigned rows do not spend the unassigned sidebar quota: 20 unique unassigned
  logical conversations survive after lineage/sidecar dedup;
* a deleted or cross-profile ``project_id`` cannot hide a session — it resolves
  to "unassigned" before either cap runs, instead of becoming a ``default_hidden``
  row with no chip left to reveal it;
* a project assignment recorded on a newer EMPTY compression continuation is
  applied to the lineage even when the root is the freshest importable segment.
"""

import json
import sqlite3

import pytest

from api import agent_sessions, models, routes

BASE_TS = 1700000000.0


def _session(
    sid,
    started_at,
    *,
    project_id=None,
    parent=None,
    ended_at=None,
    end_reason=None,
    source="cli",
    messages=1,
    title=None,
):
    """One state.db ``sessions`` row. Titled by default so the row is visible."""
    return {
        "id": sid,
        "title": sid if title is None else title,
        "model": "gpt-x",
        "message_count": messages,
        "started_at": started_at,
        "source": source,
        "project_id": project_id,
        "parent_session_id": parent,
        "ended_at": ended_at,
        "end_reason": end_reason,
        "messages": messages,
    }


def _write_state_db(db_path, rows, *, lineage_columns=True):
    """Create a minimal agent state.db from ``_session()`` rows."""
    columns = [
        "id", "title", "model", "message_count", "started_at", "source", "project_id",
    ]
    lineage_ddl = ""
    if lineage_columns:
        columns += ["parent_session_id", "ended_at", "end_reason"]
        lineage_ddl = ", parent_session_id TEXT, ended_at REAL, end_reason TEXT"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER, "
        f"started_at REAL, source TEXT, project_id TEXT{lineage_ddl})"
    )
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL, role TEXT)"
    )
    placeholders = ", ".join("?" for _ in columns)
    for row in rows:
        conn.execute(
            f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({placeholders})",
            [row.get(column) for column in columns],
        )
        for index in range(int(row.get("messages") or 0)):
            conn.execute(
                "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
                (row["id"], float(row["started_at"]) + 0.5 + index, "user"),
            )
    conn.commit()
    conn.close()


def _lineage(prefix, start_ts, segments, *, project_id=None, tip_project_id=None,
             step=10.0, messages=1):
    """A compression chain: ``segments`` rows, only the last one still open."""
    rows = []
    for index in range(segments):
        last = index == segments - 1
        started_at = start_ts + index * step
        rows.append(_session(
            f"{prefix}-seg{index}",
            started_at,
            project_id=(
                tip_project_id if last and tip_project_id is not None
                else project_id if index == 0
                else None
            ),
            parent=None if index == 0 else f"{prefix}-seg{index - 1}",
            ended_at=None if last else started_at + step / 2,
            end_reason=None if last else "compression",
            messages=messages,
        ))
    return rows


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Point get_cli_sessions() at a temporary HERMES_HOME + projects.json."""
    home = tmp_path / "hermes"
    home.mkdir()

    import api.config as cfg
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)
    # Keep root-profile aliasing hermetic: the real _is_root_profile falls back
    # to list_profiles_api(), which shells out to the agent CLI.
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    projects_file.write_text("[]", encoding="utf-8")

    models.clear_cli_sessions_cache()
    yield home
    models.clear_cli_sessions_cache()


def _register_projects(tmp_path, *project_ids, profile="default"):
    """Write ``project_ids`` into the projects.json the loader resolves against."""
    existing = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    existing.extend(
        {
            "project_id": project_id,
            "name": f"Project {project_id}",
            "color": "#6366f1",
            "profile": profile,
            "created_at": 1.0,
        }
        for project_id in project_ids
    )
    (tmp_path / "projects.json").write_text(json.dumps(existing), encoding="utf-8")
    models.clear_cli_sessions_cache()


# ── The original bug: an assigned session older than the recent window ────────


def test_project_assigned_cli_session_survives_recent_session_limit(
    fake_hermes_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-123")

    rows = [
        _session(f"recent-{index:02d}", BASE_TS + 100 + index)
        for index in range(25)
    ]
    rows.append(_session("older-assigned", BASE_TS, project_id="project-123"))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    by_id = {session["session_id"]: session for session in sessions}

    assert "older-assigned" in by_id
    assert by_id["older-assigned"]["project_id"] == "project-123"
    assert [session["session_id"] for session in sessions].count("older-assigned") == 1


@pytest.mark.parametrize(
    ("root_project_id", "tip_project_id"),
    [
        ("project-123", None),
        (None, "project-123"),
    ],
)
def test_project_assigned_compression_lineage_is_returned_once(
    fake_hermes_home,
    tmp_path,
    monkeypatch,
    root_project_id,
    tip_project_id,
):
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-123")

    rows = [
        _session(
            "lineage-root",
            BASE_TS,
            project_id=root_project_id,
            ended_at=BASE_TS + 1,
            end_reason="compression",
        ),
        _session("lineage-tip", BASE_TS + 200, project_id=tip_project_id, parent="lineage-root"),
    ]
    # Keep the tip inside the normal five-row window while pushing the root
    # outside its 8x SQL candidate window. This reproduces the real two-pass
    # boundary: the normal pass sees only the tip and the project pass must
    # bring enough lineage context to avoid appending the root separately.
    rows.extend(_session(f"newer-{index}", BASE_TS + 300 + index) for index in range(4))
    rows.extend(_session(f"middle-{index}", BASE_TS + 100 + index) for index in range(56))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    lineage_rows = [
        session
        for session in sessions
        if session["session_id"] in {"lineage-root", "lineage-tip"}
    ]

    assert len(lineage_rows) == 1
    assert lineage_rows[0]["session_id"] in {"lineage-root", "lineage-tip"}
    assert lineage_rows[0]["project_id"] == "project-123"


# ── Finding 1: the recovered assigned set must stay bounded ───────────────────


def test_assigned_conversations_are_bounded_per_project(
    fake_hermes_home, tmp_path, monkeypatch
):
    """250 assigned conversations in one project yield the newest 200, not 250."""
    monkeypatch.setattr(models, "PROJECT_ASSIGNED_CLI_LIMIT", 200)
    _register_projects(tmp_path, "project-a")

    rows = [
        _session(f"assigned-{index:04d}", BASE_TS + index, project_id="project-a")
        for index in range(250)
    ]
    rows.extend(
        _session(f"recent-{index:02d}", BASE_TS + 1000 + index) for index in range(25)
    )
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    assigned = [s for s in sessions if s["project_id"] == "project-a"]
    assigned_ids = {s["session_id"] for s in assigned}

    assert len(assigned) == 200
    # The bound drops the OLDEST assigned conversations, never a newer one.
    assert "assigned-0249" in assigned_ids
    assert "assigned-0050" in assigned_ids
    assert "assigned-0049" not in assigned_ids
    assert "assigned-0000" not in assigned_ids


def test_route_cap_bounds_imported_sidecar_assigned_rows(fake_hermes_home):
    """Imported CLI sidecars bypass every state.db cap (review finding 1).

    ``all_sessions()`` rows never pass through read_importable_agent_session_rows,
    so the merged payload is the only place that can bound them. 1,000 assigned
    rows used to yield 1,000 returned rows.
    """
    sidecars = [
        {
            "session_id": f"imported-{index:04d}",
            "is_cli_session": True,
            "project_id": "project-prime",
        }
        for index in range(1000)
    ]

    kept = routes._cap_recent_cli_sessions(sidecars)

    assert len(kept) == routes.CLI_PROJECT_ASSIGNED_CAP
    assert kept[0]["session_id"] == "imported-0000"
    # The recent window still renders normally; only the overflow is chip-only.
    visible = [row for row in kept if not row.get("default_hidden")]
    assert len(visible) == routes.CLI_VISIBLE_SESSION_CAP
    # Capping never mutates the caller's rows.
    assert not any("default_hidden" in row for row in sidecars)


def test_route_cap_bounds_each_project_independently(fake_hermes_home):
    """A busy project must not evict another project's history (greptile P1).

    The bound is per project, so a profile with more than
    ``CLI_PROJECT_ASSIGNED_CAP`` assigned conversations in TOTAL keeps them all.
    """
    rows = []
    for project in ("project-a", "project-b"):
        rows.extend(
            {
                "session_id": f"{project}-{index:04d}",
                "is_cli_session": True,
                "project_id": project,
            }
            for index in range(250)
        )

    kept = routes._cap_recent_cli_sessions(rows)

    counts: dict[str, int] = {}
    for row in kept:
        counts[row["project_id"]] = counts.get(row["project_id"], 0) + 1
    assert counts == {
        "project-a": routes.CLI_PROJECT_ASSIGNED_CAP,
        "project-b": routes.CLI_PROJECT_ASSIGNED_CAP,
    }
    assert len(kept) > routes.CLI_PROJECT_ASSIGNED_CAP


def test_more_than_two_hundred_assigned_conversations_survive_across_projects(
    fake_hermes_home, tmp_path
):
    """The model pass budgets per project too, not a single global 200."""
    _register_projects(tmp_path, "project-a", "project-b")

    rows = [
        _session(f"a-{index:04d}", BASE_TS + index, project_id="project-a")
        for index in range(150)
    ]
    rows.extend(
        _session(f"b-{index:04d}", BASE_TS + 500 + index, project_id="project-b")
        for index in range(150)
    )
    rows.extend(
        _session(f"recent-{index:02d}", BASE_TS + 2000 + index) for index in range(25)
    )
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    counts: dict[str, int] = {}
    for session in sessions:
        if session["project_id"]:
            counts[session["project_id"]] = counts.get(session["project_id"], 0) + 1

    # 300 assigned conversations total — a global 200 bound would silently drop
    # the 100 oldest, all of them project-a's.
    assert counts == {"project-a": 150, "project-b": 150}


def test_model_bounds_a_skewed_project_inside_a_wider_scan_window(
    fake_hermes_home, tmp_path
):
    """The per-project budget, not the query LIMIT, is what bounds a project.

    The recovery query asks for ``PROJECT_ASSIGNED_CLI_LIMIT * len(projects)``
    conversations, so with two registered projects its window is 400 — wide
    enough to hand back all 250 of project-a's on its own. Only the per-project
    accounting keeps project-a at 200, so a skewed distribution is the case that
    actually exercises it (a single-project profile has query_limit == the bound
    and would pass either way).
    """
    _register_projects(tmp_path, "project-a", "project-b")

    rows = [
        _session(f"a-{index:04d}", BASE_TS + index, project_id="project-a")
        for index in range(250)
    ]
    rows.extend(
        _session(f"b-{index:04d}", BASE_TS + 5000 + index, project_id="project-b")
        for index in range(5)
    )
    rows.extend(
        _session(f"recent-{index:02d}", BASE_TS + 9000 + index) for index in range(25)
    )
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    counts: dict[str, int] = {}
    for session in sessions:
        if session["project_id"]:
            counts[session["project_id"]] = counts.get(session["project_id"], 0) + 1
    assigned_ids = {s["session_id"] for s in sessions if s["project_id"] == "project-a"}

    assert counts == {"project-a": models.PROJECT_ASSIGNED_CLI_LIMIT, "project-b": 5}
    # The budget spends newest-first, so the dropped rows are the oldest 50.
    assert "a-0249" in assigned_ids
    assert "a-0050" in assigned_ids
    assert "a-0049" not in assigned_ids
    # The smaller project is untouched by its neighbour's overflow.
    assert {s["session_id"] for s in sessions if s["project_id"] == "project-b"} == {
        f"b-{index:04d}" for index in range(5)
    }


# ── greptile P1: a busy project must not starve the quieter ones ──────────────


def _assigned_counts(sessions):
    counts: dict[str, int] = {}
    for session in sessions:
        if session["project_id"]:
            counts[session["project_id"]] = counts.get(session["project_id"], 0) + 1
    return counts


def test_busy_project_does_not_starve_quieter_projects(
    fake_hermes_home, tmp_path, monkeypatch
):
    """The per-project BUDGET is not enough while the QUERY is one global window.

    The recovery pass asked for ``limit * len(projects)`` newest assigned
    conversations in a single recency-ordered query and only then applied the
    per-project budget. When one project owns that whole window, every other
    project's assigned conversations are never even considered: their chips show
    nothing at all. Reproduced with a scaled budget (3) so the fixture stays
    small; ``test_busy_project_starvation_at_production_limits`` pins the same
    behaviour at the real 200.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-busy", "project-quiet-a", "project-quiet-b")

    rows = [
        _session("quiet-a-old", BASE_TS, project_id="project-quiet-a"),
        _session("quiet-b-old", BASE_TS + 1, project_id="project-quiet-b"),
    ]
    # 12 newer assigned conversations in one project fill the 3 * 3 = 9 window.
    rows.extend(
        _session(f"busy-{index:03d}", BASE_TS + 100 + index, project_id="project-busy")
        for index in range(12)
    )
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 500 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
        project_assigned_limit=3,
    )

    # Was {'project-busy': 3}: both quiet projects were completely absent.
    assert _assigned_counts(sessions) == {
        "project-busy": 3,
        "project-quiet-a": 1,
        "project-quiet-b": 1,
    }
    # The busy project still gets its newest, and is still bounded.
    assert {s["session_id"] for s in sessions if s["project_id"] == "project-busy"} == {
        "busy-011", "busy-010", "busy-009",
    }


def test_busy_project_starvation_at_production_limits(fake_hermes_home, tmp_path):
    """The same starvation with the real PROJECT_ASSIGNED_CLI_LIMIT (200).

    Two registered projects give the old global query a 400-conversation window;
    400 newer conversations in one project consumed all of it, so the quiet
    project's single assigned conversation was unreachable.
    """
    _register_projects(tmp_path, "project-busy", "project-quiet")

    rows = [_session("quiet-old", BASE_TS, project_id="project-quiet")]
    rows.extend(
        _session(f"busy-{index:04d}", BASE_TS + 100 + index, project_id="project-busy")
        for index in range(models.PROJECT_ASSIGNED_CLI_LIMIT * 2)
    )
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 5000 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    counts = _assigned_counts(models.get_cli_sessions())

    # Was {'project-busy': 200}.
    assert counts == {
        "project-busy": models.PROJECT_ASSIGNED_CLI_LIMIT,
        "project-quiet": 1,
    }


def test_starved_project_recovers_its_whole_compression_lineage(
    fake_hermes_home, tmp_path, monkeypatch
):
    """The project-scoped follow-up query is still lineage-keyed.

    ``project_ids`` narrows only the recursive CTE's SEED, so a starved project's
    conversation still arrives as ONE row even when its assignment sits on the
    root of a compression chain.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-busy", "project-quiet")

    rows = _lineage("quiet-chain", BASE_TS, 3, project_id="project-quiet")
    rows.extend(
        _session(f"busy-{index:03d}", BASE_TS + 100 + index, project_id="project-busy")
        for index in range(12)
    )
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 500 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
        project_assigned_limit=2,
    )

    quiet = [s for s in sessions if s["project_id"] == "project-quiet"]
    assert len(quiet) == 1, [s["session_id"] for s in quiet]
    assert quiet[0]["session_id"] in {f"quiet-chain-seg{index}" for index in range(3)}


def test_no_followup_queries_when_the_global_window_is_not_saturated(
    fake_hermes_home, tmp_path, monkeypatch
):
    """A short global window already saw everything, so it must cost 1 query.

    The starvation follow-up is gated on saturation. Without that gate every
    sidebar build on every profile with projects would pay a GROUP BY probe plus
    a query per project.
    """
    _register_projects(tmp_path, "project-a", "project-b", "project-c")

    rows = [
        _session("a-1", BASE_TS, project_id="project-a"),
        _session("b-1", BASE_TS + 1, project_id="project-b"),
    ]
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 500 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    assigned_queries = []
    real_reader = models.read_importable_agent_session_rows

    def _counting_reader(*args, **kwargs):
        if kwargs.get("project_assignment") == "assigned":
            assigned_queries.append(kwargs.get("project_ids"))
        return real_reader(*args, **kwargs)

    probes = []
    real_probe = models.read_assigned_project_row_counts

    def _counting_probe(*args, **kwargs):
        probes.append(args)
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(models, "read_importable_agent_session_rows", _counting_reader)
    monkeypatch.setattr(models, "read_assigned_project_row_counts", _counting_probe)

    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
        project_assigned_limit=3,
    )

    assert _assigned_counts(sessions) == {"project-a": 1, "project-b": 1}
    assert assigned_queries == [None], "one global assigned query, no per-project ones"
    assert probes == [], "the GROUP BY probe is only paid on a saturated window"


def test_short_global_projection_still_refills_a_starved_project(
    fake_hermes_home, tmp_path, monkeypatch
):
    """A fully consumed raw window can project short without exhausting state.db.

    Three 64-segment busy lineages fill the final ``6 * 32`` raw candidate
    window for a two-project, three-conversation budget.  They collapse to only
    three logical conversations, while an older quiet-project conversation sits
    just beyond that raw window.  A gate based only on ``len(global_rows)``
    mistakes that projection shortfall for a small database and leaves the quiet
    project unreachable.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 4)
    _register_projects(tmp_path, "project-busy", "project-quiet")
    per_project_limit = 3
    global_limit = per_project_limit * 2
    raw_window = global_limit * max(agent_sessions.CANDIDATE_WINDOW_MULTIPLIERS)
    assert raw_window % 3 == 0

    rows = [_session("quiet-old", BASE_TS, project_id="project-quiet")]
    for index in range(3):
        rows.extend(_lineage(
            f"busy-chain-{index}",
            BASE_TS + 10_000 + index * 1_000,
            raw_window // 3,
            project_id="project-busy",
            step=1.0,
        ))
    _write_state_db(fake_hermes_home / "state.db", rows)

    scoped_queries = []
    real_reader = models.read_importable_agent_session_rows

    def _counting_reader(*args, **kwargs):
        if kwargs.get("project_ids"):
            scoped_queries.append((kwargs["project_ids"][0], kwargs["limit"]))
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(models, "read_importable_agent_session_rows", _counting_reader)
    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
        project_assigned_limit=per_project_limit,
    )

    assert "quiet-old" in {session["session_id"] for session in sessions}
    assert scoped_queries == [("project-quiet", per_project_limit)]


def test_saturated_window_pays_one_query_per_starved_project(
    fake_hermes_home, tmp_path, monkeypatch
):
    """Pin the cost of the worst case: probe + one query per starved project.

    The ceiling is forced to 20 over 5 projects (a share of 4) so saturation is
    reachable with a small fixture. Every project ends up represented, the
    follow-up queries are project-scoped and each carries only that project's
    remaining budget, and the recovered total stays under the ceiling.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    monkeypatch.setattr(models, "PROJECT_ASSIGNED_CLI_SCAN_CEILING", 20)
    quiet = [f"quiet-{index}" for index in range(4)]
    _register_projects(tmp_path, "project-busy", *quiet)

    rows = [
        _session(f"{project}-old", BASE_TS + index, project_id=project)
        for index, project in enumerate(quiet)
    ]
    rows.extend(
        _session(f"busy-{index:03d}", BASE_TS + 1000 + index, project_id="project-busy")
        for index in range(25)
    )
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 9000 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    scoped_queries = []
    real_reader = models.read_importable_agent_session_rows

    def _counting_reader(*args, **kwargs):
        if kwargs.get("project_ids"):
            scoped_queries.append((kwargs["project_ids"], kwargs.get("limit")))
        return real_reader(*args, **kwargs)

    probes = []
    real_probe = models.read_assigned_project_row_counts

    def _counting_probe(*args, **kwargs):
        probes.append(args)
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(models, "read_importable_agent_session_rows", _counting_reader)
    monkeypatch.setattr(models, "read_assigned_project_row_counts", _counting_probe)

    counts = _assigned_counts(models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
    ))

    assert len(probes) == 1, "exactly one GROUP BY probe per saturated build"
    assert scoped_queries == [(("quiet-0",), 4), (("quiet-1",), 4),
                              (("quiet-2",), 4), (("quiet-3",), 4)]
    assert counts == {"project-busy": 4, "quiet-0": 1, "quiet-1": 1,
                      "quiet-2": 1, "quiet-3": 1}
    assert sum(counts.values()) <= models.PROJECT_ASSIGNED_CLI_SCAN_CEILING


def test_followup_serves_every_starved_project_neediest_first(
    fake_hermes_home, tmp_path, monkeypatch
):
    """Every starved project gets its follow-up, in neediest-first order.

    The first version of the follow-up was capped at a fixed number of projects
    per build; with the cap forced to 1, only the lower-numbered project id was
    served and the other stayed unreachable on every rebuild (greptile P1 on
    #6659). The cap is gone: both starved projects are served, in the same
    deterministic order the cap used to cut.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-busy", "project-quiet-a", "project-quiet-b")

    rows = [
        _session("quiet-a-old", BASE_TS, project_id="project-quiet-a"),
        _session("quiet-b-old", BASE_TS + 1, project_id="project-quiet-b"),
    ]
    rows.extend(
        _session(f"busy-{index:03d}", BASE_TS + 100 + index, project_id="project-busy")
        for index in range(12)
    )
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 500 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    scoped_queries = []
    real_reader = models.read_importable_agent_session_rows

    def _counting_reader(*args, **kwargs):
        if kwargs.get("project_ids"):
            scoped_queries.append((kwargs["project_ids"][0], kwargs.get("limit")))
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(models, "read_importable_agent_session_rows", _counting_reader)

    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
        project_assigned_limit=3,
    )

    # Neediest first: both projects are equally starved, so id order wins.
    assert scoped_queries == [("project-quiet-a", 3), ("project-quiet-b", 3)]
    assert _assigned_counts(sessions) == {
        "project-busy": 3,
        "project-quiet-a": 1,
        "project-quiet-b": 1,
    }


def test_every_starved_project_is_served_past_the_old_refill_cap(
    fake_hermes_home, tmp_path, monkeypatch
):
    """The refill has no project-count cap: all 39 starved projects are served.

    greptile P1 on #6659: with more starved projects than the old fixed refill
    cap (32), only the first 32 were served and every later project stayed
    unreachable on each rebuild — the recovery is re-derived from the database
    on every build, so the same 32 won every time. Ceiling 40 over 40 projects
    is a share of 1, one busy project saturates the global window, and all 39
    quieter projects must still get their scoped follow-up.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    monkeypatch.setattr(models, "PROJECT_ASSIGNED_CLI_SCAN_CEILING", 40)
    quiet = [f"quiet-{index:02d}" for index in range(39)]
    _register_projects(tmp_path, "project-busy", *quiet)

    rows = [
        _session(f"{project}-old", BASE_TS + index, project_id=project)
        for index, project in enumerate(quiet)
    ]
    rows.extend(
        _session(f"busy-{index:03d}", BASE_TS + 1000 + index, project_id="project-busy")
        for index in range(40)
    )
    _write_state_db(fake_hermes_home / "state.db", rows)

    scoped_queries = []
    real_reader = models.read_importable_agent_session_rows

    def _counting_reader(*args, **kwargs):
        if kwargs.get("project_ids"):
            scoped_queries.append(kwargs["project_ids"][0])
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(models, "read_importable_agent_session_rows", _counting_reader)

    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
    )

    counts = _assigned_counts(sessions)
    assert set(counts) == {"project-busy", *quiet}
    assert len(scoped_queries) == len(quiet)
    assert set(scoped_queries) == set(quiet)
    assert counts == {"project-busy": 5, **{project: 1 for project in quiet}}


def test_scan_ceiling_is_shared_equally_between_projects(
    fake_hermes_home, tmp_path, monkeypatch
):
    """Past the ceiling every project's share shrinks; none is left with zero.

    Ceiling 6 over 3 projects is a share of 2 even though the requested budget is
    5, so the recovered payload stays under the ceiling AND every project is
    represented — the old code let the newest project take 5 of the 6.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    monkeypatch.setattr(models, "PROJECT_ASSIGNED_CLI_SCAN_CEILING", 6)
    _register_projects(tmp_path, "project-a", "project-b", "project-c")

    rows = []
    for offset, project in enumerate(("project-a", "project-b", "project-c")):
        rows.extend(
            _session(
                f"{project}-{index}",
                BASE_TS + offset * 100 + index,
                project_id=project,
            )
            for index in range(4)
        )
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + 900 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models._load_cli_sessions_uncached(
        fake_hermes_home,
        fake_hermes_home / "state.db",
        "default",
        project_assigned_limit=5,
    )

    counts = _assigned_counts(sessions)
    assert counts == {"project-a": 2, "project-b": 2, "project-c": 2}
    assert sum(counts.values()) <= models.PROJECT_ASSIGNED_CLI_SCAN_CEILING


# ── The reader-level contract the per-project follow-up relies on ─────────────


def test_project_ids_narrows_the_assigned_query_to_one_project(tmp_path):
    """``project_ids`` seeds the lineage CTE with one project's rows only."""
    db_path = tmp_path / "state.db"
    rows = [
        _session("a-1", BASE_TS, project_id="project-a"),
        _session("b-1", BASE_TS + 1, project_id="project-b"),
        _session("plain", BASE_TS + 2),
    ]
    rows.extend(_lineage("b-chain", BASE_TS + 10, 3, project_id="project-b"))
    _write_state_db(db_path, rows)

    def _ids(**kwargs):
        return sorted(
            row["id"]
            for row in agent_sessions.read_importable_agent_session_rows(
                db_path, limit=50, exclude_sources=None, **kwargs
            )
        )

    assert _ids(project_assignment="assigned") == ["a-1", "b-1", "b-chain-seg2"]
    assert _ids(project_assignment="assigned", project_ids=("project-b",)) == [
        "b-1", "b-chain-seg2",
    ]
    assert _ids(project_assignment="assigned", project_ids=("project-a",)) == ["a-1"]
    # Unknown ids are not an error, they simply select nothing.
    assert _ids(project_assignment="assigned", project_ids=("nope",)) == []


def test_project_ids_requires_the_assigned_filter(tmp_path):
    """A silently unnarrowed query is exactly the starvable one — refuse it."""
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [_session("a-1", BASE_TS, project_id="project-a")])

    for assignment in (None, "unassigned"):
        with pytest.raises(ValueError, match="project_ids requires"):
            agent_sessions.read_importable_agent_session_rows(
                db_path, project_assignment=assignment, project_ids=("project-a",)
            )


def test_empty_project_ids_selects_nothing(tmp_path):
    """``project_ids=()`` must not degrade to "every assigned conversation"."""
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [_session("a-1", BASE_TS, project_id="project-a")])

    assert agent_sessions.read_importable_agent_session_rows(
        db_path, exclude_sources=None, project_assignment="assigned", project_ids=()
    ) == []


def test_project_ids_narrows_on_a_schema_without_lineage_columns(tmp_path):
    """The no-parent_session_id schema takes the plain ``project_id IN`` path."""
    db_path = tmp_path / "state.db"
    _write_state_db(
        db_path,
        [
            _session("a-1", BASE_TS, project_id="project-a"),
            _session("b-1", BASE_TS + 1, project_id="project-b"),
        ],
        lineage_columns=False,
    )

    rows = agent_sessions.read_importable_agent_session_rows(
        db_path,
        limit=50,
        exclude_sources=None,
        project_assignment="assigned",
        project_ids=("project-b",),
    )

    assert [row["id"] for row in rows] == ["b-1"]


def test_assigned_project_row_counts_probe(tmp_path):
    """The probe counts assigned RAW rows per project and honours exclusions."""
    db_path = tmp_path / "state.db"
    rows = [
        _session("a-1", BASE_TS, project_id="project-a"),
        _session("a-2", BASE_TS + 1, project_id="project-a"),
        _session("b-1", BASE_TS + 2, project_id="project-b"),
        _session("b-cron", BASE_TS + 3, project_id="project-b", source="cron"),
        _session("plain", BASE_TS + 4),
        _session("blank", BASE_TS + 5, project_id="   "),
    ]
    _write_state_db(db_path, rows)

    assert agent_sessions.read_assigned_project_row_counts(db_path) == {
        "project-a": 2, "project-b": 2,
    }
    assert agent_sessions.read_assigned_project_row_counts(
        db_path, exclude_sources=("cron", "webhook", "kanban")
    ) == {"project-a": 2, "project-b": 1}
    # Missing db and a schema without project_id are answered, not raised.
    assert agent_sessions.read_assigned_project_row_counts(tmp_path / "nope.db") == {}


def test_assigned_project_row_counts_probe_on_schema_without_project_id(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.commit()
    conn.close()

    assert agent_sessions.read_assigned_project_row_counts(db_path) == {}


# ── Finding 2: assigned rows must not spend the unassigned quota ──────────────


def test_route_cap_keeps_full_unassigned_window_when_assigned_rows_lead(
    fake_hermes_home,
):
    """3 assigned + 20 unassigned used to yield only 17 unassigned."""
    rows = [
        {
            "session_id": f"assigned-{index}",
            "is_cli_session": True,
            "project_id": "project-prime",
        }
        for index in range(3)
    ]
    rows.extend(
        {"session_id": f"unassigned-{index:02d}", "is_cli_session": True, "project_id": None}
        for index in range(20)
    )

    kept = routes._cap_recent_cli_sessions(rows)
    unassigned_kept = [row for row in kept if not row.get("project_id")]

    assert len(unassigned_kept) == 20
    assert len(kept) == 23
    # All three assigned rows are inside the recent window here, so none of them
    # is demoted to a chip-only row.
    assert not any(row.get("default_hidden") for row in kept)


def test_route_cap_preserves_project_assigned_overflow_as_hidden(fake_hermes_home):
    recent = [
        {"session_id": f"recent-{index}", "is_cli_session": True, "project_id": None}
        for index in range(20)
    ]
    assigned_overflow = {
        "session_id": "assigned-overflow",
        "is_cli_session": True,
        "project_id": "project-prime",
    }
    unassigned_overflow = {
        "session_id": "unassigned-overflow",
        "is_cli_session": True,
        "project_id": None,
    }

    kept = routes._cap_recent_cli_sessions(
        recent + [assigned_overflow, unassigned_overflow],
        cli_cap=20,
    )

    by_id = {row["session_id"]: row for row in kept}
    assert len(kept) == 21
    assert "unassigned-overflow" not in by_id
    assert by_id["assigned-overflow"]["default_hidden"] is True
    assert "default_hidden" not in assigned_overflow


def test_model_refills_the_unassigned_window_consumed_by_assigned_rows(
    fake_hermes_home, tmp_path
):
    """The first state.db pass fetches 20 rows TOTAL, so it must be refilled."""
    _register_projects(tmp_path, "project-a")

    rows = [
        _session(f"assigned-{index}", BASE_TS + 1000 + index, project_id="project-a")
        for index in range(3)
    ]
    rows.extend(_session(f"plain-{index:02d}", BASE_TS + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    unassigned = [s for s in sessions if not s["project_id"]]

    assert len(unassigned) == models.CLI_VISIBLE_SESSION_LIMIT == 20
    assert len([s for s in sessions if s["project_id"] == "project-a"]) == 3


def test_unassigned_window_counts_logical_conversations_not_segments(
    fake_hermes_home, tmp_path
):
    """The 20 unassigned rows must be 20 distinct conversations after dedup."""
    _register_projects(tmp_path, "project-a")

    rows = [
        _session(f"assigned-{index}", BASE_TS + 9000 + index, project_id="project-a")
        for index in range(3)
    ]
    for index in range(25):
        rows.extend(_lineage(f"chain{index:02d}", BASE_TS + index * 100, 3))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    unassigned = [s for s in sessions if not s["project_id"]]
    lineage_keys = {
        s.get("_lineage_root_id") or s["session_id"] for s in unassigned
    }

    assert len(unassigned) == 20
    assert len(lineage_keys) == 20


# ── Finding 3: a stale assignment must not hide a session ─────────────────────


@pytest.mark.parametrize("project_profile", ["default", "other-profile"])
def test_unresolvable_project_id_leaves_the_session_unassigned(
    fake_hermes_home, tmp_path, monkeypatch, project_profile
):
    """A deleted or cross-profile id resolves to None BEFORE either cap runs.

    ``project-live`` is registered for the active profile; ``project-ghost`` is
    either absent from projects.json entirely (deleted) or tagged to another
    profile (foreign). Both must read as "unassigned" rather than exiling the row
    to a ``default_hidden`` chip that cannot exist.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-live")
    if project_profile != "default":
        _register_projects(tmp_path, "project-ghost", profile=project_profile)

    rows = [
        _session("newest-ghost", BASE_TS + 500, project_id="project-ghost"),
        _session("old-ghost", BASE_TS, project_id="project-ghost"),
        _session("old-live", BASE_TS + 1, project_id="project-live"),
    ]
    rows.extend(_session(f"recent-{index:02d}", BASE_TS + 100 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    by_id = {session["session_id"]: session for session in sessions}

    # Inside the recent window: still present, and back in "Unassigned".
    assert "newest-ghost" in by_id
    assert by_id["newest-ghost"]["project_id"] is None
    # Outside the window: not resurrected as an orphan chip-only row. The
    # live-project row in the same db proves the cap itself still recovers.
    assert "old-ghost" not in by_id
    assert by_id["old-live"]["project_id"] == "project-live"


@pytest.mark.parametrize(
    "row_profile, active, expected",
    [
        (None, None, True),
        ("", None, True),
        ("default", None, True),
        (None, "default", True),
        ("", "default", True),
        ("default", "default", True),
        # Renamed root: the legacy 'default' tag and the display name are the
        # same profile, in both directions.
        ("kinni", "default", True),
        ("default", "kinni", True),
        (None, "kinni", True),
        # Genuinely different profiles never cross.
        ("other", "default", False),
        ("default", "other", False),
        ("other", "other", True),
    ],
)
def test_profile_scoped_project_ids_uses_the_canonical_profile_match(
    fake_hermes_home, tmp_path, monkeypatch, row_profile, active, expected
):
    """Ownership is decided by ``_profiles_match``, not by a local copy of it.

    ``api/profiles.py::_profiles_match`` documents that it exists so callers
    stop duplicating this None/''/'default'/renamed-root matrix. Each row asserts
    the catalog read agrees with the canonical helper cell for cell, so the two
    cannot drift apart the way the duplicated body could.
    """
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active)
    monkeypatch.setattr(
        profiles, "_is_root_profile", lambda name: name in {"default", "kinni"}
    )
    (tmp_path / "projects.json").write_text(
        json.dumps([
            {"project_id": "p1", "name": "P1", "color": "#000",
             "profile": row_profile, "created_at": 1.0},
        ]),
        encoding="utf-8",
    )
    models.clear_cli_sessions_cache()

    assert profiles._profiles_match(row_profile, active) is expected
    assert ("p1" in models.profile_scoped_project_ids()) is expected


def test_unresolvable_project_overflow_is_not_marked_hidden(fake_hermes_home, tmp_path):
    """End of the same chain: routes must see None, so no orphan default_hidden."""
    _register_projects(tmp_path, "project-live")

    rows = [_session("stale-assigned", BASE_TS, project_id="project-ghost")]
    rows.extend(_session(f"recent-{index:02d}", BASE_TS + 100 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = sorted(
        models.get_cli_sessions(), key=lambda s: s["updated_at"], reverse=True
    )
    kept = routes._cap_recent_cli_sessions(sessions)
    by_id = {row["session_id"]: row for row in kept}

    assert not any(
        row.get("default_hidden") and not row.get("project_id") for row in kept
    )
    # It is plain unassigned overflow now, so the recent cap drops it outright
    # instead of parking it under a chip nothing can select.
    assert "stale-assigned" not in by_id


# ── Finding 4: lineage project id on the ``tip is row`` early return ──────────


def test_lineage_project_id_kept_when_root_is_the_freshest_segment():
    """The assignment lives on a newer EMPTY continuation (agent_sessions:468).

    ``compression_tip()`` resolves the project id across the whole lineage but
    returns the ROOT as the tip (the continuation has no messages), so the
    ``tip is row`` early return has to apply it too.
    """
    root = _session(
        "root", 100.0, ended_at=110.0, end_reason="compression", messages=4,
    )
    root["actual_message_count"] = 4
    root["last_activity"] = 120.0
    empty_tip = _session("tip", 200.0, project_id="project-123", parent="root", title="")
    empty_tip["actual_message_count"] = 0
    empty_tip["last_activity"] = 200.0

    projected = agent_sessions._project_agent_session_rows([root, empty_tip])

    assert [row["id"] for row in projected] == ["root"]
    assert projected[0]["project_id"] == "project-123"


def test_assignment_on_empty_continuation_reaches_the_sidebar(
    fake_hermes_home, tmp_path, monkeypatch
):
    """Same case end-to-end: the recovery pass must find and keep the lineage."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-123")

    rows = [
        _session("root-with-messages", BASE_TS, ended_at=BASE_TS + 1, end_reason="compression"),
        _session(
            "empty-continuation",
            BASE_TS + 2,
            project_id="project-123",
            parent="root-with-messages",
            messages=0,
        ),
    ]
    rows.extend(_session(f"recent-{index:02d}", BASE_TS + 100 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    by_id = {session["session_id"]: session for session in sessions}

    assert "root-with-messages" in by_id
    assert by_id["root-with-messages"]["project_id"] == "project-123"
    # The empty continuation is not a separately addressable conversation.
    assert "empty-continuation" not in by_id


# ── greptile P1, second half: compression segments eating the raw window ──────


def test_candidate_window_widens_when_segments_consume_it(tmp_path):
    """``limit`` counts logical conversations, so a short window must re-widen.

    12 ten-segment chains: the first 8x candidate window (80 raw rows) only
    reaches 8 whole chains, so a request for 10 conversations came back with 8.
    """
    rows = []
    for index in range(12):
        rows.extend(_lineage(f"chain{index:02d}", BASE_TS + index * 10_000, 10))
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, rows)

    projected = agent_sessions.read_importable_agent_session_rows(
        db_path, limit=10, exclude_sources=None
    )

    assert len(projected) == 10
    assert len({row["_lineage_root_id"] for row in projected}) == 10


def test_project_assignment_filters_are_exact_complements(tmp_path):
    """'assigned' and 'unassigned' must partition the logical conversations."""
    rows = [
        _session("plain-a", BASE_TS + 10),
        _session("plain-b", BASE_TS + 20),
        _session("owned", BASE_TS + 30, project_id="project-a"),
    ]
    # A lineage whose assignment only exists on the tip is assigned as a whole.
    rows.extend(_lineage("chain", BASE_TS + 40, 3, tip_project_id="project-b"))
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, rows)

    def _ids(**kwargs):
        return {
            row["id"]
            for row in agent_sessions.read_importable_agent_session_rows(
                db_path, limit=50, exclude_sources=None, **kwargs
            )
        }

    everything = _ids()
    assigned = _ids(project_assignment="assigned")
    unassigned = _ids(project_assignment="unassigned")

    assert assigned == {"owned", "chain-seg2"}
    assert unassigned == {"plain-a", "plain-b"}
    assert assigned | unassigned == everything
    assert not assigned & unassigned


def test_project_assignment_rejects_unknown_values(tmp_path):
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [_session("plain", BASE_TS)])

    with pytest.raises(ValueError):
        agent_sessions.read_importable_agent_session_rows(
            db_path, limit=5, project_assignment="maybe"
        )


def test_assigned_filter_is_empty_on_schemas_without_project_id(tmp_path):
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [_session("plain", BASE_TS)], lineage_columns=False)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN project_id")

    assert agent_sessions.read_importable_agent_session_rows(
        db_path, limit=5, exclude_sources=None, project_assignment="assigned"
    ) == []
    unassigned = agent_sessions.read_importable_agent_session_rows(
        db_path, limit=5, exclude_sources=None, project_assignment="unassigned"
    )
    assert [row["id"] for row in unassigned] == ["plain"]


def test_schema_without_project_id_retains_prior_behavior(fake_hermes_home, tmp_path):
    """An old state.db that cannot persist assignments is unchanged by all this.

    The profile HAS a project registered, so the recovery pass is only skipped by
    the schema probe. ``_state_db_supports_project_ids`` is asserted directly
    because dropping the probe is behaviour-neutral (the assigned filter already
    returns nothing for such a schema) — it is a query-budget guard, so the
    contract worth pinning is the probe's own answer plus the unchanged output.
    """
    _register_projects(tmp_path, "project-live")

    db_path = fake_hermes_home / "state.db"
    rows = [_session(f"plain-{index:02d}", BASE_TS + index) for index in range(25)]
    _write_state_db(db_path, rows, lineage_columns=False)
    assert models._state_db_supports_project_ids(db_path) is True
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN project_id")
    models.clear_cli_sessions_cache()

    assert models._state_db_supports_project_ids(db_path) is False

    sessions = models.get_cli_sessions()

    assert len(sessions) == models.CLI_VISIBLE_SESSION_LIMIT == 20
    assert all(session["project_id"] is None for session in sessions)
    # Still the newest 20, in the pre-existing order.
    assert [session["session_id"] for session in sessions][:3] == [
        "plain-24", "plain-23", "plain-22",
    ]


def test_unassigned_refill_runs_when_the_mixed_window_under_delivers(
    fake_hermes_home, tmp_path, monkeypatch
):
    """A SHORT mixed first pass is still a shortfall the refill must repair.

    The first pass can return fewer than ``CLI_VISIBLE_SESSION_LIMIT``
    conversations with plenty left in the db, because compression segments spend
    its RAW candidate window (8x, then 32x) before the logical slice. Gating the
    refill on "the first pass came back full" left the unassigned window short in
    exactly that case. Three 43-segment assigned chains saturate both candidate
    windows and yield 3 of the 4 requested conversations; the narrower
    unassigned-only query skips those chains and can still deliver 4.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 4)
    _register_projects(tmp_path, "project-live")

    rows = [_session(f"plain-{index:02d}", BASE_TS + 100 + index) for index in range(10)]
    for index in range(3):
        rows.extend(_lineage(
            f"deep{index}",
            BASE_TS + 2000 + index * 1000,
            43,
            project_id="project-live",
            step=1.0,
        ))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    unassigned = sorted(s["session_id"] for s in sessions if s["project_id"] is None)

    # Was 0: the mixed pass returned 3 conversations, so the old "was it
    # saturated?" gate concluded the db had nothing left to give.
    assert unassigned == ["plain-06", "plain-07", "plain-08", "plain-09"]
    # The lineage-heavy assigned conversations are still there, once each.
    assert sum(1 for s in sessions if s["project_id"] == "project-live") == 3


def test_unassigned_refill_cannot_clobber_a_resolved_assignment(
    fake_hermes_home, tmp_path
):
    """Recovery pass 2 must never un-fix this PR's own headline bug.

    Pass 2 merges its rows into the same lineage-keyed table pass 1 fills, and
    it used to overwrite an existing entry unconditionally. When the SQL
    ``project_assignment='unassigned'`` filter and the Python lineage projection
    disagree about where a compression lineage starts, pass 2 hands back the
    lineage ROOT as its own "unassigned" conversation — and the unconditional
    overwrite then replaced the assigned row with it, deleting the project chip
    and swapping the session identity from the lineage tip back to the root.

    They disagree here on a blank ``source``: ``_is_continuation_session``
    treats an empty/whitespace source as "unknown, same conversation", while the
    SQL lineage CTE compares ``LOWER(TRIM(parent.source)) = LOWER(TRIM(...))``
    and so refuses to walk from the assigned tip up to the blank-source root.
    The merge has to be safe whether or not the two ever line up exactly.
    """
    _register_projects(tmp_path, "project-live")

    rows = [
        # Blank source: SQL will not join this root to its assigned tip, but the
        # Python projection collapses the pair into one row keyed on both ids.
        _session(
            "chain-root",
            BASE_TS + 500,
            source="",
            ended_at=BASE_TS + 505,
            end_reason="compression",
        ),
        _session(
            "chain-tip", BASE_TS + 510, parent="chain-root", project_id="project-live"
        ),
    ]
    # Two spare unassigned rows so the window is short and pass 2 actually runs.
    rows.extend(_session(f"plain-{index}", BASE_TS + index) for index in range(2))
    _write_state_db(fake_hermes_home / "state.db", rows)

    by_id = {s["session_id"]: s for s in models.get_cli_sessions()}

    # Was: "chain-tip" gone, "chain-root" present with project_id None.
    assert "chain-tip" in by_id, "pass 2 replaced the assigned row with its root"
    assert by_id["chain-tip"]["project_id"] == "project-live"
    assert "chain-root" not in by_id, "the lineage must stay one conversation"


# ── Rebase guard: upstream's kanban pass shares the system-chip helper ────────


def test_background_source_exclusion_literal_matches_the_constant():
    """The interactive exclusion stays spelled as a tuple literal.

    ``tests/test_issue2841_show_cron_sessions_toggle.py`` reads that call site as
    SOURCE TEXT (``'("cron", "webhook", "kanban") if source_filter is None'``),
    so replacing the literal with ``BACKGROUND_CLI_SOURCES`` turned a
    pre-existing test red. The literal is therefore kept, and this test pins it
    to the constant the new code paths use so the two cannot drift apart.
    """
    from pathlib import Path

    source = Path(models.__file__).read_text(encoding="utf-8")
    literal = ", ".join(f'"{name}"' for name in models.BACKGROUND_CLI_SOURCES)
    assert f"exclude_sources=({literal}) if source_filter is None else None" in source, (
        "the interactive exclude_sources literal must list exactly "
        f"BACKGROUND_CLI_SOURCES ({models.BACKGROUND_CLI_SOURCES!r})"
    )
    assert '("cron", "webhook", "kanban") if source_filter is None' in source, (
        "test_issue2841_show_cron_sessions_toggle.py pins this exact substring"
    )


def test_kanban_second_pass_still_projects_rows(fake_hermes_home, tmp_path):
    """The system-chip helper keeps the (sid, source) signature kanban calls.

    Upstream's kanban pass calls ``_state_row_project_id(sid, _source)``; giving
    that helper a row-dict signature turned the whole pass into a swallowed
    TypeError and silently dropped every kanban row.
    """
    _register_projects(tmp_path, "project-live")

    rows = [_session("kanban-old", BASE_TS, source="kanban")]
    rows.extend(_session(f"recent-{index:02d}", BASE_TS + 100 + index) for index in range(25))
    _write_state_db(fake_hermes_home / "state.db", rows)

    sessions = models.get_cli_sessions()
    by_id = {session["session_id"]: session for session in sessions}

    assert "kanban-old" in by_id
    # Upstream semantics: kanban rows get no chip from this helper.
    assert by_id["kanban-old"]["project_id"] is None


def test_background_row_chip_is_the_same_on_both_projection_paths(
    fake_hermes_home, tmp_path
):
    """One kanban row must not report two different chips.

    The interactive projection loop is also what a ``source_filter='kanban'``
    scan walks, so resolving state.db assignments there without excusing
    background sources gave the SAME row ``None`` in the default view (its own
    bounded second pass) and a resolved id under ``?source=kanban``.
    """
    _register_projects(tmp_path, "project-live")

    rows = [_session("kan-1", BASE_TS, source="kanban", project_id="project-live")]
    rows.extend(
        _session(f"recent-{index:02d}", BASE_TS + 100 + index) for index in range(25)
    )
    _write_state_db(fake_hermes_home / "state.db", rows)

    default_view = {s["session_id"]: s for s in models.get_cli_sessions()}
    kanban_view = {s["session_id"]: s for s in models.get_cli_sessions("kanban")}

    assert "kan-1" in default_view
    assert "kan-1" in kanban_view
    assert default_view["kan-1"]["project_id"] is None
    assert kanban_view["kan-1"]["project_id"] == default_view["kan-1"]["project_id"]


# ── The all-profiles sidebar view runs the same recovery passes ───────────────


def test_all_profiles_view_recovers_project_assigned_sessions(
    fake_hermes_home, tmp_path, monkeypatch
):
    """``?all_profiles=1`` truncates by recency too, so it needs both passes.

    That path passes ``visible_session_limit=None``, which reads like
    "unbounded" but is mapped to ``CLI_VISIBLE_SESSION_LIMIT`` for the
    interactive pass — only the cron/webhook/kanban limits reach the reader as
    ``limit=`` directly, where None really does mean unbounded. Skipping the
    assigned recovery pass here therefore lost exactly the sessions this
    regression is about, in the one view that shows every profile at once.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    _register_projects(tmp_path, "project-123")

    db_path = fake_hermes_home / "state.db"
    rows = [
        _session(f"recent-{index:02d}", BASE_TS + 100 + index)
        for index in range(25)
    ]
    rows.append(_session("older-assigned", BASE_TS, project_id="project-123"))
    _write_state_db(db_path, rows)

    # Enumerating real profiles shells out to the agent CLI; pin the one context.
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: ([(fake_hermes_home, db_path, "default")], (("home", "default", 1),)),
    )

    sessions = models.get_cli_sessions(all_profiles=True)
    by_id = {session["session_id"]: session for session in sessions}

    assert "older-assigned" in by_id
    assert by_id["older-assigned"]["project_id"] == "project-123"
    assert [s["session_id"] for s in sessions].count("older-assigned") == 1
    # The unassigned window is still bounded — the recovery pass is additive.
    assert sum(1 for s in sessions if s["project_id"] is None) == 5
