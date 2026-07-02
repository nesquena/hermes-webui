"""Behavioral coverage for issue #4814 Mermaid toolbar controls."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


_DRIVER_SRC = r"""
const fs = require('fs');

const ui = fs.readFileSync(process.argv[2], 'utf8');
const helperStart = ui.indexOf('const _MERMAID_VIEWER_MIN_SCALE');
const helperEnd = ui.indexOf('function _openMermaidLightbox');
if (helperStart < 0 || helperEnd < 0) {
  throw new Error('could not locate Mermaid viewer helpers');
}

const documentListeners = {};
function makeClassList(node) {
  const classes = new Set();
  return {
    add(...items) { items.forEach((item) => item && classes.add(item)); node.className = [...classes].join(' '); },
    remove(...items) { items.forEach((item) => classes.delete(item)); node.className = [...classes].join(' '); },
    contains(item) { return classes.has(item); },
    toggle(item, force) {
      if (force === true) { classes.add(item); node.className = [...classes].join(' '); return true; }
      if (force === false) { classes.delete(item); node.className = [...classes].join(' '); return false; }
      if (classes.has(item)) { classes.delete(item); node.className = [...classes].join(' '); return false; }
      classes.add(item); node.className = [...classes].join(' '); return true;
    },
    toJSON() { return [...classes]; },
  };
}

function makeElement(tagName) {
  const node = {
    tagName: String(tagName || '').toUpperCase(),
    children: [],
    parentNode: null,
    className: '',
    dataset: {},
    style: {},
    attributes: {},
    textContent: '',
    innerHTML: '',
    clientWidth: 0,
    clientHeight: 0,
    onclick: null,
    onpointerdown: null,
    onpointermove: null,
    onpointerup: null,
    onpointercancel: null,
    onpointerleave: null,
    onwheel: null,
    classList: null,
    capturedPointerId: null,
    releasedPointerId: null,
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    replaceChild(next, prev) {
      const idx = this.children.indexOf(prev);
      if (idx < 0) throw new Error('replaceChild target missing');
      if (next.parentNode) next.parentNode.removeChild(next);
      this.children[idx] = next;
      next.parentNode = this;
      prev.parentNode = null;
      return prev;
    },
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx >= 0) this.children.splice(idx, 1);
      child.parentNode = null;
      return child;
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
      if (name === 'class') this.className = String(value);
      if (name === 'aria-label') this.ariaLabel = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    },
    removeAttribute(name) {
      delete this.attributes[name];
    },
    getBoundingClientRect() {
      return {left: 0, top: 0, width: this.clientWidth, height: this.clientHeight};
    },
    setPointerCapture(pointerId) {
      this.capturedPointerId = pointerId;
    },
    releasePointerCapture(pointerId) {
      this.releasedPointerId = pointerId;
      if (this.capturedPointerId === pointerId) this.capturedPointerId = null;
    },
    cloneNode() {
      const copy = makeElement(this.tagName);
      copy.className = this.className;
      copy.dataset = {...this.dataset};
      copy.style = {...this.style};
      copy.attributes = {...this.attributes};
      copy.textContent = this.textContent;
      copy.innerHTML = this.innerHTML;
      copy.clientWidth = this.clientWidth;
      copy.clientHeight = this.clientHeight;
      copy.classList = makeClassList(copy);
      for (const cls of String(this.className || '').split(/\s+/).filter(Boolean)) copy.classList.add(cls);
      return copy;
    },
  };
  node.classList = makeClassList(node);
  return node;
}

function makeSvg(width, height) {
  const svg = makeElement('svg');
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.viewBox = {baseVal: {x: 0, y: 0, width, height}};
  svg.getBBox = () => ({x: 0, y: 0, width, height});
  return svg;
}

const document = {
  body: makeElement('body'),
  createElement(tagName) {
    return makeElement(tagName);
  },
  addEventListener(type, handler) {
    (documentListeners[type] ||= []).push(handler);
  },
  removeEventListener(type, handler) {
    const list = documentListeners[type] || [];
    const idx = list.indexOf(handler);
    if (idx >= 0) list.splice(idx, 1);
  },
};

