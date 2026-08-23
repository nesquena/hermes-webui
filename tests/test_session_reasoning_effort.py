"""Session-scoped reasoning effort with per-model automatic fallback."""

from pathlib import Path
import inspect
import json
import re
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
    # Post-#7083 master: GPT-5.6 (and its Sol/Terra/Luna variants) natively
    # accepts ``max`` on OpenAI-family lanes; only Agent's ``ultra`` extension
    # is still unsupported there. A session ``ultra`` therefore degrades to the
    # highest provider-supported level — ``max`` — not all the way to xhigh.
    assert _gateway_reasoning_effort_for_request(
        cfg, model="gpt-5.6-sol", model_provider="openai-codex", session_effort="ultra"
    ) == "max"
    assert _gateway_reasoning_effort_for_request(
        cfg, model="gpt-5.6-sol", model_provider="openai-codex"
    ) == "xhigh"
    # Pre-5.6 GPT-5 keeps the xhigh ceiling: neither max nor ultra is accepted.
    assert _gateway_reasoning_effort_for_request(
        cfg, model="gpt-5.2", model_provider="openai-codex", session_effort="ultra"
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


def test_session_effort_survives_switch_fallback_and_next_turn_primary_restore(monkeypatch):
    """The full production lifecycle, including the next-turn primary restore.

    The installed Agent's ``switch_model()`` snapshots the value it resolved
    from profile/per-model config into ``_primary_runtime['reasoning_config']``,
    and ``restore_primary_runtime()`` copies that snapshot back at the START of
    a later turn — after WebUI's already-bound guard has returned. Rebinding
    only the live ``agent.reasoning_config`` therefore lost the session value
    one turn later, silently reverting to the global/per-model default.

    Global default is 'minimal' and the per-model default is 'medium' here, so
    a regression cannot pass by coincidentally matching either one.
    """

    class Agent:
        model = "primary"
        provider = "provider"
        base_url = None
        reasoning_config = {"enabled": True, "effort": "minimal"}

        def __init__(self):
            # Snapshot as the Agent builds it: the per-model/global value, NOT
            # the session override.
            self._primary_runtime = {
                "model": "primary",
                "provider": "provider",
                "reasoning_config": {"enabled": True, "effort": "medium"},
            }

        def switch_model(self, model, provider):
            self.model = model
            self.provider = provider
            self.reasoning_config = {"enabled": True, "effort": "medium"}
            self._primary_runtime["model"] = model
            self._primary_runtime["provider"] = provider
            self._primary_runtime["reasoning_config"] = {"enabled": True, "effort": "medium"}

        def _try_activate_fallback(self):
            self.model = "fallback"
            self.reasoning_config = {"enabled": True, "effort": "minimal"}
            return True

        def restore_primary_runtime(self):
            rt = self._primary_runtime
            self.model = rt["model"]
            self.provider = rt["provider"]
            saved = rt.get("reasoning_config")
            if saved is not None:
                self.reasoning_config = dict(saved)
            return True

    monkeypatch.setattr(
        config,
        "resolve_model_reasoning_efforts",
        lambda *a, **k: list(config.VALID_REASONING_EFFORTS),
    )
    agent = Agent()
    _bind_session_reasoning_effort(agent, {}, "high")

    # Turn 1: user switches model, then the primary rate-limits and the
    # fallback activates.
    agent.switch_model("switched", "other-provider")
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert agent._try_activate_fallback() is True
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}

    # Turn 2: the Agent restores the primary runtime from its snapshot. The
    # session override must still win over the snapshotted default.
    assert agent.restore_primary_runtime() is True
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}, (
        "restore_primary_runtime() copied the stale per-model snapshot back "
        "over the session override"
    )
    assert agent._primary_runtime["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_reasoning_command_parity_includes_ultra():
    """/reasoning must accept every level the composer menu and backend expose."""
    commands = (Path(__file__).resolve().parent.parent / "static" / "commands.js").read_text(
        encoding="utf-8"
    )
    assert 'data-effort="ultra"' in INDEX, "composer menu should expose Ultra"
    assert "ultra" in config.VALID_REASONING_EFFORTS

    reasoning_entry = commands.split("{name:'reasoning'", 1)[1].split("},", 1)[0]
    assert "'ultra'" in reasoning_entry, (
        "/reasoning subArgs/autocomplete must list ultra, otherwise the command "
        "surface silently rejects a level the UI and backend both accept"
    )

    body = commands.split("function cmdReasoning", 1)[1].split("\nfunction ", 1)[0]
    efforts = re.search(r"const EFFORTS=\[(.*?)\];", body)
    assert efforts and "'ultra'" in efforts.group(1), "cmdReasoning parser must accept ultra"
    assert "xhigh|max|ultra" in body, "/reasoning help text must advertise ultra"


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
global._reasoningMutationSeqBySession={{}};
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


def test_same_session_rapid_selections_settle_in_dispatch_order(tmp_path):
    """Greptile P1: an OLDER same-session mutation settling LAST must be ignored.

    Drives the real click handler twice for one session ('low' then 'high'),
    resolves the requests in reverse order, and asserts the newest selection
    owns the chip, session value, and toast — with no late overwrite.
    """
    reasoning_start = UI.index("// ── Reasoning effort chip")
    handler_start = UI.index("document.addEventListener('click',function(e){", reasoning_start)
    handler_end = UI.index("// ── Session toolsets chip", handler_start)
    handler = UI[handler_start:handler_end]
    script = tmp_path / "reasoning_same_session_order.js"
    script.write_text(
        f"""
const handlers={{}};
global.document={{addEventListener:(name, fn)=>{{handlers[name]=fn;}}}};
global.closeReasoningDropdown=()=>{{}};
global._reasoningEffortContext=()=>({{}});
global._reasoningMutationSeqBySession={{}};
global.fetchReasoningChip=()=>{{effects.push('fetch');}};
global._applyReasoningChip=(effort)=>{{effects.push('apply:'+effort);}};
global.showToast=(message)=>{{effects.push('toast:'+message);}};
const settlers=[];
global.api=()=>new Promise((resolve,reject)=>{{settlers.push({{resolve,reject}});}});
global.S={{session:{{session_id:'session-a',reasoning_effort:null}}}};
const effects=[];
eval({json.dumps(handler)});
const clickOn=(effort)=>{{
  const option={{dataset:{{effort}}}};
  const target={{closest:(selector)=>selector==='.reasoning-option'?option:null}};
  handlers.click({{target}});
}};
clickOn('low');
clickOn('high');
if(settlers.length!==2) throw new Error('expected two dispatched mutations, got '+settlers.length);
// Newer request ('high') settles FIRST…
settlers[1].resolve({{reasoning_effort:'high'}});
setImmediate(()=>{{
  if(S.session.reasoning_effort!=='high') throw new Error('newest selection not applied: '+S.session.reasoning_effort);
  if(!effects.includes('apply:high')) throw new Error('newest selection did not update chip: '+effects.join(','));
  const before=effects.slice();
  // …then the OLDER request ('low') completes last and must be a no-op.
  settlers[0].resolve({{reasoning_effort:'low'}});
  setImmediate(()=>{{
    if(S.session.reasoning_effort!=='high') throw new Error('stale success overwrote session value: '+S.session.reasoning_effort);
    if(effects.length!==before.length) throw new Error('stale success produced UI effects: '+effects.slice(before.length).join(','));
    // Same ordering rule for a stale FAILURE: no misleading error toast.
    const failEffects=effects.length;
    clickOn('medium');
    clickOn('xhigh');
    settlers[3].resolve({{reasoning_effort:'xhigh'}});
    setImmediate(()=>{{
      const afterNewest=effects.length;
      settlers[2].reject(new Error('late failure of superseded request'));
      setImmediate(()=>{{
        if(S.session.reasoning_effort!=='xhigh') throw new Error('stale failure disturbed session value');
        if(effects.length!==afterNewest) throw new Error('stale failure produced UI effects: '+effects.slice(afterNewest).join(','));
      }});
    }});
  }});
}});
""",
        encoding="utf-8",
    )
    subprocess.run(["node", str(script)], check=True, timeout=10)


def test_reasoning_dropdown_is_viewport_bounded_and_scrollable():
    """With Ultra the upward-opening menu is nine rows; on a short viewport it
    must cap to the visual viewport and scroll instead of clipping rows
    (overflow:hidden with no max-height made Ultra unreachable)."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    rule_start = css.index(".composer-reasoning-dropdown{")
    rule = css[rule_start:css.index("}", rule_start)]
    assert "max-height:min(60dvh,360px)" in rule
    assert "overflow-y:auto" in rule
    assert "overflow:hidden" not in rule


def test_reasoning_dropdown_short_viewport_keeps_all_options_reachable(tmp_path):
    """Short-viewport coverage: simulate a 300px-tall visual viewport against
    the dropdown's computed cap and prove every option row — including the
    ninth (Ultra) — lands inside the scrollable range."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    rule_start = css.index(".composer-reasoning-dropdown{")
    rule = css[rule_start:css.index("}", rule_start)]
    option_count = INDEX.count('class="reasoning-option"')
    assert option_count == 9  # Default..Ultra after the 'ultra' sync
    script = tmp_path / "reasoning_dropdown_short_viewport.js"
    script.write_text(
        f"""
const rule={json.dumps(rule)};
const optionCount={option_count};
const capMatch=rule.match(/max-height:min\\((\\d+)dvh,(\\d+)px\\)/);
if(!capMatch) throw new Error('viewport-relative max-height missing: '+rule);
if(!/overflow-y:auto/.test(rule)) throw new Error('dropdown must scroll vertically');
const dvhCap=Number(capMatch[1]);
const pxCap=Number(capMatch[2]);
// Mobile-landscape-ish short viewport.
const viewportHeight=300;
const maxHeight=Math.min(viewportHeight*dvhCap/100, pxCap);
if(maxHeight>viewportHeight) throw new Error('menu taller than the viewport: '+maxHeight);
// Row metrics from .reasoning-option: 8px vertical padding x2 + 13px text line.
const rowHeight=8+13+8;
const contentHeight=optionCount*rowHeight+8; // + dropdown 4px padding x2
if(contentHeight<=maxHeight) throw new Error('scenario must overflow to exercise scrolling');
// Scrollable box: every row's offset must be reachable within scrollRange.
const scrollRange=contentHeight-maxHeight;
const lastRowTop=(optionCount-1)*rowHeight+4;
if(lastRowTop>scrollRange+maxHeight-rowHeight) throw new Error('last option (Ultra) unreachable when scrolled to bottom');
""",
        encoding="utf-8",
    )
    subprocess.run(["node", str(script)], check=True, timeout=10)
