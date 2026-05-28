"""Static wiring checks for the Phase 2 todo_state frontend.

Each test pins one design *decision* to its source location so a future
refactor that drops a critical guard surfaces here rather than as a
silent UI regression.

Design intent: pin behaviour, not formatting.  Earlier review flagged the
original tests as too brittle on whitespace, exact source shape, and
function signature pinning.  This rewrite uses tolerant matching:

* whitespace inside expressions is normalised before comparison,
* function bodies are extracted by name + balanced-brace scan rather
  than literal "function X(args){" splits,
* identifier presence is preferred over operator-tight regexes.

A test should fail when a *capability* is removed (e.g. session-id
filtering disappears) and pass when only formatting / argument names
change.  Source files exercised:

  - static/ui.js        (S.todos, _compactInflightState, hash + RAF,
                         _hydrateTodosFromSession)
  - static/messages.js  (todo_state listener, run journal whitelist,
                         settle-point hydration)
  - static/sessions.js  (INFLIGHT restore schema, settle-point hydration)
  - static/panels.js    (loadTodos single-source-of-truth + legacy
                         fallback)
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
UI_JS = ROOT / "static" / "ui.js"
MESSAGES_JS = ROOT / "static" / "messages.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
PANELS_JS = ROOT / "static" / "panels.js"


def _read(p):
    return p.read_text(encoding="utf-8")


def _strip_ws(s: str) -> str:
    """Collapse all whitespace so format-only changes don't break tests."""
    return re.sub(r"\s+", "", s)


