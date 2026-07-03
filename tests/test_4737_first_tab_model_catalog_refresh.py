"""Browserless regression coverage for first-tab model catalog refresh (#4737)."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute the model catalog refresh harness",
)


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const fnSource = fs.readFileSync(process.argv[3], 'utf8');

class FakeStorage {
  constructor(seed = {}) {
    this.store = { ...seed };
  }

  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key)
      ? this.store[key]
      : null;
  }

  setItem(key, value) {
    this.store[key] = String(value);
  }

  removeItem(key) {
    delete this.store[key];
  }
}

class FakeNode {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.classList = { contains: () => false };
    this.style = {};
    this.textContent = '';
    this.label = '';
    this.value = '';
    this.parentNode = null;
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
}

class FakeSelect extends FakeNode {
  constructor() {
    super('select');
    this.id = 'modelSelect';
    this._innerHTML = '';
    this._value = '';
  }

  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }

  get options() {
    const options = [];
    for (const child of this.children) {
      if (child.tagName === 'OPTGROUP') {
        options.push(...child.children);
      } else if (child.tagName === 'OPTION') {
        options.push(child);
      }
    }
    return options;
  }

  set value(value) {
    this._value = value;
  }

  get value() {
    return this._value;
  }

  querySelector(selector) {
    if (selector === 'optgroup > option, option') {
      return this.options[0] || null;
    }
    return null;
  }
}

function buildFetchResponse(payload, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

function buildHarness(currentScenario) {
  const select = new FakeSelect();
  const dropdown = new FakeNode('div');
  const fetchCalls = [];
  const liveFetchCalls = [];
  const defaultRedirectCalls = [];
  const customRedirectCalls = [];
  const fetchQueue = (currentScenario.fetchResponses || []).map((payload) => buildFetchResponse(
    payload && payload.body ? payload.body : payload,
    payload && payload.status ? payload.status : 200,
  ));

  globalThis.window = globalThis;
  globalThis.document = {
    baseURI: 'http://localhost/session/abc',
    createElement(tag) {
      return new FakeNode(tag);
    },
  };
  globalThis.location = { href: 'http://localhost/session/abc' };
  globalThis.sessionStorage = new FakeStorage();
  globalThis.localStorage = new FakeStorage();
  globalThis.S = { pendingFiles: [] };
  globalThis._dynamicModelLabels = {};
  globalThis._modelEndpointErrors = {};
  globalThis._defaultModel = null;
  globalThis._activeProvider = null;
  globalThis._configuredModelBadges = {};
  globalThis._modelDropdownRequestSeq = 0;
  globalThis._modelCatalogFallbackRetried = false;
  globalThis.$ = (id) => {
    if (id === 'modelSelect') return select;
    if (id === 'composerModelDropdown') return dropdown;
    return null;
  };
  globalThis.getModelLabel = (id) => `label:${id}`;
  globalThis._captureModelDropdownSelection = () => null;
  globalThis._reconcileModelDropdownSelection = (_sel, data) => {
    const firstGroup = Array.isArray(data.groups) && data.groups.length ? data.groups[0] : null;
    const firstModel = firstGroup && Array.isArray(firstGroup.models) && firstGroup.models.length
      ? firstGroup.models[0]
      : null;
    if (firstModel) _sel.value = firstModel.id;
  };
  globalThis.syncModelChip = () => {};
  globalThis.renderModelDropdown = () => {};
  globalThis._positionModelDropdown = () => {};
  globalThis._redirectIfUnauth = (res) => {
    defaultRedirectCalls.push(res.status);
    return res.status === 401;
  };
  globalThis.console = { warn() {}, debug() {}, log() {} };
  globalThis._fetchLiveModels = (provider, sel, requestSeq) => {
    liveFetchCalls.push([provider, sel && sel.id ? sel.id : null, requestSeq]);
  };
  globalThis.fetch = async (url) => {
    fetchCalls.push(String(url));
    if (!fetchQueue.length) throw new Error(`unexpected fetch: ${url}`);
    return fetchQueue.shift();
  };

  return { select, fetchCalls, liveFetchCalls, defaultRedirectCalls, customRedirectCalls };
}

async function runScenario(currentScenario) {
  const { select, fetchCalls, liveFetchCalls, defaultRedirectCalls, customRedirectCalls } = buildHarness(currentScenario);
  let _modelDropdownRequestSeq = 0;
  let _modelCatalogFallbackRetried = false;
  eval(fnSource);
  const callOpts = { ...(currentScenario.opts || {}) };
  if (currentScenario.useCustomRedirect) {
    callOpts.redirectIfUnauth = (res) => {
      customRedirectCalls.push(res.status);
      return res.status === 401;
    };
  }
  await populateModelDropdown(callOpts);
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  return {
    customRedirectCalls,
    defaultRedirectCalls,
    fetchCalls,
    liveFetchCalls,
    optionValues: select.options.map((opt) => opt.value),
  };
}

runScenario(scenario).then((result) => {
  process.stdout.write(JSON.stringify(result));
}).catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("model-catalog-refresh-driver") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return str(path)


def _run(driver_path, scenario):
    marker = "async function populateModelDropdown("
    start = UI_JS.index(marker)
    paren_depth = 1
    idx = start + len(marker)
    while idx < len(UI_JS) and paren_depth > 0:
        char = UI_JS[idx]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
        idx += 1
    if paren_depth != 0:
        raise AssertionError("could not locate populateModelDropdown signature")
    brace_start = UI_JS.index("{", idx)
    depth = 0
    for idx in range(brace_start, len(UI_JS)):
        char = UI_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                fn_source = UI_JS[start : idx + 1]
                break
    else:
        raise AssertionError("could not extract populateModelDropdown")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=False) as handle:
        handle.write(fn_source)
        fn_source_path = handle.name
    try:
        process = subprocess.run(
            [NODE, driver_path, json.dumps(scenario), fn_source_path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        Path(fn_source_path).unlink(missing_ok=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def test_populate_model_dropdown_refetches_once_when_server_returns_empty_groups(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
                                {"id": "claude-opus-4", "label": "Claude Opus 4"},
                            ],
                        }
                    ],
                },
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 2
    assert "freshness=session_visit" in payload["fetchCalls"][1]
    assert payload["liveFetchCalls"] == [["anthropic", "modelSelect", 2]]
    assert "claude-opus-4" in payload["optionValues"]


def test_populate_model_dropdown_does_not_refetch_when_server_groups_already_populated(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
                            ],
                        }
                    ],
                }
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 1
    assert payload["optionValues"] == ["claude-sonnet-4"]


def test_populate_model_dropdown_retries_at_most_once_even_if_refetch_is_still_empty(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 2
    assert payload["fetchCalls"][1].endswith("freshness=session_visit")
    assert payload["optionValues"] == ["claude-sonnet-4"]


def test_populate_model_dropdown_retries_when_synth_fallback_is_empty(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "@anthropic:claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
                            ],
                        }
                    ],
                },
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 2
    assert payload["fetchCalls"][1].endswith("freshness=session_visit")
    assert payload["optionValues"] == ["claude-sonnet-4"]


def test_populate_model_dropdown_retry_preserves_custom_redirect_handler(driver_path):
    payload = _run(
        driver_path,
        {
            "useCustomRedirect": True,
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "status": 401,
                    "body": {
                        "active_provider": "anthropic",
                        "default_model": "claude-sonnet-4",
                        "configured_model_badges": {
                            "claude-sonnet-4": {"provider": "anthropic"},
                        },
                        "groups": [],
                    },
                },
            ],
        },
    )

    assert payload["fetchCalls"][1].endswith("freshness=session_visit")
    assert payload["customRedirectCalls"] == [200, 401]
    assert payload["defaultRedirectCalls"] == []
