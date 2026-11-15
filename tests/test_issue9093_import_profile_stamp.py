"""#9093: imported CLI sessions keep their named profile; lineage enrichment stays in the active profile.

1. In a named profile, the CLI metadata row exists only for the newest CLI rows (the bounded listing). Opening an
   older session saved the WebUI sidecar with ``profile = None``: the sidebar backfilled it to ``default``, so it
   left the owning profile's own list and showed up only in the all-profiles list, labelled ``default``. Both
   creation paths (``POST /api/session/import_cli`` and the claim/synthesize path behind GET ``/api/session`` and
   chat start) now stamp the active profile when the metadata names none: the messages came from that profile's
   ``state.db``.
2. ``_enrich_sidebar_lineage_metadata`` reads the ACTIVE profile's ``state.db`` and applied it by bare session id
   to every row, so with All profiles on a foreign row that shares an id inherited the active row's lineage and
   relationship. It now reads and applies it only for rows the active profile owns.

The import probe runs in an isolated process with its own HERMES_HOME and WebUI state, with real ``state.db``
files and the real route handlers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_SCHEMA = """CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL,
  message_count INTEGER, parent_session_id TEXT, model_config TEXT, end_reason TEXT, ended_at REAL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);"""
_T = 1800000000.0


def _add(db, sid, start, *, source="cli", parent=None, end_reason=None, ended=None):
    db.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, source, "Title " + sid, "m", _T + start, 2, parent, None, end_reason,
         _T + ended if ended else None),
    )
    for i, role in enumerate(("user", "assistant")):
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                   (sid, role, "x", _T + start + i))


# ── 1. imports keep the active named profile (real routes, isolated process) ──

_PROBE = r'''
import json, sqlite3, sys
from pathlib import Path

sys.path.insert(0, sys.argv[3])
from tests.test_issue9093_import_profile_stamp import _SCHEMA, _add
from api import models, profiles, routes

home, profile = Path(sys.argv[1]), sys.argv[2]
db_home = home if profile == "default" else home / "profiles" / profile
db_home.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(db_home / "state.db")
db.executescript(_SCHEMA)
for i in range(30):  # oldest first: s_00 is outside the newest-20 CLI listing, s_29 inside it
    _add(db, f"s_{i:02d}", 100 + 10 * i)
db.commit()
db.close()

routes.j = lambda handler, payload, status=200, **kw: {"status": status, **payload}
routes.bad = lambda handler, message, status=400, **kw: {"status": status, "error": message}
profiles.set_request_profile(profile)
models.clear_cli_sessions_cache()
out = {"in_listing": bool(routes._lookup_cli_session_metadata("s_29")),
       "old_in_listing": bool(routes._lookup_cli_session_metadata("s_00"))}

# POST /api/session/import_cli, for an old and a recent session.
for sid in ("s_00", "s_29"):
    response = routes._handle_session_import_cli(None, {"session_id": sid})
    out["import:" + sid] = {"status": response.get("status"), "imported": response.get("imported"),
                            "response_profile": (response.get("session") or {}).get("profile"),
                            "saved_profile": models.Session.load(sid).profile}

# The claim path (GET /api/session and chat start on a missing sidecar), for another old session.
session, reason = routes._claim_or_synthesize_cli_session("s_01")
session.save()
out["claim:s_01"] = {"reason": reason, "saved_profile": models.Session.load("s_01").profile}

models.clear_cli_sessions_cache()
listing = {}
for all_profiles in (False, True):
    payload = routes._build_session_list_cache_payload(
        active_profile=profile, all_profiles=all_profiles, show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=False, show_claude_code_sessions=False,
    )
    listing["all" if all_profiles else "own"] = {
        s["session_id"]: s.get("profile") for s in payload["sessions"] if s["session_id"] in ("s_00", "s_29")
    }
out["listing"] = listing
print(json.dumps(out))
'''


def _probe(profile: str, tmp_path: Path) -> dict:
    home = tmp_path / "home"
    state = tmp_path / "webui"
    (state / "sessions").mkdir(parents=True)
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "HERMES_HOME": str(home),
        "HERMES_BASE_HOME": str(home),
        "HERMES_WEBUI_STATE_DIR": str(state),
        "HERMES_WEBUI_ISOLATED_PROFILE": "0",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        "PYTHONPATH": str(REPO),
    })
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(home), profile, str(REPO)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_named_profile_imports_outside_the_cli_listing_keep_their_profile(tmp_path):
    out = _probe("owned", tmp_path)

    # The shape the report needs: the old session has no CLI metadata row, the recent one does.
    assert out["in_listing"] and not out["old_in_listing"], out
    for sid in ("s_00", "s_29"):
        assert out["import:" + sid]["status"] == 200 and out["import:" + sid]["imported"], out
        assert out["import:" + sid]["saved_profile"] == "owned", out
        assert out["import:" + sid]["response_profile"] == "owned", out
    assert out["claim:s_01"] == {"reason": "materialized", "saved_profile": "owned"}, out
    # The imports stay in the owning profile's sidebar, and the all-profiles list labels them with it.
    # (A bare claimed sidecar is listed by later writes, as for the root profile, so only its stamp is checked.)
    assert out["listing"]["own"] == {"s_00": "owned", "s_29": "owned"}, out
    assert out["listing"]["all"] == {"s_00": "owned", "s_29": "owned"}, out


def test_root_profile_imports_are_unchanged(tmp_path):
    out = _probe("default", tmp_path)

    for sid in ("s_00", "s_29"):
        assert out["import:" + sid]["imported"], out
        assert out["import:" + sid]["saved_profile"] in (None, "default"), out
    assert out["claim:s_01"]["saved_profile"] in (None, "default"), out
    assert out["listing"]["own"].keys() == {"s_00", "s_29"}, out


# ── 2. lineage enrichment applies only to the active profile's rows ───────


@pytest.fixture
def owned_lineage_db(tmp_path, monkeypatch):
    """The active profile's state.db: shared-old compressed into shared, and kid spawned from shared-old."""
    from api import models

    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    _add(db, "shared-old", 100, end_reason="compression", ended=200)
    _add(db, "shared", 210, parent="shared-old")
    _add(db, "kid", 150, parent="shared-old")
    db.commit()
    db.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: path)
    return path


