"""#8019: read-only per-session async-delegation status endpoint.

Covers the contributor slice maintainer approved for #8019: a READ-ONLY,
advisory observability surface so a client can learn that a session still owns
live delegations (running, or finished-but-undelivered) before switching away.

Scope pinned by the maintainer:
  * resolve the authenticated request profile and exact WebUI owner;
  * distinguish work still running from completions waiting for delivery;
  * an unavailable/unknown source is reported as ``unknown``, never as zero;
  * metadata/counts only — no prompts, results, secrets, or foreign-profile rows;
  * advisory only — a snapshot before switching is not an atomic guarantee.

The tests drive the REAL :func:`api.delegation_status.session_delegation_status`
against REAL per-profile ``state.db`` files. The only seams stubbed are the
profile-home resolver (pointed at temp dirs) and the in-memory probe (an
optional Agent-side import that is absent in most test environments).
"""

import io
import json
import sqlite3
from urllib.parse import urlparse

import pytest

delegation_status = pytest.importorskip("api.delegation_status")


# ---------------------------------------------------------------------------
# Real per-profile state.db harness
# ---------------------------------------------------------------------------

_DELEGATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS async_delegations (
    delegation_id TEXT PRIMARY KEY,
    origin_session TEXT,
    origin_ui_session_id TEXT,
    parent_session_id TEXT,
    state TEXT,
    dispatched_at REAL,
    updated_at REAL,
    delivery_state TEXT,
    delivery_attempts INTEGER DEFAULT 0,
    owner_pid INTEGER,
    owner_started_at REAL,
    task_json TEXT,
    origin_session_id TEXT
);
"""


def _write_delegation_db(db_path, rows):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DELEGATION_SCHEMA)
        conn.executemany(
            "INSERT INTO async_delegations (delegation_id, origin_session, "
            "origin_ui_session_id, parent_session_id, state, dispatched_at, "
            "updated_at, delivery_state, delivery_attempts, task_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _row(delegation_id, *, origin_ui_session_id="", session_key="",
         parent_session_id="", state="running", delivery_state="pending"):
    return (
        delegation_id, session_key, origin_ui_session_id, parent_session_id,
        state, 1780000000.0, 1780000001.0, delivery_state, 0, "{}",
    )


@pytest.fixture
def two_profile_world(tmp_path, monkeypatch):
    """Real alpha (active) + bravo profiles, each with its own state.db."""
    home = tmp_path / "home"
    profiles_root = home / "profiles"
    (profiles_root / "alpha").mkdir(parents=True)
    (profiles_root / "bravo").mkdir(parents=True)
    _write_delegation_db(profiles_root / "alpha" / "state.db", [])
    _write_delegation_db(profiles_root / "bravo" / "state.db", [])

    monkeypatch.setattr(
        delegation_status, "_profile_state_db_path",
        lambda profile=None: profiles_root / (profile or "alpha") / "state.db",
    )
    # The in-memory Agent-side probe is unavailable in this environment.
    monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)
    return profiles_root


# ---------------------------------------------------------------------------
# Behavioral cases
# ---------------------------------------------------------------------------

class TestEmptySession:
    def test_no_delegations_reports_zero_ok(self, two_profile_world):
        result = delegation_status.session_delegation_status("sess_empty", profile="alpha")
        assert result["status"] == "ok"
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 0
        assert result["session_id"] == "sess_empty"

    def test_missing_state_db_reports_zero_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            delegation_status, "_profile_state_db_path",
            lambda profile=None: tmp_path / "nonexistent" / "state.db",
        )
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)
        result = delegation_status.session_delegation_status("sess_x", profile="ghost")
        assert result["status"] == "ok"
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 0


class TestRunningDelegations:
    def test_running_row_counts_as_running(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["status"] == "ok"
        assert result["running"] == 1
        assert result["awaiting_delivery"] == 0

    def test_stalling_and_finalizing_count_as_running(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [
                _row("deleg_1", origin_ui_session_id="sess_a", state="stalling"),
                _row("deleg_2", origin_ui_session_id="sess_a", state="finalizing"),
                _row("deleg_3", origin_ui_session_id="sess_a", state="running"),
            ],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 3

    def test_other_session_rows_do_not_count(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_other", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 0


class TestAwaitingDelivery:
    def test_completed_pending_counts_as_awaiting_delivery(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="completed", delivery_state="pending")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 1

    def test_delivered_completion_does_not_count(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="completed", delivery_state="delivered")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["awaiting_delivery"] == 0

    def test_error_state_pending_counts_as_awaiting_delivery(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="error", delivery_state="pending")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["awaiting_delivery"] == 1

    def test_stalled_pending_counts_as_awaiting_delivery(self, two_profile_world):
        # Producer: the stall watchdog force-finalizes via _finalize(...,
        # "stalled"), and _persist_completion writes state='stalled' with
        # delivery_state='pending' (tools/async_delegation.py).
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="stalled", delivery_state="pending")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 1

    def test_unknown_pending_counts_as_awaiting_delivery(self, two_profile_world):
        # Producer: recover_abandoned_delegations() writes state='unknown',
        # delivery_state='pending' when the owning process disappeared.
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="unknown", delivery_state="pending")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 1

    def test_stalled_and_unknown_mix_with_running(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [
                _row("deleg_1", origin_ui_session_id="sess_a", state="running"),
                _row("deleg_2", origin_ui_session_id="sess_a",
                     state="stalled", delivery_state="pending"),
                _row("deleg_3", origin_ui_session_id="sess_a",
                     state="unknown", delivery_state="pending"),
            ],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 1
        assert result["awaiting_delivery"] == 2

    def test_stalled_delivered_does_not_count(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="stalled", delivery_state="delivered")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["awaiting_delivery"] == 0

    def test_dropped_completion_does_not_count(self, two_profile_world):
        # Retry-exhausted / replay-cap rows are terminally dropped: their
        # pending delivery is gone, so nobody is awaiting them.
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a",
                  state="completed", delivery_state="dropped")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["awaiting_delivery"] == 0


class TestCrossProfileIsolation:
    def test_bravo_rows_never_appear_for_alpha(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "bravo" / "state.db",
            [_row("deleg_b", origin_ui_session_id="sess_a", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 0

    def test_bravo_rows_visible_for_bravo(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "bravo" / "state.db",
            [_row("deleg_b", origin_ui_session_id="sess_a", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="bravo")
        assert result["running"] == 1


class TestSessionKeySelector:
    def test_session_key_selector_matches(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", session_key="sess_a", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 1

    def test_parent_session_selector_matches(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", parent_session_id="sess_a", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["running"] == 1


class TestUnknownNotZero:
    def test_corrupt_state_db_reports_unknown(self, two_profile_world, monkeypatch):
        db = two_profile_world / "alpha" / "state.db"
        db.write_bytes(b"this is not a sqlite database at all")
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["status"] == "unknown"
        assert result["running"] is None
        assert result["awaiting_delivery"] is None

    def test_resolver_failure_reports_unknown_not_zero(self, tmp_path, monkeypatch):
        """A raising profile-home resolver is an unavailable source: unknown,
        never the (0, 0) a never-created ledger would truthfully report.

        Drives the REAL _profile_state_db_path (no fixture override) with the
        api.models resolver patched to raise, mirroring the reviewer's
        profile-home-dependency repro.
        """
        import api.models as models

        def _boom(profile):
            raise RuntimeError("profile home exploded")

        monkeypatch.setattr(models, "_get_profile_home", _boom)
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)
        result = delegation_status.session_delegation_status("sess_a", profile="ghost")
        assert result["status"] == "unknown"
        assert result["running"] is None
        assert result["awaiting_delivery"] is None

    def test_resolver_failure_keeps_memory_result_independent(self, tmp_path, monkeypatch):
        """Durable unknown must not mask an independent in-memory live hit."""
        import api.models as models

        def _boom(profile):
            raise RuntimeError("profile home exploded")

        monkeypatch.setattr(models, "_get_profile_home", _boom)
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: True)
        result = delegation_status.session_delegation_status("sess_a", profile="ghost")
        assert result["status"] == "unknown"
        assert result["running"] is None
        assert result["awaiting_delivery"] is None
        assert result["memory_live"] is True

    def test_resolved_never_created_ledger_is_truthful_zero(self, tmp_path, monkeypatch):
        """Resolver succeeds but the profile never persisted state.db: ok/0/0,
        distinct from the unresolvable case above."""
        import api.models as models

        monkeypatch.setattr(
            models, "_get_profile_home", lambda profile: tmp_path / "ghostprofile"
        )
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)
        result = delegation_status.session_delegation_status("sess_a", profile="ghost")
        assert result["status"] == "ok"
        assert result["running"] == 0
        assert result["awaiting_delivery"] == 0

    def test_missing_table_reports_unknown(self, two_profile_world):
        db = two_profile_world / "alpha" / "state.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("DROP TABLE IF EXISTS async_delegations")
            conn.execute("CREATE TABLE IF NOT EXISTS unrelated (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["status"] == "unknown"
        assert result["running"] is None
        assert result["awaiting_delivery"] is None

    def test_durable_ok_memory_probe_none_is_partial(self, two_profile_world, monkeypatch):
        """Durable read succeeds, memory probe unavailable -> ok with a flag."""
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a", state="running")],
        )
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["status"] == "ok"
        assert result["running"] == 1
        assert result["memory_live"] is None

    def test_memory_probe_true_marks_live(self, two_profile_world, monkeypatch):
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: True)
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["memory_live"] is True

    def test_memory_probe_failure_is_none_not_false(self, two_profile_world, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("probe exploded")
        monkeypatch.setattr(delegation_status, "_memory_live_probe", _boom)
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        assert result["memory_live"] is None


class TestNoSecretLeakage:
    def test_payload_has_counts_only(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a", state="running")],
        )
        result = delegation_status.session_delegation_status("sess_a", profile="alpha")
        # No prompt/result/secret material may ride along.
        for forbidden in ("task_json", "goal", "context", "goals", "summary", "result"):
            assert forbidden not in result
        assert set(result) >= {"session_id", "status", "running", "awaiting_delivery"}


# ---------------------------------------------------------------------------
# Real route plumbing: GET /api/session/delegations through handle_get
# ---------------------------------------------------------------------------

class _RealGetHandler:
    """Minimal real handler surface for routes.handle_get."""

    def __init__(self, path):
        self.status = None
        self.response_headers = {}
        self.headers = {}
        self.command = "GET"
        self.path = path
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def _get_route(world, path):
    routes = pytest.importorskip("api.routes")
    handler = _RealGetHandler(path)
    routes.handle_get(handler, urlparse(path))
    payload = {}
    raw = handler.wfile.getvalue()
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
    return handler.status, payload


class TestRoute:
    def test_missing_session_id_is_400(self, two_profile_world):
        status, _ = _get_route(two_profile_world, "/api/session/delegations")
        assert status == 400

    def test_route_returns_counts_for_active_profile(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "alpha" / "state.db",
            [_row("deleg_1", origin_ui_session_id="sess_a", state="running")],
        )
        status, payload = _get_route(
            two_profile_world, "/api/session/delegations?session_id=sess_a"
        )
        assert status == 200
        assert payload["session_id"] == "sess_a"
        assert payload["running"] == 1
        assert payload["awaiting_delivery"] == 0
        assert payload["status"] == "ok"

    def test_route_never_leaks_bravo_rows(self, two_profile_world):
        _write_delegation_db(
            two_profile_world / "bravo" / "state.db",
            [_row("deleg_b", origin_ui_session_id="sess_a", state="running")],
        )
        status, payload = _get_route(
            two_profile_world, "/api/session/delegations?session_id=sess_a"
        )
        assert status == 200
        assert payload["running"] == 0
        assert payload["awaiting_delivery"] == 0



# ---------------------------------------------------------------------------
# Ledger path encoding (release-manager fold on #8449)
# ---------------------------------------------------------------------------

class TestLedgerPathEncoding:
    """The read-only URI must address exactly the resolved ledger file.

    A raw ``f"file:{path}?mode=ro"`` lets SQLite decode ``%xx`` and treat ``#``
    as a fragment, so a profile home named ``home%20demo`` read its sibling
    ``home demo``, and a home containing ``#`` dropped ``?mode=ro`` and opened
    (creating) a different file.
    """

    def _point_at(self, monkeypatch, db_path):
        monkeypatch.setattr(delegation_status, "_profile_state_db_path", lambda profile=None: db_path)
        monkeypatch.setattr(delegation_status, "_memory_live_probe", lambda *a, **kw: None)

    def test_percent_escape_in_home_reads_its_own_ledger(self, tmp_path, monkeypatch):
        sibling = tmp_path / "home demo" / "state.db"
        own = tmp_path / "home%20demo" / "state.db"
        _write_delegation_db(sibling, [_row("d-sibling", origin_ui_session_id="sess_a",
                                            state="completed", delivery_state="pending")])
        _write_delegation_db(own, [])
        self._point_at(monkeypatch, own)
        result = delegation_status.session_delegation_status("sess_a")
        assert result["status"] == "ok"
        assert result["awaiting_delivery"] == 0, "read the decoded sibling ledger, not its own"

    def test_hash_in_home_stays_read_only_and_creates_nothing(self, tmp_path, monkeypatch):
        own = tmp_path / "ho#me" / "state.db"
        _write_delegation_db(own, [_row("d-own", origin_ui_session_id="sess_a",
                                        state="completed", delivery_state="pending")])
        self._point_at(monkeypatch, own)
        result = delegation_status.session_delegation_status("sess_a")
        assert result["status"] == "ok"
        assert result["awaiting_delivery"] == 1
        assert not (tmp_path / "ho").exists(), "the fragment-truncated path was opened and created"

    def test_unstatable_ledger_path_reports_unknown(self, monkeypatch):
        class _Unstatable:
            def exists(self):
                raise PermissionError(13, "Permission denied")

            def resolve(self):
                raise PermissionError(13, "Permission denied")

            def __str__(self):
                return "/unreadable/profile/state.db"

        self._point_at(monkeypatch, _Unstatable())
        result = delegation_status.session_delegation_status("sess_a")
        assert result["status"] == "unknown"
        assert result["running"] is None
        assert result["awaiting_delivery"] is None
