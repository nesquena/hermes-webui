"""#9093: imported CLI sessions keep their profile; lineage enrichment stays in the active profile.

1. ``POST /api/session/import_cli`` and the claim path (``_claim_or_synthesize_cli_session``, reached
   by chat start) took the saved session's profile only from the CLI listing row. That row
   exists only for the newest-N listing, so in a named profile an older session was saved with
   ``profile=None``, backfilled to ``default``, and dropped out of its own profile's sidebar. Both
   paths read such a session from the active profile's state.db, so that profile is its owner.
2. ``_enrich_sidebar_lineage_metadata`` reads lineage from the ACTIVE profile's state.db and applied it
   by bare session id to every row, so in the all-profiles list a foreign row that shares an id with
   an active-profile row took the active row's lineage. It now applies only to active-owned rows.

The route tests drive a real ``server.py`` over HTTP in an isolated home seeded with real Agent
``SessionDB`` sessions.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NOW = time.time() - 5000


def _seed(path: Path, rows) -> None:
    """Write real Agent sessions: (sid, source, parent, model_config, end_reason, session_key)."""
    hermes_state = pytest.importorskip("hermes_state")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = hermes_state.SessionDB(path)
    try:
        for n, (sid, source, parent, marker, reason, key) in enumerate(rows):
            db.create_session(sid, source, parent_session_id=parent, model_config=marker,
                              session_key=key, model="m")
            db.append_message(sid, "user", "hello " + sid, timestamp=NOW + n * 10 + 1)
            db.append_message(sid, "assistant", "answer " + sid, timestamp=NOW + n * 10 + 2)
            if reason:
                db.end_session(sid, reason)
    finally:
        db.close()
    with sqlite3.connect(path) as conn:
        for n, (sid, _source, _parent, _marker, reason, _key) in enumerate(rows):
            conn.execute("UPDATE sessions SET title=?, started_at=?, ended_at=? WHERE id=?",
                         (sid, NOW + n * 10, NOW + n * 10 + 5 if reason else None, sid))


def _free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in (8787, 9222):
            return port


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    hermes_state = pytest.importorskip("hermes_state")
    root = tmp_path_factory.mktemp("issue9093")
    home, state, workspace = root / "home", root / "webui", root / "workspace"
    for d in (home, state, workspace):
        d.mkdir()
    reset = {"_reset_from": "audit_a"}
    # Root profile: a reset chain whose compressed segment ``audit_b2`` is also a foreign row's id,
    # plus a plain compression chain.
    _seed(home / "state.db", [
        ("audit_a", "telegram", None, None, "session_reset", "nav-key"),
        ("audit_b", "telegram", "audit_a", reset, "compression", "nav-key"),
        ("audit_b2", "telegram", "audit_b", reset, "compression", "nav-key"),
        ("audit_b3", "telegram", "audit_b2", reset, None, "nav-key"),
        ("plain_comp_a", "cli", None, None, "compression", "plain-comp-key"),
        ("plain_comp_b", "cli", "plain_comp_a", None, None, "plain-comp-key"),
        # Shares its id with a session of ``other``; here it is a delegated subagent child.
        ("shared_x", "subagent", None, None, None, "shared-x-root"),
    ])
    # ``other``: older filler (so ``other_00`` sits outside its listing window), then a compressed branch
    # whose tip id ``audit_b2`` collides with the root profile's compressed reset segment.
    branched = {"_branched_from": "foreign_parent_chain"}
    _seed(home / "profiles" / "other" / "state.db", [
        *((f"other_{n:02d}", "cli", None, None, None, f"other-key-{n}") for n in range(25)),
        ("foreign_parent_chain", "cli", None, None, None, "foreign-chain"),
        ("foreign_branch_chain", "cli", "foreign_parent_chain", branched, "compression", "foreign-chain"),
        ("audit_b2", "cli", "foreign_branch_chain", branched, None, "foreign-chain"),
        ("shared_x", "cli", None, None, None, "shared-x-other"),
    ])
    # ``owned``: 30 plain CLI sessions (oldest first), then an in-window compression chain.
    _seed(home / "profiles" / "owned" / "state.db", [
        *((f"owned_{n:02d}", "cli", None, None, None, f"owned-key-{n}") for n in range(30)),
        ("owned_chain_a", "cli", None, None, "compression", "owned-chain"),
        ("owned_chain_b", "cli", "owned_chain_a", None, None, "owned-chain"),
    ])
    for name in ("other", "owned"):
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")

    port = _free_port()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "HERMES_HOME": str(home),
        "HERMES_BASE_HOME": str(home),
        "HERMES_CONFIG_PATH": str(home / "config.yaml"),
        "HERMES_WEBUI_STATE_DIR": str(state),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
        "HERMES_WEBUI_AGENT_DIR": str(Path(hermes_state.__file__).resolve().parent),
        "HERMES_WEBUI_PYTHON": sys.executable,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_PASSWORD": "",
        "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(root),
    }
    log_path = root / "server.log"
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, profile=None):
        headers = {"Content-Type": "application/json"}
        if profile:
            headers["Cookie"] = "hermes_profile=" + profile
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, str(REPO / "server.py")], cwd=REPO, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if call("/health")[0] == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail("isolated server did not boot:\n" + log_path.read_text(errors="replace")[-4000:])
            time.sleep(0.2)
        assert call("/api/settings", {"show_cli_sessions": True})[0] == 200

        def ok(path, body=None, profile=None):
            status, payload = call(path, body, profile)
            assert status == 200, (path, status, payload)
            return payload

        def sidecar(sid):
            return json.loads((state / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))

        def rows(path, profile=None):
            return ok(path, profile=profile)["sessions"]

        yield type("Live", (), {"ok": staticmethod(ok), "call": staticmethod(call),
                                "sidecar": staticmethod(sidecar), "rows": staticmethod(rows)})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _row(rows, sid, profile=None):
    found = [r for r in rows if r.get("session_id") == sid and (profile is None or r.get("profile") == profile)]
    assert len(found) == 1, (sid, profile, [(r.get("session_id"), r.get("profile")) for r in rows])
    return found[0]


def _ids(rows, profile=None):
    return {r.get("session_id") for r in rows if profile is None or r.get("profile") == profile}


def test_import_outside_listing_window_keeps_the_active_named_profile(live):
    before = live.rows("/api/sessions", profile="owned")
    assert "owned_29" in _ids(before) and "owned_00" not in _ids(before), "owned_00 must sit outside the CLI window"

    newest = live.ok("/api/session/import_cli", {"session_id": "owned_29"}, profile="owned")
    assert newest["session"]["profile"] == "owned"
    assert live.sidecar("owned_29")["profile"] == "owned"

    older = live.ok("/api/session/import_cli", {"session_id": "owned_00"}, profile="owned")
    assert older["imported"] is True
    assert older["session"]["profile"] == "owned"
    assert live.sidecar("owned_00")["profile"] == "owned"

    owned_rows = live.rows("/api/sessions", profile="owned")
    assert _row(owned_rows, "owned_00")["profile"] == "owned"
    assert _row(owned_rows, "owned_29")["profile"] == "owned"
    assert "owned_00" not in _ids(live.rows("/api/sessions")), "must not move into the root profile"
    everyone = live.rows("/api/sessions?all_profiles=1", profile="owned")
    assert [r.get("profile") for r in everyone if r.get("session_id") == "owned_00"] == ["owned"]
    # Re-import is a refresh of the profile's own session, not a cross-profile refusal.
    again = live.ok("/api/session/import_cli", {"session_id": "owned_00"}, profile="owned")
    assert again["imported"] is False and again["session"]["profile"] == "owned"


def test_claim_path_outside_listing_window_keeps_the_active_named_profile(live):
    # Chat start on a CLI session with no sidecar claims it through _claim_or_synthesize_cli_session
    # and saves the sidecar before the profile gate. No message, so the request stops at message
    # validation (after that gate) and no agent run starts.
    status, payload = live.call("/api/chat/start", {"session_id": "owned_01"}, profile="owned")
    assert live.sidecar("owned_01")["profile"] == "owned"
    assert (status, payload.get("error")) == (400, "message is required")
    # Profile-scoped detail load: the owning profile opens it, the root profile is told it is another
    # profile's session. (Sidebar listing of a short claimed CLI copy is governed by the separate CLI
    # row-visibility heuristic, so it is not asserted here.)
    assert live.ok("/api/session?session_id=owned_01", profile="owned")["session"]["profile"] == "owned"
    status, payload = live.call("/api/session?session_id=owned_01")
    assert (status, payload.get("code"), payload.get("profile")) == (409, "session_profile_mismatch", "owned")


def test_claim_helper_stamps_the_profile_it_read_from(tmp_path, monkeypatch):
    routes = pytest.importorskip("api.routes")
    models = pytest.importorskip("api.models")
    from api.profiles import clear_request_profile, set_request_profile

    db = tmp_path / "state.db"
    _seed(db, [("tui_9093", "tui", None, None, None, "tui-key")])
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index = sessions_dir / "_index.json"
    index.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid, **_kw: {})
    set_request_profile("owned")
    try:
        assert routes.get_active_profile_name() == "owned"
        synth, reason = routes._claim_or_synthesize_cli_session("tui_9093")
        assert reason == "materialized" and synth.profile == "owned"
        # Listing metadata still wins when present.
        synth, _ = routes._claim_or_synthesize_cli_session("tui_9093", cli_meta={"profile": "owned"})
        assert synth.profile == "owned"
        # Claude Code transcripts are global, not read from any profile's state.db: stay untagged.
        monkeypatch.setattr(routes, "get_cli_session_messages",
                            lambda _sid, **_kw: [{"role": "user", "content": "hi"}])
        synth, reason = routes._claim_or_synthesize_cli_session(
            "claude_code_9093", cli_meta={"source_tag": "claude_code", "read_only": True})
        assert reason == "not_claimable" and synth.profile is None
    finally:
        clear_request_profile()


def test_all_profiles_lineage_keeps_the_foreign_rows_own_projection(live):
    own = _row(live.rows("/api/sessions", profile="other"), "audit_b2")
    everyone = live.rows("/api/sessions?all_profiles=1")
    foreign = _row(everyone, "audit_b2", profile="other")
    keys = ("relationship_type", "_lineage_root_id", "_lineage_tip_id", "_compression_segment_count")
    assert {k: foreign.get(k) for k in keys} == {k: own.get(k) for k in keys}
    assert foreign["_lineage_root_id"] == "foreign_branch_chain"
    assert foreign["_lineage_tip_id"] == "audit_b2"
    assert foreign.get("relationship_type") == "child_session"
    # Its parent comes from its own profile, never from the active profile's reset chain.
    assert foreign["parent_session_id"] in ("foreign_parent_chain", "foreign_branch_chain")


def test_all_profiles_list_keeps_the_foreign_rows_own_source_and_access(live):
    """The list route's state.db overrides (source, read-only, counts) also stay with the active
    profile's own rows: a foreign row sharing an id with a root subagent child keeps its own."""
    live.ok("/api/session/import_cli", {"session_id": "shared_x"}, profile="other")
    own = _row(live.rows("/api/sessions", profile="other"), "shared_x")
    foreign = _row(live.rows("/api/sessions?all_profiles=1"), "shared_x", profile="other")
    keys = ("source_tag", "raw_source", "session_source", "source_label", "is_cli_session",
            "read_only", "message_count")
    assert {k: foreign.get(k) for k in keys} == {k: own.get(k) for k in keys}
    assert not foreign.get("read_only") and foreign.get("source_tag") != "subagent"


