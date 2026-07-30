"""Shared layout-assertion helpers for Playwright browser tests."""
from __future__ import annotations

import json
from typing import Any

_LAYOUT_LINT_JS = """
function collectRenderViolations(scopeSelector, options) {
  var opts = options || {}
  var win = opts.__window || window
  var doc = opts.__document || win.document || document

  var overlapTol = opts.overlapTolerancePx != null ? opts.overlapTolerancePx : 4
  var clipTol = opts.clipTolerancePx != null ? opts.clipTolerancePx : 2
  var escapeTol = opts.escapeTolerancePx != null ? opts.escapeTolerancePx : 2
  var rawPat = new RegExp(opts.rawStringPattern || "^[a-z0-9]+(_[a-z0-9]+)+$")
  var enabledChecks = opts.checks || ["overlap", "clip", "container-escape", "degenerate", "raw-string", "a11y"]

  function enabled(name) {
    return enabledChecks.indexOf(name) !== -1
  }

  var root
  if (scopeSelector && typeof scopeSelector === "string") {
    root = doc.querySelector(scopeSelector)
    if (!root) {
      return [{ type: "degenerate", detail: "scope selector matched no element", elements: [scopeSelector], rects: [] }]
    }
  } else {
    root = doc.body || doc.documentElement
  }

  var violations = []

  function cssPath(node) {
    var parts = []
    var cur = node
    for (var i = 0; i < 4 && cur && cur.nodeType === 1; i++) {
      var tag = cur.tagName.toLowerCase()
      var id = cur.getAttribute("id")
      if (id) {
        parts.unshift(tag + "#" + id)
        break
      }
      var cls = cur.getAttribute("class")
      if (cls) {
        parts.unshift(tag + "." + cls.split(/\\s+/)[0])
      } else {
        parts.unshift(tag)
      }
      cur = cur.parentElement
    }
    return parts.join(" > ")
  }

  function getComputed(node) {
    return win.getComputedStyle(node)
  }

  function isVisible(node) {
    if (node.nodeType !== 1) return false
    var cs = getComputed(node)
    if (cs.display === "none" || cs.visibility === "hidden") return false
    var r = node.getBoundingClientRect()
    return r.width > 0 && r.height > 0
  }

  function isVisibleForDegenerate(node) {
    if (node.nodeType !== 1) return false
    var cs = getComputed(node)
    return cs.display !== "none" && cs.visibility !== "hidden"
  }

  function bearsText(node) {
    var cn = node.childNodes
    for (var i = 0; i < cn.length; i++) {
      if (cn[i].nodeType === 3 && cn[i].data && cn[i].data.trim() !== "") return true
    }
    return false
  }

  function hasSubtreeText(node) {
    if (node.nodeType === 3) return node.data && node.data.trim() !== ""
    if (node.nodeType !== 1) return false
    var cn = node.childNodes
    for (var i = 0; i < cn.length; i++) {
      if (hasSubtreeText(cn[i])) return true
    }
    return false
  }

  function getSubtreeText(node) {
    if (node.nodeType === 3) return node.data || ""
    if (node.nodeType !== 1) return ""
    var t = ""
    var cn = node.childNodes
    for (var i = 0; i < cn.length; i++) {
      t += getSubtreeText(cn[i])
    }
    return t
  }

  var INTERACTIVE_TAGS = { BUTTON: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1 }
  var CONTROL_TAGS = { BUTTON: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, A: 1, LABEL: 1 }

  function isInteractive(node) {
    if (INTERACTIVE_TAGS[node.tagName]) return true
    if (node.tagName === "A" && node.getAttribute("href") != null) return true
    if (node.getAttribute("role") === "button") return true
    return false
  }

  function isTextOrControl(node) {
    return bearsText(node) || !!CONTROL_TAGS[node.tagName]
  }

  function rectIntersection(a, b) {
    var x = Math.max(a.left, b.left)
    var y = Math.max(a.top, b.top)
    var r = Math.min(a.right, b.right)
    var bot = Math.min(a.bottom, b.bottom)
    return { width: Math.max(0, r - x), height: Math.max(0, bot - y) }
  }

  function fullyContains(outer, inner) {
    return outer.left <= inner.left && outer.top <= inner.top &&
      outer.right >= inner.right && outer.bottom >= inner.bottom
  }

  function findOverflowHiddenAncestor(node) {
    var cur = node.parentElement
    while (cur && cur !== root.parentElement) {
      var cs = getComputed(cur)
      if (cs.overflowX === "hidden" || cs.overflowY === "hidden") return cur
      cur = cur.parentElement
    }
    return null
  }

  function walk(node) {
    if (node.nodeType !== 1) return
    var cs = getComputed(node)
    if (cs.display === "none" || cs.visibility === "hidden") return

    var children = node.childNodes
    var visibleElements = []
    var degenerateElements = []
    for (var i = 0; i < children.length; i++) {
      if (children[i].nodeType !== 1) continue
      if (isVisible(children[i])) {
        visibleElements.push(children[i])
      } else if (isVisibleForDegenerate(children[i])) {
        degenerateElements.push(children[i])
      }
    }

    if (enabled("overlap")) {
      for (var i = 0; i < visibleElements.length; i++) {
        var ei = visibleElements[i]
        var csi = getComputed(ei)
        if (csi.position === "fixed") continue
        if (!isTextOrControl(ei)) continue
        var ri = ei.getBoundingClientRect()
        for (var j = i + 1; j < visibleElements.length; j++) {
          var ej = visibleElements[j]
          var csj = getComputed(ej)
          if (csj.position === "fixed") continue
          if (!isTextOrControl(ej)) continue
          var rj = ej.getBoundingClientRect()
          var inter = rectIntersection(ri, rj)
          if (inter.width > overlapTol && inter.height > overlapTol) {
            if (fullyContains(ri, rj) || fullyContains(rj, ri)) {
              if (!(bearsText(ei) && bearsText(ej))) continue
            }
            violations.push({
              type: "overlap",
              detail: "sibling elements overlap by " + Math.round(inter.width) + "x" + Math.round(inter.height) + "px",
              elements: [cssPath(ei), cssPath(ej)],
              rects: [ri, rj],
            })
          }
        }
      }
    }

    for (var i = 0; i < visibleElements.length; i++) {
      var child = visibleElements[i]

      if (enabled("clip") && bearsText(child)) {
        var csc = getComputed(child)
        if (child.scrollWidth > child.clientWidth + clipTol) {
          if (csc.textOverflow.indexOf("ellipsis") === -1 &&
            csc.overflowX !== "scroll" && csc.overflowX !== "auto") {
            violations.push({
              type: "clip",
              detail: "text clipped: scrollWidth " + child.scrollWidth + " > clientWidth " + child.clientWidth,
              elements: [cssPath(child)],
              rects: [child.getBoundingClientRect()],
            })
          }
        }
      }

      if (enabled("container-escape")) {
        var ancestor = findOverflowHiddenAncestor(child)
        if (ancestor) {
          var cr = child.getBoundingClientRect()
          var ar = ancestor.getBoundingClientRect()
          var ancestorCs = getComputed(ancestor)
          var escapeX = ancestorCs.overflowX === "hidden" && (cr.right - ar.right > escapeTol || ar.left - cr.left > escapeTol)
          var escapeY = ancestorCs.overflowY === "hidden" && (cr.bottom - ar.bottom > escapeTol || ar.top - cr.top > escapeTol)
          if (escapeX || escapeY) {
            violations.push({
              type: "container-escape",
              detail: "element extends past overflow-hidden ancestor",
              elements: [cssPath(child), cssPath(ancestor)],
              rects: [cr, ar],
            })
          }
        }
      }

      if (enabled("degenerate") && isInteractive(child)) {
        if (child.tagName === "INPUT" && child.getAttribute("type") === "hidden") {
          // skip
        } else if (isVisibleForDegenerate(child)) {
          var dr = child.getBoundingClientRect()
          var zeroSize = dr.width === 0 || dr.height === 0
          var offViewport = dr.right < 0 || dr.bottom < 0 || dr.left > win.innerWidth || dr.top > win.innerHeight
          if (zeroSize || offViewport) {
            violations.push({
              type: "degenerate",
              detail: zeroSize ? "interactive element has zero-size rect" : "interactive element is off-viewport",
              elements: [cssPath(child)],
              rects: [dr],
            })
          }
        }
      }

      if (enabled("raw-string")) {
        var cn = child.childNodes
        for (var k = 0; k < cn.length; k++) {
          if (cn[k].nodeType === 3 && cn[k].data) {
            var trimmed = cn[k].data.trim()
            if (trimmed && rawPat.test(trimmed)) {
              violations.push({
                type: "raw-string",
                detail: "text node matches raw-string pattern: " + trimmed,
                elements: [cssPath(child)],
                rects: [child.getBoundingClientRect()],
              })
            }
          }
        }
        var RAW_ATTRS = ["title", "aria-label", "placeholder"]
        for (var k = 0; k < RAW_ATTRS.length; k++) {
          var val = child.getAttribute(RAW_ATTRS[k])
          if (val && rawPat.test(val.trim())) {
            violations.push({
              type: "raw-string",
              detail: RAW_ATTRS[k] + " attribute matches raw-string pattern: " + val.trim(),
              elements: [cssPath(child)],
              rects: [],
            })
          }
        }
      }

      if (enabled("a11y") && isInteractive(child)) {
        var ariaLabel = child.getAttribute("aria-label")
        var title = child.getAttribute("title")
        var labelledby = child.getAttribute("aria-labelledby")
        var subtreeHasText = hasSubtreeText(child)
        var hasName = (ariaLabel && ariaLabel.trim()) || (title && title.trim()) || (labelledby && labelledby.trim())
        if (!subtreeHasText && !hasName) {
          violations.push({
            type: "missing-accessible-name",
            detail: "interactive element has no accessible name",
            elements: [cssPath(child)],
            rects: [child.getBoundingClientRect()],
          })
        }
        if (ariaLabel && ariaLabel.trim() && subtreeHasText) {
          var visibleText = getSubtreeText(child).trim().toLowerCase()
          if (visibleText && ariaLabel.trim().toLowerCase().indexOf(visibleText) === -1) {
            violations.push({
              type: "label-not-in-name",
              detail: "aria-label \\"" + ariaLabel.trim() + "\\" does not contain visible text \\"" + visibleText + "\\"",
              elements: [cssPath(child)],
              rects: [child.getBoundingClientRect()],
            })
          }
        }
      }
    }

    if (enabled("degenerate")) {
      for (var i = 0; i < degenerateElements.length; i++) {
        var dNode = degenerateElements[i]
        if (isInteractive(dNode)) {
          if (dNode.tagName === "INPUT" && dNode.getAttribute("type") === "hidden") continue
          var dr2 = dNode.getBoundingClientRect()
          var zs = dr2.width === 0 || dr2.height === 0
          var ov = dr2.right < 0 || dr2.bottom < 0 || dr2.left > win.innerWidth || dr2.top > win.innerHeight
          if (zs || ov) {
            violations.push({
              type: "degenerate",
              detail: zs ? "interactive element has zero-size rect" : "interactive element is off-viewport",
              elements: [cssPath(dNode)],
              rects: [dr2],
            })
          }
        }
      }
    }

    for (var i = 0; i < visibleElements.length; i++) {
      walk(visibleElements[i])
    }
  }

  walk(root)
  return violations
}
"""

