"""A delegated subagent transcript shows its thinking and tool cards between messages.

Delegated subagent sessions are loaded read-only from state.db with no WebUI
stream attached, and the whole task is one long turn. Its settled worklogs are
split at each visible interim text and open while the child runs (unless the user
collapsed one), so thinking stays in place between the messages instead of being
folded into one "Processed" block. Every other session keeps one collapsed worklog per turn.
"""

from __future__ import annotations

import json
import re
import sqlite3
import textwrap
from pathlib import Path

import pytest

from tests.test_anchor_fallback_ownership import _render_messages_harness, _run_node_script
from tests import test_5307_subagent_child_transcript as _t5307

_make_state_db = _t5307._make_state_db
routes_module = _t5307.routes_module
isolated_state_db = _t5307.isolated_state_db

USER = {"role": "user", "content": "go"}
CALL = {
    "role": "assistant",
    "content": "",
    "reasoning": "r",
    "tool_calls": [{"id": "t1", "function": {"name": "terminal", "arguments": "{}"}}],
}
RESULT = {"role": "tool", "tool_call_id": "t1", "content": "{}"}
ANSWER = {"role": "assistant", "content": "done", "reasoning": "r"}

RUNNING_SUBAGENT = {
    "session_id": "child-1",
    "parent_session_id": "parent-1",
    "relationship_type": "child_session",
    "source_tag": "subagent",
    "raw_source": "subagent",
    "read_only": True,
    "is_cli_session": False,
    "active": True,
}
ENDED_SUBAGENT = {**RUNNING_SUBAGENT, "active": False}
WEBUI_SESSION = {"session_id": "webui-1", "source_tag": "webui", "raw_source": "webui"}
ENDED_FOREIGN_CLI = {
    "session_id": "cli-1",
    "source_tag": "cli",
    "raw_source": "cli",
    "is_cli_session": True,
    "read_only": True,
    "end_reason": "user_cancelled",
}

TWO_TURNS_RUNNING = [USER, CALL, RESULT, ANSWER, USER, CALL, RESULT]


def _collapsed_flags(session, messages, *, busy=False):
    """Render through the real renderMessages() and return each worklog's collapsed flag."""
    script = textwrap.dedent(
        f"""{_render_messages_harness()}
        S = {{
          session: {json.dumps(session)},
          messages: {json.dumps(messages)},
          toolCalls: [],
          busy: {json.dumps(busy)},
        }};
        renderMessages();
        console.log(JSON.stringify(
          elements.msgInner.querySelectorAll('.tool-worklog-group')
            .map((g) => g.getAttribute('data-collapsed') === 'true')
        ));
        """
    )
    return json.loads(_run_node_script(script))


def _call(i, text=""):
    return {**CALL, "content": text, "tool_calls": [{**CALL["tool_calls"][0], "id": f"t{i}"}]}


def _result(i):
    return {**RESULT, "tool_call_id": f"t{i}"}


INTERLEAVED = [
    USER, _call(1), _result(1), _call(2, "Now C#."), _result(2),
    _call(3), _result(3), _call(4, "Restoring it."), _result(4), _call(5), _result(5),
]


def _last_turn_layout(session, messages, *, busy=False):
    """Render via renderMessages() and list the last turn's children: G<collapsed> or T<idx>."""
    script = textwrap.dedent(
        f"""{_render_messages_harness()}
        S = {{ session: {json.dumps(session)}, messages: {json.dumps(messages)},
              toolCalls: [], busy: {json.dumps(busy)} }};
        renderMessages();
        const turnEls = elements.msgInner.querySelectorAll('.assistant-turn');
        const blocks = _assistantTurnBlocks(turnEls[turnEls.length - 1]);
        console.log(JSON.stringify(blocks.children.map((el) =>
          el.className.includes('tool-worklog-group')
            ? 'G' + el.getAttribute('data-collapsed')
            : 'T' + el.getAttribute('data-msg-idx'))));
        """
    )
    return json.loads(_run_node_script(script))


def test_running_subagent_opens_only_its_last_turn():
    # Earlier, answered turns stay collapsed; only the turn still working opens.
    assert _collapsed_flags(RUNNING_SUBAGENT, TWO_TURNS_RUNNING) == [True, False]


def test_ended_subagent_without_answer_keeps_last_worklog_open():
    # The run ended after a tool result with no answer: its work stays visible.
    assert _collapsed_flags(ENDED_SUBAGENT, TWO_TURNS_RUNNING) == [True, False]


