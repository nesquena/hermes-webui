"""Tests for vault prompts (browser_vault masked cards).

Mirrors tests/test_clarify_sse.py structure:
  - Static analysis of backend and frontend code
  - Unit tests for the pending queue lifecycle
  - SECURITY assertions: values never reach SSE payloads, polls, or logs
"""

import os
import threading
import time

import pytest

# ── Paths ────────────────────────────────────────────────────────────────────
_ROUTES = os.path.join(os.path.dirname(__file__), "..", "api", "routes.py")
_STREAMING = os.path.join(os.path.dirname(__file__), "..", "api", "streaming.py")
_MESSAGES = os.path.join(os.path.dirname(__file__), "..", "static", "messages.js")
_INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
_I18N = os.path.join(os.path.dirname(__file__), "..", "static", "i18n.js")

sys_path = os.path.join(os.path.dirname(__file__), "..")

import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)


def _read(path):
    with open(path) as f:
        return f.read()


def _fresh_vault_prompts():
    """Import workspace module state fresh per test (module globals mutate)."""
    import importlib
    import api.vault_prompts as vp
    importlib.reload(vp)
    return vp


# ══════════════════════════════════════════════════════════════════════════════
# 1. Static analysis
# ══════════════════════════════════════════════════════════════════════════════
class TestVaultStaticMarkers:
    def test_module_exists_with_core_functions(self):
        src = _read(os.path.join(sys_path, "api", "vault_prompts.py"))
        for marker in ("def submit_pending", "def respond_vault", "def clear_pending",
                       "def register_gateway_notify", "class _VaultEntry"):
            assert marker in src, f"api/vault_prompts.py missing: {marker}"

    def test_routes_registered(self):
        src = _read(_ROUTES)
        assert '"/api/vault/pending"' in src
        assert '"/api/vault/respond"' in src
        assert "_handle_vault_respond" in src

    def test_streaming_registers_unlock_callbacks(self):
        src = _read(_STREAMING)
        assert "set_unlock_prompt_callback" in src
        assert "set_save_login_prompt_callback" in src
        assert "set_code_prompt_callback" in src

    def test_frontend_card_present(self):
        src = _read(_MESSAGES)
        assert "function showVaultCard" in src
        assert "/api/vault/respond" in src
        assert 'type="password"' in _read(_INDEX) and "vaultCard" in _read(_INDEX)

    def test_i18n_keys_present(self):
        src = _read(_I18N)
        assert "vault_heading_unlock" in src
        assert "vault_hint_unlock" in src

    def test_vault_values_never_echoed_to_transcript(self):
        """The respond success path must not push the answer into messages."""
        src = _read(_MESSAGES)
        vault_idx = src.index("async function respondVault")
        end = src.index("function _vaultPollTick", vault_idx)
        vault_block = src[vault_idx:end]
        assert "S.messages.push" not in vault_block
        assert "_stashClarifyDraft" not in vault_block

    def test_backend_never_returns_values_to_browser(self):
        """The respond handler must only return ok/stale metadata."""
        src = _read(_ROUTES)
        start = src.index("def _handle_vault_respond")
        end = src.index("\nclass _ManualCompressionMemoryHandler", start)
        block = src[start:end]
        # The success reply is exactly {"ok": True}: no echo of the values.
        assert 'return j(handler, {"ok": True})' in block
        assert 'return j(handler, {"ok": True, ' not in block


# ══════════════════════════════════════════════════════════════════════════════
# 2. Unit tests — pending queue lifecycle
# ══════════════════════════════════════════════════════════════════════════════
class TestVaultQueueLifecycle:
    def test_submit_then_respond_unblocks_waiter(self):
        vp = _fresh_vault_prompts()
        notified = []
        vp.register_gateway_notify("s1", lambda data: notified.append(data))
        try:
            entry = vp.submit_pending("s1", {"kind": "unlock", "display_name": "1Password"})
            assert notified, "submit must notify the UI callback"
            assert notified[0].get("display_name") == "1Password"
            assert "password" not in notified[0], "no secrets in the card payload"

            result = {}

            def waiter():
                entry.event.wait(timeout=2)
                result["values"] = dict(entry.values or {})

            t = threading.Thread(target=waiter)
            t.start()
            assert vp.respond_vault("s1", entry.prompt_id, {"password": "hunter2"})
            t.join(timeout=3)
            assert result["values"].get("password") == "hunter2"
            assert vp.get_pending("s1") is None
            assert vp.pending_count("s1") == 0
        finally:
            vp.unregister_gateway_notify("s1")
            vp.clear_pending("s1")

    def test_respond_unknown_prompt_is_stale(self):
        vp = _fresh_vault_prompts()
        assert vp.respond_vault("none", "nope", {"password": "x"}) is False

    def test_clear_pending_resolves_take_down_sse(self):
        vp = _fresh_vault_prompts()
        q = vp.sse_subscribe("s2")
        try:
            entry = vp.submit_pending("s2", {"kind": "unlock"})
            assert vp.get_pending("s2") is not None
            assert vp.clear_pending("s2") == 1
            assert entry.event.is_set()
            assert vp.get_pending("s2") is None
            # Drain to the take-down notify (submit's event arrived first).
            last = None
            while True:
                try:
                    last = q.get_nowait()
                except Exception:
                    break
            assert last == {"pending": None, "pending_count": 0}
        finally:
            vp.sse_unsubscribe("s2", q)
            vp.clear_pending("s2")

    def test_queue_maps_two_prompts_fifo(self):
        vp = _fresh_vault_prompts()
        try:
            a = vp.submit_pending("s3", {"kind": "unlock", "display_name": "A"})
            b = vp.submit_pending("s3", {"kind": "code", "site": "B"})
            assert vp.get_pending("s3").get("display_name") == "A"
            assert vp.respond_vault("s3", a.prompt_id, {"password": "p"})
            head = vp.get_pending("s3")
            assert head.get("site") == "B"
            assert b.prompt_id != a.prompt_id
        finally:
            vp.clear_pending("s3")

    def test_card_payload_has_no_ttl_leak_of_key_material(self):
        """Pending card data carries metadata only."""
        vp = _fresh_vault_prompts()
        try:
            entry = vp.submit_pending("s4", {"kind": "save_login", "label": "Example", "origin": "https://example.com"})
            data = vp.get_pending("s4")
            assert data["prompt_id"] == entry.prompt_id
            assert "values" not in data
        finally:
            vp.clear_pending("s4")
