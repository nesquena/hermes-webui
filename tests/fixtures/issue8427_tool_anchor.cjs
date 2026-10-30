'use strict';
// Dependency-free helper checks. Renderer checks additionally require jsdom.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/ui.js'), 'utf8');

function helperContext() {
  const context = vm.createContext({window: {}});
  // These declarations have column-zero closing braces. Load the real helpers,
  // not Python mirrors or a replacement ownership predicate.
  for (const name of [
    'msgContent', '_isRecoveryControlMessageText', '_isRecoveryControlMessage',
    '_assistantAnchorSceneFinalAnswerText', '_messageHasReasoningPayload',
    '_isAssistantEmptyPlaceholderContent', '_assistantMessageHasVisibleContent',
    '_assistantReasoningPayloadText', '_assistantToolAnchorIdxForMessage',
  ]) {
    const start = source.indexOf(`\nfunction ${name}(`);
    assert.notEqual(start, -1, `missing ${name}`);
    const end = source.indexOf('\n}', start);
    assert.notEqual(end, -1, `unclosed ${name}`);
    vm.runInContext(source.slice(start + 1, end + 2), context, {filename: `ui.js:${name}`});
  }
  return context;
}

function helperCases() {
  const previous = {role: 'assistant', content: 'Previous final answer'};
  const user = {role: 'user', content: 'New question'};
  const empty = {role: 'assistant', content: ''};
  const tool = {role: 'tool', content: 'Synthetic tool result'};
  return [
    // The exact reported trigger, without tool metadata or extra flags.
    ['user-boundary', [previous, user, empty], 2, 2],
    ['user-boundary-through-tool', [previous, user, tool, empty], 3, 3],
    ['nearest-user-boundary', [previous, user, empty, tool, empty], 4, 4],
    ['same-turn-through-tool', [user, previous, tool, empty], 3, 1],
    ['same-turn-nearest-prose', [user, previous, tool, {role: 'assistant', content: 'Progress'}, tool, empty], 5, 3],
    ['own-prose', [previous, {role: 'assistant', content: 'Own answer'}], 1, 1],
    ['own-text-parts', [previous, {role: 'assistant', content: [{type: 'text', text: 'Own answer'}]}], 1, 1],
    ...['reasoning', 'reasoning_content', 'thinking', '_reasoning'].map(key => [
      `own-${key}`, [previous, {...empty, [key]: 'Synthetic reasoning'}], 1, 1,
    ]),
    ['own-thinking-parts', [previous, {role: 'assistant', content: [{type: 'thinking', text: 'Synthetic reasoning'}]}], 1, 1],
    ['first-empty', [empty], 0, 0],
    ['null-gap-within-turn', [previous, null, tool, empty], 3, 0],
  ];
}

function runHelpers(selected) {
  const context = helperContext();
  const cases = helperCases().filter(([name]) => !selected || name === selected);
  assert.ok(cases.length, `unknown helper case: ${selected}`);
  for (const [name, messages, rawIdx, expected] of cases) {
    assert.equal(context._assistantToolAnchorIdxForMessage(messages, rawIdx), expected, name);
  }
  return {checks: cases.length};
}

function runRenderer(mode, showThinking) {
  const {JSDOM} = require('jsdom');
  const dom = new JSDOM(fs.readFileSync(path.join(__dirname, 'issue8427_tool_anchor.html'), 'utf8'), {
    url: 'http://localhost/tests/fixtures/issue8427_tool_anchor.html',
    runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const w = dom.window;
  // jsdom has no layout/observer engine. Browser runs use the native APIs.
  w.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}});
  w.ResizeObserver = w.IntersectionObserver = class {observe() {} unobserve() {} disconnect() {}};
  w.requestAnimationFrame = () => 0;
  w.cancelAnimationFrame = () => {};
  w.queueMicrotask = () => {};
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.CSS = {escape: value => String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&'), supports: () => false};
  try {
    const context = dom.getInternalVMContext();
    vm.runInContext(fs.readFileSync(path.join(__dirname, 'issue8427_tool_anchor.js'), 'utf8'), context);
    for (const name of ['i18n.js', 'icons.js', 'assistant_turn_anchors.js', 'ui.js', 'workspace.js', 'sessions.js', 'messages.js']) {
      vm.runInContext(fs.readFileSync(path.join(root, 'static', name), 'utf8'), context, {filename: name});
    }
    return w.issue8427.run(mode, showThinking, {layout: false});
  } finally {
    w.close();
  }
}

const [command = 'helpers', argument, thinking] = process.argv.slice(2);
try {
  const result = command === 'helpers' ? runHelpers(argument)
    : command === 'renderer' ? runRenderer(argument || 'compact_worklog', thinking !== 'false')
    : assert.fail(`unknown command: ${command}`);
  console.log(JSON.stringify({result: 'PASS', ...result}));
} catch (error) {
  console.error(error.stack);
  process.exitCode = 1;
}
