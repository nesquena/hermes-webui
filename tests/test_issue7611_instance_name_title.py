"""Behavioural coverage for issue #7611: a configurable instance name for
multi-instance deployments.

Operators running Production / Staging / Dev side by side had no way to tell
their browser tabs apart — every tab carried the same generic title. This adds
an optional ``instance_name`` (env: ``HERMES_WEBUI_INSTANCE_NAME``) that is
decorated onto the browser/desktop window title and surfaced in the app
titlebar.

The frontend tests drive the ACTUAL functions from ``static/ui.js`` through
node rather than grepping source strings (docs/GUIDELINES.md rule #6). The
driver replays a real owner write (set a base title, then let the decorator
run), so the assertions cover observable ``document.title`` output across the
decorate / re-decorate / withdraw lifecycle.

The wiring guards are asserted on the extracted owner function bodies because
the property under test there is structural — "every document.title owner
routes through the chokepoint" — not a runtime value.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
BOOT_JS = ROOT / "static" / "boot.js"
PANELS_JS = ROOT / "static" / "panels.js"
INDEX_HTML = ROOT / "static" / "index.html"
CONFIG_PY = ROOT / "api" / "config.py"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# --- backend -----------------------------------------------------------------

def _settings_default_in_subprocess(env_value):
    """Read ``_SETTINGS_DEFAULTS['instance_name']`` in a throwaway interpreter.

    The default is computed at *import* time, so observing it normally would
    require re-importing ``api.config``. Reloading that module re-runs its
    module-level setup, which mutates process-wide catalog/caches state and
    poisons every test that runs later in the same session (it breaks
    test_model_resolver / test_nous_portal_routing / test_passkey_auth). A
    subprocess gives a pristine interpreter, so this asserts the real import
    behaviour without any cross-test side effect.
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    if env_value is None:
        env.pop("HERMES_WEBUI_INSTANCE_NAME", None)
    else:
        env["HERMES_WEBUI_INSTANCE_NAME"] = env_value
    probe = (
        "import api.config as c;"
        "print(repr(c._SETTINGS_DEFAULTS['instance_name']))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return ast.literal_eval(proc.stdout.strip())


def test_instance_name_defaults_to_unset():
    """Unset by default, so existing single-instance deployments are unchanged."""
    assert _settings_default_in_subprocess(None) == ""


def test_instance_name_defaults_from_env():
    assert _settings_default_in_subprocess("US-East") == "US-East"


def test_instance_name_env_value_is_trimmed():
    assert _settings_default_in_subprocess("  Staging  ") == "Staging"


@pytest.fixture
def config(monkeypatch, tmp_path):
    """The normally-imported ``api.config``, pointed at a throwaway file.

    Deliberately NOT re-imported: reloading api.config leaks process-wide
    state into unrelated tests. The repo convention for this is to patch
    ``SETTINGS_FILE`` on the already-imported module.
    """
    import api.config as cfg

    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json", raising=False)
    return cfg


def test_instance_name_is_settable_and_persisted(config):
    saved = config.save_settings({"instance_name": "Dev"})
    reloaded = config.load_settings()

    assert saved["instance_name"] == "Dev"
    assert reloaded["instance_name"] == "Dev"


def test_instance_name_rejects_non_string_values(config):
    for bad in (123, ["Prod"], {"a": 1}, None, True):
        saved = config.save_settings({"instance_name": bad})
        assert saved["instance_name"] == "", f"accepted {bad!r}"


def test_instance_name_rejects_nul_and_control_chars(config):
    """NUL would truncate the value in C-level consumers and corrupt the title."""
    config.save_settings({"instance_name": "Pro\x00duction"})
    stored = str(config.load_settings().get("instance_name", ""))

    assert "\x00" not in stored


def test_instance_name_respects_max_length(config):
    saved = config.save_settings({"instance_name": "X" * 500})

    assert len(saved["instance_name"]) <= 80


# --- frontend decorator (real behaviour, via node) ---------------------------

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

global.document = { title: '' };
global.window = {};

// Pre-declare the decorator's remembered suffix so the eval'd bodies (each
// extracted separately) close over one shared binding.
let _lastInstanceTitleSuffix = '';

eval(extractFunc('instanceDisplayName'));
eval(extractFunc('_instanceTitleSuffix'));
eval(extractFunc('_applyInstanceTitle'));

const payload = JSON.parse(process.argv[3]);
const BULLET = ' \u2022 ';

// Replay what a document.title owner does: write its base title, then let the
// shared decorator run. This is the real call order in syncTopbar().
function ownerWrite(base) {
  document.title = base;
  _applyInstanceTitle();
  return document.title;
}

window._instanceName = payload.instance_name;
const out = {};
out.base = payload.base;

// 1. decorate
out.decorated = ownerWrite(payload.base);
// 2. a second owner write on a DIFFERENT title must not leave the old suffix
//    stuck on, and must decorate the new title exactly once
out.decoratedOther = ownerWrite(payload.other_base);
// 3. same title written again (syncTopbar fires constantly) -> idempotent
out.redecorated = ownerWrite(payload.other_base);
// 4. clearing the setting must withdraw the suffix, restoring the base title
window._instanceName = '';
out.withdrawn = ownerWrite(payload.other_base);

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("instance_name_driver") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return str(path)


def _run_decorator(driver, instance_name, base="Hermes", other_base="Settings \u2014 Hermes"):
    payload = json.dumps(
        {"instance_name": instance_name, "base": base, "other_base": other_base}
    )
    proc = subprocess.run(
        [NODE, driver, str(UI_JS), payload],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("blank", ["", "   ", None, "\t\n"])
def test_unset_instance_name_leaves_title_untouched(driver, blank):
    """Default behaviour must be byte-identical to today (#7611 guard)."""
    out = _run_decorator(driver, blank)

    assert out["decorated"] == "Hermes"
    assert out["decoratedOther"] == "Settings \u2014 Hermes"
    assert out["withdrawn"] == "Settings \u2014 Hermes"


def test_instance_name_decorates_the_title(driver):
    out = _run_decorator(driver, "Production")

    assert out["decorated"] == "Hermes \u2022 Production"


def test_each_owner_write_is_decorated_exactly_once(driver):
    """A new base title replaces the old one — no stacking, no stale suffix."""
    out = _run_decorator(driver, "Staging")

    assert out["decoratedOther"] == "Settings \u2014 Hermes \u2022 Staging"
    assert out["decoratedOther"].count("Staging") == 1


def test_repeated_application_is_idempotent(driver):
    out = _run_decorator(driver, "Staging")

    assert out["redecorated"] == out["decoratedOther"]


def test_clearing_the_instance_name_withdraws_the_suffix(driver):
    out = _run_decorator(driver, "Dev")

    assert out["withdrawn"] == "Settings \u2014 Hermes"
    assert "Dev" not in out["withdrawn"]


def test_instance_name_sanitizes_control_chars_and_whitespace(driver):
    out = _run_decorator(driver, "  US\x00-East\n  ")

    assert "\x00" not in out["decorated"]
    assert "\n" not in out["decorated"]
    assert out["decorated"] == "Hermes \u2022 US-East"


def test_very_long_instance_name_is_bounded(driver):
    out = _run_decorator(driver, "Y" * 400)

    assert len(out["decorated"]) <= 200


def test_attention_prefix_stays_leading(driver):
    """The '● ' attention dot is added by the owner before decorating."""
    out = _run_decorator(driver, "Prod", base="\u25cf Session \u2014 Hermes")

    assert out["decorated"].startswith("\u25cf Session \u2014 Hermes")
    assert out["decorated"].endswith(" \u2022 Prod")


# --- wiring guards: every document.title owner routes through the chokepoint --

def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"{signature} not found"
    depth = 0
    for idx in range(start, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{signature} body did not terminate")


def test_instance_display_name_reads_the_boot_mirror():
    assert "window._instanceName=" in _src(BOOT_JS)


def test_apply_bot_name_routes_through_instance_decorator():
    """#4086 guard: applyBotName stays the title owner, and now decorates too."""
    body = _extract_function(_src(BOOT_JS), "function applyBotName(){")

    assert "if(!S.session) document.title=name;" in body
    assert "_applyInstanceTitle()" in body


def test_sync_topbar_routes_through_instance_decorator():
    body = _extract_function(_src(UI_JS), "function syncTopbar(){")

    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in body
    assert "_applyInstanceTitle()" in body


def test_sync_app_titlebar_routes_through_instance_decorator():
    body = _extract_function(_src(PANELS_JS), "function syncAppTitlebar()")

    assert "document.title = bot ? mainText + ' \\u2014 ' + bot : mainText;" in body
    assert "_applyInstanceTitle()" in body


def test_bootstrap_settings_paths_set_the_instance_mirror():
    boot = _src(BOOT_JS)
    panels = _src(PANELS_JS)

    # settings-apply + offline fallback in boot.js, settings-save round-trip in
    # panels.js — all three refresh the mirror beside the existing bot_name write.
    assert boot.count("window._instanceName=") >= 2
    assert "window._instanceName=body.instance_name||'';" in panels


def test_index_html_has_an_instance_name_slot():
    assert 'id="appTitlebarInstance"' in _src(INDEX_HTML)


def test_instance_name_is_surfaced_in_the_app_titlebar():
    """A visible identifier, not just the tab title (#7611 asks for both)."""
    assert "appTitlebarInstance" in _src(PANELS_JS)