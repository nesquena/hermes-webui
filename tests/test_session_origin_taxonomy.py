"""Tests for the sidebar's high-level session-origin taxonomy."""

from pathlib import Path
import json
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _extract_function(source_text, function_name):
    marker = f"function {function_name}("
    return _extract_from_marker(source_text, marker)


def _extract_statement(source_text, marker):
    """Return the full statement starting at `marker`, up through the `;`
    that closes it at bracket/paren/brace depth 0.

    Unlike `_extract_from_marker` (single brace-matched block), this handles
    a multi-line statement like `const sourceRowsById=new Map(\n  ...\n);` —
    depth tracks all three bracket kinds so a statement can span function
    calls, array/object literals, and arrow bodies before its terminating
    semicolon.
    """
    start = source_text.index(marker)
    depth = 0
    openers = "([{"
    closers = ")]}"
    for index in range(start, len(source_text)):
        char = source_text[index]
        if char in openers:
            depth += 1
        elif char in closers:
            depth -= 1
        elif char == ";" and depth == 0:
            return source_text[start : index + 1]
    raise AssertionError(f"Could not find closing ';' at depth 0 for marker: {marker!r}")


def _extract_from_marker(source_text, marker):
    """Brace-match a code block starting at the first `{` after `marker`.

    Unlike `_extract_function`, `marker` need not be a `function name(...)`
    declaration — this also pulls out `const x=(...)=>{...}` closures like
    `effectiveOrigin`, so the test exercises the actual literal source instead
    of a hand-copied reimplementation that can silently drift from it.
    """
    start = source_text.index(marker)
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract block at marker: {marker!r}")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_source_filter_model_keeps_every_origin_readable_without_a_tab_strip():
    """Dropping or truncating dynamic adapters must break the source-control contract."""
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    normalize_fn = _extract_function(source, "_normalizeSessionSourceFilters")
    model_fn = _extract_function(source, "_sessionSourceFilterModel")
    script = f"""
global._sessionSourceFilters = ['matrix', 'telegram', 'slack', 'discord'];
global._serverSessionOriginCounts = {{webui: 17, cli: 2, matrix: 220, telegram: 14, slack: 8, discord: 5}};
global._serverSessionOriginLabels = {{
  webui: 'WebUI sessions',
  cli: 'CLI sessions',
  matrix: 'Matrix sessions',
  telegram: 'Telegram sessions',
  slack: 'Slack sessions',
  discord: 'Discord sessions',
}};
global._sessionOriginKeys = () => ['webui', 'cli', 'matrix', 'telegram', 'slack', 'discord'];
global._sessionSourceTabCount = (origin) => global._serverSessionOriginCounts[origin];
global._sessionSourceLabel = (origin, count) => `${{global._serverSessionOriginLabels[origin]}} (${{count}})`;
global._sessionOriginLabel = (origin) => global._serverSessionOriginLabels[origin];
{normalize_fn}
{model_fn}
console.log(JSON.stringify(_sessionSourceFilterModel(null, null)));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    model = json.loads(result.stdout)

    assert model == {
        "selectedOrigins": ["matrix", "telegram", "slack", "discord"],
        "visibleChips": [
            {"origin": "matrix", "label": "Matrix sessions"},
            {"origin": "telegram", "label": "Telegram sessions"},
        ],
        "overflowCount": 2,
        "originCount": 6,
        "items": [
            {"origin": "webui", "label": "WebUI sessions", "count": 17, "selected": False},
            {"origin": "cli", "label": "CLI sessions", "count": 2, "selected": False},
            {"origin": "matrix", "label": "Matrix sessions", "count": 220, "selected": True},
            {"origin": "telegram", "label": "Telegram sessions", "count": 14, "selected": True},
            {"origin": "slack", "label": "Slack sessions", "count": 8, "selected": True},
            {"origin": "discord", "label": "Discord sessions", "count": 5, "selected": True},
        ],
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_source_menu_item_uses_checkbox_and_reports_immediate_checked_state():
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    render_fn = _extract_function(source, "_renderSessionSourceMenuItem")
    script = f"""
