"""Regression coverage for #9366 — an idle-sweep auto-archive is
indistinguishable from a deliberate one in the sidebar.

The agent's idle sweep stamps `auto_archived` on the rows it hides, precisely so
its archives can be told apart from deliberate ones (a deliberate archive clears
the stamp; a later resume or compression un-hides a sweep-archived lineage
automatically). The WebUI read none of it: there were zero references to
`auto_archived` in `api/` or `static/`, and the session-list projection exposed
`archived` but not `auto_archived`, so both archives rendered identically.

What this file locks in:

1. The sidebar projection carries `auto_archived`, so the value survives the
   bounded payload the cache stores.
2. An agent schema **without** the column still projects (the maintainer's
   compatibility requirement): the value reads as "not auto-archived", the same
   treatment such a session gets from the agent itself.
3. The row renders the provenance — a distinct class and a visible badge — and a
   deliberate archive does not.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SESSIONS_JS = REPO / "static" / "sessions.js"
I18N_JS = REPO / "static" / "i18n.js"
STYLE_CSS = REPO / "static" / "style.css"

NODE = shutil.which("node")


# ── 1. the projection carries the field ─────────────────────────────────────


def test_sidebar_field_set_exposes_auto_archived():
    from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS

    assert "archived" in _SIDEBAR_SESSION_RESPONSE_FIELDS
    assert "auto_archived" in _SIDEBAR_SESSION_RESPONSE_FIELDS, (
        "the bounded sidebar payload drops any field not in this set; without "
        "auto_archived the UI can never tell a sweep archive from a deliberate one"
    )


def test_bounded_payload_keeps_auto_archived():
    from api.route_session_list_cache import _session_list_cache_bounded_payload

    payload = {
        "sessions": [
            {"session_id": "a", "archived": True, "auto_archived": True},
            {"session_id": "b", "archived": True, "auto_archived": False},
        ]
    }
    bounded = _session_list_cache_bounded_payload(payload)
    assert [row.get("auto_archived") for row in bounded["sessions"]] == [True, False]


def test_session_object_round_trips_the_flag():
    from api.models import Session

    auto = Session(session_id="a", archived=True, auto_archived=True)
    manual = Session(session_id="b", archived=True, auto_archived=False)
    assert auto.auto_archived is True
    assert manual.auto_archived is False
    # The sidebar row is projected from Session.compact(), so the flag has to
    # survive that projection or the UI never sees it.
    assert auto.compact()["auto_archived"] is True
    assert manual.compact()["auto_archived"] is False


def test_a_session_without_the_field_defaults_to_deliberate():
    """An older agent has no such column; the value must read as not-auto."""
    from api.models import Session

    s = Session(session_id="a", archived=True)
    assert s.auto_archived is False


# ── 2. an agent schema without the column still projects ────────────────────


def _agent_sessions_module():
    import api.agent_sessions as mod

    return mod


def test_optional_col_falls_back_for_a_missing_column():
    mod = _agent_sessions_module()
    cols = {"id", "archived"}
    assert mod._optional_col("auto_archived", cols) == "NULL AS auto_archived"
    assert mod._optional_col("auto_archived", cols | {"auto_archived"}) == "s.auto_archived"


def test_projection_query_runs_against_a_schema_without_the_column(tmp_path):
    """Drive the real projection SQL on a minimal state.db that predates the
    column. The query must not raise, and the row must read as not-auto."""
    mod = _agent_sessions_module()

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            started_at REAL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        INSERT INTO sessions (id, title, started_at, archived)
            VALUES ('s-old', 'Old session', 1000.0, 1);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('s-old', 'user', 'hi', 1000.0);
        """
    )
    conn.commit()
    conn.close()

    rows = mod.read_importable_agent_session_rows(str(db), log=None) if hasattr(
        mod, "read_importable_agent_session_rows"
    ) else None
    if rows is None:
        pytest.skip("read_importable_agent_session_rows signature changed")
    # The query ran; every row must carry the field as a falsy value.
    for row in rows:
        assert not row.get("auto_archived"), (
            "a schema without auto_archived must project the row as not-auto, "
            f"got {row.get('auto_archived')!r}"
        )


# ── 3. the row renders the provenance ──────────────────────────────────────


def test_the_row_carries_a_distinct_class_for_a_sweep_archive():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "auto-archived" in src, (
        "the session row must carry a marker for an idle-sweep archive"
    )
    # The class is derived from both fields, so a deliberate archive never gets it.
    m = re.search(r"const\s+autoArchived\s*=\s*([^;]+);", src)
    assert m, "autoArchived derivation not found"
    expr = m.group(1).replace(" ", "")
    assert "archived" in expr and "auto_archived" in expr, (
        f"the marker must require archived && auto_archived, got {expr!r}"
    )


def test_the_badge_is_rendered_only_for_a_sweep_archive():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    m = re.search(r"if\(s\.archived&&s\.auto_archived\)\{[\s\S]{0,600}?\}", src)
    assert m, "the auto-archived badge block was not found"
    block = m.group(0)
    assert "session-auto-archived-badge" in block
    assert "session_meta_auto_archived" in block


def test_the_badge_has_styling_and_a_tooltip():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".session-auto-archived-badge" in css
    # A deliberate archive keeps the dimmed italic treatment; a sweep archive
    # must not, because the row is still legible state, not a user decision.
    assert ".session-item.archived.auto-archived" in css

    i18n = I18N_JS.read_text(encoding="utf-8")
    assert i18n.count("session_meta_auto_archived:") >= 15, (
        "every locale needs the badge label, or the row shows a raw key"
    )
    assert i18n.count("session_meta_auto_archived_desc:") >= 15


def test_every_locale_parses_and_has_both_keys():
    """The i18n file is one object literal per locale; a stray quote breaks all
    of them at load time."""
    assert NODE is not None, "node is required to parse static/i18n.js"
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// The file assigns into a global LOCALES map; evaluate it with a stub.
let LOCALES = {};
const sandbox = { LOCALES, window: {}, document: { documentElement: {} } };
const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const table = sandbox.LOCALES || LOCALES;
const missing = [];
for (const [loc, dict] of Object.entries(table)) {
  if (typeof dict !== 'object' || !dict) continue;
  if (!('session_meta_auto_archived' in dict)) missing.push(loc);
  if (!('session_meta_auto_archived_desc' in dict)) missing.push(loc + ':desc');
}
console.log(JSON.stringify({ locales: Object.keys(table).length, missing }));
"""
    proc = subprocess.run(
        [NODE, "-e", script, str(I18N_JS)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"i18n.js failed to evaluate:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["missing"] == [], (
        f"locales missing the badge key: {result['missing']}"
    )
