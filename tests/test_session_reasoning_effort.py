"""Session-scoped reasoning effort with per-model automatic fallback."""

from pathlib import Path
import inspect

import api.config as config
from api.models import Session


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STREAMING = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


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


def test_clearing_session_effort_refetches_effective_model_override():
    click_handler = UI.split("if(e.target.closest('.reasoning-option')){", 1)[1].split("// ── Session toolsets chip", 1)[0]
    assert "if(!effort) fetchReasoningChip()" in click_handler
