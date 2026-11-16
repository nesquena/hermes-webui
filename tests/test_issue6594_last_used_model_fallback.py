import json
import queue
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest
import api.oauth
import api.streaming as streaming
from api.models import Session
from api.routes import _SIDEBAR_SESSION_RESPONSE_FIELDS, _sidebar_session_response_item

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = ROOT / "static" / "sessions.js"
UI_JS = ROOT / "static" / "ui.js"
BOOT_JS = ROOT / "static" / "boot.js"
NODE = shutil.which("node")


def _extract_block(source: str, prefix: str) -> str:
    start = source.find(prefix)
    assert start != -1, f"Could not find {prefix}"
    brace = source.find("{", start)
    assert brace != -1, f"Could not find opening brace for {prefix}"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    pytest.fail(f"Could not extract complete block for {prefix}")


def _extract_function(source: str, name: str) -> str:
    for prefix in (f"async function {name}(", f"function {name}("):
        if prefix in source:
            return _extract_block(source, prefix)
    pytest.fail(f"Could not find function {name}")


def _run_node(script: str) -> str:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run(
            [NODE, script_path],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Node execution failed (code {proc.returncode}):\n{proc.stderr}")
        return proc.stdout.strip()
    finally:
        Path(script_path).unlink(missing_ok=True)


def _sidebar_harness(eval_code: str) -> str:
    sessions_source = SESSIONS_JS.read_text(encoding="utf-8")
    ui_source = UI_JS.read_text(encoding="utf-8")

    fn_gateway_routing_label = _extract_function(ui_source, "_gatewayRoutingLabel")
    fn_format_gateway_model_label = _extract_function(ui_source, "_formatGatewayModelLabel")
    fn_latest_gateway_routing = _extract_function(ui_source, "_latestGatewayRoutingForSession")
    fn_format_session_model_with_gateway = _extract_function(sessions_source, "_formatSessionModelWithGateway")

    header = """
function _gatewayProviderName(p) { return p ? String(p) : ''; }
function _compactComposerModelChipLabel(id, label) { return label || id; }
function getModelLabel(id) { return id ? ('Model(' + id + ')') : ''; }
"""
    full_script = f"{header}\n{fn_gateway_routing_label}\n{fn_format_gateway_model_label}\n{fn_latest_gateway_routing}\n{fn_format_session_model_with_gateway}\n{eval_code}"
    return _run_node(full_script)


def _production_event_harness(eval_code: str) -> str:
    ui_source = UI_JS.read_text(encoding="utf-8")
    boot_source = BOOT_JS.read_text(encoding="utf-8")
    sessions_source = SESSIONS_JS.read_text(encoding="utf-8")

    fn_provider_from_model_value = _extract_function(ui_source, "_providerFromModelValue")
    fn_gateway_routing_label = _extract_function(ui_source, "_gatewayRoutingLabel")
    fn_format_gateway_model_label = _extract_function(ui_source, "_formatGatewayModelLabel")
    fn_latest_gateway_routing = _extract_function(ui_source, "_latestGatewayRoutingForSession")
    fn_format_session_model_with_gateway = _extract_function(sessions_source, "_formatSessionModelWithGateway")
    fn_sync_model_chip = _extract_function(ui_source, "syncModelChip")
    fn_select_model = _extract_function(ui_source, "selectModelFromDropdown")
    fn_apply_ctx = _extract_function(boot_source, "_applySessionContextMetadataUpdate")
    # The explicit-pick marker gates the composer route guard: wire the real
    # helpers (and a sessionStorage shim) so onchange writes a real marker.
    fn_explicit_pick = "\n".join(
        _extract_function(ui_source, name)
        for name in (
            "_pickerExplicitPickKey",
            "_rememberExplicitPickerPick",
            "_readExplicitPickerPick",
            "_clearExplicitPickerPick",
        )
    )
    onchange_block = _extract_block(boot_source, "$('modelSelect').onchange=")

    harness = f"""
const elements = {{
  modelSelect: {{ id: 'modelSelect', value: 'claude-3-5-sonnet', onchange: null }},
  composerModelChip: {{ title: '', classList: {{ toggle: () => {{}}, contains: () => false }} }},
  composerModelLabel: {{ textContent: '' }},
  composerMobileModelLabel: {{ textContent: '' }},
  composerMobileModelAction: {{ classList: {{ toggle: () => {{}}, contains: () => false }} }},
  composerModelDropdown: {{ classList: {{ toggle: () => {{}}, contains: () => false }} }}
}};

function $(id) {{ return elements[id] || null; }}

const S = {{
  _bootReady: true,
  session: {{
    session_id: 'test-session-1',
    workspace: 'default',
    model: 'claude-3-5-sonnet',
    model_provider: null,
    last_used_model: 'claude-3-haiku',
    gateway_routing: null,
    gateway_routing_history: []
  }}
}};

function _gatewayProviderName(p) {{ return p ? String(p) : ''; }}
function _compactComposerModelChipLabel(id, label) {{ return label || id; }}
function _selectedModelOption() {{ return null; }}
let _selectModelProviders = {{
  '@custom:backup:gpt-4o': {{ model: '@custom:backup:gpt-4o', model_provider: 'custom:backup' }},
  '@openrouter:gpt-5.5': {{ model: '@openrouter:gpt-5.5', model_provider: 'openrouter' }}
}};
function _modelStateForSelect(sel, v) {{
  if (v && _selectModelProviders[v]) {{
    return _selectModelProviders[v];
  }}
  if (typeof v === 'string' && v.startsWith('@')) {{
    const lastColon = v.lastIndexOf(':');
    if (lastColon !== -1) {{
      const prov = v.slice(1, lastColon);
      return {{ model: v, model_provider: prov }};
    }}
  }}
  const prov = (sel && sel._selectedProvider !== undefined) ? sel._selectedProvider : null;
  return {{ model: v, model_provider: prov }};
}}
function _ensureModelOptionInDropdown(v, sel, provider) {{
  if (sel) {{
    sel.value = v;
    sel._selectedProvider = provider || null;
  }}
}}
function getModelLabel(id) {{ return id ? ('Model(' + id + ')') : ''; }}
function closeModelDropdown() {{}}
function clearProfileTransitionReasoningContext() {{}}
function _writePersistedModelState() {{}}
function _rememberPendingSessionModel() {{}}
function syncReasoningChip() {{}}
function syncTopbar() {{}}
function showToast() {{}}
function t() {{ return ''; }}

async function api(endpoint, opts) {{
  if (endpoint === '/api/session/update') {{
    const body = JSON.parse(opts.body);
    return {{
      session: {{
        session_id: body.session_id,
        model: body.model,
        model_provider: body.model_provider,
        last_used_model: null,
        gateway_routing: null,
        gateway_routing_history: (S.session && S.session.gateway_routing_history) || []
      }}
    }};
  }}
  return {{}};
}}

{fn_provider_from_model_value}
{fn_gateway_routing_label}
{fn_format_gateway_model_label}
{fn_latest_gateway_routing}
{fn_format_session_model_with_gateway}
const sessionStorage = (() => {{
  const store = new Map();
  return {{
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => {{ store.set(k, String(v)); }},
    removeItem: (k) => {{ store.delete(k); }},
  }};
}})();
{fn_explicit_pick}
{fn_sync_model_chip}
{fn_select_model}
{fn_apply_ctx}
{onchange_block}

{eval_code}
"""
    return _run_node(harness)


def test_session_model_preserved_and_last_used_model_persisted():
    """session.model must not be overwritten; last_used_model must be persisted."""
    session = Session(
        session_id="6594fallback",
        title="Direct Fallback Test",
        model="claude-3-5-sonnet",
        last_used_model="claude-3-haiku",
    )
    session.save()

    loaded = Session.load("6594fallback")
    assert loaded is not None
    # Requested route preserved
    assert loaded.model == "claude-3-5-sonnet"
    # Fallback model captured separately
    assert loaded.last_used_model == "claude-3-haiku"

    # Verify compact representation
    compact = loaded.compact()
    assert compact["model"] == "claude-3-5-sonnet"
    assert compact["last_used_model"] == "claude-3-haiku"

    # Verify load_metadata_only
    meta = Session.load_metadata_only("6594fallback")
    assert meta is not None
    assert meta.model == "claude-3-5-sonnet"
    assert meta.last_used_model == "claude-3-haiku"


def test_sidebar_session_response_fields_allowlists_last_used_model():
    """Sidebar session serialization must include last_used_model."""
    assert "last_used_model" in _SIDEBAR_SESSION_RESPONSE_FIELDS

    raw_session = {
        "session_id": "sid6594",
        "title": "Conversation",
        "model": "gpt-4o",
        "last_used_model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }
    sidebar_item = _sidebar_session_response_item(raw_session)
    assert sidebar_item.get("last_used_model") == "gpt-4o-mini"
    assert sidebar_item.get("model") == "gpt-4o"


def test_streaming_post_run_hook_updates_last_used_model_without_mutating_session_model():
    """Post-run agent model capture sets s.last_used_model and preserves s.model."""
    from api.streaming import _capture_turn_served_model

    # 1. Fake agent with model = "claude-3-haiku" sets last_used_model and preserves requested model
    s = Session(session_id="post_run_test", model="claude-3-5-sonnet")

    class MockAgent:
        model = "claude-3-haiku"

    agent = MockAgent()
    used = _capture_turn_served_model(s, agent)
    assert used == "claude-3-haiku"
    assert s.last_used_model == "claude-3-haiku"
    assert s.model == "claude-3-5-sonnet"

    # 2. Fake agent with model = None leaves last_used_model as None
    s_none = Session(session_id="post_run_none", model="claude-3-5-sonnet")
    agent_none = MockAgent()
    agent_none.model = None
    used_none = _capture_turn_served_model(s_none, agent_none)
    assert used_none is None
    assert s_none.last_used_model is None
    assert s_none.model == "claude-3-5-sonnet"

    # 3. Fake agent without .model attribute leaves last_used_model as None
    s_missing = Session(session_id="post_run_missing", model="claude-3-5-sonnet")
    used_missing = _capture_turn_served_model(s_missing, object())
    assert used_missing is None
    assert s_missing.last_used_model is None
    assert s_missing.model == "claude-3-5-sonnet"

    # 4. Fake agent with non-string .model attribute leaves last_used_model as None
    s_non_str = Session(session_id="post_run_non_str", model="claude-3-5-sonnet")
    agent_non_str = MockAgent()
    agent_non_str.model = 12345
    used_non_str = _capture_turn_served_model(s_non_str, agent_non_str)
    assert used_non_str is None
    assert s_non_str.last_used_model is None
    assert s_non_str.model == "claude-3-5-sonnet"


def test_sidebar_model_resolution_observable_precedence():
    """Sidebar formatter returns observable string according to precedence hierarchy."""
    # Case 1: Direct fallback (last_used_model takes precedence over requested model)
    out1 = _sidebar_harness("""
const s = { model: 'claude-3-5-sonnet', last_used_model: 'claude-3-haiku', gateway_routing: null };
console.log(_formatSessionModelWithGateway(s));
""")
    assert out1 == "Model(claude-3-haiku)"

    # Case 2: Gateway fallback with explicit used_model (routing.used_model takes precedence)
    out2 = _sidebar_harness("""
const s = {
  model: 'claude-3-5-sonnet',
  last_used_model: 'claude-3-haiku',
  gateway_routing: { used_model: 'llama-3-70b', provider: 'openrouter' }
};
console.log(_formatSessionModelWithGateway(s));
""")
    assert out2 == "Model(llama-3-70b) via openrouter"

    # Case 3: Gateway routing without used_model preserves last_used_model with provider tag
    out3 = _sidebar_harness("""
const s = {
  model: 'claude-3-5-sonnet',
  last_used_model: 'claude-3-haiku',
  gateway_routing: { provider: 'openrouter' }
};
console.log(_formatSessionModelWithGateway(s));
""")
    assert out3 == "Model(claude-3-haiku) via openrouter"

    # Case 4: No fallback occurred (requested model shown)
    out4 = _sidebar_harness("""
const s = { model: 'claude-3-5-sonnet', last_used_model: null, gateway_routing: null };
console.log(_formatSessionModelWithGateway(s));
""")
    assert out4 == "Model(claude-3-5-sonnet)"


def test_server_session_update_clears_stale_fallback_and_routing():
    """Server /api/session/update invalidates display fields while preserving gateway_routing_history."""
    from unittest.mock import MagicMock, patch
    from urllib.parse import urlparse
    from api import routes

    session = Session(
        session_id="server_update_clear_test",
        title="Server Invalidation",
        model="claude-3-5-sonnet",
        model_provider="anthropic",
        last_used_model="claude-3-haiku",
        gateway_routing={"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"},
        gateway_routing_history=[{"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"}],
    )
    session.save = MagicMock()

    captured = {}
    def fake_j(h, payload, status=200):
        captured["payload"] = payload
        return True

    handler = MagicMock()
    body = {
        "session_id": "server_update_clear_test",
        "model": "gpt-4o",
        "model_provider": "openai",
        "workspace": "/tmp",
    }
    with patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=session), \
         patch("api.routes.resolve_trusted_workspace", return_value="/tmp"), \
         patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/session/update"))

    assert handled is True
    # Assert session model and provider updated
    assert session.model == "gpt-4o"
    assert session.model_provider == "openai"
    # Assert display fallback and current routing invalidated
    assert session.last_used_model is None
    assert session.gateway_routing is None
    # Assert bounded history is preserved on the session
    assert session.gateway_routing_history == [{"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"}]
    session.save.assert_called_once()

    # Assert returned API response projection
    resp_session = captured["payload"]["session"]
    assert resp_session["model"] == "gpt-4o"
    assert resp_session.get("last_used_model") is None
    assert resp_session.get("gateway_routing") is None
    assert resp_session.get("gateway_routing_history") == [{"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"}]


def test_chat_start_preparation_route_invalidation_scenarios():
    """Verify chat-start preparation route invalidation and preservation:
    1. An existing fallback plus a different incoming model/provider clears current attribution but preserves history.
    2. The same requested route preserves attribution.
    3. A failed/interrupted turn after the route switch cannot expose the old last_used_model through the session-list projection.
    """
    from unittest.mock import MagicMock, patch
    from api import routes
    from api.helpers import public_session_projection
    from api.route_session_list_cache import _session_list_cache_bounded_payload

    # 1. Existing fallback + different incoming model/provider clears current attribution but preserves history
    s1 = Session(
        session_id="chat_start_switch_test",
        title="Chat Start Switch",
        model="claude-3-5-sonnet",
        model_provider="anthropic",
        last_used_model="claude-3-haiku",
        gateway_routing={"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"},
        gateway_routing_history=[{"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"}],
    )
    s1.save = MagicMock()
    with patch("api.routes.register_session_writeback_owner"):
        routes._prepare_chat_start_session_for_stream(
            s1,
            msg="Hello from new model",
            attachments=[],
            workspace="/tmp",
            model="gpt-4o",
            model_provider="openai",
            stream_id="stream_switch_1",
            defer_save=True,
        )
    assert s1.model == "gpt-4o"
    assert s1.model_provider == "openai"
    assert s1.last_used_model is None
    assert s1.gateway_routing is None
    assert s1.gateway_routing_history == [{
        "used_model": "claude-3-haiku",
        "provider": "anthropic",
        "requested_model": "claude-3-5-sonnet",
    }]

    # 2. Same requested route preserves attribution
    s2 = Session(
        session_id="chat_start_same_route_test",
        title="Chat Start Same Route",
        model="claude-3-5-sonnet",
        model_provider="anthropic",
        last_used_model="claude-3-haiku",
        gateway_routing={"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"},
        gateway_routing_history=[{"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"}],
    )
    s2.save = MagicMock()
    with patch("api.routes.register_session_writeback_owner"):
        routes._prepare_chat_start_session_for_stream(
            s2,
            msg="Followup on same route",
            attachments=[],
            workspace="/tmp",
            model="claude-3-5-sonnet",
            model_provider="anthropic",
            stream_id="stream_same_1",
            defer_save=True,
        )
    assert s2.model == "claude-3-5-sonnet"
    assert s2.model_provider == "anthropic"
    assert s2.last_used_model == "claude-3-haiku"
    assert s2.gateway_routing == {
        "used_model": "claude-3-haiku",
        "provider": "anthropic",
        "requested_model": "claude-3-5-sonnet",
    }
    assert s2.gateway_routing_history == [{
        "used_model": "claude-3-haiku",
        "provider": "anthropic",
        "requested_model": "claude-3-5-sonnet",
    }]

    # 3. Failed/interrupted turn after route switch cannot expose old last_used_model through session-list projection
    s3 = Session(
        session_id="chat_start_interrupted_test",
        title="Interrupted Turn Test",
        model="claude-3-5-sonnet",
        model_provider="anthropic",
        last_used_model="claude-3-haiku",
        gateway_routing={"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"},
        gateway_routing_history=[{"used_model": "claude-3-haiku", "provider": "anthropic", "requested_model": "claude-3-5-sonnet"}],
    )
    s3.save = MagicMock()
    with patch("api.routes.register_session_writeback_owner"):
        routes._prepare_chat_start_session_for_stream(
            s3,
            msg="Start turn that fails",
            attachments=[],
            workspace="/tmp",
            model="gpt-4o",
            model_provider="openai",
            stream_id="stream_failed_1",
            defer_save=True,
        )
    # Simulate turn failure/interruption before any post-run settlement
    payload = {"sessions": [dict(s3.__dict__)]}
    projected = _session_list_cache_bounded_payload(payload)
    proj_session = projected["sessions"][0]

    assert proj_session["model"] == "gpt-4o"
    assert proj_session.get("last_used_model") is None
    assert proj_session.get("gateway_routing") is None

    # Also verify public_session_projection matches
    pub_proj = public_session_projection(dict(s3.__dict__))
    assert pub_proj["model"] == "gpt-4o"
    assert pub_proj.get("last_used_model") is None
    assert pub_proj.get("gateway_routing") is None

    # 4. Composed case: start with gpt-4o/provider-a + legacy provider-less history,
    # submit direct chat-start for gpt-4o/provider-b, persist/project session,
    # then run production composer and sidebar formatters.
    s4 = Session(
        session_id="chat_start_legacy_history_switch_test",
        title="Chat Start Legacy History Switch",
        model="gpt-4o",
        model_provider="provider-a",
        last_used_model="gpt-4o-mini",
        gateway_routing={"used_model": "gpt-4o-mini", "provider": "provider-a", "requested_model": "gpt-4o"},
        gateway_routing_history=[{"used_model": "gpt-4o-mini", "provider": "provider-a", "requested_model": "gpt-4o"}],
    )
    s4.save = MagicMock()
    with patch("api.routes.register_session_writeback_owner"):
        routes._prepare_chat_start_session_for_stream(
            s4,
            msg="Switch to provider-b for gpt-4o",
            attachments=[],
            workspace="/tmp",
            model="gpt-4o",
            model_provider="provider-b",
            stream_id="stream_prov_b_1",
            defer_save=True,
        )
    # Session state updated and invalidated
    assert s4.model == "gpt-4o"
    assert s4.model_provider == "provider-b"
    assert s4.last_used_model is None
    assert s4.gateway_routing is None
    assert len(s4.gateway_routing_history) == 1
    assert s4.gateway_routing_history == [{"used_model": "gpt-4o-mini", "provider": "provider-a", "requested_model": "gpt-4o"}]

    # Project session through public_session_projection
    proj_s4 = public_session_projection({k: v for k, v in s4.__dict__.items() if not callable(v)})
    assert proj_s4["model"] == "gpt-4o"
    assert proj_s4["model_provider"] == "provider-b"
    assert proj_s4.get("last_used_model") is None
    assert proj_s4.get("gateway_routing") is None
    assert proj_s4.get("gateway_routing_history") == [{"used_model": "gpt-4o-mini", "provider": "provider-a", "requested_model": "gpt-4o"}]

    # Run through production composer and sidebar formatters in Node harness
    composed_script = f"""
S.session = {json.dumps(proj_s4)};
elements.modelSelect.value = 'gpt-4o';
syncModelChip();
const res = {{
  chip: elements.composerModelLabel.textContent,
  sidebar: _formatSessionModelWithGateway(S.session),
  routing_match: !!_latestGatewayRoutingForSession(S.session),
  history_len: Array.isArray(S.session.gateway_routing_history) ? S.session.gateway_routing_history.length : 0
}};
console.log(JSON.stringify(res));
"""
    harness_res = json.loads(_production_event_harness(composed_script))
    # Both composer and sidebar must show requested provider-b route without provider-a historical failover
    assert harness_res["routing_match"] is False
    assert harness_res["chip"] == "Model(gpt-4o)"
    assert harness_res["sidebar"] == "Model(gpt-4o)"
    # History remains unchanged
    assert harness_res["history_len"] == 1


def test_production_model_selection_lifecycle_observable_behavior():
    """Test full production sequence: selectModelFromDropdown -> modelSelect.onchange -> syncModelChip -> session update -> in flight."""
    raw_output = _production_event_harness("""
async function run() {
  const log = [];

  // 1. Initial state after turn 1 served fallback model
  syncModelChip();
  log.push({ phase: 'initial', label: elements.composerModelLabel.textContent });

  // 2. User selects 'gpt-4o' via shipped selectModelFromDropdown
  // This executes:
  //   - sel.value = 'gpt-4o'
  //   - syncModelChip() (first call: manual pick active)
  //   - modelSelect.onchange() (routeChanged invalidates last_used_model, S.session.model = 'gpt-4o')
  //   - syncModelChip() (second call: sel.value === S.session.model, last_used_model is cleared)
  //   - api('/api/session/update') returns session projection with preserved history
  //   - _applySessionContextMetadataUpdate(data)
  //   - syncModelChip() (third call after server update)
  await selectModelFromDropdown('gpt-4o');
  log.push({ phase: 'after_select_and_update', label: elements.composerModelLabel.textContent });

  // 3. While next turn is in flight (before any streaming event returns a new fallback)
  syncModelChip();
  log.push({ phase: 'turn_in_flight', label: elements.composerModelLabel.textContent });

  // 4. Test gateway routing session with history
  S.session.model = 'gpt-4o';
  S.session.gateway_routing = { used_model: 'llama-3', provider: 'openrouter', requested_model: 'gpt-4o' };
  S.session.gateway_routing_history = [{ used_model: 'llama-3', provider: 'openrouter', requested_model: 'gpt-4o' }];
  elements.modelSelect.value = 'gpt-4o';
  syncModelChip();
  log.push({
    phase: 'gateway_initial',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session)
  });

  // User selects 'claude-3-5-sonnet' from dropdown
  await selectModelFromDropdown('claude-3-5-sonnet');
  log.push({
    phase: 'gateway_after_switch',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session),
    history_len: S.session.gateway_routing_history.length
  });

  // While next turn is in flight
  syncModelChip();
  log.push({
    phase: 'gateway_turn_in_flight',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session)
  });

  // 5. Test same-model, different-provider route transition with history containing requested_model and requested_provider
  S.session.model = 'gpt-4o';
  S.session.model_provider = 'provider-a';
  S.session.gateway_routing = {
    used_model: 'gpt-4o-mini',
    provider: 'provider-a',
    requested_model: 'gpt-4o',
    requested_provider: 'provider-a'
  };
  S.session.gateway_routing_history = [{
    used_model: 'gpt-4o-mini',
    provider: 'provider-a',
    requested_model: 'gpt-4o',
    requested_provider: 'provider-a'
  }];
  elements.modelSelect.value = 'gpt-4o';
  elements.modelSelect._selectedProvider = 'provider-a';
  syncModelChip();
  log.push({
    phase: 'provider_route_initial',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session)
  });

  // User selects the same bare model ID 'gpt-4o', but through 'provider-b'
  await selectModelFromDropdown('gpt-4o', 'provider-b');
  log.push({
    phase: 'provider_route_after_switch',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session),
    history_len: S.session.gateway_routing_history.length,
    model: S.session.model,
    provider: S.session.model_provider
  });

  // During next turn in flight
  syncModelChip();
  log.push({
    phase: 'provider_route_turn_in_flight',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session)
  });

  // 6. Test legacy history without requested_provider preserved when model matches
  S.session.model = 'gpt-4o';
  S.session.model_provider = 'provider-b';
  S.session.gateway_routing = null;
  S.session.gateway_routing_history = [{
    used_model: 'gpt-4o-mini',
    provider: 'provider-a',
    requested_model: 'gpt-4o'
    // no requested_provider
  }];
  syncModelChip();
  log.push({
    phase: 'legacy_history_without_requested_provider',
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(S.session)
  });

  console.log(JSON.stringify(log));
}
run();
""")
    results = {item["phase"]: item for item in json.loads(raw_output)}

    # Phase 1: Initial served direct fallback model displayed
    assert results["initial"]["label"] == "Model(claude-3-haiku)"

    # Phase 2: After selectModelFromDropdown + onchange + session update, chip remains the newly selected model
    assert results["after_select_and_update"]["label"] == "Model(gpt-4o)"

    # Phase 3: In-flight turn does not snap back to stale last_used_model
    assert results["turn_in_flight"]["label"] == "Model(gpt-4o)"

    # Phase 4: Gateway routing session initially displays gateway routed label on chip and sidebar
    assert results["gateway_initial"]["chip"] == "Model(llama-3) via openrouter"
    assert results["gateway_initial"]["sidebar"] == "Model(llama-3) via openrouter"

    # Phase 5: After switching to claude-3-5-sonnet:
    # - Chip and sidebar display the new model
    # - Routing history from prior route is preserved (length 1), but cannot drive display
    assert results["gateway_after_switch"]["chip"] == "Model(claude-3-5-sonnet)"
    assert results["gateway_after_switch"]["sidebar"] == "Model(claude-3-5-sonnet)"
    assert results["gateway_after_switch"]["history_len"] == 1

    # Phase 6: In-flight turn retains the new model on both surfaces
    assert results["gateway_turn_in_flight"]["chip"] == "Model(claude-3-5-sonnet)"
    assert results["gateway_turn_in_flight"]["sidebar"] == "Model(claude-3-5-sonnet)"

    # Phase 7: Initial state for provider-a routed turn displays failover
    assert results["provider_route_initial"]["chip"] == "Model(gpt-4o-mini) via provider-a"
    assert results["provider_route_initial"]["sidebar"] == "Model(gpt-4o-mini) via provider-a"

    # Phase 8: After same-model, different-provider switch:
    # - history length remains unchanged (1)
    # - session model_provider is updated to provider-b
    # - composer and sidebar show newly selected route without provider-a historical failover
    assert results["provider_route_after_switch"]["history_len"] == 1
    assert results["provider_route_after_switch"]["model"] == "gpt-4o"
    assert results["provider_route_after_switch"]["provider"] == "provider-b"
    assert results["provider_route_after_switch"]["chip"] == "Model(gpt-4o)"
    assert results["provider_route_after_switch"]["sidebar"] == "Model(gpt-4o)"

    # Phase 9: During in-flight turn, composer and sidebar remain Model(gpt-4o)
    assert results["provider_route_turn_in_flight"]["chip"] == "Model(gpt-4o)"
    assert results["provider_route_turn_in_flight"]["sidebar"] == "Model(gpt-4o)"

    # Phase 10: Legacy history without requested_provider fails closed when active route has an explicit provider
    assert results["legacy_history_without_requested_provider"]["chip"] == "Model(gpt-4o)"
    assert results["legacy_history_without_requested_provider"]["sidebar"] == "Model(gpt-4o)"


def test_production_composed_provider_route_dimension_scenarios():
    """Composed end-to-end tests for the 5 maintainer-specified producer/consumer scenarios:
    1. Upstream response requested_provider="CanopyWave" with session provider "canopywave" (case-insensitive match)
    2. Named custom:<slug> after runtime rewrite to "custom" (named identity preserved end-to-end)
    3. Exact matching providers ("openrouter" vs "openrouter")
    4. Legacy/empty requested-provider metadata (preserves valid legacy routing history)
    5. Same bare model on genuinely different providers ("provider-a" vs "provider-b" rejected)
    """
    from api.routes import _clean_session_model_provider
    from api.streaming import _extract_gateway_routing_metadata, _normalize_gateway_routing_metadata

    # Scenario 1: Upstream response requested_provider="CanopyWave" with session provider "canopywave"
    raw_canopy = {
        "requested_model": "deepseek-v3.2",
        "requested_provider": "CanopyWave",
        "used_model": "deepseek-v3.2-fast",
        "used_provider": "CanopyWave",
        "routing": [
            {"provider": "CanopyWave", "status": "failed", "reason": "timeout"},
            {"provider": "CanopyWave", "status": "success"},
        ],
    }
    norm_canopy = _normalize_gateway_routing_metadata(raw_canopy)
    assert norm_canopy["requested_provider"] == "CanopyWave"
    sess_canopy_provider = _clean_session_model_provider("CanopyWave")
    assert sess_canopy_provider == "canopywave"

    # Scenario 2: Named custom:<slug> after runtime rewrite to "custom"
    _session_req_prov = "custom:backup-endpoint"
    _resolved_prov = "custom"
    norm_custom = _extract_gateway_routing_metadata(
        agent=None,
        result={"used_model": "llama-local-q4", "used_provider": "custom"},
        requested_model="llama-local",
        requested_provider=_session_req_prov or _resolved_prov,
    )
    assert norm_custom["requested_provider"] == "custom:backup-endpoint"
    sess_custom_provider = _clean_session_model_provider("custom:backup-endpoint")
    assert sess_custom_provider == "custom:backup-endpoint"

    # Scenario 3: Exact matching providers
    raw_exact = {
        "requested_model": "gpt-4o",
        "requested_provider": "openrouter",
        "used_model": "gpt-4o-mini",
        "used_provider": "openrouter",
    }
    norm_exact = _normalize_gateway_routing_metadata(raw_exact)
    sess_exact_provider = _clean_session_model_provider("openrouter")

    # Scenario 4: Legacy/empty requested-provider metadata
    raw_legacy = {
        "requested_model": "gpt-4o",
        "used_model": "gpt-4o-mini",
        "used_provider": "openrouter",
    }
    norm_legacy = _normalize_gateway_routing_metadata(raw_legacy)
    assert "requested_provider" not in norm_legacy
    sess_legacy_provider = _clean_session_model_provider("openrouter")

    # Scenario 5: Same bare model on genuinely different providers
    raw_cross = {
        "requested_model": "gpt-4o",
        "requested_provider": "provider-a",
        "used_model": "gpt-4o-mini",
        "used_provider": "provider-a",
    }
    norm_cross = _normalize_gateway_routing_metadata(raw_cross)
    sess_cross_provider = _clean_session_model_provider("provider-b")

    script = f"""
const scenarios = {{
  case1_canopywave: {{
    session: {{
      model: 'deepseek-v3.2',
      model_provider: {json.dumps(sess_canopy_provider)},
      gateway_routing: {json.dumps(norm_canopy)},
      gateway_routing_history: [{json.dumps(norm_canopy)}]
    }},
    modelSelectValue: 'deepseek-v3.2'
  }},
  case2_custom_slug: {{
    session: {{
      model: 'llama-local',
      model_provider: {json.dumps(sess_custom_provider)},
      gateway_routing: {json.dumps(norm_custom)},
      gateway_routing_history: [{json.dumps(norm_custom)}]
    }},
    modelSelectValue: 'llama-local'
  }},
  case3_exact_match: {{
    session: {{
      model: 'gpt-4o',
      model_provider: {json.dumps(sess_exact_provider)},
      gateway_routing: {json.dumps(norm_exact)},
      gateway_routing_history: [{json.dumps(norm_exact)}]
    }},
    modelSelectValue: 'gpt-4o'
  }},
  case4_legacy_empty: {{
    session: {{
      model: 'gpt-4o',
      model_provider: {json.dumps(sess_legacy_provider)},
      gateway_routing: {json.dumps(norm_legacy)},
      gateway_routing_history: [{json.dumps(norm_legacy)}]
    }},
    modelSelectValue: 'gpt-4o'
  }},
  case5_different_provider_rejection: {{
    session: {{
      model: 'gpt-4o',
      model_provider: {json.dumps(sess_cross_provider)},
      gateway_routing: null,
      gateway_routing_history: [{json.dumps(norm_cross)}]
    }},
    modelSelectValue: 'gpt-4o'
  }}
}};

const out = {{}};
for (const [k, v] of Object.entries(scenarios)) {{
  S.session = v.session;
  elements.modelSelect.value = v.modelSelectValue;
  syncModelChip();
  out[k] = {{
    chip: elements.composerModelLabel.textContent,
    sidebar: _formatSessionModelWithGateway(v.session),
    routing_match: !!_latestGatewayRoutingForSession(v.session)
  }};
}}
console.log(JSON.stringify(out));
"""
    results = json.loads(_production_event_harness(script))

    # Verification 1: Case-insensitive match (CanopyWave vs canopywave)
    assert results["case1_canopywave"]["routing_match"] is True
    assert "deepseek-v3.2-fast" in results["case1_canopywave"]["chip"]
    assert "deepseek-v3.2-fast" in results["case1_canopywave"]["sidebar"]

    # Verification 2: Named custom:<slug> preserved and matches
    assert results["case2_custom_slug"]["routing_match"] is True
    assert "llama-local-q4" in results["case2_custom_slug"]["chip"]
    assert "llama-local-q4" in results["case2_custom_slug"]["sidebar"]

    # Verification 3: Exact matching providers
    assert results["case3_exact_match"]["routing_match"] is True
    assert "gpt-4o-mini" in results["case3_exact_match"]["chip"]
    assert "gpt-4o-mini" in results["case3_exact_match"]["sidebar"]

    # Verification 4: Legacy/empty requested_provider metadata is preserved
    assert results["case4_legacy_empty"]["routing_match"] is True
    assert "gpt-4o-mini" in results["case4_legacy_empty"]["chip"]
    assert "gpt-4o-mini" in results["case4_legacy_empty"]["sidebar"]

    # Verification 5: Same bare model on genuinely different providers is REJECTED
    assert results["case5_different_provider_rejection"]["routing_match"] is False
    assert results["case5_different_provider_rejection"]["chip"] == "Model(gpt-4o)"
    assert results["case5_different_provider_rejection"]["sidebar"] == "Model(gpt-4o)"


def test_composer_chip_matches_provider_qualified_picker_values():
    """Verify composer chip derives bare model from provider-qualified dropdown values.

    When picker value is '@custom:backup:gpt-4o' or '@openrouter:gpt-5.5',
    the route guard recognizes that the bare model matches the session model,
    activating the fallback attribution instead of resetting to raw value.
    """
    raw_output = _production_event_harness("""
const res = [];

// Case 1: @custom:backup:gpt-4o with session model 'gpt-4o'
S.session = {
  session_id: 'custom_prov_1',
  model: 'gpt-4o',
  model_provider: 'custom:backup',
  last_used_model: 'gpt-4o-mini',
  gateway_routing: null,
  gateway_routing_history: []
};
elements.modelSelect.value = '@custom:backup:gpt-4o';
syncModelChip();
res.push({
  case: 'custom_backup',
  label: elements.composerModelLabel.textContent
});

// Case 2: @openrouter:gpt-5.5 with session model 'gpt-5.5'
S.session = {
  session_id: 'openrouter_prov_1',
  model: 'gpt-5.5',
  model_provider: 'openrouter',
  last_used_model: 'gpt-5-mini',
  gateway_routing: null,
  gateway_routing_history: []
};
elements.modelSelect.value = '@openrouter:gpt-5.5';
syncModelChip();
res.push({
  case: 'openrouter',
  label: elements.composerModelLabel.textContent
});

console.log(JSON.stringify(res));
""")
    results = {item["case"]: item["label"] for item in json.loads(raw_output)}
    assert results["custom_backup"] == "Model(gpt-4o-mini)"
    assert results["openrouter"] == "Model(gpt-5-mini)"


def test_composer_chip_keeps_attribution_when_dropdown_restores_without_user_pick():
    """A restored session whose dropdown fell back to a different option (no
    explicit pick in this tab) keeps its served-model attribution (#7438 r4).

    Only an explicit picker pick may make the attribution stale; after one, the
    chip follows the pick as before.
    """
    raw_output = _production_event_harness("""
const res = [];
S.session = {
  session_id: 'restored_gateway_1',
  model: '@openrouter:gpt-4o',
  model_provider: 'openrouter',
  last_used_model: 'gpt-4o-mini',
  gateway_routing: null,
  gateway_routing_history: []
};
elements.modelSelect.value = 'openai/gpt-4o';
syncModelChip();
res.push({ case: 'restored_no_pick', label: elements.composerModelLabel.textContent });
_rememberExplicitPickerPick('restored_gateway_1', 'openai/gpt-4o', 'openai');
syncModelChip();
res.push({ case: 'after_explicit_pick', label: elements.composerModelLabel.textContent });
console.log(JSON.stringify(res));
""")
    results = {item["case"]: item["label"] for item in json.loads(raw_output)}
    assert results["restored_no_pick"] == "Model(gpt-4o-mini)"
    assert results["after_explicit_pick"] != "Model(gpt-4o-mini)"


def test_named_custom_gateway_routing_no_false_failover():
    """Named custom request with canonical used_provider does not falsely trigger failover (#7438)."""
    from api.streaming import _extract_gateway_routing_metadata

    norm = _extract_gateway_routing_metadata(
        agent=None,
        result={"used_model": "llama-local-q4", "used_provider": "custom"},
        requested_model="llama-local",
        requested_provider="custom",
        requested_route_provider="custom:backup",
    )
    assert norm is not None
    assert norm["used_provider"] == "custom"
    assert norm["requested_provider"] == "custom"
    assert norm["requested_route_provider"] == "custom:backup"
    assert norm["provider_changed"] is False
    assert norm["has_failover"] is False


def test_legacy_saved_routing_preserves_via_attribution_for_named_custom_and_aliases():
    """Existing saved metadata lacking requested_route_provider preserves via attribution (#7438)."""
    script = """
const res = {};

// Case 1: Legacy session.gateway_routing with custom:backup and requested_provider custom
const legacyCustomSession = {
  model: 'llama-local',
  model_provider: 'custom:backup',
  gateway_routing: {
    requested_model: 'llama-local',
    used_model: 'llama-local-q4',
    requested_provider: 'custom',
    used_provider: 'custom'
  },
  gateway_routing_history: []
};
res.legacy_active = !!_latestGatewayRoutingForSession(legacyCustomSession);

// Case 2: Legacy history entry without requested_route_provider fails closed
const legacyHistorySession = {
  model: 'llama-local',
  model_provider: 'custom:backup',
  gateway_routing: null,
  gateway_routing_history: [{
    requested_model: 'llama-local',
    used_model: 'llama-local-q4',
    requested_provider: 'custom',
    used_provider: 'custom'
  }]
};
res.legacy_history = !!_latestGatewayRoutingForSession(legacyHistorySession);

console.log(JSON.stringify(res));
"""
    results = json.loads(_production_event_harness(script))
    # Active legacy routing preserves working attribution
    assert results["legacy_active"] is True
    # Conservative history filtering fails closed
    assert results["legacy_history"] is False


# ponytail: isolated worker turn harness verifying route lifecycle (#7438)
class _WorkerTestSession:
    def __init__(self, session_id, *, model, model_provider=None, profile=None):
        self.session_id = session_id
        self.title = "Worker test"
        self.workspace = "/tmp"
        self.model = model
        self.model_provider = model_provider
        self.profile = profile
        self.messages = []
        self.last_used_model = None
        self.gateway_routing = None
        self.gateway_routing_history = []
        self.active_stream_id = None
        self.personality = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.tool_calls = []
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None

    def save(self, touch_updated_at=True, **kwargs):
        pass

    def compact(self):
        return {"session_id": self.session_id, "model": self.model}


class _WorkerTestAgent:
    mid_turn_hook = None
    served_model = "fallback-served-model"
    entry_count = 0
    gateway_metadata = None

    def __init__(self, **kwargs):
        self.model = _WorkerTestAgent.served_model
        self.session_id = kwargs.get("session_id")
        self.context_compressor = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None

    def run_conversation(self, **kwargs):
        _WorkerTestAgent.entry_count += 1
        if _WorkerTestAgent.mid_turn_hook:
            _WorkerTestAgent.mid_turn_hook()
        result = {
            "messages": [
                {"role": "user", "content": kwargs.get("persist_user_message", "test")},
                {"role": "assistant", "content": "test reply"},
            ]
        }
        if _WorkerTestAgent.gateway_metadata is not None:
            result["llm_gateway"] = dict(_WorkerTestAgent.gateway_metadata)
        return result

    def interrupt(self, _message):
        return None


def _run_worker_test_turn(
    monkeypatch,
    session,
    *,
    stream_id,
    dispatch_model,
    dispatch_provider=None,
    served_model="fallback-served-model",
    mid_turn_hook=None,
    profile_enrichment_hook=None,
    config=None,
    resolved_model=None,
    gateway_metadata=None,
):
    from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={"provider": "anthropic", "base_url": None, "api_key": "k"}
    )
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=object())

    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _WorkerTestAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: (resolved_model or dispatch_model, dispatch_provider or "anthropic", None),
    )
    _cfg = dict(config or {})
    monkeypatch.setattr("api.config.get_config", lambda: _cfg)
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    if profile_enrichment_hook:
        monkeypatch.setattr(
            streaming,
            "_apply_profile_home_context_to_streaming_model",
            profile_enrichment_hook,
        )

    _WorkerTestAgent.served_model = served_model
    _WorkerTestAgent.gateway_metadata = gateway_metadata
    _WorkerTestAgent.mid_turn_hook = mid_turn_hook
    _WorkerTestAgent.entry_count = 0

    session.active_stream_id = stream_id
    stream_q = queue.Queue()
    streaming.STREAMS[stream_id] = stream_q
    try:
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text="worker test prompt",
            model=dispatch_model,
            model_provider=dispatch_provider,
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.pop(session.session_id, None)

    # Assert agent entry and no terminal error events
    assert _WorkerTestAgent.entry_count >= 1
    events = []
    while not stream_q.empty():
        events.append(stream_q.get_nowait())
    terminal_errors = [e for e in events if isinstance(e, tuple) and e[0] == "error"]
    assert not terminal_errors, f"Terminal stream error observed: {terminal_errors}"


