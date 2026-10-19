"""#8061 review follow-up: the exact-id fallback must pass the admission.

``get_cli_session_metadata_by_id`` widened what the session-scoped write paths
can see: a foreign-origin row that aged out of the bounded sidebar window now
resolves instead of coming back empty. That is the point of the fallback — but
it also means the missing-sidecar lanes reach ``import_cli_session()`` /
``Session.save()`` with metadata the window-never-served branch used to refuse
by default:

* a foreign-owned row (gateway / unknown / cron / external_agent / …) must not
  gain a writable WebUI sidecar,
* a deleted WebUI session must keep answering 404 so the client self-heals
  (#2782) — including the case where its ``state.db`` row carries no ``source``
  at all, which previously read as ``cli`` because the reader defaulted it,
* an id the WebUI session index still marks as WebUI-owned must not be
  re-imported from ``state.db``,
* while ordinary local agent rows (cli / tui / desktop / acp) keep working.

These tests drive the real routes (``POST /api/chat/start``, ``POST
/api/session/archive``, ``GET /api/session``) against a real ``state.db``, a
real session index / tombstone file and the real ``Session`` persistence. Only
the response encoder, the model-alias lane and the final ``_start_run`` dispatch
are replaced with a capture.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from api import models

# Sids must look like real agent ids (``is_safe_session_id``).
TARGET = "20261001_101010_fallback_target"
FOREIGN = "20261001_202020_foreign_owner"
TOMBSTONED = "20261001_303030_deleted_webui"
INDEXED = "20261001_404040_index_marked"
NULLSOURCE = "20261001_505050_no_provenance"
# A local session whose state.db row survives a WebUI delete: the external
# writer re-created it after the tombstone was recorded (#5504).
RESTORED = "20261001_606060_restored_local"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_state_db(path, rows, *, message_count=2):
    """state.db with the columns the reader, the claim path and chat start read.

    ``source`` is written verbatim, so a row can carry ``None`` (NULL
    provenance) — the shape the reader used to turn into ``cli``.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT,"
        " message_count INTEGER, started_at REAL, ended_at REAL, source TEXT, user_id TEXT,"
        " cwd TEXT, archived INTEGER DEFAULT 0, parent_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    for sid, title, source, ts in rows:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, cwd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, title, "gpt-x", message_count, ts, source, "/tmp"),
        )
        for i in range(message_count):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, "user" if i % 2 == 0 else "assistant", f"turn {i}", ts + i),
            )
    conn.commit()
    conn.close()


def _rows(count, *, source="cli", newest_ts=1_800_000_000.0, prefix="20261010_120000_"):
    return [
        (f"{prefix}{i:06x}", f"Session {i}", source, newest_ts - (count - i))
        for i in range(count)
    ]