def test_ended_subagent_with_answer_collapses_every_worklog():
    assert _collapsed_flags(ENDED_SUBAGENT, [*TWO_TURNS_RUNNING, ANSWER]) == [True, True]


def _running_marks(session, messages):
    script = textwrap.dedent(
        f"""{_render_messages_harness()}
        S = {{ session: {json.dumps(session)}, messages: {json.dumps(messages)},
              toolCalls: [], busy: false }};
        renderMessages();
        console.log(JSON.stringify(elements.msgInner.querySelectorAll('.tool-worklog-group')
          .map((g) => g.getAttribute('data-subagent-running') === '1')));
        """
    )
    return json.loads(_run_node_script(script))


def test_only_the_running_subagents_newest_worklog_is_marked_running():
    assert _running_marks(RUNNING_SUBAGENT, INTERLEAVED) == [False, False, False, False, True]
    assert _running_marks(ENDED_SUBAGENT, INTERLEAVED) == [False] * 5
    assert _running_marks(WEBUI_SESSION, TWO_TURNS_RUNNING) == [False, False]


def test_running_marked_worklog_is_labelled_running_not_processed():
    from tests.test_anchor_fallback_ownership import _function_source, _ui_js

    sync = _function_source(_ui_js(), "_syncToolCallGroupSummary")
    script = textwrap.dedent(
        f"""
        const attrs = (o) => ({{ getAttribute: (k) => (k in o ? o[k] : null),
                                 setAttribute() {{}}, removeAttribute() {{}} }});
        const label = {{ textContent: '', setAttribute() {{}}, removeAttribute() {{}} }};
        const mk = (o) => ({{ ...attrs(o), dataset: {{}},
          querySelector: (sel) => (sel.includes('label') ? label : null),
          querySelectorAll: () => [] }});
        const _toolWorklogListEl = () => null, _syncToolWorklogToolGroup = () => {{}};
        const _activitySettledProcessedLabel = () => 'Processed in 4s';
        const _activityProcessedElapsedLabel = () => '';
        const t = (k, v) => (k === 'gateway_running_label' ? 'In esecuzione' : 'Processed ' + v);
        eval({json.dumps(sync)});
        const out = [];
        _syncToolCallGroupSummary(mk({{ 'data-tool-worklog-group': '1', 'data-subagent-running': '1' }}));
        out.push(label.textContent);
        _syncToolCallGroupSummary(mk({{ 'data-tool-worklog-group': '1' }}));
        out.push(label.textContent);
        console.log(JSON.stringify(out));
        """
    )
    # The running label comes from the locale catalog, not a hard-coded English string.
    assert json.loads(_run_node_script(script)) == ["In esecuzione", "Processed in 4s"]