const window = {innerWidth: 1200, innerHeight: 800};
global.document = document;
global.window = window;
global.requestAnimationFrame = (fn) => fn();
global.cancelAnimationFrame = () => {};

eval(ui.slice(helperStart, helperEnd));

function makeHost(svg) {
  const host = makeElement('div');
  host.appendChild(svg);
  return host;
}

function labelsFromToolbar(viewer) {
  return viewer._mermaidViewer.toolbar.children.map((child) => child.getAttribute('aria-label'));
}

function runScenario(payload) {
  const svg = makeSvg(payload.width || 480, payload.height || 320);
  const host = makeHost(svg);
  const viewer = _mountMermaidViewer(svg, payload.options || {});
  const state = viewer._mermaidViewer;
  state.viewport.clientWidth = payload.viewportWidth || 960;
  state.viewport.clientHeight = payload.viewportHeight || 540;
  state.viewport.getBoundingClientRect = () => ({left: 0, top: 0, width: state.viewport.clientWidth, height: state.viewport.clientHeight});
  if (payload.fitBefore) state.fit();

  const result = {
    className: viewer.className,
    labels: labelsFromToolbar(viewer),
    scale: state.scale,
    x: state.x,
    y: state.y,
    canvasWidth: state.canvas.style.width,
    canvasHeight: state.canvas.style.height,
    viewportWidth: state.viewport.style.width,
    viewportHeight: state.viewport.style.height,
  };

  if (payload.scenario === 'toolbar') {
    result.hasInlineFullscreen = result.labels.includes('Fullscreen');
    return result;
  }

  if (payload.scenario === 'zoom') {
    state.fit();
    const fitScale = state.scale;
    state.zoomIn();
    const zoomInScale = state.scale;
    state.zoomOut();
    const zoomOutScale = state.scale;
    for (let i = 0; i < 20; i += 1) state.zoomIn();
    const maxScale = state.scale;
    state.reset();
    const reset = {scale: state.scale, x: state.x, y: state.y};
    state.fit();
    const beforeWheel = {scale: state.scale, x: state.x, y: state.y};
    state.viewport.onwheel({deltaY: -120, clientX: 240, clientY: 160, preventDefault() {}});
    const afterWheel = {scale: state.scale, x: state.x, y: state.y};
    state.fit();
    const beforeLineWheel = {scale: state.scale, x: state.x, y: state.y};
    state.viewport.onwheel({deltaY: -3, deltaMode: 1, clientX: 240, clientY: 160, preventDefault() {}});
    const afterLineWheel = {scale: state.scale, x: state.x, y: state.y};
    return {fitScale, zoomInScale, zoomOutScale, maxScale, reset, beforeWheel, afterWheel, beforeLineWheel, afterLineWheel};
  }

  if (payload.scenario === 'drag') {
    const opens = [];
    const interactive = _mountMermaidViewer(svg, {
      mode: 'inline',
      openLightbox() { opens.push('open'); },
    });
    const interactiveState = interactive._mermaidViewer;
    interactiveState.viewport.clientWidth = 960;
    interactiveState.viewport.clientHeight = 540;
    interactiveState.viewport.getBoundingClientRect = () => ({left: 0, top: 0, width: interactiveState.viewport.clientWidth, height: interactiveState.viewport.clientHeight});
    interactiveState.fit();
    const start = {x: interactiveState.x, y: interactiveState.y, scale: interactiveState.scale};
    interactiveState.viewport.onpointerdown({button: 0, clientX: 100, clientY: 100, pointerId: 7, preventDefault() {}});
    interactiveState.viewport.onpointermove({clientX: 160, clientY: 150});
    interactiveState.viewport.onpointerup({pointerId: 7, preventDefault() {}});
    interactiveState.viewport.onclick({preventDefault() {}, stopPropagation() {}, target: interactiveState.viewport});
    const afterDrag = {
        x: interactiveState.x,
        y: interactiveState.y,
        dragged: interactiveState.dragged,
        opens: opens.length,
        releasedPointerId: interactiveState.viewport.releasedPointerId,
        capturedPointerId: interactiveState.viewport.capturedPointerId,
    };
    interactiveState.dragged = false;
    interactiveState.viewport.onclick({preventDefault() {}, stopPropagation() {}, target: interactiveState.viewport});
    const afterClick = {opens: opens.length};
    return {start, afterDrag, afterClick};
  }

  if (payload.scenario === 'fullscreen') {
    const opens = [];
    const inlineViewer = _mountMermaidViewer(svg, {
      mode: 'inline',
      openLightbox() { opens.push('inline-lightbox'); },
    });
    const inlineLabels = labelsFromToolbar(inlineViewer);
    const fullscreen = inlineViewer._mermaidViewer.toolbar.children.find((btn) => btn.getAttribute('aria-label') === 'Fullscreen');
    fullscreen.onclick({preventDefault() {}, stopPropagation() {}});

    const lightboxSvg = makeSvg(payload.width || 480, payload.height || 320);
    const lightboxViewer = _mountMermaidViewer(lightboxSvg, {mode: 'lightbox'});
    const lightboxLabels = labelsFromToolbar(lightboxViewer);
    return {
      inlineLabels,
      lightboxLabels,
      opens,
      lightboxClassName: lightboxViewer.className,
    };
  }

  throw new Error('unknown scenario: ' + payload.scenario);
}