def test_root_profile_lineage_collapse_is_unchanged(live):
    rows = live.rows("/api/sessions")
    tip = _row(rows, "plain_comp_b")
    assert tip["_lineage_root_id"] == "plain_comp_a" and tip["_lineage_tip_id"] == "plain_comp_b"
    assert "plain_comp_a" not in _ids(rows), "compressed segment must collapse into its tip"
    imported = live.ok("/api/session/import_cli", {"session_id": "audit_b3"})
    assert imported["session"]["profile"] == "default"
    root_rows = live.rows("/api/sessions")
    reset_tip = _row(root_rows, "audit_b3")
    assert reset_tip["profile"] == "default"
    assert reset_tip["_lineage_tip_id"] == "audit_b3" and reset_tip["_lineage_root_id"]
    # The root profile's own row keeps its active-db lineage in the all-profiles view too.
    everyone = live.rows("/api/sessions?all_profiles=1")
    assert _row(everyone, "audit_b3", profile="default")["_lineage_tip_id"] == "audit_b3"


def test_named_profile_lineage_collapse_is_unchanged(live):
    rows = live.rows("/api/sessions", profile="owned")
    tip = _row(rows, "owned_chain_b")
    assert tip["_lineage_root_id"] == "owned_chain_a" and tip["_lineage_tip_id"] == "owned_chain_b"
    assert "owned_chain_a" not in _ids(rows)
    imported = live.ok("/api/session/import_cli", {"session_id": "owned_chain_b"}, profile="owned")
    assert imported["session"]["profile"] == "owned"
    tip = _row(live.rows("/api/sessions", profile="owned"), "owned_chain_b")
    assert tip["profile"] == "owned"
    assert tip["_lineage_root_id"] == "owned_chain_a" and tip["_lineage_tip_id"] == "owned_chain_b"