global.document = {{
  createElement(tag) {{
    return {{
      tagName: tag.toUpperCase(), type: '', className: '', textContent: '', checked: false,
      dataset: {{}}, attrs: {{}},
      children: [],
      appendChild(child) {{ this.children.push(child); }},
      setAttribute(key, value) {{ this.attrs[key] = value; }},
    }};
  }},
}};
{render_fn}
const changes = [];
const row = _renderSessionSourceMenuItem(
  {{origin:'slack', label:'Slack sessions', count:8, selected:false}},
  (origin, selected) => changes.push([origin, selected])
);
const checkbox = row.children[0];
checkbox.checked = true;
checkbox.onchange({{stopPropagation(){{}}}});
console.log(JSON.stringify({{
  rowTag: row.tagName,
  checkboxTag: checkbox.tagName,
  checkboxType: checkbox.type,
  origin: checkbox.dataset.origin,
  initialSelected: checkbox.attrs['aria-checked'],
  rowRole: row.attrs['role'],
  checkboxRole: checkbox.attrs['role'],
  changes,
}}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "rowTag": "DIV",
        "checkboxTag": "INPUT",
        "checkboxType": "checkbox",
        "origin": "slack",
        "initialSelected": "false",
        "rowRole": "presentation",
        "checkboxRole": "menuitemcheckbox",
        "changes": [["slack", True]],
    }


def test_sidebar_origin_preserves_raw_channel_identity():
    from api.routes import _normalize_sidebar_source_flags, _sidebar_session_origin

    cases = {
        "webui": "webui",
        "cli": "cli",
        "tui": "tui",
        "matrix": "matrix",
        "telegram": "telegram",
        "slack": "slack",
        "discord": "discord",
        "api_server": "api",
    }
    for raw, expected in cases.items():
        row = {"source_tag": raw, "session_source": "messaging"}
        assert _sidebar_session_origin(row) == expected
        assert _normalize_sidebar_source_flags(row)["session_origin"] == expected


def test_sidebar_origin_defaults_blank_rows_to_webui_and_unknown_rows_to_their_source():
    from api.routes import _sidebar_session_origin

    assert _sidebar_session_origin({"session_id": "native"}) == "webui"
    assert _sidebar_session_origin({"source_tag": "new_adapter"}) == "new_adapter"
    assert _sidebar_session_origin({"session_source": "cli", "is_cli_session": True}) == "cli"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_client_origin_preserves_legacy_webui_markers():
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    origin_fn = _extract_function(source, "_sessionOrigin")
    script = f"""
const _SESSION_ORIGIN_ORDER = ['webui','cli','subagent','other'];
function _isCliSession() {{ return false; }}
{origin_fn}
console.log(JSON.stringify([
  _sessionOrigin({{source:'webui', session_source:'webui'}}),
  _sessionOrigin({{raw_source:'webui'}}),
  _sessionOrigin({{source_tag:'webui'}}),
]));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["webui", "webui", "webui"]


def test_sidebar_payload_exposes_origin_metadata_and_dynamic_filtering_contract(monkeypatch):
    import api.routes as routes
    import api.profiles as profiles

    rows = []
    for sid, source in (("matrix-1", "matrix"), ("telegram-1", "telegram"), ("tui-1", "tui")):
        rows.append({
            "session_id": sid,
            "title": sid,
            "profile": "default",
            "source_tag": source,
            "raw_source": source,
            "session_source": "messaging" if source in {"matrix", "telegram"} else "cli",
            "source_label": source.title(),
            "message_count": 2,
            "actual_message_count": 2,
            "updated_at": 10,
            "last_message_at": 10,
            "archived": False,
        })
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: list(rows))
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=True,
        show_cron_sessions=False,
        show_matrix_sessions=True,
        sidebar_source="matrix",
        visible_only=True,
    )
    assert [row["session_id"] for row in payload["sessions"]] == ["matrix-1"]
    assert payload["session_origin_counts"] == {"matrix": 1, "telegram": 1, "tui": 1}
    assert payload["session_origin_labels"]["matrix"] == "Matrix sessions"


def test_webui_filtered_payload_counts_available_state_db_origins(monkeypatch, tmp_path):
    import sqlite3
    import api.routes as routes

    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute("create table sessions (id text primary key, source text)")
    con.executemany(
        "insert into sessions (id, source) values (?, ?)",
        [("matrix-1", "matrix"), ("telegram-1", "telegram")],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)
    webui_row = {
        "session_id": "webui-1",
        "title": "WebUI session",
        "profile": "default",
        "source": "webui",
        "message_count": 2,
        "updated_at": 20,
        "last_message_at": 20,
        "archived": False,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [dict(webui_row)])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [])

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=False,
        sidebar_source="webui",
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["webui-1"]
    # session_origin_counts now only from filtered scoped rows, not max-merged state.db aggregate
    assert payload["session_origin_counts"] == {
        "webui": 1,
    }


def test_sidebar_payload_exposes_origin_metadata_fields():
    routes = REPO_ROOT / "api" / "routes.py"
    source = routes.read_text(encoding="utf-8")
    assert '"session_origin_counts"' in source
    assert '"session_origin_labels"' in source
    assert "_effective_sidebar_origin(s) in selected_sidebar_source_set" in source
    assert '"session_origin",' in source


def test_sidebar_frontend_renders_origin_tabs_and_accepts_non_cli_origins():
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "_serverSessionOriginCounts" in source
    assert "session_origin_counts" in source
    assert "_sessionOriginKeys" in source
    assert "selectedOrigins.has(_sessionOrigin(s))" in source
    assert "session_origin" in source


def _client_effective_origin_script(all_matched_js, lookups_js):
    """Build a script that runs the REAL effectiveOrigin closure (and its
    real sourceRowsById/_rowProfileKey/_rowLineageKey helpers) straight out
    of sessions.js — not a hand-copied reimplementation — against a test
    `allMatched` array, then logs whatever `lookups_js` (a JS object literal
    body referencing `bySid`, a plain by-session_id Map, and `effectiveOrigin`)
    produces. Shared by every client-side effectiveOrigin test so a change to
    sessions.js's helper set only needs updating once.
    """
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    order_line = next(line for line in source.splitlines() if line.startswith("const _SESSION_ORIGIN_ORDER="))
    origin_fn = _extract_function(source, "_sessionOrigin")
    is_child_fn = _extract_function(source, "_isChildSession")
    is_cli_fn = _extract_function(source, "_isCliSession")
    is_messaging_fn = _extract_function(source, "_isMessagingSession")
    row_profile_key_stmt = _extract_statement(source, "const _rowProfileKey=")
    row_lineage_key_stmt = _extract_statement(source, "const _rowLineageKey=")
    source_rows_stmt = _extract_statement(source, "const sourceRowsById=")
    effective_origin_block = _extract_from_marker(source, "const effectiveOrigin=s=>{")
    return f"""
{order_line}
const _MESSAGING_RAW_SOURCES = new Set();
{is_messaging_fn}
{is_cli_fn}
{origin_fn}
{is_child_fn}
const allMatched = {all_matched_js};
{row_profile_key_stmt}
{row_lineage_key_stmt}
{source_rows_stmt}
const effectiveOrigin={effective_origin_block.split("=", 1)[1]}
const bySid = new Map(allMatched.map(s => [s.session_id, s]));
console.log(JSON.stringify({lookups_js}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_effective_origin_inherits_plain_webui_grandparent_through_subagent_child():
    """A legacy subagent-marked child with no explicit session_origin must
    resolve to its grandparent's real origin, not stay stuck on the
    'subagent' compatibility sentinel — the exact case CHANGES_REQUESTED
    review round 2 on #6985 flagged: the walk previously fell back to
    `_sessionOrigin(s)` (the *original* argument) instead of `_sessionOrigin(cur)`
    (the resolved ancestor), silently discarding the walk whenever every
    hop in the chain was itself a placeholder ('webui' or 'subagent')."""
    script = _client_effective_origin_script(
        """[
  {session_id:'grandparent', title:'root'},
  {session_id:'child', parent_session_id:'grandparent', relationship_type:'child_session', session_origin:'subagent'},
  {session_id:'grandchild', parent_session_id:'child', relationship_type:'child_session', session_origin:'subagent'},
]""",
        """{
  child: effectiveOrigin(bySid.get('child')),
  grandchild: effectiveOrigin(bySid.get('grandchild')),
}""",
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"child": "webui", "grandchild": "webui"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_effective_origin_resolves_real_external_ancestor_and_falls_back_deterministically_on_cycle():
    """#6985 review round 3: the client walk's exhausted-chain fallback must
    return the single deterministic 'webui' sentinel, not re-derive from
    `cur`/`s` (which could echo a non-standard explicit marker raw). Covers
    two cases against the REAL effectiveOrigin closure (not a reimplementation):
    (1) a genuine external ancestor present in the loaded row set is inherited
    correctly through a subagent child, and (2) a parent_session_id cycle among
    loaded rows terminates via the existing visited-set bound and falls back
    to 'webui' instead of hanging or returning something else raw."""
    script = _client_effective_origin_script(
        """[
  {session_id:'external-ancestor', session_origin:'matrix'},
  {session_id:'subagent-child', parent_session_id:'external-ancestor', relationship_type:'child_session', session_origin:'subagent'},
  {session_id:'cycle-a', parent_session_id:'cycle-b', relationship_type:'child_session', session_origin:'subagent'},
  {session_id:'cycle-b', parent_session_id:'cycle-a', relationship_type:'child_session', session_origin:'subagent'},
]""",
        """{
  externalAncestor: effectiveOrigin(bySid.get('subagent-child')),
  cycle: effectiveOrigin(bySid.get('cycle-a')),
}""",
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"externalAncestor": "matrix", "cycle": "webui"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_effective_origin_classifies_definitive_ancestor_reached_exactly_at_hop_limit():
    """#6985 review round 4: both walks classify `cur` then advance to its
    parent, so if the walk's LAST permitted hop is the one that resolves an
    external ancestor, the loop ran out of iterations before ever
    classifying it — silently dropping a genuinely-resolved ancestor to the
    'webui' give-up value instead of honoring it. Covers the exact boundary
    (ancestor reached on hop 16, the last one allowed) and one hop beyond it
    (ancestor would need hop 17, which must still fail closed to 'webui')."""
    # A chain of 16 subagent hops (child_1 -> child_2 -> ... -> child_16)
    # whose parent_session_id finally points at a real external ancestor.
    # child_1's own walk: hop 1 resolves child_2 ... hop 16 resolves the
    # external ancestor itself — exactly the last permitted iteration.
    at_limit = [
        f"{{session_id:'child_{i}', parent_session_id:'{'external-ancestor' if i == 16 else f'child_{i + 1}'}', relationship_type:'child_session', session_origin:'subagent'}}"
        for i in range(1, 17)
    ]
    at_limit.append("{session_id:'external-ancestor', session_origin:'matrix'}")
    script = _client_effective_origin_script(
        "[\n  " + ",\n  ".join(at_limit) + ",\n]",
        "{ atLimit: effectiveOrigin(bySid.get('child_1')) }",
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"atLimit": "matrix"}

    # One hop further: the external ancestor is now 17 hops away from
    # child_1 (child_1 -> ... -> child_17 -> external-ancestor), one past
    # what the bound allows — must still fail closed to 'webui', not error
    # or classify something reached beyond the bound.
    beyond_limit = [
        f"{{session_id:'child_{i}', parent_session_id:'{'external-ancestor' if i == 17 else f'child_{i + 1}'}', relationship_type:'child_session', session_origin:'subagent'}}"
        for i in range(1, 18)
    ]
    beyond_limit.append("{session_id:'external-ancestor', session_origin:'matrix'}")
    script = _client_effective_origin_script(
        "[\n  " + ",\n  ".join(beyond_limit) + ",\n]",
        "{ beyondLimit: effectiveOrigin(bySid.get('child_1')) }",
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"beyondLimit": "webui"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_source_chip_labels_pass_scalar_args_not_objects():
    """#6985 review round 2: the localized source-chip controls called
    `t(key, {{count: n}})` / `t(key, {{label: x}})`, but `t()` forwards its
    arguments positionally to the locale function, which expects a bare
    scalar. The object form rendered literal '[object Object]' text and
    made `Number({{count:1}})` (NaN) so the singular branch could never
    fire. Assert both the count-1 (singular) and count-many (plural) shapes
    render real text, not '[object Object]' or 'NaN'."""
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    trigger_line = next(
        line for line in source.splitlines()
        if "t('session_source_trigger'" in line and "textContent" in line
    )
    remove_line = next(
        line for line in source.splitlines()
        if "t('session_source_remove'" in line
    )
    assert "{count:" not in trigger_line and "{ count:" not in trigger_line
    assert "{label:" not in remove_line and "{ label:" not in remove_line
    script = """
global.t = (key, arg) => {
  if (key === 'session_source_trigger') {
    const n = Number(arg);
    return n === 1 ? 'Source' : `Sources (${n})`;
  }
  if (key === 'session_source_remove') return `Remove ${arg}`;
  return key;
};
console.log(JSON.stringify({
  singular: global.t('session_source_trigger', 1),
  plural: global.t('session_source_trigger', 5),
  remove: global.t('session_source_remove', 'Slack'),
}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    rendered = json.loads(result.stdout)
    assert rendered == {"singular": "Source", "plural": "Sources (5)", "remove": "Remove Slack"}
    assert "[object Object]" not in json.dumps(rendered)
    assert "NaN" not in json.dumps(rendered)


def _make_state_db_rows(path, rows):
    """Create a minimal state.db at ``path`` with one row per (sid, source,
    parent_session_id) in ``rows``. Schema mirrors the subset
    _state_db_lineage_lookup actually reads."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT)"
    )
    for sid, source, parent_session_id in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, parent_session_id) VALUES (?, ?, ?)",
            (sid, source, parent_session_id),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def origin_fallback_env(tmp_path, monkeypatch):
    """Isolated environment for the missing-ancestor state.db fallback:
    an empty WebUI sessions dir (so Session.load_metadata_only() always
    misses — no sidecar exists for any id used here, matching the real
    delegated-subagent shape from #5307) plus per-profile state.db files
    the fallback can be pointed at via _get_profile_home.
    """
    import api.models as models
    import api.routes as routes

    sessions_dir = tmp_path / "webui-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    profile_homes = {}

    def _get_profile_home(profile):
        home = profile_homes.setdefault(str(profile), tmp_path / f"profile-{profile}")
        home.mkdir(parents=True, exist_ok=True)
        return home

    monkeypatch.setattr(models, "_get_profile_home", _get_profile_home)

    default_db = tmp_path / "default-state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: default_db)

    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [])

    return {"sessions_dir": sessions_dir, "profile_homes": profile_homes, "default_db": default_db}


def _run_payload(routes, leaf_row, *, active_profile, sidebar_source):
    import api.routes as _routes  # noqa: F401  (routes param kept for readability)

    payload = routes._build_session_list_cache_payload(
        active_profile=active_profile,
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=True,
        sidebar_source=sidebar_source,
    )
    return [s["session_id"] for s in payload["sessions"]]


def test_effective_sidebar_origin_resolves_external_ancestor_with_no_sidecar_via_state_db(
    origin_fallback_env, monkeypatch,
):
    """#6985 review round 3: the missing-ancestor fallback only read the
    WebUI sidecar (Session.load_metadata_only), which misses every delegated
    subagent ancestor — those ran server-side and have NO sidecar at all
    (#5307), only a state.db row. Covers external parent -> subagent child
    -> grandchild, with BOTH ancestors omitted from the payload builder's
    loaded rows (only the leaf grandchild is 'in scope'), proving the
    grandchild inherits the external origin through two state.db-only hops.
    """
    import api.routes as routes

    # Use the fixture's _get_profile_home so the path matches exactly what
    # _agent_state_db_path(profile="default") will resolve.
    from api.models import _get_profile_home
    home = _get_profile_home("default")
    db_path = home / "state.db"
    _make_state_db_rows(
        db_path,
        [
            ("external-ancestor", "matrix", None),
            ("subagent-child", "subagent", "external-ancestor"),
        ],
    )
    assert not (origin_fallback_env["sessions_dir"] / "external-ancestor.json").exists()
    assert not (origin_fallback_env["sessions_dir"] / "subagent-child.json").exists()

    grandchild_row = {
        "session_id": "grandchild-of-ghost",
        "parent_session_id": "subagent-child",
        "message_count": 1,
        "project_id": None,
        "profile": "default",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [grandchild_row])

    assert _run_payload(routes, grandchild_row, active_profile="default", sidebar_source="matrix") == [
        "grandchild-of-ghost"
    ]
    assert _run_payload(routes, grandchild_row, active_profile="default", sidebar_source="webui") == []


def test_effective_sidebar_origin_does_not_leak_ancestor_across_profiles(
    origin_fallback_env, monkeypatch,
):
    """The state.db fallback must be profile-scoped: an ancestor that only
    exists in a DIFFERENT profile's state.db must not be found when
    resolving a child under the active profile — the sidebar filter must
    fall back to 'webui', not leak the other profile's classification.

    Deliberately does NOT create the "default" profile's own state.db file
    first. Verified this reproduces a real leak against the original
    implementation: ``_state_db_lineage_lookup`` used to resolve the target
    profile's db via ``_agent_state_db_path()``, which falls back to
    ``_active_state_db_path()`` (the server-wide currently-active session's
    db, independent of the profile this payload is being built for)
    whenever the named profile's own db file doesn't exist yet — silently
    serving a foreign profile's data instead of correctly resolving to
    nothing. Populating the mocked "active" db (not "default"'s own) with
    the foreign row reproduces that leak; the fix resolves the named
    profile's path directly with no such fallback.
    """
    import api.routes as routes
    from api.models import _get_profile_home

    other_home = _get_profile_home("other-profile")
    _make_state_db_rows(other_home / "state.db", [("cross-profile-ancestor", "matrix", None)])
    _make_state_db_rows(origin_fallback_env["default_db"], [("cross-profile-ancestor", "matrix", None)])

    child_row = {
        "session_id": "child-of-cross-profile-ghost",
        "parent_session_id": "cross-profile-ancestor",
        "message_count": 1,
        "project_id": None,
        "profile": "default",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [child_row])

    assert _run_payload(routes, child_row, active_profile="default", sidebar_source="matrix") == []
    assert _run_payload(routes, child_row, active_profile="default", sidebar_source="webui") == [
        "child-of-cross-profile-ghost"
    ]


def test_effective_sidebar_origin_missing_ancestor_row_falls_back_to_webui(
    origin_fallback_env, monkeypatch,
):
    """A parent_session_id that exists in NEITHER a sidecar NOR any state.db
    row (already deleted, or never existed) must fall back to the
    deterministic 'webui' sentinel, not raise or misclassify."""
    import api.routes as routes
    from api.models import _get_profile_home

    _make_state_db_rows(_get_profile_home("default") / "state.db", [])

    child_row = {
        "session_id": "child-of-nothing",
        "parent_session_id": "truly-gone",
        "message_count": 1,
        "project_id": None,
        "profile": "default",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [child_row])

    assert _run_payload(routes, child_row, active_profile="default", sidebar_source="webui") == [
        "child-of-nothing"
    ]
    assert _run_payload(routes, child_row, active_profile="default", sidebar_source="matrix") == []


def test_effective_sidebar_origin_cycle_falls_back_to_webui_without_hanging(
    origin_fallback_env, monkeypatch,
):
    """A parent_session_id cycle resolved entirely through the state.db
    fallback (A -> B -> A) must terminate via the existing visited-set bound
    and fall back to 'webui', not loop forever."""
    import api.routes as routes
    from api.models import _get_profile_home

    _make_state_db_rows(
        _get_profile_home("default") / "state.db",
        [("cycle-a", None, "cycle-b"), ("cycle-b", None, "cycle-a")],
    )

    child_row = {
        "session_id": "child-of-cycle",
        "parent_session_id": "cycle-a",
        "message_count": 1,
        "project_id": None,
        "profile": "default",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [child_row])

    assert _run_payload(routes, child_row, active_profile="default", sidebar_source="webui") == [
        "child-of-cycle"
    ]


def test_effective_origin_fallback_lookup_queries_state_db_at_most_once_per_ancestor(
    origin_fallback_env, monkeypatch,
):
    """Repeated-miss/query-bound (#6985 review round 3, item 1).

    A HIT (the ancestor resolves successfully) is already deduped by the
    pre-existing ``_effective_session_by_id`` cache regardless of whether
    ``_effective_origin_fallback_lookup``'s own ``_origin_fallback_cache``
    exists at all: `_effective_session_by_id[pid] = parent` only runs on a
    successful resolution, so re-testing that path proves nothing about the
    NEW memoization layer (verified: deleting `_origin_fallback_cache`'s
    early-return still passes a same-ancestor-HIT version of this test).

    The new cache's actual, distinct value is deduping repeated MISSES: on
    a miss `_effective_origin_fallback_lookup` returns None and the caller
    `break`s BEFORE ever writing to `_effective_session_by_id` — so without
    `_origin_fallback_cache` covering the miss too, every child referencing
    the same nonexistent ancestor re-triggers a full sidecar-then-state.db
    lookup. Assert both the sidecar read (`Session.load_metadata_only`) and
    the state.db read are each invoked at most once despite 5 children all
    missing on the same nonexistent ancestor id.
    """
    import api.models as models
    import api.routes as routes
    from api.models import _get_profile_home

    home = _get_profile_home("default")
    _make_state_db_rows(home / "state.db", [])  # ancestor id never exists

    sidecar_calls = {"n": 0}
    state_db_calls = {"n": 0}
    real_state_db_lookup = routes._state_db_lineage_lookup
    real_load_metadata_only = models.Session.load_metadata_only

    def _counting_state_db_lookup(pid, profile):
        state_db_calls["n"] += 1
        return real_state_db_lookup(pid, profile)

    def _counting_load_metadata_only(pid):
        sidecar_calls["n"] += 1
        return real_load_metadata_only(pid)

    monkeypatch.setattr(routes, "_state_db_lineage_lookup", _counting_state_db_lookup)
    monkeypatch.setattr(models.Session, "load_metadata_only", staticmethod(_counting_load_metadata_only))

    rows = [
        {
            "session_id": f"child-{i}",
            "parent_session_id": "nonexistent-ancestor",
            "message_count": 1,
            "project_id": None,
            "profile": "default",
            "updated_at": i,
        }
        for i in range(5)
    ]
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: rows)

    result = _run_payload(routes, rows[0], active_profile="default", sidebar_source="webui")
    assert sorted(result) == [f"child-{i}" for i in range(5)]
    assert sidecar_calls["n"] == 1, f"expected exactly one sidecar lookup for the shared missing ancestor, got {sidecar_calls['n']}"
    assert state_db_calls["n"] == 1, f"expected exactly one state.db query for the shared missing ancestor, got {state_db_calls['n']}"


def _run_payload_all_profiles(routes, rows, *, active_profile, sidebar_source):
    monkeypatch_unused = routes  # noqa: F841  (kept for call-site symmetry with _run_payload)
    payload = routes._build_session_list_cache_payload(
        active_profile=active_profile,
        all_profiles=True,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=True,
        sidebar_source=sidebar_source,
    )
    return [s["session_id"] for s in payload["sessions"]]


def test_effective_sidebar_origin_resolves_missing_ancestor_in_the_rows_own_profile(
    origin_fallback_env, monkeypatch,
):
    """#6985 review round 4, item 1 (the important one): an all_profiles=True
    payload mixes rows from EVERY profile, but the missing-ancestor fallback
    used to always query the REQUEST's active_profile's state.db, never the
    child row's OWN profile. A child owned by profile B whose ancestor only
    exists in B's state.db (absent from the active profile A entirely) must
    still resolve to B's ancestor's real origin — not silently fall back to
    webui because A's db has no such row.
    """
    import api.routes as routes
    from api.models import _get_profile_home

    # Ancestor exists ONLY in profile-b's state.db — profile-a's db (the
    # request's active_profile) has no row for it at all.
    _make_state_db_rows(_get_profile_home("profile-a") / "state.db", [])
    _make_state_db_rows(
        _get_profile_home("profile-b") / "state.db",
        [("b-only-ancestor", "matrix", None)],
    )

    child_row = {
        "session_id": "child-of-profile-b",
        "parent_session_id": "b-only-ancestor",
        "message_count": 1,
        "project_id": None,
        "profile": "profile-b",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [child_row])

    assert _run_payload_all_profiles(
        routes, [child_row], active_profile="profile-a", sidebar_source="matrix"
    ) == ["child-of-profile-b"]
    assert _run_payload_all_profiles(
        routes, [child_row], active_profile="profile-a", sidebar_source="webui"
    ) == []


def test_effective_sidebar_origin_same_ancestor_id_in_two_profiles_never_borrows_the_wrong_one(
    origin_fallback_env, monkeypatch,
):
    """#6985 review round 4, item 1: the reviewer's exact collision case —
    an ancestor session id exists in BOTH profile A's and profile B's
    state.db, with DIFFERENT origins (A=matrix, B=telegram). A child owned
    by profile B must resolve via B's row, never A's, even though both
    rows share the same session id and the active profile is A.
    """
    import api.routes as routes
    from api.models import _get_profile_home

    _make_state_db_rows(
        _get_profile_home("profile-a") / "state.db",
        [("shared-ancestor-id", "matrix", None)],
    )
    _make_state_db_rows(
        _get_profile_home("profile-b") / "state.db",
        [("shared-ancestor-id", "telegram", None)],
    )

    child_row = {
        "session_id": "child-of-profile-b-2",
        "parent_session_id": "shared-ancestor-id",
        "message_count": 1,
        "project_id": None,
        "profile": "profile-b",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [child_row])

    # Filtering for the ACTIVE profile's (A's) origin must NOT reveal B's
    # child — B's ancestor is telegram, not matrix, regardless of what a
    # same-id row in A's own db says.
    assert _run_payload_all_profiles(
        routes, [child_row], active_profile="profile-a", sidebar_source="matrix"
    ) == []
    # Filtering for B's actual (telegram) origin correctly reveals it.
    assert _run_payload_all_profiles(
        routes, [child_row], active_profile="profile-a", sidebar_source="telegram"
    ) == ["child-of-profile-b-2"]


def test_effective_sidebar_origin_in_memory_map_never_borrows_a_same_id_row_from_another_profile(
    origin_fallback_env, monkeypatch,
):
    """#6985 round 4 named TWO profile-scoping bugs: the state.db fallback
    AND "the in-memory lineage map and fallback cache are also keyed only by
    session id". Both were fixed, but every existing cross-profile regression
    puts the ancestor ONLY in state.db (absent from the loaded row set), so
    they all exercise `_effective_origin_fallback_lookup` and none exercise
    `_effective_session_by_id` — reverting that map to a bare-session-id
    lookup passed the entire suite.

    Here BOTH same-id ancestors are present in the loaded rows, so the walk
    resolves them from the in-memory map and never reaches the fallback. A
    profile-B child must bind to B's telegram ancestor, never A's matrix one.
    """
    import api.routes as routes

    # No state.db anywhere: if the in-memory map misses, the fallback finds
    # nothing and the child fails closed to webui — so a wrong-profile hit
    # here can only come from the in-memory map itself.
    parent_a = {
        "session_id": "shared-parent-id", "profile": "profile-a",
        "session_origin": "matrix", "message_count": 1,
        "project_id": None, "updated_at": 9,
    }
    parent_b = {
        "session_id": "shared-parent-id", "profile": "profile-b",
        "session_origin": "telegram", "message_count": 1,
        "project_id": None, "updated_at": 9,
    }
    child_b = {
        "session_id": "child-in-b", "profile": "profile-b",
        "parent_session_id": "shared-parent-id",
        "relationship_type": "child_session",
        "message_count": 1, "project_id": None, "updated_at": 5,
    }
    rows = [parent_a, parent_b, child_b]
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [dict(r) for r in rows])

    matrix_ids = _run_payload_all_profiles(
        routes, rows, active_profile="profile-a", sidebar_source="matrix"
    )
    telegram_ids = _run_payload_all_profiles(
        routes, rows, active_profile="profile-a", sidebar_source="telegram"
    )
    # The child must never appear under profile A's ancestor's origin...
    assert "child-in-b" not in matrix_ids
    # ...and must appear under its OWN profile's ancestor's origin.
    assert "child-in-b" in telegram_ids


def test_effective_sidebar_origin_classifies_definitive_ancestor_reached_exactly_at_hop_limit(
    origin_fallback_env, monkeypatch,
):
    """#6985 review round 4, item 2 (server side): both walks classify `cur`
    then advance to its parent, so a definitive external ancestor resolved
    on the walk's LAST permitted hop was never classified — the loop simply
    ran out of iterations first, silently dropping it to the 'webui'
    give-up value. Covers the exact boundary (16 hops, the last one
    allowed) and one hop beyond it (17 hops, which must still fail closed).
    """
    import api.routes as routes
    from api.models import _get_profile_home

    # child_1 -> child_2 -> ... -> child_16 -> external-ancestor: 16 hops
    # from child_1 to the ancestor, exactly the bound.
    at_limit_rows = [
        (f"child_{i}", None, f"child_{i + 1}" if i < 16 else "external-ancestor")
        for i in range(1, 17)
    ]
    at_limit_rows.append(("external-ancestor", "matrix", None))
    _make_state_db_rows(_get_profile_home("default") / "state.db", at_limit_rows)

    leaf_row = {
        "session_id": "child_1",
        "parent_session_id": "child_2",
        "relationship_type": "child_session",
        "message_count": 1,
        "project_id": None,
        "profile": "default",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [leaf_row])
    assert _run_payload(routes, leaf_row, active_profile="default", sidebar_source="matrix") == [
        "child_1"
    ]

    # One hop further: 17 hops from child_1 to the ancestor — one past what
    # the bound allows. Must still fail closed to webui, not error.
    beyond_limit_rows = [
        (f"beyond_{i}", None, f"beyond_{i + 1}" if i < 17 else "external-ancestor-2")
        for i in range(1, 18)
    ]
    beyond_limit_rows.append(("external-ancestor-2", "matrix", None))
    _make_state_db_rows(_get_profile_home("default2") / "state.db", beyond_limit_rows)

    leaf_row_2 = {
        "session_id": "beyond_1",
        "parent_session_id": "beyond_2",
        "relationship_type": "child_session",
        "message_count": 1,
        "project_id": None,
        "profile": "default2",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [leaf_row_2])
    assert _run_payload_all_profiles(
        routes, [leaf_row_2], active_profile="default2", sidebar_source="matrix"
    ) == []
    assert _run_payload_all_profiles(
        routes, [leaf_row_2], active_profile="default2", sidebar_source="webui"
    ) == ["beyond_1"]


def test_sidebar_response_item_adds_profile_key_without_touching_display_profile(monkeypatch):
    """#6985 round 5: round 4's fix needed a canonical, alias-folded key for
    client-side lineage matching (`static/sessions.js`'s `_rowProfileKey` has
    no way to replicate the server's `_is_root_profile()` alias-folding
    itself), but it implemented this by OVERWRITING `item["profile"]` in
    place — the same field the detailed all-profiles sidebar displays as the
    user's configured profile name. A renamed-root profile's rows then all
    showed literal "default" instead of the real name.

    `_sidebar_session_response_item` must add a SEPARATE `profile_key` field
    for lineage matching and leave `profile` exactly as given, for display."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_root_profile", lambda name: name == "my-renamed-root")

    aliased = routes._sidebar_session_response_item(
        {"session_id": "s1", "profile": "my-renamed-root"}
    )
    literal_default = routes._sidebar_session_response_item(
        {"session_id": "s2", "profile": "default"}
    )
    blank = routes._sidebar_session_response_item({"session_id": "s3", "profile": ""})
    other = routes._sidebar_session_response_item(
        {"session_id": "s4", "profile": "genuinely-other-profile"}
    )

    # profile_key: canonical, alias-folded — for lineage matching only.
    assert aliased["profile_key"] == "default"
    assert literal_default["profile_key"] == "default"
    assert blank["profile_key"] == "default"
    assert other["profile_key"] == "genuinely-other-profile"

    # profile: untouched — the user's actual configured name, for display.
    assert aliased["profile"] == "my-renamed-root"
    assert literal_default["profile"] == "default"
    assert blank["profile"] == ""
    assert other["profile"] == "genuinely-other-profile"


def _search_payload(url, sessions, *, scan_messages=None):
    """Invoke the REAL `_handle_sessions_search` endpoint and return its body.

    `scan_messages` (a list of message dicts) backs `get_session_for_scan`, so
    the content-search branch can actually be exercised rather than skipped.
    """
    import api.routes as routes
    from types import SimpleNamespace
    from unittest.mock import patch
    from urllib.parse import urlparse

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload

    with patch.object(routes, "_is_root_profile", lambda name: name == "my-renamed-root"), \
         patch.object(routes, "all_sessions", return_value=[dict(s) for s in sessions]), \
         patch.object(
             routes, "get_session_for_scan",
             side_effect=lambda sid: SimpleNamespace(messages=list(scan_messages or [])),
         ), \
         patch("api.profiles.get_active_profile_name", return_value="my-renamed-root"), \
         patch.object(routes, "j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(url))
    return captured["payload"]


def _list_payload_row(row):
    """Run a row through the REAL `/api/sessions` response pipeline."""
    import api.routes as routes
    from unittest.mock import patch

    with patch.object(routes, "_is_root_profile", lambda name: name == "my-renamed-root"):
        response = routes._session_list_payload_to_response({"sessions": [dict(row)]})
    return response["sessions"][0]


# A raw session row carrying three things the wire contract must NOT trust:
# an out-of-contract internal field, a forged canonical lineage key, and forged
# search-match metadata.
_SENTINEL_ROW = {
    "session_id": "shared-contract-row",
    "title": "a needle title",
    "profile": "my-renamed-root",
    "message_count": 1,
    "_out_of_contract_sentinel": "LEAKED",
    "messages": [{"role": "user", "content": "unbounded transcript payload"}],
    "profile_key": "FORGED-PROFILE-KEY",
    "match_type": "FORGED-MATCH",
    "match_preview": "FORGED-PREVIEW",
}


def test_list_and_all_search_branches_share_one_bounded_row_contract():
    """#6985 round 6: `/api/sessions` bounded every row through
    `_SIDEBAR_SESSION_RESPONSE_FIELDS`, but all three `/api/sessions/search`
    branches started from a bare `dict(s)`. The identical row was therefore
    serialized under two different wire contracts depending on which endpoint
    produced it — search forwarded internal out-of-contract fields (and a
    forged `profile_key`/`match_type` riding on the raw session dict) that the
    list endpoint strips, while a content search merges those rows straight
    back into the sidebar result set.

    This exercises the REAL list response pipeline plus the empty-query, title,
    and content search branches (the previous version of this test invoked only
    the empty-query branch and compared it against a direct helper call, so it
    could not observe the title/content branches or the list pipeline at all).
    """
    list_row = _list_payload_row(_SENTINEL_ROW)
    empty_row = _search_payload("/api/sessions/search", [_SENTINEL_ROW])["sessions"][0]
    title_row = _search_payload("/api/sessions/search?q=needle", [_SENTINEL_ROW])["sessions"][0]
    content_row = _search_payload(
        "/api/sessions/search?q=haystack",
        [dict(_SENTINEL_ROW, title="no title match here")],
        scan_messages=[{"role": "user", "content": "a haystack lives in the body"}],
    )["sessions"][0]

    for label, row in (
        ("list", list_row), ("empty", empty_row),
        ("title", title_row), ("content", content_row),
    ):
        # 1. Out-of-contract internal fields are stripped everywhere.
        assert "_out_of_contract_sentinel" not in row, label
        assert "messages" not in row, label
        # 2. The canonical lineage key is DERIVED, never accepted from the row.
        assert row["profile_key"] == "default", label
        # 3. The display value is preserved, never folded to "default".
        assert row["profile"] == "my-renamed-root", label

    # 4. Trusted match metadata: forged values never survive; only the
    #    server-computed value for the branch that actually matched appears.
    assert "match_type" not in list_row
    assert "match_type" not in empty_row
    assert "match_preview" not in list_row
    assert "match_preview" not in empty_row
    assert title_row["match_type"] == "title"
    assert "match_preview" not in title_row
    assert content_row["match_type"] == "content"
    assert "haystack" in content_row["match_preview"]
    assert content_row["match_preview"] != "FORGED-PREVIEW"

    # 5. List and search agree on the shared bounded contract. The list path
    #    additionally overlays LIVE RUNTIME state (`_session_list_cache_overlay_runtime_rows`
    #    fills `is_streaming`/`cron_running` from in-process state before
    #    serialization); search does not run that overlay. That is a runtime-state
    #    difference, not a second wire contract — search must never carry a field
    #    the bounded serializer would not also emit for the list.
    _RUNTIME_OVERLAY_FIELDS = {"is_streaming", "cron_running"}
    assert set(empty_row) <= set(list_row)
    assert set(list_row) - set(empty_row) <= _RUNTIME_OVERLAY_FIELDS


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_serialized_alias_child_joins_default_keyed_parent_through_real_client_closure():
    """#6985 round 6 item 3 (first requested in round 5): the composed client
    control. A child row serialized with the renamed-root DISPLAY alias in
    `profile` and a parent row serialized with the literal `default` display
    value must still join, because both carry the same server-derived canonical
    `profile_key`. This runs the REAL `sourceRowsById`/`_rowProfileKey`/
    `_rowLineageKey`/`effectiveOrigin` closures out of sessions.js against rows
    shaped exactly as the wire now emits them — proving the display/lineage
    split actually holds end to end, which no server-side test can show.
    """
    script = _client_effective_origin_script(
        """[
  {session_id:'ext-parent', profile:'default', profile_key:'default', session_origin:'matrix'},
  {session_id:'alias-child', profile:'my-renamed-root', profile_key:'default',
   parent_session_id:'ext-parent', relationship_type:'child_session', session_origin:'subagent'},
  {session_id:'other-profile-child', profile:'genuinely-other', profile_key:'genuinely-other',
   parent_session_id:'ext-parent', relationship_type:'child_session', session_origin:'subagent'},
]""",
        """{
  aliasChild: effectiveOrigin(bySid.get('alias-child')),
  otherProfileChild: effectiveOrigin(bySid.get('other-profile-child')),
}""",
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        # Different display labels, same canonical key -> the join succeeds and
        # the child inherits the external parent's origin.
        "aliasChild": "matrix",
        # A genuinely different profile must NOT borrow the parent's taxonomy,
        # even though the bare session id matches.
        "otherProfileChild": "webui",
    }


def test_renamed_alias_child_still_finds_canonically_keyed_ancestor(
    origin_fallback_env, monkeypatch,
):
    """#6985 round 5, requested control #2: a child row tagged with the RAW
    renamed-root alias must still find its ancestor, whose on-disk profile
    home the fallback lookup resolves via the CANONICAL profile key
    (`_canonical_row_profile`) — never the raw alias string. This predates
    round 5 (the lineage walk has scoped its fallback by the canonical
    profile since round 4) but round 5's display-vs-lineage split must not
    regress it, and the reviewer explicitly asked for this composed as its
    own control."""
    import api.routes as routes
    from api.models import _get_profile_home

    monkeypatch.setattr(routes, "_is_root_profile", lambda name: name == "my-renamed-root")

    # The lineage walk scopes the fallback lookup by the CANONICAL profile
    # (_canonical_row_profile), so the on-disk home it resolves is keyed by
    # "default" regardless of which raw alias a row happens to carry — this
    # is the mechanism under test: a parent whose home is only reachable via
    # the canonical key must still be found for a child tagged with either
    # spelling of the same profile.
    _make_state_db_rows(
        _get_profile_home("default") / "state.db",
        [("external-parent", "matrix", None)],
    )

    child_row = {
        "session_id": "child-tagged-with-alias",
        "parent_session_id": "external-parent",
        "message_count": 1,
        "project_id": None,
        # Child tagged with the RAW alias string — the reviewer's "one
        # aliased, one literal" scenario: the parent's home is keyed by the
        # canonical form ("default"), the child by the alias, and both must
        # resolve to the one lineage group.
        "profile": "my-renamed-root",
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [child_row])

    # If the child's alias tag and the parent's canonically-keyed state.db
    # home were treated as different profiles, this fallback lookup would
    # never find the parent and the child would stay misclassified as webui.
    assert _run_payload(
        routes, child_row, active_profile="my-renamed-root", sidebar_source="matrix"
    ) == ["child-tagged-with-alias"]


def test_all_profiles_sidebar_displays_configured_name_not_default(monkeypatch):
    """#6985 round 5, requested control #3: the detailed all-profiles
    sidebar must keep showing the user's actual configured profile name for
    a renamed-root profile's rows, not the literal string 'default' — the
    exact visible-label regression round 4's follow-up commit introduced."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_root_profile", lambda name: name == "my-company-workspace")
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [])

    row = {
        "session_id": "renamed-root-session",
        "title": "hi",
        "profile": "my-company-workspace",
        "message_count": 1,
        "project_id": None,
        "updated_at": 5,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [row])

    payload = routes._build_session_list_cache_payload(
        active_profile="my-company-workspace",
        all_profiles=True,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=True,
    )
    # _build_session_list_cache_payload returns the internal (pre-wire) row
    # set; _session_list_payload_to_response is the actual serialization
    # step (calls _sidebar_session_response_item per row) that produces what
    # /api/sessions really sends — the detailed sidebar's `s.profile` read
    # happens against THIS shape, not the internal one.
    response = routes._session_list_payload_to_response(payload)
    served = next(s for s in response["sessions"] if s["session_id"] == "renamed-root-session")

    # The wire-level display field is untouched; only profile_key is folded.
    assert served["profile"] == "my-company-workspace"
    assert served["profile_key"] == "default"