def test_worker_turn_omitted_provider_rejects_session_attribution_on_mid_turn_provider_switch(monkeypatch):
    """When turn dispatches with omitted model_provider=None, a mid-turn provider
    switch on the same model must not overwrite session-level attribution (#7438)."""
    session = _WorkerTestSession("sess-worker-omitted-provider-race", model="haiku-4-5", model_provider=None)

    def _switch_provider():
        session.model_provider = "openrouter"

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-omitted-provider-race",
        dispatch_model="haiku-4-5",
        dispatch_provider=None,
        served_model="haiku-fallback",
        mid_turn_hook=_switch_provider,
    )

    # Session-level last_used_model is rejected due to route mismatch
    assert session.last_used_model is None
    # Session provider remains the newly selected openrouter
    assert session.model_provider == "openrouter"
    # Assistant message preserves turn-served model attribution
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "haiku-fallback"


def test_worker_turn_rejects_session_attribution_on_mid_turn_model_switch(monkeypatch):
    """When session model changes mid-turn, session-level attribution is rejected (#7438)."""
    session = _WorkerTestSession("sess-worker-model-switch", model="haiku-4-5", model_provider="anthropic")

    def _switch_model():
        session.model = "opus-4-8"

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-model-switch",
        dispatch_model="haiku-4-5",
        dispatch_provider="anthropic",
        served_model="haiku-fallback",
        mid_turn_hook=_switch_model,
    )

    assert session.last_used_model is None
    assert session.model == "opus-4-8"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "haiku-fallback"


