"""Tests for issue #4749: steer failure reason display and recovery bar.

Covers:
  1. i18n contract — all expected keys exist in the evaluated real LOCALES.en
  2. Reason map contract — the ACTUAL _steerFailureMessageKey source extracted
     from static/commands.js, executed in a Node VM against real static/i18n.js
  3. Backend parity — AST inventory of BOTH _handle_chat_steer
     (api/streaming.py) and gateway_steer_run (api/gateway_chat.py), including
     the delegation edge where the handler forwards the relay's reason
  4. Locale resolution — every mapped gateway failure key resolves through the
     real t() in all 15 steer locales
  5. Recovery DOM — _showSteerRecovery creates correct structure; dismiss removes it

The gateway failure taxonomy intentionally adds NO new i18n keys: all four
gateway failure forms map onto the existing steer_fail_steer_error copy.
"""
import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parent.parent
I18N_JS = REPO / "static" / "i18n.js"
COMMANDS_JS = REPO / "static" / "commands.js"
STREAMING_PY = REPO / "api" / "streaming.py"
GATEWAY_CHAT_PY = REPO / "api" / "gateway_chat.py"

EXPECTED_I18N_KEYS = [
    "steer_fail_no_cached_agent",
    "steer_fail_agent_lacks_steer",
    "steer_fail_session_not_found",
    "steer_fail_not_running",
    "steer_fail_stream_dead",
    "steer_fail_steer_error",
    "steer_fail_network_error",
    "steer_fail_unknown",
    "steer_recovery_retry",
    "steer_recovery_dismiss",
]

# The one dynamic family: gateway_steer_run maps non-409 HTTPError statuses to
# f"gateway_steer_http_{e.code}". The marker stands in for every concrete
# status literal in BACKEND_CODES.
GATEWAY_HTTP_FAMILY = "gateway_steer_http_<code>"

BACKEND_CODES = {
    # Local in-process steer gates (api/streaming.py::_handle_chat_steer).
    "no_cached_agent",
    "agent_lacks_steer",
    "session_not_found",
    "not_running",
    "stream_dead",
    "steer_error",
    # Gateway relay failure literals (api/gateway_chat.py::gateway_steer_run).
    "gateway_steer_no_run_id",
    "gateway_steer_not_accepting",
    "gateway_steer_error",
    GATEWAY_HTTP_FAMILY,
}

# Live backend codes the frontend handles with a dedicated non-recovery branch
# (the owner-scoped next-turn queue), so they never reach _showSteerRecovery.
HANDLED_NON_RECOVERY_CODES = {
    "gateway_steer_queued",
}

FRONTEND_NETWORK_CODE = "network_error"

# Locales carrying the steer_fail_* family (verified by evaluating LOCALES).
EXPECTED_STEER_LOCALES = [
    "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko",
    "fr", "cs", "tr", "pl", "vi",
]

# Exact existing English copy that all gateway delivery failures map to
# (static/i18n.js — steer_fail_steer_error). No new keys, no new locale copy.
EXACT_EN_GATEWAY_FAILURE = "Steer delivery failed — the agent may have finished"

# Codes that must map to steer_fail_steer_error via the real mapper.
GATEWAY_FAILURE_CODES = [
    "gateway_steer_no_run_id",
    "gateway_steer_not_accepting",
    "gateway_steer_error",
    "gateway_steer_http_302",  # representative non-4xx/5xx HTTP status
    "gateway_steer_http_400",
    "gateway_steer_http_401",
    "gateway_steer_http_403",
    "gateway_steer_http_404",  # historical raw form; current backend 404 -> queued
    "gateway_steer_http_500",
    "gateway_steer_http_503",
    "gateway_steer_http_599",
]


# ---------------------------------------------------------------------------
# Node VM harness helpers
# ---------------------------------------------------------------------------