def _extract_function_body(src: str, name: str) -> str:
    """Return the body of `function <name>(...) { ... }`.

    Tolerates whitespace around the name, parameter list, and opening
    brace so signature reformatting doesn't break tests.  Raises
    AssertionError with a clear message if the function is missing —
    that is itself a meaningful regression.
    """
    pat = re.compile(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{")
    m = pat.search(src)
    assert m, f"function {name}(...) not found"
    start = m.end()  # position of first char inside body
    depth = 1
    i = start
    while depth > 0 and i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:i]
        i += 1
    raise AssertionError(f"function {name} body unterminated")


# ---------------------------------------------------------------------------
# State model — ui.js
# ---------------------------------------------------------------------------


class TestStateModel:
    def test_S_initialises_todos_array(self):
        # Match `todos: []` regardless of whitespace.  The empty array
        # doubles as a safe default for callers that read S.todos before
        # any signal has arrived.
        src = _strip_ws(_read(UI_JS))
        assert "todos:[]" in src, "S must initialise todos to []"

    def test_S_initialises_todoStateMeta_null_sentinel(self):
        # null is a sentinel: while null, no explicit signal has been
        # seen, so loadTodos() falls through to the legacy reverse-scan.
        src = _strip_ws(_read(UI_JS))
        assert "todoStateMeta:null" in src, (
            "S must initialise todoStateMeta to null so loadTodos can "
            "distinguish 'never received a signal' from 'received empty list'."
        )

    def test_compact_inflight_state_persists_todos(self):
        body = _extract_function_body(_read(UI_JS), "_compactInflightState")
        # Both fields must be persisted; either omission breaks reload
        # recovery for the panel.
        compact = _strip_ws(body)
        assert "todos:" in compact and "todoStateMeta:" in compact, (
            "_compactInflightState must persist todos and todoStateMeta "
            "into the localStorage snapshot."
        )


# ---------------------------------------------------------------------------
# Hash + RAF + hydrate helpers — ui.js
# ---------------------------------------------------------------------------


class TestHashAndScheduling:
    def test_hash_function_exists(self):
        # Tolerant: function exists with any parameter name.
        assert re.search(r"function\s+_todosHash\s*\(", _read(UI_JS)), (
            "_todosHash must exist; it short-circuits no-op renders."
        )

    def test_hash_separates_id_content_status(self):
        body = _extract_function_body(_read(UI_JS), "_todosHash")
        # All three fields must contribute to the hash; otherwise a
        # status-only transition (pending -> in_progress) would silently
        # be skipped because the hash would not change.  We check field
        # *names* are referenced; the exact accessor (t.id, item['id'],
        # destructure) doesn't matter.
        assert ".id" in body or "['id']" in body or '["id"]' in body, "hash must read id"
        assert ".content" in body or "['content']" in body or '["content"]' in body, "hash must read content"
        assert ".status" in body or "['status']" in body or '["status"]' in body, "hash must read status"

    def test_hash_uses_low_overhead_concat(self):
        body = _extract_function_body(_read(UI_JS), "_todosHash")
        # Strip line comments — the comment itself mentions JSON.stringify
        # deliberately to explain *why* the implementation avoids it.
        code_lines = []
        for ln in body.split("\n"):
            stripped = ln.lstrip()
            if stripped.startswith("//"):
                continue
            code_lines.append(ln)
        code = "\n".join(code_lines)
        # JSON.stringify allocates intermediate objects per call — for
        # bursty events this is GC pressure.  Concat-based hash avoids it.
        assert "JSON.stringify" not in code, (
            "_todosHash must avoid JSON.stringify so live updates do not "
            "create per-event GC pressure."
        )

    def test_schedule_uses_raf_with_idempotency_guard(self):
        body = _extract_function_body(_read(UI_JS), "scheduleTodosRefresh")
        assert "_todosRenderRafId" in body, (
            "scheduleTodosRefresh must guard against re-entry so multiple "
            "events in the same frame coalesce to one render."
        )
        assert "requestAnimationFrame" in body, (
            "scheduleTodosRefresh must use requestAnimationFrame to "
            "coalesce bursty live updates into a single paint."
        )

    def test_schedule_skips_when_panel_inactive(self):
        body = _extract_function_body(_read(UI_JS), "scheduleTodosRefresh")
        # Inside the RAF callback we re-check active state — the panel
        # may have been switched away in the same frame.
        assert "_todosPanelIsActive" in body, (
            "scheduleTodosRefresh must short-circuit when the Todos "
            "panel is not active so background events do no DOM work."
        )

    def test_schedule_falls_back_when_raf_missing(self):
        body = _extract_function_body(_read(UI_JS), "scheduleTodosRefresh")
        # In test/Node environments without raf we must still render.
        # Tolerate any equivalent form: typeof !== 'function' / != / etc.
        compact = _strip_ws(body)
        assert "typeofrequestAnimationFrame" in compact, (
            "scheduleTodosRefresh must check typeof requestAnimationFrame "
            "so a synchronous fallback runs when raf is unavailable."
        )

    def test_hydrate_function_exists(self):
        assert re.search(
            r"function\s+_hydrateTodosFromSession\s*\(", _read(UI_JS)
        ), "_hydrateTodosFromSession must exist"

    def test_hydrate_reconciles_inflight_and_cold_load(self):
        # P1-2: hydrate must consider BOTH cold-load and INFLIGHT and
        # reconcile by recency rather than letting one side blindly win.
        # Behavioural correctness is covered by test_phase2_todo_behavior;
        # here we only pin that the function reads from both sources and
        # compares timestamps.
        body = _extract_function_body(_read(UI_JS), "_hydrateTodosFromSession")
        compact = _strip_ws(body)
        assert "session.todo_state" in compact or "todo_state" in body, (
            "hydrate must read session.todo_state (the cold-load snapshot)."
        )
        assert "INFLIGHT" in body, (
            "hydrate must read INFLIGHT (the live persisted snapshot)."
        )
        # ts comparison evidence: the function references `ts` from both
        # sides somewhere in the body.  We don't pin operator shape.
        assert body.count(".ts") >= 2 or compact.count("ts:") >= 2, (
            "hydrate must compare timestamps from cold-load and INFLIGHT "
            "to pick the newer snapshot (P1-2 recency reconciliation)."
        )

    def test_hydrate_resets_render_cache(self):
        body = _extract_function_body(_read(UI_JS), "_hydrateTodosFromSession")
        assert "_resetTodosRenderCache" in body, (
            "_hydrateTodosFromSession must reset the render hash on "
            "cross-session navigation so stale hashes from the previous "
            "session do not short-circuit the next render."
        )


# ---------------------------------------------------------------------------
# SSE listener — messages.js
# ---------------------------------------------------------------------------


def _todo_state_listener_body(src: str) -> str:
    """Extract the body of source.addEventListener('todo_state', e=>{...})."""
    m = re.search(r"addEventListener\(\s*['\"]todo_state['\"]", src)
    assert m, "todo_state listener registration not found"
    # Find the arrow body opening brace after this position.
    arrow = src.find("=>", m.end())
    assert arrow != -1, "todo_state listener arrow not found"
    body_open = src.find("{", arrow)
    assert body_open != -1, "todo_state listener body open brace not found"
    depth = 1
    i = body_open + 1
    while depth > 0 and i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[body_open + 1:i]
        i += 1
    raise AssertionError("todo_state listener body unterminated")


class TestSseListener:
    def test_listener_registered(self):
        src = _read(MESSAGES_JS)
        assert re.search(
            r"addEventListener\(\s*['\"]todo_state['\"]", src
        ), "todo_state listener must be registered on the EventSource."

    def test_listener_filters_by_session_id(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        # Mirrors every other live listener: session-tagged events from
        # a session the user has navigated away from must be dropped.
        assert "session_id" in body and "activeSid" in body, (
            "todo_state listener must guard against cross-session events "
            "by comparing payload.session_id against activeSid."
        )

    def test_listener_filters_against_S_session(self):
        # P1-1: even when payload.session_id matches activeSid, the UI
        # may have navigated to another session in the meantime.  The
        # handler must check S.session.session_id before writing global
        # state.  Pin presence of S.session reference in the body.
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        assert "S.session" in body, (
            "todo_state listener must double-check S.session.session_id "
            "against activeSid before writing global state, so a late "
            "event arriving after the user navigated away cannot "
            "pollute the now-active view."
        )

    def test_listener_validates_todos_array(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        assert "Array.isArray" in body and "todos" in body, (
            "todo_state listener must require Array.isArray(d.todos) so "
            "a malformed payload never overwrites S.todos with garbage."
        )

    def test_listener_swallows_parse_errors(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        assert "try" in body and "catch" in body, (
            "todo_state listener must swallow JSON.parse errors silently."
        )

    def test_listener_drops_strictly_older_ts(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        # Equal-ts allowed (compression refresh can land on same second
        # as the tool emit it follows); strictly older is rejected.
        # Tolerate any whitespace around the < operator.
        compact = _strip_ws(body)
        assert "incomingTs<currentTs" in compact, (
            "todo_state listener must reject strictly-older snapshots "
            "to defend against out-of-order event delivery."
        )

    def test_listener_replaces_never_merges(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        # Snapshot is full; we never merge.  Pin the assignment shape
        # but tolerate whitespace.
        compact = _strip_ws(body)
        assert "S.todos=d.todos" in compact, (
            "todo_state must apply the snapshot wholesale (no merge); "
            "merging would resurrect items the agent intended to drop."
        )

    def test_listener_mirrors_to_inflight(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        compact = _strip_ws(body)
        assert "INFLIGHT[activeSid]" in compact, (
            "todo_state listener must mirror to INFLIGHT so a tab switch "
            "back into this session restores the panel from the in-memory "
            "live snapshot."
        )

    def test_listener_persists(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        assert "persistInflightState" in body, (
            "todo_state listener must call persistInflightState so a hard "
            "browser reload mid-stream still sees the latest snapshot."
        )

    def test_listener_schedules_render(self):
        body = _todo_state_listener_body(_read(MESSAGES_JS))
        assert "scheduleTodosRefresh" in body, (
            "todo_state listener must schedule a render via the RAF "
            "coalescer rather than calling loadTodos() directly."
        )

    def test_run_journal_whitelist_includes_todo_state(self):
        # Journal cursor only advances for whitelisted events; without
        # this entry, a reconnect would replay every todo_state since
        # session start.  Find the whitelist literal robustly.
        src = _read(MESSAGES_JS)
        m = re.search(
            r"for\s*\(\s*const\s+_runJournalEventName\s+of\s+\[(.*?)\]",
            src, re.DOTALL,
        )
        assert m, "run journal whitelist literal not found"
        whitelist = m.group(1)
        assert "'todo_state'" in whitelist or '"todo_state"' in whitelist, (
            "todo_state must be in the run journal cursor whitelist so "
            "Last-Event-ID advances past it on reconnect."
        )


# ---------------------------------------------------------------------------
# Settle-point hydration — messages.js + sessions.js
# ---------------------------------------------------------------------------


class TestSettlePointHydration:
    def test_messages_js_settle_points_hydrate(self):
        src = _read(MESSAGES_JS)
        # All `S.session=` non-clear assignments must trigger hydrate so
        # cross-session navigation never leaves a stale list visible.
        # We allow up to 8 lines of slack to absorb harmless intermediate
        # statements (e.g. a comment, an autosave call, a debug log).
        lines = src.split("\n")
        settle_points = [
            i for i, ln in enumerate(lines)
            if re.search(r"\bS\.session\s*=", ln)
            and not re.search(r"\bS\.session\s*=\s*null", ln)
        ]
        assert settle_points, (
            "Expected at least one S.session settle point in messages.js"
        )
        for i in settle_points:
            window = "\n".join(lines[i:i + 8])
            assert "_hydrateTodosFromSession" in window, (
                f"S.session settle point at messages.js line {i + 1} "
                f"must call _hydrateTodosFromSession within 8 lines."
            )

    def test_sessions_js_inflight_restore_carries_todos(self):
        # The INFLIGHT restore block must carry the persisted todos and
        # todoStateMeta from loadInflightState.  We locate the restore
        # block by searching for `loadInflightState` and confirm both
        # field names are referenced within ~30 lines of it.  This
        # tolerates harmless reformatting of the surrounding `if(...)`.
        src = _read(SESSIONS_JS)
        m = re.search(r"loadInflightState\s*\(", src)
        assert m, "INFLIGHT restore site must call loadInflightState"
        # Take ~30 lines around the call.
        start = src.rfind("\n", 0, m.start())
        end = src.find("\n", m.end())
        for _ in range(40):
            nxt = src.find("\n", end + 1)
            if nxt == -1:
                break
            end = nxt
        window = src[start:end]
        compact = _strip_ws(window)
        assert "todos:" in compact and "todoStateMeta:" in compact, (
            "INFLIGHT restore must carry todos + todoStateMeta from "
            "the persisted localStorage snapshot."
        )

    def test_sessions_js_inflight_branch_calls_hydrate(self):
        # The INFLIGHT-restore path must also call hydrate so cold-load
        # and INFLIGHT both resolve to a deterministic S.todos.  Pin
        # presence in the file rather than in a specific source slice.
        src = _read(SESSIONS_JS)
        # There should be at least one hydrate call in sessions.js
        # itself — that's what guarantees the restore path renders.
        assert "_hydrateTodosFromSession" in src, (
            "sessions.js must call _hydrateTodosFromSession on at "
            "least one settle point."
        )

    def test_sessions_js_ensure_messages_loaded_hydrates_todo_state(self):
        # Page refresh path: loadSession() runs `messages=0` (which is
        # gated out of attach_todo_state on the server), then
        # _ensureMessagesLoaded() runs `messages=1` — and *that*
        # response carries the canonical cold-load todo_state derived
        # from the FULL untruncated message list.  Without applying it
        # in _ensureMessagesLoaded(), long sessions whose latest todo
        # write falls outside the _INITIAL_MSG_LIMIT tail would lose
        # the panel on refresh: the legacy reverse-scan fallback only
        # sees the tail S.messages, while the authoritative snapshot
        # sits unread in the response.  Pin _ensureMessagesLoaded()'s
        # body to (a) read data.session.todo_state, (b) call
        # _hydrateTodosFromSession() so the meta/timestamp reconcile
        # path runs, and (c) trigger a render.
        body = _extract_function_body(_read(SESSIONS_JS), "_ensureMessagesLoaded")
        assert "data.session.todo_state" in body, (
            "_ensureMessagesLoaded must read data.session.todo_state from "
            "the messages=1 response so cold-load todos survive refresh "
            "for long sessions where the todo tool message fell outside "
            "the _INITIAL_MSG_LIMIT tail."
        )
        assert "_hydrateTodosFromSession" in body, (
            "_ensureMessagesLoaded must call _hydrateTodosFromSession "
            "after applying the cold-load todo_state so the cold-load vs "
            "INFLIGHT timestamp reconcile path runs."
        )

    def test_sessions_js_session_clear_paths_hydrate_null(self):
        # Both delete-session and bulk-delete paths must clear S.session
        # AND call _hydrateTodosFromSession so the panel clears
        # synchronously.  We only check the delete paths — those are
        # identified by clearing S.messages alongside S.session.  Other
        # S.session=null assignments (e.g. project-picker mount) are
        # transient UI state and don't drive the Todos panel.
        src = _read(SESSIONS_JS)
        # A real "delete" path zeroes both S.session and S.messages in
        # the same line or within ~3 lines.  We pin the 2-statement
        # combo so picker-style assignments are excluded.
        delete_paths = list(re.finditer(
            r"S\.session\s*=\s*null\s*;\s*S\.messages\s*=\s*\[\s*\]",
            src,
        ))
        assert delete_paths, (
            "expected at least one delete path with S.session=null;S.messages=[]"
        )
        for m in delete_paths:
            # Search ±10 lines for the hydrate call — clears are sometimes
            # `_hydrateTodosFromSession(null);` on the line before or
            # after.
            start = src.rfind("\n", max(0, m.start() - 600), m.start())
            end = src.find("\n", m.end())
            for _ in range(10):
                nxt = src.find("\n", end + 1)
                if nxt == -1:
                    break
                end = nxt
            window = src[start:end]
            assert "_hydrateTodosFromSession" in window, (
                f"delete path at offset {m.start()} must be paired with "
                f"_hydrateTodosFromSession so the Todos panel clears "
                f"synchronously on session deletion."
            )


# ---------------------------------------------------------------------------
# Renderer — panels.js
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_loadTodos_reads_S_todos_when_meta_present(self):
        body = _extract_function_body(_read(PANELS_JS), "loadTodos")
        # Single source of truth: when meta is present, we trust S.todos.
        assert "S.todoStateMeta" in body and "S.todos" in body

    def test_loadTodos_falls_back_to_legacy_when_meta_null(self):
        body = _extract_function_body(_read(PANELS_JS), "loadTodos")
        assert "_legacyTodosFromMessages" in body, (
            "loadTodos must fall back to legacy reverse-scan when no "
            "explicit signal has been received (S.todoStateMeta === null)."
        )

    def test_loadTodos_short_circuits_identical_hash(self):
        body = _extract_function_body(_read(PANELS_JS), "loadTodos")
        assert "_todosHash" in body and "_todosLastRenderedHash" in body

    def test_loadTodos_short_circuits_repeated_empty(self):
        body = _extract_function_body(_read(PANELS_JS), "loadTodos")
        assert "__empty__" in body, (
            "Repeated empty-list emissions must short-circuit; otherwise "
            "every todo write to a cleared list re-paints the empty state."
        )

    def test_loadTodos_uses_esc_for_user_strings(self):
        body = _extract_function_body(_read(PANELS_JS), "loadTodos")
        # Tolerate `esc(td.content)` and equivalents like `esc(item.content)`.
        assert re.search(r"esc\([^)]*\.content\)", body), (
            "User-controlled `content` must go through esc(); the "
            "renderer uses innerHTML so any unescaped path is XSS."
        )
        assert re.search(r"esc\([^)]*\.id\)", body), (
            "User-controlled `id` must go through esc()."
        )

    def test_legacy_fallback_skips_non_todo_payloads_fast(self):
        body = _extract_function_body(_read(PANELS_JS), "_legacyTodosFromMessages")
        # Substring guard avoids JSON.parse on every tool result —
        # most tool calls are not todo writes.  Tolerate single or
        # double quotes around the literal.
        compact = _strip_ws(body)
        assert (
            'indexOf(\'"todos"\')' in compact
            or 'indexOf("\\"todos\\"")' in compact
            or 'includes(\'"todos"\')' in compact
            or 'includes("\\"todos\\"")' in compact
        ), (
            "_legacyTodosFromMessages must use a substring fast-path "
            "to skip non-todo tool results without parsing JSON."
        )

    def test_legacy_fallback_swallows_parse_errors(self):
        body = _extract_function_body(_read(PANELS_JS), "_legacyTodosFromMessages")
        assert "try" in body and "catch" in body
