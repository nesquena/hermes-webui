#!/usr/bin/env python3
"""scope_undef_gate.py — catch the "ReferenceError: X is not defined" brick class.

The WebUI front-end ships as classic (non-module) ``<script>`` tags that all share
ONE implicit global scope. A function declared *inside* another function is NOT a
global, so calling it (un-guarded) from a different top-level function throws
``ReferenceError`` at runtime — but only when that code path actually executes in
the browser. ``node --check`` (syntax only), source-presence tests, and the
existing ``no-const-assign`` ESLint runtime gate all MISS it.

Canonical bug — #3696 (v0.51.269 regression): ``_sessionAttentionState`` was
declared *inside* ``renderSessionListFromCache()`` and relied on "function
hoisting", but a *separate* top-level function ``_sidebarRowHasVisibleMessages``
called it bare. Hoisting is scoped to the enclosing function, so every sidebar
cache-render crashed with ``_sessionAttentionState is not defined`` and the
session list went blank.

How the gate works (and why it has no cross-file false positives):
  1. It scans every static ``*.js`` file for the union of all TOP-LEVEL symbols
     (``function NAME``, top-level ``const/let/var``, and ``window.NAME = ...``).
     That union IS the real shared global namespace at runtime.
  2. It lints each file individually with ESLint ``no-undef``, supplying that
     union (plus browser/library builtins) as ``globals``. Cross-file references
     (``api``, ``loadSession``, ``renderSessionList`` …) resolve cleanly because
     they ARE top-level somewhere; meanwhile ESLint's per-file scope analysis
     still flags a name that is *defined only nested* and called from a sibling
     scope in the same file — the #3696 shape.
  3. A short, documented allowlist covers names that are legitimately dynamic and
     ESLint can't see are safe: helpers exposed via ``window.NAME = ...`` and
     called bare elsewhere, and OPTIONAL helpers always called behind a
     ``typeof NAME === 'function'`` guard (those can't throw — the guard is the
     contract). The un-guarded bare call is exactly what makes #3696 a bug.

Usage:
  python3 scripts/scope_undef_gate.py [/path/to/webui/checkout]
  (defaults to the repo this script lives in)

Exit 0 = clean (or eslint unavailable → skip). Non-zero = a new undefined /
scope-misplaced reference that throws at runtime. DO NOT TAG/RELEASE on non-zero.

Known false-negative classes (the gate is a strong net, not a proof):
  * Name-collision shadowing — if two files both declare a top-level symbol of the
    same name, the union includes it, so a bare cross-scope call to that name goes
    unflagged (runtime binds to the other file's symbol = a wrong-function bug, not
    a ReferenceError). A duplicate top-level symbol across files is itself a smell.
  * Non-`,;`-terminated top-level destructuring (`const {a, b} = x` where `b` ends
    the line) is not captured by the symbol regex below. None exist in the bundle
    today; revisit the regex if that pattern is introduced.
  * Exposure escape hatches not scanned: `globalThis.X = X`, `Object.assign(window,
    {X})`, `(0,eval)(...)`. None used today.
  * The allowlist is keyed by NAME, not call-site — an entry green-lights every bare
    reference to that name everywhere. Only add an entry when EVERY call site is a
    `window.X =` exposure or a `typeof X === 'function'` guard; otherwise fix the
    bug (hoist / pass the value as a param), don't allowlist it. (#3696 review:
    a `source` allowlist entry was masking a real same-class bug at messages.js:876,
    fixed by threading `source` as a parameter instead of allowlisting.)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

# Browser + ECMAScript builtins + CDN libraries loaded before our bundle. Explicit
# (not an eslint "env") so the gate is self-contained and reviewable.
BROWSER_GLOBALS = [
    "window", "document", "console", "localStorage", "sessionStorage", "setTimeout",
    "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "requestIdleCallback", "cancelIdleCallback", "queueMicrotask", "reportError", "fetch", "URL",
    "URLSearchParams", "Blob", "File", "FileList", "FileReader", "FormData", "navigator",
    "location", "history", "alert", "prompt", "confirm", "EventSource", "WebSocket",
    "BroadcastChannel", "Image", "Audio", "MediaRecorder", "speechSynthesis",
    "SpeechSynthesisUtterance", "AudioContext", "webkitAudioContext", "MutationObserver",
    "IntersectionObserver", "ResizeObserver", "DataTransfer", "DragEvent", "Event",
    "CustomEvent", "KeyboardEvent", "MouseEvent", "PointerEvent", "TouchEvent",
    "WheelEvent", "ClipboardEvent", "getComputedStyle", "matchMedia", "atob", "btoa",
    "structuredClone", "crypto", "performance", "screen", "DOMParser", "Node", "NodeList",
    "HTMLElement", "Element", "Text", "AbortController", "AbortSignal", "TextDecoder",
    "TextEncoder", "caches", "self", "CSS", "Notification", "Response", "Request",
    "Headers", "getSelection", "scrollTo", "scrollBy", "postMessage", "indexedDB",
    # ECMAScript builtins
    "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "Proxy", "Reflect", "JSON",
    "Math", "Date", "RegExp", "Array", "Object", "String", "Number", "Boolean", "Error",
    "TypeError", "RangeError", "Intl", "BigInt", "Uint8Array", "Int32Array",
    "Float64Array", "ArrayBuffer", "DataView", "Function", "parseInt", "parseFloat",
    "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent", "encodeURI",
    "decodeURI", "globalThis",
    # CDN libraries loaded via <script> before our bundle
    "Prism", "mermaid", "katex", "hljs", "jsyaml", "Terminal", "FitAddon", "WebLinksAddon",
]

# Names ESLint can't statically prove are safe, verified NON-bugs. Each MUST be one of:
#   (a) exposed via `window.NAME = ...` (often inside an IIFE) and called bare, or
#   (b) an OPTIONAL helper ALWAYS called behind `typeof NAME === 'function'` (the
#       guard means a missing binding is a no-op, never a throw), or
#   (c) a closure variable from an enclosing function scope ESLint can't bind per-file.
# Keep this SHORT and justified. A bare (un-guarded) call to a nested-only function is
# a real bug and must NOT be added here — hoist the function to top level instead.
PROJECT_DYNAMIC_GLOBALS = {
    "placeLiveToolCardsHost": "typeof-guarded optional (ui/sessions/messages call sites)",
    "watchInflightSession": "typeof-guarded optional fallback (sessions.js)",
    "_applyMediaPlaybackPreferences": "typeof-guarded optional (ui.js / workspace.js)",
}


def _toplevel_symbols(src: str) -> set[str]:
    syms: set[str] = set()
    syms |= set(re.findall(r"^function\s+([A-Za-z_$][\w$]*)", src, re.M))
    syms |= set(re.findall(r"^async\s+function\s+([A-Za-z_$][\w$]*)", src, re.M))
    for m in re.finditer(r"^(?:const|let|var)\s+(.+)", src, re.M):
        decl = m.group(1)
        for name in re.findall(r"([A-Za-z_$][\w$]*)\s*[=;,]", decl):
            syms.add(name)
        lead = re.match(r"([A-Za-z_$][\w$]*)", decl)
        if lead:
            syms.add(lead.group(1))
    syms |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", src))
    return syms


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    static_dir = root / "static"
    eslint = which("eslint")
    if not eslint:
        print("⚠ eslint not found on PATH — scope_undef_gate SKIPPED (install eslint to enable).")
        return 0
    if not static_dir.is_dir():
        print(f"❌ static dir not found at {static_dir}")
        return 2

    files = [f for f in sorted(static_dir.glob("*.js")) if not f.name.endswith(".min.js")]
    project_syms: set[str] = set()
    for f in files:
        project_syms |= _toplevel_symbols(f.read_text(encoding="utf-8"))

    allow = project_syms | set(BROWSER_GLOBALS) | set(PROJECT_DYNAMIC_GLOBALS)
    globals_obj = "{" + ",".join(f'"{n}":"readonly"' for n in sorted(allow)) + "}"

    findings: list[tuple[str, int, str]] = []
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "scope.config.mjs"
        config_path.write_text(
            "export default [{files:[\"**/*.js\"],"
            "languageOptions:{ecmaVersion:\"latest\",sourceType:\"script\","
            f"globals:{globals_obj}}},"
            "rules:{\"no-undef\":\"error\"}}];",
            encoding="utf-8",
        )
        for f in files:
            proc = subprocess.run(
                [eslint, "--no-config-lookup", "-c", str(config_path), "-f", "json", str(f)],
                capture_output=True, text=True,
            )
            try:
                report = json.loads(proc.stdout)
            except json.JSONDecodeError:
                print(f"❌ eslint failed on {f.name}:\n" + (proc.stderr or proc.stdout)[:1500])
                return 2
            for file_report in report:
                for msg in file_report.get("messages", []):
                    if msg.get("ruleId") == "no-undef":
                        findings.append((f.name, msg.get("line", 0), msg.get("message", "")))

    if not findings:
        print(f"✅ scope_undef_gate: CLEAN ({len(files)} static files, "
              f"{len(project_syms)} project globals, no undefined references).")
        return 0

    print("❌ scope_undef_gate FAILED — undefined reference(s) that throw at runtime in the "
          "browser (brick class, see #3696):\n")
    for src_file, line, message in findings:
        print(f"   {src_file}:{line}: {message}")
    print(
        "\nA flagged name is a REAL bug when it is a function defined inside another\n"
        "function and called BARE from a sibling/top-level scope — hoist it to top\n"
        "level (the #3696 fix). It is only safe to add to PROJECT_DYNAMIC_GLOBALS in\n"
        "this script (with a one-line justification) if every call site is either a\n"
        "`window.NAME = ...` exposure or a `typeof NAME === 'function'` guard."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
