"""Session-scoped reasoning effort with per-model automatic fallback."""

from pathlib import Path
import inspect
import json
import subprocess
import sys

import api.config as config
from api.gateway_chat import _gateway_reasoning_effort_for_request
from api.models import Session
from api.streaming import _bind_session_reasoning_effort


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STREAMING = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_session_persists_reasoning_effort_in_compact_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("api.models.SESSION_DIR", tmp_path)
    session = Session(session_id="reasoning-session", reasoning_effort="low")

    session.save()
    restored = Session.load("reasoning-session")

    assert restored.reasoning_effort == "low"
    assert restored.compact()["reasoning_effort"] == "low"


def test_effective_reasoning_prefers_session_then_model_override_then_global(monkeypatch):
    monkeypatch.setattr(
        config,
        "_load_yaml_config_file",
        lambda _path: {
            "agent": {
                "reasoning_effort": "medium",
                "reasoning_overrides": {"kimi-k3": "max", "gpt-5.6-sol": "low"},
            }
        },
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: Path("ignored"))
    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *a, **k: ["low", "medium", "max"])

    assert config.get_reasoning_status(model_id="kimi-k3")["reasoning_effort"] == "max"
    assert config.get_reasoning_status(model_id="gpt-5.6-sol")["reasoning_effort"] == "low"
    assert config.get_reasoning_status(model_id="kimi-k3", session_effort="medium")["reasoning_effort"] == "medium"


def test_effective_reasoning_clamps_model_override_to_provider_capability(monkeypatch):
    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *a, **k: ["low", "high"])
    cfg = {"agent": {"reasoning_effort": "medium", "reasoning_overrides": {"kimi-k3": "max"}}}

    assert config.resolve_effective_reasoning_effort(cfg, "kimi-k3") == "high"


def test_effective_reasoning_has_standalone_fallback_without_agent_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *a, **k: ["low", "medium", "max"])
    cfg = {"agent": {"reasoning_effort": "medium", "reasoning_overrides": {"gpt-5.6-sol": "low"}}}

    assert config.resolve_effective_reasoning_effort(cfg, "@openai-codex:gpt-5.6-sol") == "low"
    assert config.resolve_effective_reasoning_effort(cfg, "other-model") == "medium"


def test_standalone_fallback_skips_invalid_override_and_preserves_disabled_aliases(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *a, **k: list(config.VALID_REASONING_EFFORTS))
    cfg = {"agent": {"reasoning_effort": False, "reasoning_overrides": {"gpt-5-6-sol": "bogus"}}}

    assert config.resolve_effective_reasoning_effort(cfg, "openai/gpt-5.6-sol") == "none"


def test_ultra_is_valid_and_gateway_uses_session_aware_resolution(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *a, **k: list(config.VALID_REASONING_EFFORTS))
    cfg = {"agent": {"reasoning_effort": "medium", "reasoning_overrides": {"gpt-5.6-sol": "xhigh"}}}

    assert config.parse_reasoning_effort("ultra") == {"enabled": True, "effort": "ultra"}
    assert 'data-effort="ultra"' in INDEX
    assert _gateway_reasoning_effort_for_request(
        cfg, model="gpt-5.6-sol", model_provider="openai-codex", session_effort="ultra"
    ) == "xhigh"
    assert _gateway_reasoning_effort_for_request(
        cfg, model="gpt-5.6-sol", model_provider="openai-codex"
    ) == "xhigh"


def test_session_reasoning_survives_fallback_and_model_switch(monkeypatch):
    class Agent:
        model = "primary"
        provider = "provider"
        base_url = None
        reasoning_config = {"enabled": True, "effort": "low"}

        def switch_model(self, model, provider):
            self.model = model
            self.provider = provider
            self.reasoning_config = {"enabled": True, "effort": "medium"}

        def _try_activate_fallback(self):
            self.model = "fallback"
            self.reasoning_config = {"enabled": True, "effort": "medium"}
            return True

    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *a, **k: list(config.VALID_REASONING_EFFORTS))
    agent = Agent()
    _bind_session_reasoning_effort(agent, {}, "high")

    assert agent._try_activate_fallback() is True
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    agent.switch_model("switched", "other-provider")
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_reasoning_parameter_does_not_shift_legacy_session_arguments():
    params = list(inspect.signature(Session.__init__).parameters)
    assert params.index("reasoning_effort") > params.index("share_created_at")


