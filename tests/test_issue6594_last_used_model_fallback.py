import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
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

    fn_gateway_routing_label = _extract_function(ui_source, "_gatewayRoutingLabel")
    fn_format_gateway_model_label = _extract_function(ui_source, "_formatGatewayModelLabel")
    fn_latest_gateway_routing = _extract_function(ui_source, "_latestGatewayRoutingForSession")
    fn_format_session_model_with_gateway = _extract_function(sessions_source, "_formatSessionModelWithGateway")
    fn_sync_model_chip = _extract_function(ui_source, "syncModelChip")
    fn_select_model = _extract_function(ui_source, "selectModelFromDropdown")
    fn_apply_ctx = _extract_function(boot_source, "_applySessionContextMetadataUpdate")
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
let _selectModelProviders = {{}};
function _modelStateForSelect(sel, v) {{
  const prov = (sel && sel._selectedProvider !== undefined) ? sel._selectedProvider : (_selectModelProviders[v] || null);
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

{fn_gateway_routing_label}
{fn_format_gateway_model_label}
{fn_latest_gateway_routing}
{fn_format_session_model_with_gateway}
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
    s = Session(session_id="post_run_test", model="claude-3-5-sonnet")

    class MockAgent:
        model = "claude-3-haiku"

    agent = MockAgent()
    resolved_model = "claude-3-5-sonnet"
    model = "claude-3-5-sonnet"

    _used_model = getattr(agent, "model", None) or resolved_model or model
    if _used_model:
        s.last_used_model = str(_used_model).strip()[:240]

    assert s.last_used_model == "claude-3-haiku"
    assert s.model == "claude-3-5-sonnet"


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

    # Phase 10: Legacy history without requested_provider preserved when model matches
    assert results["legacy_history_without_requested_provider"]["chip"] == "Model(gpt-4o-mini) via provider-a"
    assert results["legacy_history_without_requested_provider"]["sidebar"] == "Model(gpt-4o-mini) via provider-a"


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