def test_worker_turn_rejects_session_attribution_on_mid_turn_provider_switch(monkeypatch):
    """When session provider changes mid-turn, session-level attribution is rejected (#7438)."""
    session = _WorkerTestSession("sess-worker-provider-switch", model="haiku-4-5", model_provider="anthropic")

    def _switch_provider():
        session.model_provider = "openrouter"

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-provider-switch",
        dispatch_model="haiku-4-5",
        dispatch_provider="anthropic",
        served_model="haiku-fallback",
        mid_turn_hook=_switch_provider,
    )

    assert session.last_used_model is None
    assert session.model_provider == "openrouter"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "haiku-fallback"


def test_worker_turn_updates_session_attribution_when_route_is_unchanged(monkeypatch):
    """When route remains unchanged during turn, session-level attribution is updated (#7438)."""
    session = _WorkerTestSession("sess-worker-unchanged-route", model="haiku-4-5", model_provider="anthropic")

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-unchanged-route",
        dispatch_model="haiku-4-5",
        dispatch_provider="anthropic",
        served_model="haiku-fallback",
        mid_turn_hook=None,
    )

    assert session.last_used_model == "haiku-fallback"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "haiku-fallback"


def test_worker_turn_profile_enrichment_accepts_session_attribution(monkeypatch):
    """When profile enriches omitted provider to anthropic, session attribution is accepted (#7438)."""
    session = _WorkerTestSession(
        "sess-worker-profile-enrichment",
        model="haiku-4-5",
        model_provider=None,
        profile="dev-profile",
    )

    def _profile_hook(model, provider_context, profile_home, has_profile):
        return model, "anthropic", False

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-profile-enrichment",
        dispatch_model="haiku-4-5",
        dispatch_provider=None,
        served_model="haiku-fallback",
        mid_turn_hook=None,
        profile_enrichment_hook=_profile_hook,
    )

    assert session.last_used_model == "haiku-fallback"
    assert session.model_provider == "anthropic"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "haiku-fallback"