def test_all_profiles_import_with_requested_profile_is_unchanged(live):
    payload = live.ok("/api/session/import_cli",
                      {"session_id": "other_00", "profile": "other", "all_profiles": True})
    assert payload["session"]["profile"] == "other"
    assert live.sidecar("other_00")["profile"] == "other"
    assert _row(live.rows("/api/sessions", profile="other"), "other_00")["profile"] == "other"
    assert "other_00" not in _ids(live.rows("/api/sessions"))
    # An unqualified import from the root profile still refuses the foreign session.
    status, _ = live.call("/api/session/import_cli", {"session_id": "other_01"})
    assert status == 404


# The tests above need the Agent's ``hermes_state`` (real sessions and a real server) and are skipped
# where it isn't installed. The two below drive the same code in process without it.

_SUBAGENT_OVERRIDE = {
    "_state_db_source": "subagent",
    "_state_db_source_tag": "subagent",
    "_state_db_raw_source": "subagent",
    "_state_db_session_source": "other",
    "_state_db_source_label": "Subagent",
}


def _overlay_rows():
    common = {"source_tag": "cli", "raw_source": "cli", "session_source": "cli", "source_label": "CLI",
              "is_cli_session": True, "read_only": False, "message_count": 2}
    return [{"session_id": "x_9093", "profile": "other", **common},
            {"session_id": "y_9093", "profile": None, **common}]


