"""Node behavioural tests for static/model_router.js (PR #7146 review fixes).

Drives the REAL static/model_router.js with a mocked DOM/api/S, mirroring the
node-driver pattern used by test_reasoning_chip_js_behaviour.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROUTER_JS_PATH = REPO_ROOT / "static" / "model_router.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeEl(tag) {
  return {
    tag: tag || 'div',
    style: {},
    dataset: {},
    value: '',
    textContent: '',
    checked: false,
    title: '',
    options: [],
    onchange: null,
    appendChild() {},
    addEventListener() {},
    dispatchEvent() {},
  };
}

const select = makeEl('select');
select.value = 'old-model';
let selectChangeCalls = 0;
select.onchange = function () { selectChangeCalls += 1; };

const els = { modelSelect: select };
const ensureCalls = [];

global.document = {
  readyState: 'loading',
  addEventListener() {},
  getElementById(id) { return els[id] || null; },
  querySelector() { return null; },
  createElement(tag) { return makeEl(tag); },
};
global.window = {};
global.localStorage = {
  getItem() { return null; },
  setItem() {},
  removeItem() {},
};
global.S = { session: { session_id: 'sid-1', message_count: 4 }, messages: [1, 2, 3] };
global._ensureModelOptionInDropdown = function (model, sel, provider) {
  ensureCalls.push({ model: String(model || ''), provider: String(provider || '') || null });
  sel.value = provider ? '@' + provider + ':' + model : model;
  return sel.value;
};

let statusResponse = { enabled: true };
let recommendResponse = { model: 'gpt-5', provider: 'openai', reason: 'test' };
let holdRecommend = false;
let releaseRecommend = null;

const calls = [];
global.api = async function (path, opts) {
  calls.push({ path, opts: opts || {} });
  if (path === '/api/model-router/status') return statusResponse;
  if (path === '/api/model-router/recommend') {
    if (holdRecommend) {
      await new Promise(resolve => { releaseRecommend = resolve; });
    }
    return recommendResponse;
  }
  if (path === '/api/model-router/failure') return { ok: true };
  throw new Error('unexpected api path: ' + path);
};

eval(src);

(async () => {
  const out = {};
  try {
    // enable -> reload 持久化：init() 从后端 status 读回 master switch。
    window.ModelRouter.init();
    await new Promise(r => setTimeout(r, 0));
    out.reloadEnabled = window.ModelRouter.enabled;

    window.ModelRouter.setMaster(true);
    await window.ModelRouter.beforeSend('hello world');

    const rec = calls.find(c => c.path === '/api/model-router/recommend');
    out.recommendPath = rec ? rec.path : null;
    out.recommendMethod = rec && rec.opts ? rec.opts.method : null;
    out.recommendHasQuery = rec ? rec.path.indexOf('?') !== -1 : null;
    out.recommendBody = rec && rec.opts ? JSON.parse(rec.opts.body) : null;
    out.ensureCalls = ensureCalls;
    out.selectValue = select.value;
    out.selectChangeCalls = selectChangeCalls;

    // failure cooldown producer 交付。
    await window.ModelRouter.recordFailure('gpt-5', 'openai');
    const fail = calls.find(c => c.path === '/api/model-router/failure');
    out.failurePath = fail ? fail.path : null;
    out.failureMethod = fail && fail.opts ? fail.opts.method : null;
    out.failureBody = fail && fail.opts ? JSON.parse(fail.opts.body) : null;

    // 会话切换：recommend 在途时切走会话，不得应用推荐。
    holdRecommend = true;
    recommendResponse = { model: 'gpt-6', provider: 'openai', reason: 'test' };
    global.S.session.session_id = 'sid-2';
    const p = window.ModelRouter.beforeSend('session switch text');
    await new Promise(r => setTimeout(r, 0));
    global.S.session.session_id = 'sid-3';
    releaseRecommend();
    await p;
    holdRecommend = false;
    out.sessionSwitchSelectValue = select.value;
    out.sessionSwitchRecommendBody = (() => {
      const c = calls.filter(x => x.path === '/api/model-router/recommend').pop();
      return c && c.opts ? JSON.parse(c.opts.body) : null;
    })();

    // 关闭总开关后，recommend / failure 均不得再发请求。
    const callsBeforeOff = calls.length;
    window.ModelRouter.setMaster(false);
    await window.ModelRouter.beforeSend('should-not-send');
    await window.ModelRouter.recordFailure('gpt-5', 'openai');
    out.offNoop = calls.length === callsBeforeOff;
    out.enabledAfterOff = window.ModelRouter.enabled;
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }
  process.stdout.write(JSON.stringify(out));
})().catch(e => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("model_router_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path):
    result = subprocess.run(
        [NODE, driver_path, str(MODEL_ROUTER_JS_PATH)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_model_router_js_behaviour(driver_path):
    out = _run(driver_path)
    assert "error" not in out, out.get("error")

    # enable -> reload 持久化
    assert out["reloadEnabled"] is True

    # prompt 不在 URL：POST JSON body
    assert out["recommendPath"] == "/api/model-router/recommend"
    assert out["recommendMethod"] == "POST"
    assert out["recommendHasQuery"] is False
    assert out["recommendBody"] == {
        "text": "hello world",
        "message_count": 4,
        "session_id": "sid-1",
    }

    # 跨 provider 重复模型 ID / 合成冒号模型：委托 _ensureModelOptionInDropdown
    assert out["ensureCalls"] == [{"model": "gpt-5", "provider": "openai"}]
    assert out["selectValue"] == "@openai:gpt-5"
    assert out["selectChangeCalls"] == 1

    # failure cooldown producer 交付
    assert out["failurePath"] == "/api/model-router/failure"
    assert out["failureMethod"] == "POST"
    assert out["failureBody"] == {"model": "gpt-5", "model_provider": "openai"}

    # 会话切换：迟到的推荐不得覆盖新会话模型
    assert out["sessionSwitchSelectValue"] == "@openai:gpt-5"
    assert out["sessionSwitchRecommendBody"]["session_id"] == "sid-2"

    # 总开关关闭后 no-op
    assert out["offNoop"] is True
    assert out["enabledAfterOff"] is False
