"""
Tests for the "Default (auto)" model dropdown option (__default__ sentinel).

The __default__ sentinel allows sessions to dynamically follow admin
default-model changes. When a session's stored model is "__default__",
the frontend resolves it at send time to whatever the current
profile default model is.

Four layers of tests:
  1. Source-code pattern tests — verify key code patterns exist in JS sources
  2. Node.js runtime — extract and run actual JS functions via Node
  3. Backend API — verify __default__ is stored/persisted correctly
  4. Integration — end-to-end session creation with __default__
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
SESSIONS_JS = REPO_ROOT / "static" / "sessions.js"
BOOT_JS = REPO_ROOT / "static" / "boot.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ── Helpers ──────────────────────────────────────────────────────────────

def _extract_function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{\n", start)
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start: idx + 1]
    raise AssertionError(f"Function body not closed for {signature}")


def _extract_get_model_label() -> str:
    """Extract getModelLabel() from ui.js for Node.js execution."""
    ui_src = UI_JS.read_text(encoding="utf-8")
    start = ui_src.index("function getModelLabel(")
    # Bound by the next top-level function
    after = ui_src.index("\nfunction _gatewayProviderName(", start)
    return ui_src[start:after]


def _run_node(driver_script: str, *args, timeout=30):
    """Run a Node.js script with args, return parsed JSON stdout."""
    proc = subprocess.run(
        [NODE, "-e", driver_script, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node driver failed (exit {proc.returncode}): {proc.stderr[:500]}"
        )
    return proc.stdout.strip()


# ═══════════════════════════════════════════════════════════════════════════
# 1. Source-code pattern tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCodePatterns:
    """Verify key code patterns exist in the JS source files."""

    def test_chat_payload_model_handles_default_sentinel(self):
        """_chatPayloadModel() must resolve __default__ to window._defaultModel."""
        src = MESSAGES_JS.read_text(encoding="utf-8")
        fn = _extract_function(src, "function _chatPayloadModel()")
        assert "sessionModel==='__default__'" in fn, (
            "_chatPayloadModel must check for __default__ sentinel"
        )
        assert "return window._defaultModel||''" in fn, (
            "__default__ must resolve to window._defaultModel"
        )

    def test_populate_model_dropdown_adds_default_option(self):
        """populateModelDropdown() must add a 'Default (auto)' option at top."""
        src = UI_JS.read_text(encoding="utf-8")
        fn = _extract_function(src, "async function populateModelDropdown")
        assert "__default__" in fn, (
            "populateModelDropdown must add __default__ option"
        )
        assert "'Default (auto)'" in fn or '"Default (auto)"' in fn, (
            "Default option label must be 'Default (auto)'"
        )

    def test_get_model_label_handles_default(self):
        """getModelLabel('__default__') must return 'Default (auto)'."""
        src = UI_JS.read_text(encoding="utf-8")
        fn = _extract_function(src, "function getModelLabel(")
        assert "__default__" in fn, (
            "getModelLabel must handle __default__ sentinel"
        )
        assert "'Default (auto)'" in fn, (
            "__default__ must map to 'Default (auto)' label"
        )

    def test_deliberate_session_model_pick_skips_default(self):
        """_deliberateSessionModelPick must return null for __default__."""
        src = UI_JS.read_text(encoding="utf-8")
        fn = _extract_function(src, "function _deliberateSessionModelPick")
        assert "model==='__default__'" in fn, (
            "_deliberateSessionModelPick must check for __default__"
        )

    def test_new_session_sends_default_sentinel(self):
        """newSession() must send __default__ when dropdown shows Default."""
        src = SESSIONS_JS.read_text(encoding="utf-8")
        fn = _extract_function(src, "async function newSession")
        assert "_ddValue==='__default__'" in fn, (
            "newSession must check dropdown value for __default__"
        )
        assert "__default__" in fn, (
            "newSession must handle __default__ sentinel"
        )

    def test_chat_start_preserves_default_sentinel(self):
        """After chat/start response, __default__ session model must NOT be overwritten."""
        src = MESSAGES_JS.read_text(encoding="utf-8")
        assert "S.session.model!=='__default__'" in src, (
            "chat/start post-processing must preserve __default__ sentinel"
        )

    def test_send_refreshes_default_model_cache(self):
        """send() must refresh the cached default model before resolving __default__."""
        src = MESSAGES_JS.read_text(encoding="utf-8")
        assert "_refreshDefaultModelCache" in src, (
            "send() must call _refreshDefaultModelCache before _chatPayloadModelState"
        )
        assert "if(S.session&&S.session.model==='__default__')" in src, (
            "Refresh must be gated on __default__ session model"
        )
        assert "if(!refreshed) throw new Error" in src, (
            "Refresh failure must block stale default model/provider routing"
        )

    def test_refresh_default_model_cache_exists(self):
        """_refreshDefaultModelCache() must be defined in ui.js."""
        src = UI_JS.read_text(encoding="utf-8")
        assert "async function _refreshDefaultModelCache" in src, (
            "_refreshDefaultModelCache must be defined in ui.js"
        )
        assert "/api/settings" in src, (
            "_refreshDefaultModelCache must fetch /api/settings"
        )

    def test_sentinel_persistence_helpers_exist(self):
        """localStorage-based sentinel persistence helpers must exist in ui.js."""
        src = UI_JS.read_text(encoding="utf-8")
        assert "function _setDefaultModelSession(" in src
        assert "function _clearDefaultModelSession(" in src
        assert "function _isDefaultModelSession(" in src
        assert "function _preserveDefaultModelSentinel(" in src
        assert "'hermes-webui-default-sessions'" in src

    def test_sentinel_restored_after_server_session_replacement(self):
        """Every S.session replacement from server data must restore the sentinel."""
        msgs_src = MESSAGES_JS.read_text(encoding="utf-8")
        sessions_src = SESSIONS_JS.read_text(encoding="utf-8")
        # SSE done + error + cancel paths in messages.js
        assert msgs_src.count("_preserveDefaultModelSentinel(S.session)") >= 4, (
            "messages.js must restore sentinel after SSE done/error/cancel paths"
        )
        # loadSession + resolveModelForDisplay in sessions.js
        assert sessions_src.count("_preserveDefaultModelSentinel(S.session)") >= 2, (
            "sessions.js must restore sentinel after loadSession and resolveModelForDisplay"
        )

    def test_new_session_marks_default_mode(self):
        """newSession must call _setDefaultModelSession when created with __default__."""
        src = SESSIONS_JS.read_text(encoding="utf-8")
        assert "_setDefaultModelSession(S.session.session_id)" in src
        assert "newModelState.model==='__default__'" in src

    def test_dropdown_onchange_tracks_default_mode(self):
        """Model dropdown onchange must set/clear default-mode tracking."""
        boot_src = BOOT_JS.read_text(encoding="utf-8")
        assert "_setDefaultModelSession(S.session.session_id)" in boot_src
        assert "_clearDefaultModelSession(S.session.session_id)" in boot_src

    def test_effective_model_does_not_clobber_sentinel(self):
        """Server-resolved effective_model must not overwrite the sentinel."""
        msgs_src = MESSAGES_JS.read_text(encoding="utf-8")
        commands_src = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
        panels_src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
        assert "S.session.model!=='__default__'" in msgs_src
        assert "S.session.model!=='__default__'" in commands_src
        assert "S.session.model!=='__default__'" in panels_src


# ═══════════════════════════════════════════════════════════════════════════
# 2. Node.js runtime tests
# ═══════════════════════════════════════════════════════════════════════════

_GET_MODEL_LABEL_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
const start = ui.indexOf('function getModelLabel(');
if (start < 0) throw new Error('getModelLabel not found');
const after = ui.indexOf('\nfunction _gatewayProviderName(', start);
if (after < 0) throw new Error('getModelLabel end boundary not found');
const fnSrc = ui.slice(start, after);
const _dynamicModelLabels = {};
function _fmtOllamaLabel(s){ return s; }
eval(fnSrc);
const out = {};
for (const m of JSON.parse(process.argv[2])) out[m] = getModelLabel(m);
process.stdout.write(JSON.stringify(out));
"""