def test_worker_turn_profile_repair_accepts_session_attribution(monkeypatch):
    """When profile repairs stale model to target model, session attribution is accepted (#7438)."""
    session = _WorkerTestSession(
        "sess-worker-profile-repair",
        model="stale-model",
        model_provider="anthropic",
        profile="dev-profile",
    )

    def _profile_hook(model, provider_context, profile_home, has_profile):
        return "repaired-haiku", provider_context, True

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-profile-repair",
        dispatch_model="stale-model",
        dispatch_provider="anthropic",
        served_model="repaired-haiku-served",
        mid_turn_hook=None,
        profile_enrichment_hook=_profile_hook,
    )

    assert session.last_used_model == "repaired-haiku-served"
    assert session.model == "repaired-haiku"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "repaired-haiku-served"


def test_worker_turn_openrouter_free_suffix_preserves_attribution(monkeypatch):
    """OpenRouter qualified models with :free tag suffix preserve session attribution (#7438)."""
    session = _WorkerTestSession(
        "sess-worker-openrouter-free",
        model="tencent/hy3-preview:free",
        model_provider="openrouter",
    )

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-openrouter-free",
        dispatch_model="@openrouter:tencent/hy3-preview:free",
        dispatch_provider="openrouter",
        served_model="tencent/hy3-preview:free-served",
        mid_turn_hook=None,
    )

    assert session.last_used_model == "tencent/hy3-preview:free-served"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "tencent/hy3-preview:free-served"


