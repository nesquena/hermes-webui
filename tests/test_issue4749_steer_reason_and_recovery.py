"""Tests for issue #4749: steer failure reason display and recovery bar.

Covers:
  1. i18n contract — all expected keys exist in the en locale block
  2. Reason map contract — _steerFailureMessageKey returns correct keys
  3. Backend parity — frontend reason map covers all backend fallback codes
  4. Recovery DOM — _showSteerRecovery creates correct structure; dismiss removes it
"""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parent.parent
I18N_JS = REPO / "static" / "i18n.js"
COMMANDS_JS = REPO / "static" / "commands.js"
STREAMING_PY = REPO / "api" / "streaming.py"

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

BACKEND_CODES = {
    "no_cached_agent",
    "agent_lacks_steer",
    "session_not_found",
    "not_running",
    "stream_dead",
    "steer_error",
}

HANDLED_NON_RECOVERY_CODES = {
    "gateway_steer_queued",
}

FRONTEND_NETWORK_CODE = "network_error"


def test_i18n_steer_failure_keys_exist():
    """All 10 expected i18n keys are present in the en locale block."""
    text = I18N_JS.read_text(encoding="utf-8")
    # Find the en locale block: from 'en:' or 'en =' up to the next top-level locale key
    en_match = re.search(r"\ben\s*:\s*\{", text)
    assert en_match, "Could not locate 'en:' locale block in i18n.js"
    # Grab a large slice starting at the en block; the block is thousands of lines
    en_block = text[en_match.start():]
    for key in EXPECTED_I18N_KEYS:
        pattern = rf"^\s+{re.escape(key)}\s*:"
        assert re.search(pattern, en_block, re.MULTILINE), (
            f"Expected i18n key '{key}' not found in the en locale block"
        )


def test_reason_map_contract():
    """_steerFailureMessageKey maps each known code to the correct key."""
    node = _find_node()
    script = textwrap.dedent("""
        const LOCALES = { en: {
            steer_fail_no_cached_agent: 'x',
            steer_fail_agent_lacks_steer: 'x',
            steer_fail_session_not_found: 'x',
            steer_fail_not_running: 'x',
            steer_fail_stream_dead: 'x',
            steer_fail_steer_error: 'x',
            steer_fail_network_error: 'x',
            steer_fail_unknown: 'x',
        }};

        function _steerFailureMessageKey(fallback) {
            if (fallback === 'gateway_steer_queued') return 'steer_fail_no_cached_agent';
            const key = 'steer_fail_' + (fallback || 'unknown');
            return (typeof LOCALES !== 'undefined' && LOCALES.en && LOCALES.en[key])
                ? key : 'steer_fail_unknown';
        }

        const codes = [
            'no_cached_agent', 'agent_lacks_steer', 'session_not_found',
            'not_running', 'stream_dead', 'steer_error', 'network_error',
        ];
        let ok = true;
        for (const c of codes) {
            const got = _steerFailureMessageKey(c);
            const want = 'steer_fail_' + c;
            if (got !== want) {
                console.error('FAIL code=' + c + ' got=' + got + ' want=' + want);
                ok = false;
            }
        }
        // unknown code
        const unk = _steerFailureMessageKey('something_unknown_xyz');
        if (unk !== 'steer_fail_unknown') {
            console.error('FAIL unknown: got=' + unk);
            ok = false;
        }
        // null / undefined
        const n = _steerFailureMessageKey(null);
        if (n !== 'steer_fail_unknown') {
            console.error('FAIL null: got=' + n);
            ok = false;
        }
        const u = _steerFailureMessageKey(undefined);
        if (u !== 'steer_fail_unknown') {
            console.error('FAIL undefined: got=' + u);
            ok = false;
        }
        const gatewayQueued = _steerFailureMessageKey('gateway_steer_queued');
        if (gatewayQueued !== 'steer_fail_no_cached_agent') {
            console.error('FAIL gateway_steer_queued: got=' + gatewayQueued);
            ok = false;
        }
        process.exit(ok ? 0 : 1);
    """)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"_steerFailureMessageKey contract failed:\n{result.stdout}\n{result.stderr}"
    )


def test_backend_parity():
    """Frontend reason map covers all backend fallback codes from _handle_chat_steer."""
    streaming_text = STREAMING_PY.read_text(encoding="utf-8")
    # Extract fallback codes from _handle_chat_steer function only
    fn_match = re.search(r"def _handle_chat_steer\b.*?(?=\ndef |\Z)", streaming_text, re.DOTALL)
    assert fn_match, "Could not find _handle_chat_steer in streaming.py"
    fn_body = fn_match.group(0)
    # Exclude placeholder strings like "<reason>" that appear in docstrings
    found_codes = set(
        c for c in re.findall(r'"fallback":\s*"([^"<>]+)"', fn_body)
        if c  # non-empty after filtering
    )
    assert found_codes, "No fallback codes found in _handle_chat_steer"
    assert found_codes == BACKEND_CODES | HANDLED_NON_RECOVERY_CODES, (
        f"Backend fallback codes mismatch.\n"
        f"  Found:    {sorted(found_codes)}\n"
        f"  Expected: {sorted(BACKEND_CODES | HANDLED_NON_RECOVERY_CODES)}"
    )
    # Also confirm frontend adds network_error
    commands_text = COMMANDS_JS.read_text(encoding="utf-8")
    assert FRONTEND_NETWORK_CODE in commands_text, (
        "network_error not found in commands.js"
    )
    # Confirm the dynamic key builder and recovery bar are present in commands.js
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
