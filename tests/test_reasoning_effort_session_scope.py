import json
from pathlib import Path
from types import SimpleNamespace

from api.models import Session


ROOT = Path(__file__).resolve().parents[1]


def test_reasoning_effort_is_session_scoped(tmp_path, monkeypatch):
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)

    low = Session(session_id="reasoning-low", reasoning_effort="low")
    high = Session(session_id="reasoning-high", reasoning_effort="high")
    low.save()
    high.save()

    assert Session(**json.loads(low.path.read_text())).reasoning_effort == "low"
    assert Session(**json.loads(high.path.read_text())).reasoning_effort == "high"

    ui = (ROOT / "static" / "ui.js").read_text()
    streaming = (ROOT / "api" / "streaming.py").read_text()
    gateway = (ROOT / "api" / "gateway_chat.py").read_text()

    assert "ctx.session_id=S.session.session_id" in ui
    assert "getattr(_session_meta, 'reasoning_effort', None)" in streaming
    assert 'getattr(s, "reasoning_effort", None)' in gateway


def test_reasoning_status_prefers_session_override(monkeypatch):
    import api.config as config

    monkeypatch.setattr(
        config,
        "_load_yaml_config_file",
        lambda _path: {"agent": {"reasoning_effort": "high"}},
    )
    monkeypatch.setattr(config, "resolve_model_reasoning_efforts", lambda *_args, **_kwargs: ["low", "high"])
    monkeypatch.setattr(config, "_zai_glm_thinking_toggle_supported", lambda *_args: None)

    assert config.get_reasoning_status(model_id="test-model")["reasoning_effort"] == "high"
    assert config.get_reasoning_status(model_id="test-model", reasoning_effort="low")["reasoning_effort"] == "low"
    assert config.get_reasoning_status(model_id="test-model", reasoning_effort="")["reasoning_effort"] == ""


def test_reasoning_post_updates_only_target_session(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    low = Session(session_id="reasoning-low", reasoning_effort="low")
    high = Session(session_id="reasoning-high", reasoning_effort="high")
    low.save()
    high.save()

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": low.session_id, "effort": "medium"},
    )
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid: low if sid == low.session_id else high)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(routes, "get_reasoning_status", lambda **kwargs: kwargs)
    responses = []
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: responses.append(payload) or True)

    assert routes.handle_post(SimpleNamespace(), SimpleNamespace(path="/api/reasoning")) is True
    assert low.reasoning_effort == "medium"
    assert high.reasoning_effort == "high"
    assert responses[-1]["reasoning_effort"] == "medium"


def test_gateway_prefers_session_override(monkeypatch):
    import api.gateway_chat as gateway_chat

    monkeypatch.setattr(gateway_chat, "coerce_reasoning_effort_for_model", lambda effort, *_args, **_kwargs: effort)
    cfg = {"agent": {"reasoning_effort": "high"}}

    assert gateway_chat._gateway_reasoning_effort_for_request(cfg) == "high"
    assert gateway_chat._gateway_reasoning_effort_for_request(cfg, reasoning_effort="low") == "low"
    assert gateway_chat._gateway_reasoning_effort_for_request(cfg, reasoning_effort="") is None