# Current master fails closed on a named custom:<slug> route that no
# custom_providers[] row owns (raise_for_custom_provider_route), before the
# agent is built. These attribution tests need a configured provider so the
# turn actually reaches the worker.
_NAMED_CUSTOM_BACKUP_CONFIG = {
    "custom_providers": [
        {"name": "backup", "base_url": "http://127.0.0.1:9/v1", "api_key": "k", "model": "gpt-4o"},
    ],
}


def test_worker_turn_qualified_route_served_unchanged_is_not_a_model_switch(monkeypatch):
    """@openrouter:gpt-4o resolved to gpt-4o and served by gpt-4o is not a
    switch: the gateway metadata keeps model_changed False, as on master
    (#7438 r4; the qualified hint must not replace the resolved model)."""
    session = _WorkerTestSession(
        "sess-worker-qualified-unchanged",
        model="@openrouter:gpt-4o",
        model_provider="openrouter",
    )

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-qualified-unchanged",
        dispatch_model="@openrouter:gpt-4o",
        dispatch_provider="openrouter",
        resolved_model="gpt-4o",
        served_model="gpt-4o",
        gateway_metadata={"used_model": "gpt-4o", "used_provider": "openrouter"},
        mid_turn_hook=None,
    )

    routing = session.gateway_routing
    assert routing, "the worker should persist the gateway routing metadata"
    assert routing.get("model_changed") is False
    assert routing.get("requested_model") == "gpt-4o"