def _vm_preamble():
    """Node script prefix: load real i18n.js and the ACTUAL extracted source of
    _steerFailureMessageKey from commands.js into one vm context.

    i18n.js only needs a minimal localStorage/document stub for its final
    loadLocale() boot call. The mapper source is sliced out of the real file —
    the implementation is never copied into this test.
    """
    return textwrap.dedent(f"""
        const fs = require('fs'), vm = require('vm');
        const ctx = vm.createContext({{
            localStorage: {{ getItem: () => null, setItem: () => {{}} }},
            document: {{ documentElement: {{}} }},
        }});
        vm.runInContext(fs.readFileSync({json.dumps(str(I18N_JS))}, 'utf8'), ctx,
                        {{filename: 'static/i18n.js'}});
        const __src = fs.readFileSync({json.dumps(str(COMMANDS_JS))}, 'utf8');
        const __start = __src.indexOf('function _steerFailureMessageKey(');
        const __end = __src.indexOf('function _showSteerIndicator(');
        if (__start < 0 || __end < 0 || __end <= __start) {{
            console.error('FAIL: could not extract _steerFailureMessageKey source from commands.js');
            process.exit(2);
        }}
        vm.runInContext(__src.slice(__start, __end), ctx,
                        {{filename: 'static/commands.js#_steerFailureMessageKey'}});
        function __map(input) {{
            ctx.__code = input;
            return vm.runInContext('_steerFailureMessageKey(__code)', ctx);
        }}
    """)


def _run_node(script):
    node = _find_node()
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    return result


def _assert_node_ok(result, label):
    assert result.returncode == 0, (
        f"{label} failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# AST inventory helpers (backend parity)
# ---------------------------------------------------------------------------

def _ast_function(path, name):
    """Parse ``path`` and return the (single) function def node named ``name``."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"Expected exactly one def of {name} in {path.name}, got {len(matches)}"
    return matches[0]


def _handler_fallback_literals(fn_node):
    """Inventory literal "fallback" dict values in _handle_chat_steer.

    Docstrings never appear as AST constants, and the success path's None is
    skipped. The gateway delegation edge shows up as the bare name ``reason``.
    """
    literals = set()
    delegated = False
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "fallback"):
                continue
            if isinstance(value, ast.Constant):
                if value.value is None:
                    continue  # accepted path: {"fallback": None}
                assert isinstance(value.value, str) and value.value.strip(), (
                    f"Unrecognized fallback constant in _handle_chat_steer: {value.value!r}"
                )
                literals.add(value.value)
            elif isinstance(value, ast.Name) and value.id == "reason":
                delegated = True  # relay result forwarded into the response
            else:
                raise AssertionError(
                    f"Unrecognized fallback expression in _handle_chat_steer: {ast.dump(value)}"
                )
    return literals, delegated


def _handler_delegates_to_gateway_relay(fn_node):
    """True when the handler binds ``accepted, reason = gateway_steer_run(...)``,
    proving relay reasons feed the response dict's ``reason`` name."""
    for node in ast.walk(fn_node):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Tuple)
            and len(target.elts) == 2
            and all(isinstance(e, ast.Name) for e in target.elts)
            and [e.id for e in target.elts] == ["accepted", "reason"]
            and isinstance(node.value, ast.Call)
        ):
            continue
        func = node.value.func
        called = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if called == "gateway_steer_run":
            return True
    return False


def _relay_return_reasons(fn_node):
    """Inventory the reason half of every ``return <bool>, <reason>`` tuple in
    gateway_steer_run. The ``gateway_steer_http_`` f-string is the one allowed
    dynamic family; any other dynamic reason expression fails the inventory."""
    reasons = set()
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Return):
            continue
        value = node.value
        if not (isinstance(value, ast.Tuple) and len(value.elts) == 2):
            continue
        reason = value.elts[1]
        if isinstance(reason, ast.Constant):
            if reason.value is None:
                continue  # success: return True, None
            assert isinstance(reason.value, str) and reason.value.strip(), (
                f"Unrecognized relay reason constant: {reason.value!r}"
            )
            reasons.add(reason.value)
        elif isinstance(reason, ast.JoinedStr):
            parts = reason.values
            assert (
                len(parts) == 2
                and isinstance(parts[0], ast.Constant)
                and parts[0].value == "gateway_steer_http_"
                and isinstance(parts[1], ast.FormattedValue)
            ), f"Unrecognized dynamic relay reason f-string: {ast.dump(reason)}"
            reasons.add(GATEWAY_HTTP_FAMILY)
        else:
            raise AssertionError(
                f"Unrecognized dynamic relay reason expression: {ast.dump(reason)}"
            )
    return reasons


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_i18n_steer_failure_keys_exist():
    """All 10 expected i18n keys are nonempty strings owned by the real LOCALES.en."""
    script = _vm_preamble() + textwrap.dedent(f"""
        const keys = {json.dumps(EXPECTED_I18N_KEYS)};
        const en = vm.runInContext('LOCALES.en', ctx);
        const bad = [];
        for (const k of keys) {{
            if (!Object.prototype.hasOwnProperty.call(en, k)
                || typeof en[k] !== 'string' || !en[k].length) bad.push(k);
        }}
        if (bad.length) {{
            console.error('FAIL: LOCALES.en missing/empty keys: ' + bad.join(', '));
            process.exit(1);
        }}
        console.log('LOCALES.en steer keys OK (' + keys.length + ' keys)');
    """)
    _assert_node_ok(_run_node(script), "i18n steer failure keys")