@pytest.mark.parametrize("active, owned_sid, foreign_sid", [
    ("default", "y_9093", "x_9093"),  # an untagged row is the root profile's
    ("other", "x_9093", "y_9093"),
])
def test_state_db_overlays_apply_only_to_active_owned_rows_in_process(monkeypatch, tmp_path, active, owned_sid,
                                                                      foreign_sid):
    models = pytest.importorskip("api.models")
    from api.profiles import clear_request_profile, get_active_profile_name, set_request_profile

    asked = {}

    def overrides(_path, ids, count_session_ids=None):
        asked["overrides"] = set(ids)
        return {sid: dict(_SUBAGENT_OVERRIDE) for sid in ids}

    def lineage(_path, ids):
        asked["lineage"] = set(ids)
        return {sid: {**_SUBAGENT_OVERRIDE, "_lineage_root_id": "root_" + sid, "relationship_type": "child_session"}
                for sid in ids}

    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", overrides)
    monkeypatch.setattr(models, "read_session_lineage_metadata", lineage)
    set_request_profile(active)
    try:
        assert get_active_profile_name() == active
        before = {r["session_id"]: dict(r) for r in _overlay_rows()}

        rows = _overlay_rows()
        models._apply_sidebar_state_db_overrides(rows)
        by_id = {r["session_id"]: r for r in rows}
        assert asked["overrides"] == {owned_sid}
        assert by_id[foreign_sid] == before[foreign_sid]
        assert by_id[owned_sid]["source_tag"] == "subagent" and by_id[owned_sid]["read_only"] is True

        rows = _overlay_rows()
        models._enrich_sidebar_lineage_metadata(rows)
        by_id = {r["session_id"]: r for r in rows}
        assert asked["lineage"] == {owned_sid}
        assert by_id[foreign_sid] == before[foreign_sid]
        assert by_id[owned_sid]["_lineage_root_id"] == "root_" + owned_sid
        assert by_id[owned_sid]["read_only"] is True
    finally:
        clear_request_profile()