def test_worker_turn_named_custom_plain_preserves_attribution(monkeypatch):
    """Named custom routes like @custom:backup:gpt-4o preserve session attribution (#7438)."""
    session = _WorkerTestSession(
        "sess-worker-custom-plain",
        model="gpt-4o",
        model_provider="custom:backup",
    )

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-custom-plain",
        config=_NAMED_CUSTOM_BACKUP_CONFIG,
        dispatch_model="@custom:backup:gpt-4o",
        dispatch_provider="custom:backup",
        served_model="gpt-4o-mini",
        mid_turn_hook=None,
    )

    assert session.last_used_model == "gpt-4o-mini"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "gpt-4o-mini"


def test_worker_turn_named_custom_free_suffix_preserves_attribution(monkeypatch):
    """Named custom routes with :free tag suffix preserve session attribution (#7438)."""
    session = _WorkerTestSession(
        "sess-worker-custom-free",
        model="some-model:free",
        model_provider="custom:backup",
    )

    _run_worker_test_turn(
        monkeypatch,
        session,
        stream_id="stream-test-custom-free",
        config=_NAMED_CUSTOM_BACKUP_CONFIG,
        dispatch_model="@custom:backup:some-model:free",
        dispatch_provider="custom:backup",
        served_model="custom-served-free",
        mid_turn_hook=None,
    )

    assert session.last_used_model == "custom-served-free"
    asst_msg = [m for m in session.messages if m.get("role") == "assistant"][-1]
    assert asst_msg.get("_usedModel") == "custom-served-free"




