"""Executed composer-fit lifecycle tests using the production JavaScript."""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  const open = src.indexOf('{', start);
  let depth = 1;
  let end = open + 1;
  while (depth > 0 && end < src.length) {
    if (src[end] === '{') depth++;
    else if (src[end] === '}') depth--;
    end++;
  }
  return src.slice(start, end);
}

const production = [
  'let _composerFitScheduled=false;',
  'let _composerFitResizeObserver=null;',
  'let _composerFitMutationObserver=null;',
  'let _composerFitObservedFooter=null;',
  'let _composerFitResizeListenerBound=false;',
  'let _composerFitLastFooter=null;',
  'let _composerFitLastKey="";',
  extractFunc('_fitComposerFooter'),
  extractFunc('_scheduleComposerFit'),
  extractFunc('_initComposerFooterFit'),
].join('\n');

function makeWorld(contentWidth) {
  const raf = [];
  const mutationObservers = [];
  let measurements = 0;
  let boundaryCalls = 0;
  const classes = new Set();
  const left = {
    name: 'left',
    clientWidth: 400,
    get scrollWidth() { return contentWidth; },
    children: [{hidden: false}, {hidden: true}, {display: 'none'}],
  };
  const footer = {
    name: 'footer',
    clientWidth: 800,
    style: {},
    offsetWidth: 800,
    classList: {
      contains(c) { return classes.has(c); },
      add(...cs) { cs.forEach(c => classes.add(c)); },
      remove(...cs) { cs.forEach(c => classes.delete(c)); },
      toggle(c, force) { if (force) classes.add(c); else classes.delete(c); },
    },
    querySelector(sel) { return sel === '.composer-left' ? left : null; },
    getBoundingClientRect() { measurements++; return {height: 42}; },
  };
  const messages = {name: 'messages', children: []};
  left.parentNode = footer;
  const ctx = {
    document: {querySelector(sel) {
      if (sel === '.composer-footer') return footer;
      if (sel === '#messages') return messages;
      return null;
    }},
    requestAnimationFrame(cb) { raf.push(cb); return raf.length; },
    getComputedStyle(el) { return {display: el.display || 'inline-flex'}; },
    _onPhoneBoundaryChange() { boundaryCalls++; },
    addEventListener() {},
  };
  ctx.window = ctx;
  ctx.ResizeObserver = class {
    constructor(cb) { this.cb = cb; }
    observe() {}
    disconnect() {}
  };
  ctx.MutationObserver = class {
    constructor(cb) { this.cb = cb; }
    observe(target, options) {
      this.target = target;
      this.options = options;
      mutationObservers.push(this);
    }
    disconnect() {}
  };
  vm.createContext(ctx);
  vm.runInContext(production, ctx);
  function flush() { while (raf.length) raf.shift()(); }
  function notify(target) {
    for (const observer of mutationObservers) {
      let node = target;
      let contained = false;
      while (node) {
        if (node === observer.target) { contained = true; break; }
        if (!observer.options.subtree) break;
        node = node.parentNode;
      }
      if (contained) observer.cb([]);
    }
  }
  ctx._initComposerFooterFit();
  flush();
  return {
    ctx, footer, left, messages, raf, mutationObservers, flush, notify,
    measurements() { return measurements; },
    boundaryCalls() { return boundaryCalls; },
    burger() { return classes.has('cf-burger'); },
  };
}

const transcript = makeWorld(300);
transcript.messages.children.push({});
transcript.notify(transcript.messages);

const leftChange = makeWorld(300);
const added = {hidden: false, parentNode: leftChange.left};
leftChange.left.children.push(added);
leftChange.notify(added);
const leftScheduled = leftChange.raf.length;
leftChange.flush();

const unchanged = makeWorld(300);
unchanged.ctx._fitComposerFooter(false);
unchanged.ctx._fitComposerFooter(false);

const explicit = makeWorld(300);
explicit.ctx._fitComposerFooter(true);

const burger = makeWorld(900);
burger.ctx._fitComposerFooter(false);
burger.ctx._fitComposerFooter(false);

process.stdout.write(JSON.stringify({
  transcript: {
    scheduled: transcript.raf.length,
    targets: transcript.mutationObservers.map(o => o.target.name),
  },
  leftChange: {scheduled: leftScheduled, measurements: leftChange.measurements()},
  unchanged: {measurements: unchanged.measurements()},
  explicit: {measurements: explicit.measurements()},
  burger: {calls: burger.boundaryCalls(), active: burger.burger()},
}));
"""


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    driver = tmp_path_factory.mktemp("composer-fit-scope") / "driver.js"
    driver.write_text(textwrap.dedent(DRIVER), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_transcript_mutations_do_not_schedule_a_footer_refit(outcome):
    assert outcome["transcript"] == {"scheduled": 0, "targets": ["footer"]}


def test_composer_left_content_change_schedules_and_completes_refit(outcome):
    assert outcome["leftChange"] == {"scheduled": 1, "measurements": 2}


def test_unchanged_scheduled_fit_returns_before_layout_measurement(outcome):
    assert outcome["unchanged"]["measurements"] == 1


def test_explicit_caller_forces_fit_even_with_unchanged_key(outcome):
    assert outcome["explicit"]["measurements"] == 2
    body = re.search(
        r"function _applyComposerControlOrder\(order\)\{(?P<body>.*?)\n\}",
        BOOT_JS,
        re.DOTALL,
    )
    assert body and "_fitComposerFooter(true)" in body.group("body")


def test_burger_transition_still_closes_composer_menus_once(outcome):
    assert outcome["burger"] == {"calls": 1, "active": True}