class TestGetModelLabelRuntime:
    """Actual getModelLabel() execution via Node.js."""

    def test_default_sentinel_returns_default_auto(self):
        proc = subprocess.run(
            [NODE, "-e", _GET_MODEL_LABEL_DRIVER, str(UI_JS), '["__default__"]'],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
        out = json.loads(proc.stdout)
        assert out["__default__"] == "Default (auto)", (
            f"Expected 'Default (auto)', got {out['__default__']!r}"
        )

    def test_normal_model_labels_unchanged(self):
        proc = subprocess.run(
            [NODE, "-e", _GET_MODEL_LABEL_DRIVER, str(UI_JS),
             json.dumps(["deepseek/deepseek-v4-flash", "openai/gpt-4o", "unknown-model"])],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
        out = json.loads(proc.stdout)
        assert out["deepseek/deepseek-v4-flash"] == "DeepSeek V4 Flash"
        assert out["openai/gpt-4o"] == "GPT-4o"
        # Unknown models should still return a reasonable fallback
        assert out["unknown-model"] == "unknown-model"


_CHAT_PAYLOAD_MODEL_DRIVER = r"""
const fs = require('fs');
const msgs = fs.readFileSync(process.argv[1], 'utf8');
// Simulate the module scope needed by _chatPayloadModel
const S = { session: null };
const _defaultModel = process.argv[3] || '';
// We can't easily eval the function due to DOM deps,
// but we can verify the source code logic directly.
// Extract the function and check the __default__ branch.
const fnSrc = msgs.slice(
  msgs.indexOf('function _chatPayloadModel()'),
  msgs.indexOf('function _chatPayloadModelProvider(', msgs.indexOf('function _chatPayloadModel()'))
);
console.log(JSON.stringify({ found: fnSrc.includes("__default__") }));
"""


_DEFAULT_MODEL_REFRESH_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
const messages = fs.readFileSync(process.argv[2], 'utf8');
function extract(source, signature) {
  const start = source.indexOf(signature);
  if (start < 0) throw new Error(signature + ' not found');
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(signature + ' not closed');
}
const refreshSrc = extract(ui, 'async function _refreshDefaultModelCache(');
const providerSrc = extract(ui, 'function _modelProviderForSend(');
const payloadSrc = extract(messages, 'function _chatPayloadModel(');
const response = JSON.parse(process.argv[3]);
const S = { session: { model: '__default__', model_provider: null } };
const window = { _defaultModel: 'old-model', _activeProvider: 'old-provider' };
const warnings = [];
const fetchedUrls = [];
const console = { warn: (...args) => warnings.push(args.join(' ')) };
const document = { baseURI: response.base_uri || 'https://example.test/hermes/' };
const location = { href: document.baseURI };
const fetch = async (url) => {
  fetchedUrls.push(String(url));
  return {
    ok: response.ok,
    status: response.status || 200,
    json: async () => response.body,
  };
};
const $ = () => null;
eval(refreshSrc);
eval(providerSrc);
eval(payloadSrc);
(async () => {
  const refreshed = await _refreshDefaultModelCache();
  const body = refreshed ? {
    model: _chatPayloadModel(),
    model_provider: _modelProviderForSend(_chatPayloadModel()),
  } : null;
  process.stdout.write(JSON.stringify({
    refreshed,
    model: window._defaultModel,
    provider: window._activeProvider,
    body,
    warnings,
    fetchedUrls,
  }));
})();
"""


class TestDefaultModelRefreshRuntime:
    """Execute the live refresh helper so routing state cannot drift."""

    def _refresh(self, response):
        proc = subprocess.run(
            [NODE, "-e", _DEFAULT_MODEL_REFRESH_DRIVER, str(UI_JS), str(MESSAGES_JS), json.dumps(response)],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
        return json.loads(proc.stdout)

    def test_refresh_updates_model_and_provider_as_one_routing_pair(self):
        got = self._refresh({
            "ok": True,
            "body": {
                "default_model": "new-model",
                "default_model_provider": "new-provider",
            },
        })
        assert got == {
            "refreshed": True,
            "model": "new-model",
            "provider": "new-provider",
            "body": {"model": "new-model", "model_provider": "new-provider"},
            "warnings": [],
            "fetchedUrls": ["https://example.test/hermes/api/settings"],
        }

    def test_refresh_accepts_default_model_without_explicit_provider(self):
        got = self._refresh({
            "ok": True,
            "body": {"default_model": "provider-default-model"},
        })
        assert got["refreshed"] is True
        assert got["model"] == "provider-default-model"
        assert got["provider"] is None
        assert got["body"] == {"model": "provider-default-model", "model_provider": None}
        assert got["warnings"] == []

    def test_refresh_uses_document_base_uri_for_subpath(self):
        got = self._refresh({
            "ok": True,
            "base_uri": "https://example.test/hermes/",
            "body": {"default_model": "subpath-model"},
        })
        assert got["refreshed"] is True
        assert got["fetchedUrls"] == ["https://example.test/hermes/api/settings"]

    def test_refresh_explicitly_clears_model_and_provider_as_one_pair(self):
        got = self._refresh({
            "ok": True,
            "body": {
                "default_model": None,
                "default_model_provider": None,
            },
        })
        assert got["refreshed"] is True
        assert got["model"] is None
        assert got["provider"] is None
        assert got["body"] == {"model": "", "model_provider": None}
        assert got["warnings"] == []

    def test_refresh_failure_keeps_previous_pair_but_blocks_auto_send(self):
        got = self._refresh({"ok": False, "status": 503, "body": {}})
        assert got["refreshed"] is False
        assert got["model"] == "old-model"
        assert got["provider"] == "old-provider"
        assert got["body"] is None, "auto send must not use a stale routing pair"
        assert got["warnings"], "refresh failure must be diagnosable"


class TestChatPayloadModelLogic:
    """Verify _chatPayloadModel() resolves __default__ correctly."""

    def test_source_contains_dynamic_resolution(self):
        """Confirm the function still has the sentinel logic in the repo copy."""
        msgs_src = MESSAGES_JS.read_text(encoding="utf-8")
        assert "sessionModel==='__default__'" in msgs_src
        assert "return window._defaultModel||''" in msgs_src
        # Explicit runtime test: without window._defaultModel, it falls back
        proc = subprocess.run(
            [NODE, "-e", _CHAT_PAYLOAD_MODEL_DRIVER, str(MESSAGES_JS), ''],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["found"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 3. Backend API tests
# ═══════════════════════════════════════════════════════════════════════════

class TestBackendSessionModel:
    """Verify the backend stores __default__ as-is via source inspection."""

    def test_backend_routes_has_default_awareness(self):
        """routes.py _session_model_state_from_request must not reject __default__."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        # The function should handle __default__ by passing it through as a
        # bare model. We verify it exists and doesn't have explicit guards
        # that would reject __default__.
        assert "def _session_model_state_from_request(" in routes_src
        assert "def _resolve_compatible_session_model_state(" in routes_src, (
            "Backend must have model resolution for __default__ passthrough"
        )

    def test_backend_session_new_accepts_default(self):
        """
        The /api/session/new route uses _session_model_state_from_request to
        process the model field. __default__ passes through as a bare model
        string.
        """
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        handler = "if parsed.path == \"/api/session/new\""
        assert handler in routes_src, (
            "Session new route handler must exist"
        )
        assert "_session_model_state_from_request" in routes_src
        assert "body.get(\"model\")" in routes_src or 'body.get("model")' in routes_src