def test_session_update_route_accepts_and_evicts_reasoning_effort():
    assert '"reasoning_effort" in body' in ROUTES
    assert "s.reasoning_effort" in ROUTES
    assert "_evict_session_agent(body[\"session_id\"])" in ROUTES
    handler = ROUTES.split('if parsed.path == "/api/session/update":', 1)[1].split('if parsed.path == "/api/session/worktree/remove":', 1)[0]
    assert handler.index('raw_effort =') < handler.index('s.workspace = new_ws')


def test_reasoning_status_checks_session_profile_visibility():
    handler = ROUTES.split('if parsed.path == "/api/reasoning":', 1)[1].split('if parsed.path == "/api/onboarding/status":', 1)[0]
    assert "_session_id_visible_to_request_profile" in handler


def test_duplicate_and_fork_inherit_session_reasoning_effort():
    assert ROUTES.count('reasoning_effort=getattr(') >= 3


def test_streaming_uses_session_effort_before_resolved_model_config():
    assert "getattr(s, 'reasoning_effort', None)" in STREAMING
    assert "resolve_effective_reasoning_effort" in STREAMING


def test_reasoning_chip_queries_session_and_writes_session_update():
    assert "params.set('session_id',session.session_id)" in UI
    assert "api('/api/session/update'" in UI
    assert "reasoning_effort:effort" in UI


def test_reasoning_chip_ignores_response_after_session_switch():
    fetch = UI.split("function fetchReasoningChip(keyOverride){", 1)[1].split(
        "function refreshProfileTransitionReasoningChip", 1
    )[0]
    assert "const requestedSessionId=" in fetch
    assert fetch.count("currentSessionId!==requestedSessionId") == 2


def test_reasoning_mutation_ignores_success_and_failure_after_session_switch(tmp_path):
    """A session A mutation settling on session B must have no visible effect."""
    reasoning_start = UI.index("// ── Reasoning effort chip")
    handler_start = UI.index("document.addEventListener('click',function(e){", reasoning_start)
    handler_end = UI.index("// ── Session toolsets chip", handler_start)
    handler = UI[handler_start:handler_end]
    script = tmp_path / "reasoning_navigation_race.js"
    script.write_text(
        f"""
const handlers={{}};
global.document={{addEventListener:(name, fn)=>{{handlers[name]=fn;}}}};
global.closeReasoningDropdown=()=>{{}};
global._reasoningEffortContext=()=>({{}});
global.fetchReasoningChip=()=>{{effects.push('fetch');}};
global._applyReasoningChip=(effort)=>{{effects.push('apply:'+effort);}};
global.showToast=(message)=>{{effects.push('toast:'+message);}};
let resolveRequest, rejectRequest;
global.api=()=>new Promise((resolve,reject)=>{{resolveRequest=resolve;rejectRequest=reject;}});
global.S={{session:{{session_id:'session-a',reasoning_effort:null}}}};
const effects=[];
eval({json.dumps(handler)});
const option={{dataset:{{effort:'high'}}}};
const target={{closest:(selector)=>selector==='.reasoning-option'?option:null}};
handlers.click({{target}});
const originalSession=S.session;
S.session={{session_id:'session-b',reasoning_effort:'low'}};
resolveRequest({{reasoning_effort:'high'}});
setImmediate(()=>{{
  if(originalSession.reasoning_effort!==null) throw new Error('stale owner session mutated');
  if(S.session.reasoning_effort!=='low') throw new Error('active session mutated');
  if(effects.length) throw new Error('stale success produced UI effects: '+effects.join(','));
  S.session={{session_id:'session-a',reasoning_effort:null}};
  handlers.click({{target}});
  S.session={{session_id:'session-b',reasoning_effort:'low'}};
  rejectRequest(new Error('late failure'));
  setImmediate(()=>{{
    if(effects.length) throw new Error('stale failure produced UI effects: '+effects.join(','));
  }});
}});
""",
        encoding="utf-8",
    )
    subprocess.run(["node", str(script)], check=True, timeout=10)


def test_clearing_session_effort_refetches_effective_model_override():
    click_handler = UI.split("if(e.target.closest('.reasoning-option')){", 1)[1].split("// ── Session toolsets chip", 1)[0]
    assert "if(!effort) fetchReasoningChip()" in click_handler
