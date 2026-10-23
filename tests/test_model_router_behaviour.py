"""Behavioural tests for the model-router backend contract (PR #7146 review fixes).

Pins:
  * settings.model_scheduler_enabled is the single authority for enabled/status
  * recommend refuses when disabled, even if policy.enabled is true
  * recommend is POST-only with {text, message_count, session_id} JSON body
  * missing model-scheduler dependency degrades gracefully
  * prompt is not accepted via GET query URL
  * model_router.js delegates option selection to ui.js _ensureModelOptionInDropdown
"""
from __future__ import annotations

import inspect
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import model_router
from api import config as api_config

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeModelScheduler:
    QUOTA_WINDOW_SECONDS = 3600

    def __init__(self, policy_enabled: bool = False):
        self.policy_enabled = policy_enabled
        self.recommend_calls: list[tuple[str, int, str | None]] = []

    def get_policy(self):
        return {
            "enabled": self.policy_enabled,
            "schedule": [{"start": "09:00", "end": "12:00"}],
        }

    def list_models(self):
        return [{"id": "gpt-5", "provider": "openai"}]

    def recommend_for_session(self, text, message_count=0, session_id=None):
        self.recommend_calls.append((text, message_count, session_id))
        return {"model": "gpt-5", "provider": "openai", "reason": "test"}


def test_status_uses_settings_as_single_gate(monkeypatch):
    """settings.model_scheduler_enabled 是 enabled 的唯一权威。"""
    fake = FakeModelScheduler(policy_enabled=False)
    monkeypatch.setattr(model_router, "_load_lib", lambda: fake)
    monkeypatch.setattr(api_config, "load_settings", lambda: {"model_scheduler_enabled": True})

    assert model_router.get_status()["enabled"] is True

    monkeypatch.setattr(api_config, "load_settings", lambda: {"model_scheduler_enabled": False})
    assert model_router.get_status()["enabled"] is False


def test_get_policy_uses_settings_as_single_gate(monkeypatch):
    """get_policy 的 enabled 只读 settings，不再与 policy.enabled 做 conjunction。"""
    fake = FakeModelScheduler(policy_enabled=False)
    monkeypatch.setattr(model_router, "_load_lib", lambda: fake)
    monkeypatch.setattr(api_config, "load_settings", lambda: {"model_scheduler_enabled": True})

    policy = model_router.get_policy()
    assert policy["enabled"] is True


def test_recommend_refuses_when_settings_off(monkeypatch):
    """disabled-backend 拒绝：settings 关闭时 recommend 不产生推荐。"""
    fake = FakeModelScheduler(policy_enabled=True)
    monkeypatch.setattr(model_router, "_load_lib", lambda: fake)
    monkeypatch.setattr(api_config, "load_settings", lambda: {"model_scheduler_enabled": False})

    out = model_router.recommend("hello", message_count=2, session_id="sess-1")
    assert out["model"] == ""
    assert out["reason"] == "model scheduler disabled"
    assert fake.recommend_calls == []


def test_recommend_passes_message_count_and_session_id(monkeypatch):
    """settings 开启时，message_count 与 session_id 透传给 scheduler。"""
    fake = FakeModelScheduler(policy_enabled=True)
    monkeypatch.setattr(model_router, "_load_lib", lambda: fake)
    monkeypatch.setattr(api_config, "load_settings", lambda: {"model_scheduler_enabled": True})

    out = model_router.recommend("hello", message_count=7, session_id="sess-1")
    assert fake.recommend_calls == [("hello", 7, "sess-1")]
    assert out["model"] == "gpt-5"


def test_status_reports_missing_dependency(monkeypatch):
    """缺依赖：model-scheduler 未安装时 status 降级且 enabled 为 False。"""
    monkeypatch.setattr(model_router, "_load_lib", lambda: None)
    monkeypatch.setattr(api_config, "load_settings", lambda: {"model_scheduler_enabled": True})

    status = model_router.get_status()
    assert status["enabled"] is False
    assert "not installed" in status["error"]


def test_recommend_route_is_post_json(monkeypatch):
    """POST /api/model-router/recommend 解析 JSON body 并传给 model_router.recommend。"""
    import api.routes as routes

    captured = {}

    def fake_recommend(text, message_count=0, session_id=None):
        captured.update(text=text, message_count=message_count, session_id=session_id)
        return {"model": "gpt-5", "provider": "openai", "reason": "ok"}

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "j", lambda *args, **kwargs: True)
    monkeypatch.setattr(model_router, "recommend", fake_recommend)

    body = {"text": "hello", "message_count": 4, "session_id": "sess-1"}
    body_bytes = json.dumps(body).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body_bytes))},
        rfile=BytesIO(body_bytes),
    )

    assert routes.handle_post(handler, SimpleNamespace(path="/api/model-router/recommend")) is True
    assert captured == {"text": "hello", "message_count": 4, "session_id": "sess-1"}


def test_recommend_is_not_routed_as_get():
    """prompt 不再进入 GET URL：handle_get 中没有 recommend 路由。"""
    import api.routes as routes

    assert "/api/model-router/recommend" not in inspect.getsource(routes.handle_get)
    assert "/api/model-router/recommend" in inspect.getsource(routes.handle_post)


def test_recommend_prompt_is_not_in_url():
    """前端源码不再用 URLSearchParams 拼 text 到 GET URL。"""
    src = (REPO_ROOT / "static" / "model_router.js").read_text(encoding="utf-8")
    assert "/api/model-router/recommend?" not in src
    assert "URLSearchParams({ text:" not in src


def test_model_router_uses_upstream_ensure_function():
    """删除自写 selector 逻辑，统一走 ui.js _ensureModelOptionInDropdown。"""
    src = (REPO_ROOT / "static" / "model_router.js").read_text(encoding="utf-8")
    assert "_ensureModelOptionInDropdown(" in src
    assert "_mrFindOption" not in src
    assert "_mrToSelectValue" not in src