_DEFAULT_CHECKS = ["overlap", "clip", "container-escape", "degenerate", "raw-string"]


def _build_evaluate_script(scope_selector=None, checks=None):
    """Build a self-contained JS IIFE for page.evaluate()."""
    checks_json = json.dumps(checks or _DEFAULT_CHECKS)
    scope_json = json.dumps(scope_selector)
    return "(" + _LAYOUT_LINT_JS + ")(" + scope_json + ", {checks: " + checks_json + "})"


def _format_violations(violations):
    """Format violation dicts into a readable assertion message."""
    lines = []
    for v in violations:
        elements = ", ".join(v.get("elements", []))
        lines.append(f"[{v['type']}] {v['detail']} ({elements})")
    return f"{len(violations)} layout violation(s):\n" + "\n".join(lines)


def collect_layout_violations(page: Any, scope_selector: str | None = None, *, checks: list[str] | None = None) -> list[dict]:
    """Run geometry + raw-string checks in-browser. Returns list of {type, detail, elements, rects}."""
    script = _build_evaluate_script(scope_selector, checks)
    return page.evaluate(script)


def assert_layout_sane(page: Any, scope_selector: str | None = None, *, checks: list[str] | None = None) -> None:
    """Assert no layout violations. Raises AssertionError with formatted details."""
    violations = collect_layout_violations(page, scope_selector, checks=checks)
    if violations:
        raise AssertionError(_format_violations(violations))


def assert_no_raw_i18n_keys(page: Any, scope_selector: str | None = None) -> None:
    """Assert no leaked raw i18n keys are visible."""
    violations = collect_layout_violations(page, scope_selector, checks=["raw-string"])
    if violations:
        raise AssertionError(_format_violations(violations))
