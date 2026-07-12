import json
from pathlib import Path
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 1
    cursor = brace + 1
    while depth and cursor < len(source):
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    return source[brace + 1 : cursor - 1]


def test_browser_effort_validation_is_strict_and_optional():
    from api.routes import _normalize_run_reasoning_effort

    assert _normalize_run_reasoning_effort(None) is None
    assert _normalize_run_reasoning_effort("") is None
    assert _normalize_run_reasoning_effort(" XHigh ") == "xhigh"
    assert _normalize_run_reasoning_effort(" ULTRA ") == "ultra"
    assert _normalize_run_reasoning_effort("none") == "none"

    with pytest.raises(ValueError, match="Unknown reasoning effort"):
        _normalize_run_reasoning_effort("extreme")


def test_explicit_effort_snapshot_wins_without_reading_profile_config(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes.api_config,
        "get_config",
        lambda: (_ for _ in ()).throw(AssertionError("explicit effort must not read config")),
    )
    monkeypatch.setattr(
        routes.api_config,
        "coerce_reasoning_effort_for_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("explicit effort is coerced downstream for the run model")
        ),
    )

    assert routes._snapshot_run_reasoning_effort(
        " XHigh ",
        profile_config={"agent": {"reasoning_effort": "low"}},
        model="test-model",
        model_provider="test-provider",
    ) == "xhigh"


def test_omitted_effort_snapshots_coerced_profile_value(monkeypatch):
    import api.routes as routes

    calls = []
    profile_config = {"agent": {"reasoning_effort": "max"}}

    def coerce(effort, model, *, provider_id=None):
        calls.append((effort, model, provider_id))
        return "xhigh"

    monkeypatch.setattr(routes.api_config, "coerce_reasoning_effort_for_model", coerce)

    snapshot = routes._snapshot_run_reasoning_effort(
        None,
        profile_config=profile_config,
        model="gpt-5",
        model_provider="openai-codex",
    )
    profile_config["agent"]["reasoning_effort"] = "low"

    assert snapshot == "xhigh"
    assert calls == [("max", "gpt-5", "openai-codex")]


def test_omitted_effort_reads_default_config_when_no_profile_config(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes.api_config,
        "get_config",
        lambda: {"agent": {"reasoning_effort": "medium"}},
    )
    monkeypatch.setattr(
        routes.api_config,
        "coerce_reasoning_effort_for_model",
        lambda effort, model, *, provider_id=None: effort,
    )

    assert routes._snapshot_run_reasoning_effort(
        "",
        profile_config=None,
        model="test-model",
        model_provider="test-provider",
    ) == "medium"


def test_chat_start_passes_accepted_profile_effort_snapshot(monkeypatch, tmp_path):
    import api.routes as routes

    session = SimpleNamespace(
        session_id="reasoning-fallback-snapshot",
        workspace=str(tmp_path),
        model="gpt-5",
        model_provider="openai-codex",
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
    )
    profile_config = {"agent": {"reasoning_effort": "max"}}
    captured = {}

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda *_args: str(tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda *_args: ("openai-codex", "gpt-5", profile_config),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("gpt-5", "openai-codex", False),
    )
    monkeypatch.setattr(
        routes,
        "_repair_foreign_session_model_provider",
        lambda *_args, **_kwargs: "openai-codex",
    )
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *_args: True)
    monkeypatch.setattr(
        routes.api_config,
        "coerce_reasoning_effort_for_model",
        lambda effort, *_args, **_kwargs: "xhigh" if effort == "max" else effort,
    )
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda _session, **kwargs: captured.update(kwargs) or {"stream_id": "snapshot-stream"},
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    routes._handle_chat_start(
        None,
        {"session_id": session.session_id, "message": "use the accepted default"},
    )
    profile_config["agent"]["reasoning_effort"] = "low"

    assert captured["reasoning_effort"] == "xhigh"


def test_gateway_request_effort_wins_over_shared_profile_default(monkeypatch):
    import api.gateway_chat as gateway_chat

    monkeypatch.setattr(
        gateway_chat,
        "coerce_reasoning_effort_for_model",
        lambda effort, *args, **kwargs: effort,
    )
    cfg = {"agent": {"reasoning_effort": "medium"}}

    assert gateway_chat._gateway_reasoning_effort_for_request(
        cfg,
        model="test-model",
        model_provider="test-provider",
        reasoning_effort="xhigh",
    ) == "xhigh"
    assert gateway_chat._gateway_reasoning_effort_for_request(
        cfg,
        model="test-model",
        model_provider="test-provider",
    ) == "medium"