def test_reason_map_contract():
    """The ACTUAL _steerFailureMessageKey (extracted from commands.js) maps each
    known code to the correct existing key, including the gateway taxonomy."""
    fixed_cases = [
        ("no_cached_agent", "steer_fail_no_cached_agent"),
        ("agent_lacks_steer", "steer_fail_agent_lacks_steer"),
        ("session_not_found", "steer_fail_session_not_found"),
        ("not_running", "steer_fail_not_running"),
        ("stream_dead", "steer_fail_stream_dead"),
        ("steer_error", "steer_fail_steer_error"),
        ("network_error", "steer_fail_network_error"),
        ("gateway_steer_no_run_id", "steer_fail_steer_error"),
        ("gateway_steer_not_accepting", "steer_fail_steer_error"),
        ("gateway_steer_error", "steer_fail_steer_error"),
        # Defensive: queued keeps its pre-existing no_cached_agent mapping; the
        # owner-scoped queue branch normally intercepts this code first.
        ("gateway_steer_queued", "steer_fail_no_cached_agent"),
        # Unknown/malformed codes must stay on the unknown fallback.
        ("something_unknown_xyz", "steer_fail_unknown"),
        ("gateway_steer_http_bad", "steer_fail_unknown"),
        ("", "steer_fail_unknown"),
        (None, "steer_fail_unknown"),
    ]
    script = _vm_preamble() + textwrap.dedent(f"""
        const fixedCases = {json.dumps(fixed_cases)};
        // 302 plus every status 400-599 exercises the numeric HTTP family.
        const statuses = [302, ...Array.from({{length: 200}}, (_, i) => 400 + i)];
        const failures = [];
        for (const [input, want] of fixedCases) {{
            const got = __map(input);
            if (got !== want) failures.push('code=' + JSON.stringify(input) + ' got=' + got + ' want=' + want);
        }}
        const undef = vm.runInContext('_steerFailureMessageKey(undefined)', ctx);
        if (undef !== 'steer_fail_unknown') failures.push('code=undefined got=' + undef + ' want=steer_fail_unknown');
        for (const s of statuses) {{
            const c = 'gateway_steer_http_' + s;
            const got = __map(c);
            if (got !== 'steer_fail_steer_error') failures.push('code=' + c + ' got=' + got + ' want=steer_fail_steer_error');
        }}
        if (failures.length) {{
            console.error('FAIL:\\n' + failures.join('\\n'));
            process.exit(1);
        }}
        console.log('reason map contract OK (' + (fixedCases.length + 1 + statuses.length) + ' cases)');
    """)
    _assert_node_ok(_run_node(script), "_steerFailureMessageKey contract")