def test_running_subagent_dot_pulses_and_respects_reduced_motion():
    css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")
    sel = '.tool-worklog-group[data-tool-worklog-group="1"][data-subagent-running="1"] .as-dot'
    rule = re.search(re.escape(sel) + r"\{([^}]*)\}", css).group(1)
    assert "animation:wlpulse 1.3s ease-in-out infinite" in rule
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\{\s*" + re.escape(sel) + r"\{animation:none;\}", css)
    assert reduced


def _real_disclosure_harness():
    """Real ensureActivityGroup()/_toggleActivityGroup() over a fake DOM and localStorage."""
    from tests.test_anchor_fallback_ownership import _function_source, _ui_js

    src = _ui_js()
    names = [
        "_activityDisclosureStorageKey", "_readActivityDisclosureState",
        "_writeActivityDisclosureState", "ensureActivityGroup", "_toggleActivityGroup",
        "_messageRenderCacheSignature",
    ]
    evals = "\n".join(f"eval({json.dumps(_function_source(src, n))});" for n in names)
    return f"""
        const _store = {{}};
        const localStorage = {{ getItem: (k) => (k in _store ? _store[k] : null),
                               setItem: (k, v) => {{ _store[k] = String(v); }} }};
        const window = {{}};
        const _activityDisclosureStoragePrefix = 'p:';
        let _liveActivityUserExpanded = null;
        const _sessionHtmlCache = new Map();
        function _addBoundedHash(add, v) {{ add(JSON.stringify(v)); }}
        function msgContent(m) {{ return String(m.content || ''); }}
        function _messageHasReasoningPayload() {{ return false; }}
        function li() {{ return ''; }}
        function _activityKeyForLiveTurn() {{ return null; }}
        function _syncToolCallGroupSummary() {{}}
        function _materializeDeferredWorklogRows() {{}}
        function _onLiveActivityToggle() {{}}
        function mkEl() {{
          const cls = new Set();
          const el = {{ attrs: {{}}, children: [], parentElement: null, innerHTML: '',
            classList: {{ contains: (c) => cls.has(c),
              toggle: (c, on) => {{ const v = on === undefined ? !cls.has(c) : !!on;
                                   v ? cls.add(c) : cls.delete(c); return v; }} }},
            setAttribute(k, v) {{ this.attrs[k] = String(v); }},
            getAttribute(k) {{ return k in this.attrs ? this.attrs[k] : null; }},
            removeAttribute(k) {{ delete this.attrs[k]; }},
            querySelector() {{ return null; }}, querySelectorAll() {{ return []; }},
            appendChild(c) {{ c.parentElement = this; this.children.push(c); }},
            closest() {{ return el; }} }};
          Object.defineProperty(el, 'className', {{ set(v) {{ cls.clear();
            String(v).split(/\\s+/).filter(Boolean).forEach((c) => cls.add(c)); }} }});
          return el;
        }}
        const document = {{ createElement: mkEl }};
        const CSS = {{ escape: (v) => String(v) }};
        let S = {{ session: {json.dumps(RUNNING_SUBAGENT)}, messages: [], toolCalls: [] }};
        {evals}
        const build = (opts) => ensureActivityGroup(mkEl(),
          {{ collapsed: false, activityKey: 'assistant:1', ...opts }})
          .classList.contains('tool-call-group-collapsed');
    """


def _run_disclosure(body):
    return json.loads(_run_node_script(_real_disclosure_harness() + textwrap.dedent(body)))


def test_saved_closed_state_wins_for_open_subagent_worklog():
    assert _run_disclosure("""
        const before = build({ honourSavedDisclosure: true });
        _writeActivityDisclosureState('assistant:1', false);
        console.log(JSON.stringify([before, build({ honourSavedDisclosure: true }),
                                    build({})]));
    """) == [False, True, False]


def test_toggling_worklog_saves_state_and_drops_cached_render():
    assert _run_disclosure("""
        _sessionHtmlCache.set('child-1', { html: 'stale' });
        const group = mkEl();
        group.setAttribute('data-activity-disclosure-key', 'assistant:1');
        _toggleActivityGroup({ closest: () => group, setAttribute() {} });
        console.log(JSON.stringify([_readActivityDisclosureState('assistant:1'),
                                    _sessionHtmlCache.has('child-1')]));
    """) == ["closed", False]


def test_render_signature_changes_when_subagent_ends():
    assert _run_disclosure("""
        const running = _messageRenderCacheSignature();
        S.session = { ...S.session, active: false };
        console.log(JSON.stringify(running !== _messageRenderCacheSignature()));
    """) is True


def test_subagent_ending_re_renders_open_state_from_lifecycle():
    script = textwrap.dedent(
        f"""{_render_messages_harness()}
        S = {{ session: {json.dumps(RUNNING_SUBAGENT)}, messages: {json.dumps(TWO_TURNS_RUNNING)},
              toolCalls: [], busy: false }};
        const flags = () => elements.msgInner.querySelectorAll('.tool-worklog-group')
          .map((g) => g.getAttribute('data-collapsed') === 'true');
        renderMessages();
        const running = flags();
        S.session = {{ ...S.session, active: false }};
        renderMessages();
        const endedNoAnswer = flags();
        S.messages = [...S.messages, {json.dumps(ANSWER)}];
        renderMessages();
        console.log(JSON.stringify([running, endedNoAnswer, flags()]));
        """
    )
    assert json.loads(_run_node_script(script)) == [[True, False], [True, False], [True, True]]


def test_subagent_worklog_splits_at_each_interim_text():
    assert _last_turn_layout(RUNNING_SUBAGENT, INTERLEAVED) == [
        "Gfalse", "T1", "Gfalse", "T3", "Gfalse", "T5", "Gfalse", "T7", "Gfalse", "T9",
    ]


def test_webui_session_keeps_one_collapsed_worklog_per_turn():
    assert _collapsed_flags(WEBUI_SESSION, TWO_TURNS_RUNNING) == [True, True]
    assert _last_turn_layout(WEBUI_SESSION, INTERLEAVED)[0] == "Gtrue"
    assert _last_turn_layout(WEBUI_SESSION, INTERLEAVED).count("Gtrue") == 1


def test_foreign_cli_session_stays_collapsed():
    assert _collapsed_flags(ENDED_FOREIGN_CLI, TWO_TURNS_RUNNING) == [True, True]


def test_live_stream_path_is_untouched():
    script = textwrap.dedent(
        f"""{_render_messages_harness()}
        S = {{ session: {json.dumps(RUNNING_SUBAGENT)}, messages: [], toolCalls: [], busy: false }};
        const idle = _isDelegatedSubagentTranscript();
        S.busy = true;
        console.log(JSON.stringify([idle, _isDelegatedSubagentTranscript()]));
        """
    )
    assert json.loads(_run_node_script(script)) == [True, False]


def _seed_child(db, *, ended_at):
    _make_state_db(db, "parent-1", source="cli")
    _make_state_db(db, "child-1", source="subagent", message_count=3)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE sessions SET parent_session_id='parent-1', ended_at=? WHERE id='child-1'",
        (ended_at,),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("ended_at, active", [(None, True), (1781024999.0, False)])
def test_synthesized_subagent_carries_lineage_and_state_db_lifecycle(
    routes_module, isolated_state_db, ended_at, active
):
    _seed_child(isolated_state_db["db"], ended_at=ended_at)
    sess, reason = routes_module._claim_or_synthesize_cli_session("child-1")
    assert reason == "not_claimable"
    assert sess.read_only is True
    assert sess.parent_session_id == "parent-1"
    assert sess.relationship_type == "child_session"
    assert sess.active is active


def test_synthesized_foreign_session_has_no_lifecycle_marker(
    routes_module, isolated_state_db
):
    _make_state_db(isolated_state_db["db"], "cli-1", source="claude_code")
    sess, reason = routes_module._claim_or_synthesize_cli_session("cli-1")
    assert reason == "not_claimable"
    assert getattr(sess, "active", None) is None
    assert getattr(sess, "relationship_type", None) is None


def test_get_session_projects_subagent_lineage_and_lifecycle(
    routes_module, isolated_state_db, monkeypatch
):
    from types import SimpleNamespace

    import api.config

    _seed_child(isolated_state_db["db"], ended_at=None)
    monkeypatch.setattr(routes_module, "_session_visible_to_active_profile", lambda *_: True)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda *_: {})
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    response = {}
    monkeypatch.setattr(
        routes_module, "j", lambda _h, payload, status=200, **_k: response.update(payload=payload)
    )
    routes_module._handle_session_get(
        None, SimpleNamespace(path="/api/session", query="session_id=child-1")
    )
    sess = response["payload"]["session"]
    assert (sess["parent_session_id"], sess["relationship_type"], sess["active"]) == (
        "parent-1",
        "child_session",
        True,
    )
    assert sess["read_only"] is True and sess["source_tag"] == "subagent"