def test_concurrent_session_starts_keep_independent_efforts(tmp_path, monkeypatch):
    from api.models import Session
    import api.routes as routes

    started = []

    class CapturingThread:
        def __init__(self, *, target, args, kwargs, daemon):
            started.append({"target": target, "args": args, "kwargs": kwargs, "daemon": daemon})

        def start(self):
            return None

    monkeypatch.setattr(Session, "save", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: True)
    monkeypatch.setattr(routes.threading, "Thread", CapturingThread)

    responses = []
    try:
        for session_id, effort in (("run-effort-low", "low"), ("run-effort-xhigh", "xhigh")):
            responses.append(
                routes._start_chat_stream_for_session(
                    Session(session_id=session_id, title="Untitled"),
                    msg=f"Use {effort}",
                    attachments=[],
                    workspace=str(tmp_path),
                    model="test-model",
                    model_provider="test-provider",
                    reasoning_effort=effort,
                )
            )
    finally:
        for response in responses:
            stream_id = response.get("stream_id")
            routes.STREAMS.pop(stream_id, None)
            routes.unregister_stream_owner(stream_id)

    assert [call["kwargs"]["reasoning_effort"] for call in started] == ["low", "xhigh"]
    assert all(call["target"] is routes._run_gateway_chat_streaming for call in started)


def test_browser_snapshots_immediate_and_queued_turn_effort():
    messages = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    commands = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")

    assert "reasoning_effort:reasoningEffortForSend||undefined" in messages
    assert messages.count("reasoning_effort:reasoningEffortForSend||undefined") >= 4
    assert "reasoning_effort:_targetReasoningEffort||undefined" in messages
    assert "_sendInProgressReasoningEffort=reasoningEffortForSend" in messages
    assert "send({reasoningEffort:next.reasoning_effort})" in ui
    assert "window.getComposerReasoningEffortForRun=getComposerReasoningEffortForRun" in ui
    assert "typeof getComposerReasoningEffortForRun==='function'" in commands
    assert "reasoning_effort:ownerReasoningEffort||undefined" in commands


def test_queue_helper_snapshots_active_effort_without_overwriting_explicit_value():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    helper = _function_body(ui, "_reasoningEffortForQueuedMessage")
    script = textwrap.dedent(
        f"""
        const S = {{session: {{session_id: 'active'}}}};
        const window = {{getComposerReasoningEffortForRun: () => 'low'}};
        function resolve(sid, payload) {{{helper}}}
        console.log(JSON.stringify([
          resolve('active', {{}}),
          resolve('active', {{reasoning_effort: 'xhigh'}}),
          resolve('background', {{}}) ?? null
        ]));
        """
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["low", "xhigh", None]


_MODEL_SWITCH_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++;
  }
  return src.slice(start, i);
}

function makeEl() {
  return {
    style: {}, dataset: {}, title: '', textContent: '', value: '',
    classList: { add(){}, remove(){}, toggle(){}, contains(){return false} },
    setAttribute(){}, querySelectorAll(){return []}, querySelector(){return null},
  };
}

const els = {
  composerReasoningWrap: makeEl(),
  composerReasoningLabel: makeEl(),
  composerReasoningChip: makeEl(),
  composerReasoningDropdown: makeEl(),
  modelSelect: makeEl(),
};
global.S = {
  activeProfile: 'default',
  session: {
    session_id: 'switch-race', profile: 'default',
    model: 'gpt-5', model_provider: 'openai-codex',
  },
};
global.window = {};
global.document = {addEventListener(){}, querySelector(){return null}};
global.$ = id => els[id] || null;
global._modelStateForSelect = () => ({model_provider: ''});
global._highlightReasoningOption = () => {};
global._applyReasoningOptions = () => {};
global.showToast = () => {};

let requests = [];
global.api = (url, options) => new Promise((resolve, reject) => {
  requests.push({url, options: options || null, resolve, reject});
});

var _currentReasoningEffort = null;
var _currentReasoningEffortsSupported = null;
var _currentReasoningEffortKey = null;
var _profileTransitionReasoningContext = null;
var _pendingReasoningEffortSelection = null;
var _reasoningEffortWriteSeq = 0;
var _lastReasoningFetchKey = null;
var _reasoningFetchSeq = 0;

for (const name of [
  '_normalizeReasoningEffort',
  '_formatReasoningEffortLabel',
  '_reasoningEffortContext',
  '_reasoningEffortQuery',
  '_reasoningEffortProfileKey',
  '_reasoningEffortStateKey',
  'getComposerReasoningEffortForRun',
  '_applyReasoningChip',
  'fetchReasoningChip',
  '_invalidateReasoningChipForKey',
  'syncReasoningChip',
  '_setComposerReasoningEffort',
]) eval(extractFunc(name));

