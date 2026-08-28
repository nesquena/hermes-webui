"""Runtime regressions for session model/provider base-URL transitions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_BOOT_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3]);
const start = src.indexOf("$('modelSelect').onchange=async()=>{");
const end = src.indexOf("$('msg').addEventListener", start);
if (start < 0 || end < 0) throw new Error('model onchange handler not found');
const handlerSource = src.slice(start, end);

const select = {value: args.newModel, onchange: null};
const sandbox = {
  select,
  S: {
    session: {
      session_id: 'session-1',
      workspace: '/tmp/workspace',
      model: args.oldModel,
      model_provider: args.oldProvider,
      base_url: args.oldBaseUrl,
    },
  },
  calls: [],
  localStorage: {setItem() {}},
  clearProfileTransitionReasoningContext() {},
  closeModelDropdown() {},
  _writePersistedModelState() {},
  _rememberPendingSessionModel() {},
  syncModelChip() {},
  syncReasoningChip() {},
  syncTopbar() {},
  showToast() {},
  t(key) { return key; },
  _applySessionContextMetadataUpdate() {},
};
sandbox.$ = function(id) { return id === 'modelSelect' ? sandbox.select : null; };
sandbox._modelStateForSelect = function() {
  return {model: args.newModel, model_provider: args.newProvider};
};
sandbox.api = function(url, options) {
  sandbox.calls.push({url, body: JSON.parse(options.body)});
  return new Promise(() => {});
};
vm.createContext(sandbox);
vm.runInContext(handlerSource, sandbox);
sandbox.select.onchange();
process.stdout.write(JSON.stringify({session: sandbox.S.session, calls: sandbox.calls}));
"""


def _run_model_change(driver: Path, *, old_provider: str, new_provider: str) -> dict:
    payload = {
        "oldModel": "old-model",
        "oldProvider": old_provider,
        "oldBaseUrl": "http://old-provider.test/v1",
        "newModel": "new-model",
        "newProvider": new_provider,
    }
    result = subprocess.run(
        [str(NODE), str(driver), str(BOOT_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_browser_model_change_clears_base_url_only_when_provider_changes(tmp_path):
    driver = tmp_path / "boot-model-change.js"
    driver.write_text(_BOOT_DRIVER, encoding="utf-8")

    changed = _run_model_change(
        driver, old_provider="lmstudio", new_provider="openai"
    )
    retained = _run_model_change(
        driver, old_provider="lmstudio", new_provider="lmstudio"
    )

    assert changed["session"]["base_url"] is None
    assert retained["session"]["base_url"] == "http://old-provider.test/v1"


def test_session_update_clears_base_url_only_when_provider_changes(monkeypatch, tmp_path):
    from threading import RLock
    from types import SimpleNamespace

    import api.config as config
    import api.routes as routes
    from api.models import Session

    sessions = {
        "changed": Session(
            session_id="changed",
            title="Changed",
            workspace=str(tmp_path),
            model="old-model",
            model_provider="lmstudio",
            base_url="http://old-provider.test/v1",
        ),
        "retained": Session(
            session_id="retained",
            title="Retained",
            workspace=str(tmp_path),
            model="old-model",
            model_provider="lmstudio",
            base_url="http://old-provider.test/v1",
        ),
    }
    for session in sessions.values():
        monkeypatch.setattr(session, "save", lambda: None)

    current_body = {}
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(routes, "read_body", lambda _handler: current_body)
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        routes, "_get_or_materialize_session", lambda session_id: sessions[session_id]
    )
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _session_id: RLock())
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: workspace)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        routes, "_resolve_context_length_for_session_model", lambda *_args: 128_000
    )
    monkeypatch.setattr(config, "_evict_session_agent", lambda _session_id: None)

    def fake_json(_handler, payload, status=200, extra_headers=None):
        captured.clear()
        captured.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", fake_json)

    def update(session_id, provider):
        current_body.clear()
        current_body.update(
            session_id=session_id,
            workspace=str(tmp_path),
            model="new-model",
            model_provider=provider,
        )
        assert routes.handle_post(
            SimpleNamespace(command="POST", headers={}),
            SimpleNamespace(path="/api/session/update"),
        ) is True
        return captured["payload"]["session"]

    changed_response = update("changed", "openai")
    retained_response = update("retained", "lmstudio")

    assert sessions["changed"].base_url is None
    assert changed_response["base_url"] is None
    assert sessions["retained"].base_url == "http://old-provider.test/v1"
    assert retained_response["base_url"] == "http://old-provider.test/v1"