def test_import_and_claim_stamp_the_owner_profile_in_process(monkeypatch, tmp_path):
    routes = pytest.importorskip("api.routes")
    models = pytest.importorskip("api.models")
    from api.profiles import clear_request_profile, set_request_profile

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index = sessions_dir / "_index.json"
    index.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "no-state.db")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    transcript = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid, **_kw: list(transcript))
    set_request_profile("owned")
    try:
        # The owner: the listing row's (or authorized requested) profile, else the active one;
        # Claude Code transcripts are global and stay untagged.
        assert routes._cli_read_owner_profile("cli_9093") == "owned"
        assert routes._cli_read_owner_profile("cli_9093", "listed") == "listed"
        assert routes._cli_read_owner_profile("claude_code_9093") is None
        assert routes._cli_read_owner_profile("claude_code_9093", "listed") == "listed"

        # Claim path (chat start / GET of a sidecar-less session).
        tui = {"source_tag": "tui", "raw_source": "tui", "session_source": "other"}
        synth, reason = routes._claim_or_synthesize_cli_session("tui_ci_9093", cli_meta=dict(tui))
        assert reason == "materialized" and synth.profile == "owned"
        synth, _ = routes._claim_or_synthesize_cli_session("tui_ci_9093", cli_meta={**tui, "profile": "listed"})
        assert synth.profile == "listed"

        # Import path, through its read-only branch (which saves nothing).
        replies = []
        monkeypatch.setattr(routes, "j", lambda _h, payload, status=200, **_kw: replies.append((status, payload)))
        monkeypatch.setattr(routes, "bad", lambda _h, msg, status=400, **_kw: replies.append((status, msg)))
        monkeypatch.setattr(routes, "get_last_workspace", lambda **_kw: str(tmp_path))
        monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda _sid: False)
        for listed, expected in ((None, "owned"), ("listed", "listed")):
            meta = {"session_id": "tg_ci_9093", "source_tag": "telegram", "read_only": True}
            if listed:
                meta["profile"] = listed
            monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda _sid, _m=meta, **_kw: dict(_m))
            routes._handle_session_import_cli(None, {"session_id": "tg_ci_9093"})
            status, payload = replies.pop()
            assert status == 200, payload
            assert payload["session"]["profile"] == expected and payload["session"]["read_only"] is True
        assert not (sessions_dir / "tg_ci_9093.json").exists()
    finally:
        clear_request_profile()