const flush = () => new Promise(resolve => setImmediate(resolve));
const reset = () => {
  S.activeProfile = 'default';
  S.session = {
    session_id: 'switch-race', profile: 'default',
    model: 'gpt-5', model_provider: 'openai-codex',
  };
  requests = [];
  _currentReasoningEffort = null;
  _currentReasoningEffortsSupported = null;
  _currentReasoningEffortKey = null;
  _profileTransitionReasoningContext = null;
  _pendingReasoningEffortSelection = null;
  _reasoningEffortWriteSeq = 0;
  _lastReasoningFetchKey = null;
  _reasoningFetchSeq = 0;
};

(async () => {
  const result = {};

  reset();
  const oldKey = _reasoningEffortStateKey();
  _lastReasoningFetchKey = oldKey;
  _applyReasoningChip('xhigh', {supported_efforts:['low','high','xhigh']}, oldKey);
  S.session.model = 'claude-opus-4.6';
  S.session.model_provider = 'anthropic';
  syncReasoningChip();
  result.switchPending = {
    chip: _currentReasoningEffort,
    outgoing: getComposerReasoningEffortForRun(),
    request: requests[0].url,
  };
  requests[0].resolve({
    reasoning_effort: 'max',
    supported_efforts: ['low','high','xhigh','max'],
  });
  await flush();
  result.switchResolved = {
    chip: _currentReasoningEffort,
    outgoing: getComposerReasoningEffortForRun(),
  };

  reset();
  const writePromise = _setComposerReasoningEffort('max');
  result.pendingSelectionOldModel = getComposerReasoningEffortForRun();
  S.session.model = 'claude-opus-4.6';
  S.session.model_provider = 'anthropic';
  syncReasoningChip();
  result.pendingSelectionNewModel = getComposerReasoningEffortForRun();
  S.activeProfile = 'work';
  result.pendingSelectionOtherProfile = getComposerReasoningEffortForRun();
  S.activeProfile = 'default';

  // This GET began before the profile write settled and may observe old config.
  requests[1].resolve({
    reasoning_effort: 'medium',
    supported_efforts: ['low','medium','high','xhigh','max'],
  });
  await flush();
  result.afterPrewriteGet = getComposerReasoningEffortForRun();

  // The POST response was coerced for GPT-5 and must not claim Claude's key.
  requests[0].resolve({
    reasoning_effort: 'xhigh',
    supported_efforts: ['low','high','xhigh'],
  });
  await flush();
  result.afterWriteBeforeRefetch = getComposerReasoningEffortForRun();
  result.requestTrace = requests.map(req => ({
    url: req.url,
    method: req.options && req.options.method || 'GET',
  }));

  requests[2].resolve({
    reasoning_effort: 'max',
    supported_efforts: ['low','medium','high','xhigh','max'],
  });
  await writePromise;
  await flush();
  result.afterRefetch = {
    chip: _currentReasoningEffort,
    outgoing: getComposerReasoningEffortForRun(),
  };

  process.stdout.write(JSON.stringify(result));
})().catch(err => { console.error(err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def model_switch_outcome(tmp_path_factory):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    driver = tmp_path_factory.mktemp("reasoning_model_switch") / "driver.js"
    driver.write_text(_MODEL_SWITCH_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [node, str(driver), str(ROOT / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_model_switch_invalidates_previous_effort_synchronously(model_switch_outcome):
    assert model_switch_outcome["switchPending"] == {
        "chip": "",
        "outgoing": "",
        "request": "/api/reasoning?model=claude-opus-4.6&provider=anthropic",
    }
    assert model_switch_outcome["switchResolved"] == {
        "chip": "max",
        "outgoing": "max",
    }


def test_pending_raw_selection_survives_target_model_change(model_switch_outcome):
    assert model_switch_outcome["pendingSelectionOldModel"] == "max"
    assert model_switch_outcome["pendingSelectionNewModel"] == "max"
    assert model_switch_outcome["pendingSelectionOtherProfile"] == ""
    assert model_switch_outcome["afterPrewriteGet"] == "max"


def test_old_model_write_response_cannot_claim_new_model(model_switch_outcome):
    assert model_switch_outcome["afterWriteBeforeRefetch"] == ""
    assert model_switch_outcome["requestTrace"] == [
        {
            "url": "/api/reasoning",
            "method": "POST",
        },
        {
            "url": "/api/reasoning?model=claude-opus-4.6&provider=anthropic",
            "method": "GET",
        },
        {
            "url": "/api/reasoning?model=claude-opus-4.6&provider=anthropic",
            "method": "GET",
        },
    ]
    assert model_switch_outcome["afterRefetch"] == {
        "chip": "max",
        "outgoing": "max",
    }