@pytest.fixture
def active_profile():
    from api import profiles

    def activate(name):
        profiles.set_request_profile(name)

    yield activate
    profiles.clear_request_profile()


def _row(sid, profile, **extra):
    row = {"session_id": sid, "title": f"{profile} {sid}", "profile": profile, "source_tag": "webui",
           "raw_source": "webui", "session_source": "webui", "message_count": 2}
    row.update(extra)
    return row


def test_a_foreign_row_sharing_an_id_keeps_its_own_lineage(owned_lineage_db, active_profile):
    from api import models

    active_profile("owned")
    foreign_kid = _row("kid", "beta", parent_session_id="beta-parent", relationship_type="child_session")
    foreign_shared = _row("shared", "beta")
    before = [dict(foreign_kid), dict(foreign_shared)]
    own_kid = _row("kid", "owned")
    own_shared = _row("shared", "owned")

    models._enrich_sidebar_lineage_metadata([foreign_kid, own_kid, foreign_shared, own_shared])

    assert [foreign_kid, foreign_shared] == before
    assert own_kid["_parent_lineage_root_id"] == "shared-old"
    assert own_kid["relationship_type"] == "child_session"
    assert own_shared["_lineage_root_id"] == "shared-old"
    assert own_shared["_lineage_tip_id"] == "shared"


def test_root_profile_rows_with_or_without_a_tag_are_still_enriched(owned_lineage_db, active_profile):
    from api import models

    active_profile("default")
    untagged = _row("shared", None)
    tagged = _row("kid", "default")

    models._enrich_sidebar_lineage_metadata([untagged, tagged])

    assert untagged["_lineage_root_id"] == "shared-old"
    assert tagged["_parent_lineage_root_id"] == "shared-old"


def test_single_profile_lineage_collapse_is_unchanged(owned_lineage_db, active_profile):
    from api import models

    active_profile("owned")
    rows = [_row("shared", "owned"), _row("kid", "owned")]

    models._enrich_sidebar_lineage_metadata(rows)

    assert rows[0]["_compression_segment_count"] == 2
    assert rows[1]["_parent_lineage_tip_id"] == "shared"


def test_an_unknown_active_profile_enriches_nothing(owned_lineage_db, monkeypatch):
    from api import models, profiles

    def boom():
        raise RuntimeError("no profile")

    monkeypatch.setattr(profiles, "get_active_profile_name", boom)
    row = _row("shared", None)
    before = dict(row)

    models._enrich_sidebar_lineage_metadata([row])

    assert row == before


def test_the_top_n_window_still_covers_every_profile(owned_lineage_db, active_profile, monkeypatch):
    """Newer foreign rows still take window slots, so the rows read do not depend on the owner filter."""
    from api import models

    active_profile("owned")
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", "2")
    rows = [_row("x1", "beta"), _row("x2", "beta"), _row("shared", "owned")]

    models._enrich_sidebar_lineage_metadata(rows)

    assert "_lineage_root_id" not in rows[2]  # third row: outside the window, as on master


# ── profile-agnostic rows (Claude Code JSONL) stay profile-less ───────────

_CLAUDE_META = {"profile": None, "source_tag": "claude_code", "raw_source": "claude_code",
                "session_source": "claude_code", "title": "Claude Code session", "read_only": True}
_MSGS = [{"role": "user", "content": "x", "timestamp": 1.0}, {"role": "assistant", "content": "y", "timestamp": 2.0}]


@pytest.fixture
def claude_code_import(monkeypatch, active_profile):
    from api import routes

    active_profile("owned")
    monkeypatch.setattr(routes, "_refuse_cross_profile_import_cli_before_load", lambda *a, **k: False)
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda cls, sid, *a, **k: None))
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda sid, **k: dict(_CLAUDE_META))
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid, profile=None: list(_MSGS))
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **k: {"status": status, **payload})
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400, **k: {"status": status, "error": msg})
    return routes


@pytest.mark.parametrize("body", [
    {"session_id": "cc-1"},
    {"session_id": "cc-1", "all_profiles": True, "profile": "default"},
])
def test_a_claude_code_import_gets_no_owner_in_a_named_profile(claude_code_import, monkeypatch, body):
    routes = claude_code_import
    monkeypatch.setattr(routes, "_request_wants_all_profiles_import", lambda b: bool(b.get("all_profiles")))
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)

    response = routes._handle_session_import_cli(None, body)

    assert response["status"] == 200, response
    # Profile-less, as on master: never the active named profile.
    assert response["session"]["profile"] is None


def test_a_claimed_claude_code_session_gets_no_owner_in_a_named_profile(claude_code_import, monkeypatch, tmp_path):
    from api import models

    routes = claude_code_import
    monkeypatch.setattr(routes, "_session_index_marks_was_webui", lambda sid: False)
    monkeypatch.setattr(routes, "_session_deleted_tombstone_marks_was_webui", lambda sid: False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "missing.db")

    session, _reason = routes._claim_or_synthesize_cli_session("cc-2", dict(_CLAUDE_META))

    assert session is not None and session.profile is None