class _FakeHandler:
    """The minimum the POST dispatcher touches (same shape as test_465)."""

    def __init__(self):
        self.status = None
        self.headers = {"Content-Type": "application/json", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = "/"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


class _Env:
    """Fixture state plus the three route calls under test."""

    def __init__(self, home, sessions_dir, index_path, captured, body_state, start_run):
        self.home = home
        self.sessions_dir = sessions_dir
        self.index_path = index_path
        self._captured = captured
        self._body_state = body_state
        self.start_run = start_run

    # -- routes ------------------------------------------------------------
    def chat_start(self, sid, message="go on"):
        import api.routes as routes

        self._captured.clear()
        self.start_run.clear()
        self._body_state["body"] = {"session_id": sid, "message": message}
        routes.handle_post(_FakeHandler(), urlparse("/api/chat/start"))
        return self._captured.get("status"), self._captured.get("data")

    def archive(self, sid):
        import api.routes as routes

        self._captured.clear()
        self._body_state["body"] = {"session_id": sid, "archived": True}
        routes.handle_post(_FakeHandler(), urlparse("/api/session/archive"))
        return self._captured.get("status"), self._captured.get("data")

    def rename(self, sid, title="Renamed session"):
        """POST /api/session/rename — shares ``_get_or_materialize_session``."""
        import api.routes as routes

        self._captured.clear()
        self._body_state["body"] = {"session_id": sid, "title": title}
        routes.handle_post(_FakeHandler(), urlparse("/api/session/rename"))
        return self._captured.get("status"), self._captured.get("data")

    def move(self, sid, project_id=None):
        """POST /api/session/move — shares ``_get_or_materialize_session``.

        No ``project_id`` on purpose: the project lane answers its own 404 for an
        unknown project, which would be indistinguishable from the admission
        refusal this suite measures.
        """
        import api.routes as routes

        self._captured.clear()
        body = {"session_id": sid}
        if project_id:
            body["project_id"] = project_id
        self._body_state["body"] = body
        routes.handle_post(_FakeHandler(), urlparse("/api/session/move"))
        return self._captured.get("status"), self._captured.get("data")

    def session_get(self, sid):
        """GET /api/session with the exact id (the read contract)."""
        import api.routes as routes

        self._captured.clear()
        routes.handle_get(
            SimpleNamespace(),
            urlparse(f"/api/session?session_id={sid}&messages=0&resolve_model=0"),
        )
        return self._captured.get("status"), self._captured.get("data")

    # -- helpers -----------------------------------------------------------
    def sidecar(self, sid) -> Path:
        return self.sessions_dir / f"{sid}.json"

    def write_index(self, entries):
        self.index_path.write_text(json.dumps(entries), encoding="utf-8")

    def db_path(self) -> Path:
        return self.home / "state.db"


def _build_env(tmp_path, monkeypatch, *, profile="arbeit", sources=None, null_source_sid=None):
    """One aged-out target per requested source, plus a newer window filler."""
    import api.config as cfg
    import api.helpers as helpers
    import api.profiles as profiles
    import api.routes as routes

    home = tmp_path / "hermes" / "profiles" / profile if profile else tmp_path / "hermes"
    home.mkdir(parents=True)
    sessions_dir = tmp_path / "webui-state" / "sessions"
    sessions_dir.mkdir(parents=True)
    index = sessions_dir / "_index.json"
    index.write_text("[]", encoding="utf-8")
    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: profile or None)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: profile or None)
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)

    rows = []
    target_sources = {src for src in (sources or {}).values()}
    for sid, source in (sources or {}).items():
        rows.append((sid, f"Target {source or 'null'}", source, 1_700_100_000.0))
    if null_source_sid:
        rows.append((null_source_sid, "Null provenance", None, 1_700_100_000.0))
    # Age every target past its own window: the sidebar cap is 20, but cron rows
    # are projected through a separate 200-row project chip ("a real miss past
    # its separate 200-row cap" in the review), so seed enough newer siblings of
    # the target's own source to miss either cap.
    for i, source in enumerate(sorted(target_sources)):
        rows += _rows(
            220,
            source=source,
            newest_ts=1_750_000_000.0,
            prefix=f"2026101{min(i, 9)}_090000_",
        )
    rows += _rows(25)
    _seed_state_db(home / "state.db", rows)
    models.clear_cli_sessions_cache()
    routes.SESSIONS.clear()

    visible = {row["session_id"] for row in models.get_cli_sessions()}
    for sid in list((sources or {}).keys()) + ([null_source_sid] if null_source_sid else []):
        assert sid not in visible, f"fixture must age {sid} out of the window"

    captured = {}
    body_state = {"body": {}}
    start_run = []

    def _j(_handler, data, status=200, extra_headers=None):
        captured["data"], captured["status"] = data, status
        return data

    def _bad(_handler, message, code=400):
        captured["data"], captured["status"] = message, code
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(helpers, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(helpers, "bad", _bad)
    monkeypatch.setattr(routes, "read_body", lambda _handler: dict(body_state["body"]))
    # Chat start's dispatch is captured: these tests are about admission, not
    # about the runner. The real Session persistence and the real state.db reads
    # stay in place.
    monkeypatch.setattr(
        routes, "_start_run",
        lambda *a, **k: (start_run.append(k) or {"ok": True, "session_id": k.get("session_id")}),
        raising=False,
    )
    # Model-alias resolution is a catalog concern; neutralise it so a fixture
    # model name cannot decide the outcome of an admission test.
    monkeypatch.setattr(
        routes, "_external_model_alias_lane_response", lambda *a, **k: None, raising=False
    )
    return _Env(home, sessions_dir, index, captured, body_state, start_run)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return _build_env(
        tmp_path,
        monkeypatch,
        sources={
            TARGET: "cli",
            FOREIGN: "gateway",
            TOMBSTONED: "webui",
            INDEXED: "webui",
            RESTORED: "cli",
        },
        null_source_sid=NULLSOURCE,
    )


@pytest.fixture
def sources_env(tmp_path, monkeypatch):
    """A factory for one-env-per-source parametrised cases."""
    def _factory(source):
        return _build_env(
            tmp_path, monkeypatch, sources={FOREIGN: source}
        )
    return _factory


# ---------------------------------------------------------------------------
# Unit level: the reader no longer invents provenance
# ---------------------------------------------------------------------------


def test_reader_does_not_invent_cli_provenance(env):
    """A NULL ``source`` stays unknown instead of being read as ``cli``."""
    row = models.get_cli_session_metadata_by_id(NULLSOURCE)
    assert row, "the row exists and must resolve by exact id"
    assert row["source_tag"] == "unknown"
    assert row["raw_source"] is None
    assert row["exact_id_fallback"] is True


def test_window_rows_are_not_marked_as_fallback(env):
    """Only the exact-id read marks its row, so in-window behaviour is untouched."""
    window = [row for row in models.get_cli_sessions() if row["session_id"] == TARGET]
    assert window == [], "fixture sanity: the target is outside the window"
    resolved = models.get_cli_session_metadata_by_id(TARGET)
    assert resolved.get("exact_id_fallback") is True


def test_unnamed_origin_is_not_claimable(monkeypatch):
    """Neither foreign store nor state.db names an origin → no write ownership."""
    import api.routes as routes

    claimable, reason = routes._is_claimable_cli_source({}, "")
    assert claimable is False
    assert reason == "unknown_source"


def test_state_db_source_still_decides_when_cli_meta_is_empty(monkeypatch):
    """The TUI/Desktop fall-through (empty cli_meta, named state.db source) holds."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_state_db_session_source", lambda _sid: "tui")
    assert routes._is_claimable_cli_source({}, "tui")[0] is True


# ---------------------------------------------------------------------------
# POST /api/chat/start — the P1 from the review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["gateway", "unknown", "cron", "external_agent"])
def test_chat_start_refuses_foreign_owned_out_of_window_row(sources_env, source):
    """A foreign-owned aged-out row must not become a writable WebUI session."""
    env = sources_env(source)
    status, payload = env.chat_start(FOREIGN)
    assert status == 403, payload
    assert not env.sidecar(FOREIGN).exists(), "no sidecar may be written for a refused claim"
    assert env.start_run == [], "a refused claim must not dispatch a run"


def test_chat_start_refuses_foreign_owned_row_for_root_profile(tmp_path, monkeypatch):
    """Same refusal on the root profile (the second profile the review measured)."""
    env = _build_env(
        tmp_path, monkeypatch, profile=None, sources={FOREIGN: "gateway"}
    )
    status, payload = env.chat_start(FOREIGN)
    assert status == 403, payload
    assert not env.sidecar(FOREIGN).exists()


def test_chat_start_deleted_webui_without_provenance_stays_404(env):
    """The tombstone wins over a surviving NULL-source row: 404, nothing written."""
    models._record_webui_deleted_session_tombstone(NULLSOURCE)
    status, payload = env.chat_start(NULLSOURCE)
    assert status == 404, payload
    assert not env.sidecar(NULLSOURCE).exists()
    assert env.start_run == []
    assert NULLSOURCE in models._load_webui_deleted_session_tombstone(), (
        "the tombstone must survive the attempt"
    )


def test_chat_start_index_marked_webui_row_stays_404(env):
    """An index-marked WebUI row is not re-imported from state.db."""
    env.write_index(
        [
            {
                "session_id": INDEXED,
                "source_tag": "webui",
                "raw_source": "webui",
                "session_source": "webui",
            }
        ]
    )
    status, payload = env.chat_start(INDEXED)
    assert status == 404, payload
    assert not env.sidecar(INDEXED).exists()
    assert env.start_run == []
    # The read contract agrees: GET stays 404 so the client self-heals.
    get_status, _ = env.session_get(INDEXED)
    assert get_status == 404


@pytest.mark.parametrize("source", ["cli", "tui", "desktop", "acp"])
def test_chat_start_still_admits_local_agent_rows(sources_env, source):
    """The controls: ordinary local agent rows keep working (and persist)."""
    env = sources_env(source)
    status, payload = env.chat_start(FOREIGN)
    assert status == 200, payload
    assert env.sidecar(FOREIGN).exists(), "an admitted claim persists its sidecar"
    assert env.start_run, "an admitted claim dispatches the run"


# ---------------------------------------------------------------------------
# POST /api/session/archive — the second missing-sidecar write path
# ---------------------------------------------------------------------------


def test_archive_refuses_foreign_owned_out_of_window_row(sources_env):
    env = sources_env("gateway")
    status, payload = env.archive(FOREIGN)
    assert status == 404, payload
    assert not env.sidecar(FOREIGN).exists()


def test_archive_still_reaches_local_agent_out_of_window_row(env):
    """Control: archive keeps reaching an aged-out sidecar-less local session."""
    status, payload = env.archive(TARGET)
    assert status == 200, payload
    assert env.sidecar(TARGET).exists()


# ---------------------------------------------------------------------------
# Restored local rows keep their mutation verbs (review regression, #5504)
# ---------------------------------------------------------------------------
#
# The admission added for the fallback originally refused *any* tombstoned
# exact-id row. A durable delete tombstone is only meaningful for a row the
# WebUI owns, so a local session whose state.db row the external writer
# re-created after a WebUI delete lost rename/move/archive (404 on all three)
# while chat start still worked — the regression the review found on exact head
# ``70c7834``. Every verb now runs the same source-aware predicate
# ``_claim_or_synthesize_cli_session`` already used. Each case below gets a
# fresh environment, so no verb can inherit a sidecar materialized by another.

MUTATION_VERBS = ("chat_start", "rename", "move", "archive")


def _mutate(env, verb, sid):
    return getattr(env, verb)(sid)


@pytest.mark.parametrize("verb", MUTATION_VERBS)
def test_restored_local_row_survives_stale_tombstone(env, verb):
    """``source='cli'`` plus a tombstone: the writer re-created the row, so it
    stays usable — the tombstone only refuses WebUI/blank provenance."""
    models._record_webui_deleted_session_tombstone(RESTORED)
    assert RESTORED in models._load_webui_deleted_session_tombstone()
    status, payload = _mutate(env, verb, RESTORED)
    assert status == 200, payload
    assert env.sidecar(RESTORED).exists(), "an admitted mutation persists its sidecar"


@pytest.mark.parametrize("verb", MUTATION_VERBS)
def test_webui_owned_tombstoned_row_stays_refused(env, verb):
    """Control: a WebUI-owned row behind a tombstone keeps its self-heal 404."""
    models._record_webui_deleted_session_tombstone(TOMBSTONED)
    status, _ = _mutate(env, verb, TOMBSTONED)
    assert status == 404
    assert not env.sidecar(TOMBSTONED).exists()


@pytest.mark.parametrize("verb", MUTATION_VERBS)
def test_null_provenance_tombstoned_row_stays_refused(env, verb):
    """Control: NULL provenance names no owner, so the tombstone still refuses."""
    models._record_webui_deleted_session_tombstone(NULLSOURCE)
    status, _ = _mutate(env, verb, NULLSOURCE)
    assert status == 404
    assert not env.sidecar(NULLSOURCE).exists()


@pytest.mark.parametrize("verb", MUTATION_VERBS)
def test_index_marked_webui_row_stays_refused(env, verb):
    """Control: the index mark stays source-blind and refuses every verb."""
    env.write_index(
        [
            {
                "session_id": INDEXED,
                "source_tag": "webui",
                "raw_source": "webui",
                "session_source": "webui",
            }
        ]
    )
    status, _ = _mutate(env, verb, INDEXED)
    assert status == 404
    assert not env.sidecar(INDEXED).exists()


@pytest.mark.parametrize("verb", MUTATION_VERBS)
@pytest.mark.parametrize("source", ["cli", "tui", "desktop", "acp"])
def test_local_agent_rows_still_admitted_per_verb(sources_env, source, verb):
    """Controls: with no tombstone, every local source keeps working per verb."""
    env = sources_env(source)
    status, payload = _mutate(env, verb, FOREIGN)
    assert status == 200, payload
    assert env.sidecar(FOREIGN).exists()