def _refresh_fn_sources():
    from tests.test_issue6999_session_updated_coalesce import SESSIONS_JS, _function_source

    helpers = [_function_source(SESSIONS_JS, n) for n in ("_isChildSession", "_isDelegatedSubagentRow")]
    # _function_source starts at "function", so restore the async keyword.
    return "\n".join([*helpers, "async " + _function_source(SESSIONS_JS, "refreshActiveSessionIfExternallyUpdated")])


def test_subagent_finishing_with_unchanged_count_collapses_worklog():
    answered = [*TWO_TURNS_RUNNING, ANSWER]
    remote = {**ENDED_SUBAGENT, "message_count": len(answered)}
    script = textwrap.dedent(
        f"""{_render_messages_harness()}
        let _activeSessionExternalRefreshInFlight = false;
        const _isMessageReaderUnpinned = () => false, _drainSessionUpdatedPendingCount = () => {{}};
        const _isExternalSession = () => false;
        let probes = 0;
        const api = async () => {{ probes += 1; return {{ session: {json.dumps(remote)} }}; }};
        const loadSession = async () => {{ throw new Error('no reload expected'); }};
        {_refresh_fn_sources()}
        S = {{ session: {{ ...{json.dumps(RUNNING_SUBAGENT)}, message_count: {len(answered)} }},
              messages: {json.dumps(answered)}, toolCalls: [], busy: false }};
        const flags = () => elements.msgInner.querySelectorAll('.tool-worklog-group')
          .map((g) => g.getAttribute('data-collapsed') === 'true');
        (async () => {{
          renderMessages();
          const running = flags();
          const poll = await refreshActiveSessionIfExternallyUpdated('poll');
          const ended = flags();
          const after = await refreshActiveSessionIfExternallyUpdated('poll');
          console.log(JSON.stringify([running, poll, ended, S.session.active, after, probes]));
        }})();
        """
    )
    # The running child is polled once; after it ends the poll gate skips again.
    assert json.loads(_run_node_script(script)) == [
        [True, False], "unchanged", [True, True], False, "skipped", 1,
    ]