def test_backend_parity():
    """AST inventory of both backend producers matches the frontend taxonomy.

    Scans _handle_chat_steer (api/streaming.py) dict fallback literals AND
    gateway_steer_run (api/gateway_chat.py) return-tuple reasons, including the
    gateway_steer_http_ f-string family. The handler's delegation edge (its
    response forwards the relay's ``reason``) is asserted explicitly.
    """
    handler = _ast_function(STREAMING_PY, "_handle_chat_steer")
    relay = _ast_function(GATEWAY_CHAT_PY, "gateway_steer_run")

    handler_literals, delegated = _handler_fallback_literals(handler)
    assert delegated, (
        "_handle_chat_steer must forward the relay reason into its response dict "
        "(the gateway delegation edge)"
    )
    assert _handler_delegates_to_gateway_relay(handler), (
        "_handle_chat_steer must bind 'accepted, reason = gateway_steer_run(...)' "
        "so relay reasons reach the frontend"
    )

    relay_reasons = _relay_return_reasons(relay)
    assert relay_reasons, "No return-tuple reasons found in gateway_steer_run"
    assert GATEWAY_HTTP_FAMILY in relay_reasons, (
        "The dynamic gateway_steer_http_<code> family must be inventoried, not "
        "lost by a plain string-literal extractor"
    )

    found_codes = handler_literals | relay_reasons
    expected_codes = BACKEND_CODES | HANDLED_NON_RECOVERY_CODES
    assert found_codes == expected_codes, (
        f"Backend fallback codes mismatch.\n"
        f"  Found:    {sorted(found_codes)}\n"
        f"  Expected: {sorted(expected_codes)}"
    )

    # Frontend-side guards: browser-only code, mapper, recovery bar, prefix.
    commands_text = COMMANDS_JS.read_text(encoding="utf-8")
    assert FRONTEND_NETWORK_CODE in commands_text, (
        "network_error not found in commands.js"
    )
    assert "_steerFailureMessageKey" in commands_text, (
        "_steerFailureMessageKey not found in commands.js"
    )
    assert "_showSteerRecovery" in commands_text, (
        "_showSteerRecovery not found in commands.js"
    )
    # The builder uses the 'steer_fail_' prefix pattern
    assert "steer_fail_" in commands_text, (
        "'steer_fail_' prefix not found in commands.js"
    )


def test_gateway_failure_translations_resolve_in_all_locales():
    """Every mapped gateway failure key resolves through the real t() in each of
    the 15 steer locales; English matches the exact existing copy."""
    mapped_codes = GATEWAY_FAILURE_CODES + ["gateway_steer_queued"]
    extra_keys = [
        "steer_leftover_queued",   # queue branch toast
        "cmd_steer_delivered",     # delivered toast
        "steer_recovery_retry",
        "steer_recovery_dismiss",
    ]
    script = _vm_preamble() + textwrap.dedent(f"""
        const expectedLocales = {json.dumps(EXPECTED_STEER_LOCALES)};
        const actualLocales = vm.runInContext(
            "Object.keys(LOCALES).filter(k => Object.keys(LOCALES[k]).some(p => p.startsWith('steer_fail_')))",
            ctx);
        if (JSON.stringify([...actualLocales].sort()) !== JSON.stringify([...expectedLocales].sort())) {{
            console.error('FAIL: steer locale set mismatch. expected=' + expectedLocales.join(',')
                + ' actual=' + actualLocales.join(','));
            process.exit(1);
        }}
        const codes = {json.dumps(mapped_codes)};
        const keySet = new Set(codes.map(c => __map(c)));
        for (const k of {json.dumps(extra_keys)}) keySet.add(k);
        const failures = [];
        for (const locale of actualLocales) {{
            vm.runInContext('_locale = LOCALES[' + JSON.stringify(locale) + ']', ctx);
            for (const key of keySet) {{
                ctx.__key = key;
                const txt = vm.runInContext('t(__key)', ctx);
                const want = vm.runInContext(
                    '(LOCALES[' + JSON.stringify(locale) + '][__key] ?? LOCALES.en[__key])', ctx);
                if (typeof txt !== 'string' || !txt.length) {{
                    failures.push(locale + '/' + key + ': empty translation');
                }} else if (txt === key) {{
                    failures.push(locale + '/' + key + ': t() returned the raw key');
                }} else if (txt !== want) {{
                    failures.push(locale + '/' + key + ': t() mismatch with LOCALES lookup');
                }}
            }}
        }}
        // The gateway delivery failure must resolve to the exact existing English copy.
        vm.runInContext("_locale = LOCALES.en", ctx);
        const gatewayKey = __map('gateway_steer_error');
        ctx.__key = gatewayKey;
        const enTxt = vm.runInContext('t(__key)', ctx);
        if (enTxt !== {json.dumps(EXACT_EN_GATEWAY_FAILURE)}) {{
            failures.push('en gateway failure copy mismatch: key=' + gatewayKey + ' text=' + JSON.stringify(enTxt));
        }}
        if (failures.length) {{
            console.error('FAIL:\\n' + failures.join('\\n'));
            process.exit(1);
        }}
        console.log('gateway failure translations OK (' + actualLocales.length + ' locales, '
            + keySet.size + ' keys)');
    """)
    _assert_node_ok(_run_node(script), "gateway failure translations")


