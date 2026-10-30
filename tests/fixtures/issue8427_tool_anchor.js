/* Synthetic presentation-only fixture for #8427. No server, provider or DB.
 * Loads the actual renderer; only network access is disabled here. jsdom's
 * layout/observer stubs live in the Node driver, not in the browser fixture.
 */
'use strict';
window.fetch = () => Promise.reject(new Error('Network disabled in issue8427 fixture'));
window.issue8427 = (() => {
  const fixture = {
    session_id: 'synthetic-tool-anchor-boundary', title: 'Synthetic #8427',
    workspace: '.', profile: 'default', session_source: 'webui', messages: [], tool_calls: [],
  };
  for (let turn = 0; turn < 3; turn++) {
    const rawIdx = fixture.messages.length;
    const tid = `synthetic-tool-${turn}`;
    const call = {id: tid, type: 'function', function: {name: 'read_file', arguments: '{"path":"synthetic.txt"}'}};
    const assistant = {role: 'assistant', content: '', finish_reason: 'tool_calls'};
    // Each supported legacy source reaches the shared helper. The first turn
    // owns progress; later turns begin with no prose/reasoning of their own.
    // Partial/tool_use shapes intentionally stay on the compatibility path,
    // rather than being promoted by the strict ID-linked historical hydrator.
    if (turn === 0) {
      assistant.content = 'Synthetic progress';
      assistant.tool_calls = [call];
    } else if (turn === 1) {
      assistant._partial_tool_calls = [{tid, name: 'read_file', args: {path: 'synthetic.txt'}}];
    } else {
      assistant.content = [{type: 'tool_use', id: tid, name: 'read_file', input: {path: 'synthetic.txt'}}];
    }
    fixture.messages.push(
      {role: 'user', content: `Synthetic question ${turn}`},
      assistant,
      {role: 'tool', content: 'Synthetic tool result', tool_call_id: tid},
      {role: 'assistant', content: `Synthetic final answer ${turn}`, finish_reason: 'stop'},
    );
    fixture.tool_calls.push({name: 'read_file', tid, snippet: 'Synthetic tool result', assistant_msg_idx: rawIdx + 1});
  }
  const clone = value => JSON.parse(JSON.stringify(value));
  function check(condition, message) {
    if (!condition) throw new Error(message);
  }
  function prepare(mode = 'compact_worklog', showThinking = true) {
    check(['compact_worklog', 'transparent_stream', 'hide_all_activity'].includes(mode), 'Unknown activity mode');
    S.session = clone(fixture);
    S.messages = clone(fixture.messages);
    S.toolCalls = clone(fixture.tool_calls);
    S.busy = false;
    S.activeStreamId = null;
    window._chatActivityDisplayMode = mode;
    window._showThinking = showThinking;
    window._simplifiedToolCalling = true;
  }
  function render(preserveScroll = false) {
    renderMessages({_virtualFallback: true, preserveScroll});
  }
  function assertAnswers(stage, layout = true) {
    for (const idx of [3, 7, 11]) {
      const nodes = document.querySelectorAll(`.assistant-segment[data-msg-idx="${idx}"]`);
      check(nodes.length === 1, `${stage}: answer ${idx} must appear once`);
      const node = nodes[0];
      check(!node.hidden && node.getAttribute('aria-hidden') !== 'true', `${stage}: answer ${idx} must not be hidden`);
      check(!node.classList.contains('assistant-segment-worklog-source'), `${stage}: answer ${idx} must not become Worklog source`);
      check(!node.closest('.tool-worklog-group,.tool-call-group'), `${stage}: answer ${idx} must stay outside activity`);
      check(node.dataset.rawText === fixture.messages[idx].content, `${stage}: full answer ${idx} preserved`);
      check(node.textContent.includes(fixture.messages[idx].content), `${stage}: answer ${idx} has rendered prose`);
      check(!document.querySelector(`.wl-reason[data-worklog-anchor-key="msg:${idx}"]`), `${stage}: answer ${idx} duplicated into Worklog`);
      if (layout) {
        check(node.getClientRects().length > 0, `${stage}: answer ${idx} has no browser layout box`);
        for (let ancestor = node; ancestor; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
          check(!ancestor.hidden && style.display !== 'none' && style.visibility !== 'hidden', `${stage}: answer ${idx} has hidden ancestor`);
        }
      }
    }
  }
  function assertOwners() {
    for (let turn = 0; turn < 3; turn++) {
      const cards = S.toolCalls.filter(tc => tc.tid === `synthetic-tool-${turn}`);
      check(cards.length > 0, `tool ${turn} must survive fallback rebuild`);
      check(cards.every(tc => tc.assistant_msg_idx === turn * 4 + 1), `tool ${turn} borrowed a different user turn's anchor`);
    }
  }
  function expand(browserClick = false) {
    const groups = document.querySelectorAll('.tool-worklog-group,.tool-call-group');
    for (const group of groups) {
      const summary = group.querySelector('.tool-call-group-summary,.activity-summary');
      if (summary && group.classList.contains('tool-call-group-collapsed')) {
        if (browserClick) summary.click();
        else _toggleToolWorklogGroup(summary);
      }
    }
    check(Array.from(groups).every(group => !group.classList.contains('tool-call-group-collapsed')), 'all activity groups must expand');
    return groups.length;
  }
  function collapse(browserClick = false) {
    const groups = document.querySelectorAll('.tool-worklog-group,.tool-call-group');
    for (const group of groups) {
      const summary = group.querySelector('.tool-call-group-summary,.activity-summary');
      if (summary && !group.classList.contains('tool-call-group-collapsed')) {
        if (browserClick) summary.click();
        else _toggleToolWorklogGroup(summary);
      }
    }
    check(Array.from(groups).every(group => group.classList.contains('tool-call-group-collapsed')), 'all activity groups must collapse');
    return groups.length;
  }
  function run(mode, showThinking, {layout = true} = {}) {
    prepare(mode, showThinking);
    render();
    assertAnswers('initial', layout);
    assertOwners();
    expand(layout);
    assertAnswers('expanded', layout);
    assertOwners();
    const groups = collapse(layout);
    if (mode === 'compact_worklog') check(groups > 0, 'Compact Worklog must exercise disclosure groups');
    assertAnswers('collapsed', layout);
    render(true);
    assertAnswers('rerender', layout);
    assertOwners();
    return {mode, showThinking, finalAnswers: 3, stages: ['initial', 'expanded', 'collapsed', 'rerender'], layoutChecked: layout};
  }
  return {fixture, prepare, render, assertAnswers, assertOwners, expand, collapse, run};
})();
