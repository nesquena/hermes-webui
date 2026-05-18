"""Tests for Capy autonomy/security/model-routing policy status."""
import io
import json
from urllib.parse import urlparse

from api.capy_policy import policy_status


def test_policy_status_defaults_to_supervised_metadata_only(monkeypatch):
    monkeypatch.delenv("CAPY_AUTONOMY_MODE", raising=False)

    status = policy_status()

    assert status == {
        "available": True,
        "mode": "supervised",
        "label": "Supervised",
        "summary": "Approval required before writes, mutations, network side effects, creator commits, and sandboxed widget execution.",
        "approval_gates": [
            "creator_commit",
            "destructive_external_action",
            "generated_widget_execution",
            "credential_change",
        ],
        "prompt_preflight": {
            "status": "required",
            "protected_boundaries": [
                "creator_preview",
                "creator_commit",
                "widget_runtime_prompt",
                "auto_fetched_source",
            ],
        },
        "model_routing": {
            "status": "configured_by_hermes",
            "default_hint": "hint:reasoning",
            "safe_fallback": "current Hermes provider",
        },
        "local_only": True,
    }


def test_policy_status_accepts_semiautonomous_env_without_echoing_hostile_values(monkeypatch):
    monkeypatch.setenv("CAPY_AUTONOMY_MODE", "semi_autonomous")
    monkeypatch.setenv("CAPY_AUTONOMY_LABEL", "renderer <script>bad()</script> SECRET_VALUE_DO_NOT_LEAK")

    status = policy_status()
    serialized = json.dumps(status, sort_keys=True).lower()

    assert status["mode"] == "semi_autonomous"
    assert status["label"] == "Semi-autonomous"
    assert "safe reads" in status["summary"].lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "api_key" not in serialized


class _RouteHandler:
    def __init__(self):
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.headers = {"Accept-Encoding": "", "Host": "127.0.0.1:8787"}
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_capy_policy_status_route_returns_metadata_only_status(monkeypatch):
    import api.routes as routes

    monkeypatch.setenv("CAPY_AUTONOMY_MODE", "autonomous")
    handler = _RouteHandler()

    handled = routes.handle_get(handler, urlparse("/api/capy-policy/status"))

    assert handled is None
    assert handler.status == 200
    body = handler.json_body()
    assert body["mode"] == "autonomous"
    assert body["label"] == "Autonomous"
    assert body["local_only"] is True
    assert "credential_change" in body["approval_gates"]
    serialized = json.dumps(body, sort_keys=True).lower()
    assert "api_key" not in serialized
    assert "secret" not in serialized
    assert "<script" not in serialized