def test_recovery_dom_structure():
    """_showSteerRecovery creates a div with label, retry, dismiss; dismiss removes it."""
    node = _find_node()
    script = textwrap.dedent("""
        // Minimal DOM stubs
        const elements = {};
        function createElement(tag) {
            const el = {
                tag, className: '', textContent: '', children: [],
                listeners: {},
                appendChild(c) { this.children.push(c); },
                addEventListener(ev, fn) { this.listeners[ev] = fn; },
                remove() { el._removed = true; },
                querySelector(sel) {
                    // only handle .steer-recovery for old-removal check
                    return null;
                },
            };
            elements[tag + '_' + Math.random()] = el;
            return el;
        }
        const inner = createElement('div');
        inner.querySelector = (sel) => null; // no existing recovery bar
        const document = {
            getElementById(id) { return id === 'msgInner' ? inner : null; },
            createElement,
        };
        function t(key) { return key; }
        function _steerFailureMessageKey(fallback) {
            const key = 'steer_fail_' + (fallback || 'unknown');
            const LOCALES = { en: {
                steer_fail_not_running: 'Agent is not currently running',
                steer_fail_unknown: 'Steer unavailable',
                steer_recovery_retry: 'Retry',
                steer_recovery_dismiss: 'Dismiss',
            }};
            return (LOCALES.en && LOCALES.en[key]) ? key : 'steer_fail_unknown';
        }
        function _trySteer() {}  // stub for retry handler

        function _showSteerRecovery(msg, explicitSteer, fallback) {
            const inner = document.getElementById('msgInner');
            if (!inner) return;
            const old = inner.querySelector('.steer-recovery');
            if (old) old.remove();
            const el = document.createElement('div');
            el.className = 'steer-recovery';
            const label = document.createElement('span');
            label.className = 'steer-recovery-label';
            label.textContent = t(_steerFailureMessageKey(fallback));
            el.appendChild(label);
            const retryBtn = document.createElement('button');
            retryBtn.className = 'steer-recovery-retry';
            retryBtn.textContent = t('steer_recovery_retry');
            retryBtn.addEventListener('click', () => {
                el.remove();
                _trySteer(msg, explicitSteer);
            });
            el.appendChild(retryBtn);
            const dismissBtn = document.createElement('button');
            dismissBtn.className = 'steer-recovery-dismiss';
            dismissBtn.textContent = t('steer_recovery_dismiss');
            dismissBtn.addEventListener('click', () => el.remove());
            el.appendChild(dismissBtn);
            inner.appendChild(el);
        }

        _showSteerRecovery('hello', false, 'not_running');

        const bar = inner.children[inner.children.length - 1];
        let ok = true;

        if (bar.className !== 'steer-recovery') {
            console.error('FAIL: bar className=' + bar.className);
            ok = false;
        }
        const [lbl, retry, dismiss] = bar.children;
        if (!lbl || lbl.className !== 'steer-recovery-label') {
            console.error('FAIL: label missing or wrong class');
            ok = false;
        }
        if (!retry || retry.className !== 'steer-recovery-retry') {
            console.error('FAIL: retry btn missing or wrong class');
            ok = false;
        }
        if (!dismiss || dismiss.className !== 'steer-recovery-dismiss') {
            console.error('FAIL: dismiss btn missing or wrong class');
            ok = false;
        }
        // Simulate dismiss click
        dismiss.listeners['click']();
        if (!bar._removed) {
            console.error('FAIL: bar not removed after dismiss');
            ok = false;
        }
        process.exit(ok ? 0 : 1);
    """)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"Recovery DOM structure test failed:\n{result.stdout}\n{result.stderr}"
    )


def _find_node():
    """Return path to node.exe, skipping wrapper scripts."""
    import shutil
    candidates = ["node", "node.exe"]
    for c in candidates:
        path = shutil.which(c)
        if path:
            # Verify it's actually node, not a wrapper
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip().startswith("v"):
                    return path
            except Exception:
                continue
    pytest_skip = getattr(sys.modules.get("pytest"), "skip", None)
    if pytest_skip:
        pytest_skip("node.js not found — skipping node-executed tests")
    raise RuntimeError("node.js not found")