const payload = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(runScenario(payload)));
"""


def _run_node(driver_path: str, payload: dict) -> dict:
    result = subprocess.run(
        [NODE, driver_path, str(UI), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def _driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("mermaid_toolbar_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_inline_viewer_mounts_toolbar_and_shell(_driver_path):
    result = _run_node(_driver_path, {
        "scenario": "toolbar",
        "options": {"mode": "inline", "openLightbox": "ignored"},
    })

    assert result["className"] == "mermaid-viewer mermaid-viewer--inline"
    assert result["labels"] == ["Zoom in", "Zoom out", "Reset view", "Fit to screen", "Fullscreen"]
    assert result["canvasWidth"] == "480px"
    assert result["viewportWidth"] == "100%"
    assert result["hasInlineFullscreen"] is True


def test_zoom_fit_reset_and_wheel_update_state(_driver_path):
    result = _run_node(_driver_path, {"scenario": "zoom", "options": {"mode": "inline"}})

    assert 1.0 < result["fitScale"] < 8.0
    assert result["zoomInScale"] > result["fitScale"]
    assert result["zoomOutScale"] < result["zoomInScale"]
    assert result["maxScale"] <= 8.0
    assert result["reset"]["scale"] == 1
    assert result["afterWheel"]["scale"] > result["beforeWheel"]["scale"]
    assert (result["afterWheel"]["x"], result["afterWheel"]["y"]) != (result["beforeWheel"]["x"], result["beforeWheel"]["y"])
    assert result["afterLineWheel"]["scale"] > result["beforeLineWheel"]["scale"] * 1.1


def test_drag_suppresses_accidental_lightbox_open(_driver_path):
    result = _run_node(_driver_path, {"scenario": "drag", "options": {"mode": "inline"}})

    assert result["start"]["scale"] > 0
    assert (result["afterDrag"]["x"], result["afterDrag"]["y"]) != (result["start"]["x"], result["start"]["y"])
    assert result["afterDrag"]["opens"] == 0
    assert result["afterDrag"]["releasedPointerId"] == 7
    assert result["afterDrag"]["capturedPointerId"] is None
    assert result["afterClick"]["opens"] == 1


def test_lightbox_mode_uses_same_viewer_helper_without_fullscreen(_driver_path):
    result = _run_node(_driver_path, {"scenario": "fullscreen", "options": {"mode": "inline"}})

    assert result["inlineLabels"] == ["Zoom in", "Zoom out", "Reset view", "Fit to screen", "Fullscreen"]
    assert result["lightboxLabels"] == ["Zoom in", "Zoom out", "Reset view", "Fit to screen"]
    assert result["opens"] == ["inline-lightbox"]
    assert result["lightboxClassName"] == "mermaid-viewer mermaid-viewer--lightbox"
