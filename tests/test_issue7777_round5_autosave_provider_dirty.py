"""#7777 re-gate 2026-11-23 — provider-only change must stay dirty on autosave.

The reviewer's counterexample:

    changing only the Settings provider (``custom:foo`` → ``custom:bar``)
    with the bare model unchanged reports clean, so the unsaved-changes
    warning disappears. Pinned-base production keeps it dirty.

Mechanism: ``_autosavePreferencesSettings`` computes
``modelUnchanged`` from three model-equality forms (raw / captured /
prefix-stripped) and then gates the WHOLE ``modelDirty`` predicate behind
``&& !modelUnchanged``. A provider-only change has the same bare model, so
``modelUnchanged`` is true and the provider-change arm is swallowed.

Fix contract (the reviewer's): ``!modelUnchanged || providerChanged`` — the
equality short-circuit may only suppress a model substitution that keeps the
same provider; a provider change always stays dirty.

These tests drive the REAL ``_autosavePreferencesSettings`` in a Node sandbox
with mock DOM/post; they are not source-shape assertions, so a reverted guard
cannot pass.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from tests.js_source_extract import extract_function
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

# Extract the real function once; the driver only supplies the sandbox.
_AUTOSAVE_FN = extract_function(PANELS_JS, "_autosavePreferencesSettings", prefix="async function")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "saved_provider,current_provider,saved_model,current_raw,expect_dirty",
    [
        # The reviewer's counterexample: provider changed, bare model same.
        ("custom:foo", "custom:bar", "claude-sonnet-5", "@custom:bar:claude-sonnet-5", True),
        # Control: provider and model both unchanged -> clean.
        ("custom:foo", "custom:foo", "claude-sonnet-5", "@custom:foo:claude-sonnet-5", False),
        # Control: model changed under the same provider -> dirty.
        ("custom:foo", "custom:foo", "claude-sonnet-5", "@custom:foo:claude-opus-5", True),
        # Control: both changed -> dirty.
        ("custom:foo", "custom:bar", "claude-sonnet-5", "@custom:bar:claude-opus-5", True),
    ],
)
def test_provider_only_change_keeps_the_unsaved_warning(
    saved_provider,
    current_provider,
    saved_model,
    current_raw,
    expect_dirty,
):
    driver = textwrap.dedent(
        """
        const fs = require('fs');
        const vm = require('vm');
        const fn = fs.readFileSync(process.argv[2], 'utf8');
        const scenario = JSON.parse(process.argv[3]);

        const bar = { style: { display: 'block' }, textContent: '' };
        const select = {
          id: 'settingsModel',
          value: scenario.current_raw,
          options: [],
          dataset: {},
        };
        const sandbox = {
          console,
          _settingsDirty: true,
          _settingsHermesDefaultModelOnOpen: scenario.saved_model,
          _settingsHermesDefaultModelProviderOnOpen: scenario.saved_provider,
          _captureModelDropdownSelection: () => ({
            model: scenario.current_model,
            model_provider: scenario.current_provider,
          }),
          _enqueueSettingsPost: async () => ({}),
          _setPreferencesAutosaveStatus: () => {},
          _settingsPreferencesAutosaveRetryPayload: null,
          localStorage: { setItem(){}, getItem(){ return null; } },
          document: {
            getElementById: (id) => {
              if (id === 'settingsModel') return select;
              if (id === 'settingsPassword') return { value: '' };
              if (id === 'settingsUnsavedBar') return bar;
              if (id === 'settingsMaxTokens') return { value: '0', dataset: { initialValue: '0' } };
              return null;
            },
            querySelector: () => null,
            documentElement: { dataset: {} },
          },
          window: {},
          clearTimeout, setTimeout,
        };
        sandbox.$ = (id) => sandbox.document.getElementById(id);
        vm.createContext(sandbox);
        vm.runInContext(fn + '\\n;globalThis.__run = _autosavePreferencesSettings;', sandbox);
        sandbox.__run({}).then(() => {
          console.log(JSON.stringify({
            dirty: !!sandbox._settingsDirty,
            bar_display: bar.style.display,
          }));
        }).catch((e) => {
          console.log(JSON.stringify({ error: String((e && e.stack) || e) }));
          process.exitCode = 1;
        });
        """
    )
    fn_tmp = REPO / "tests" / "_tmp_autosave_fn.js"
    driver_tmp = REPO / "tests" / "_tmp_autosave_driver.js"
    fn_tmp.write_text(_AUTOSAVE_FN, encoding="utf-8")
    driver_tmp.write_text(driver, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                str(NODE),
                str(driver_tmp),
                str(fn_tmp),
                json.dumps(
                    {
                        "saved_provider": saved_provider,
                        "current_provider": current_provider,
                        "saved_model": saved_model,
                        "current_model": current_raw.split(":")[-1]
                        if current_raw.startswith("@")
                        else current_raw,
                        "current_raw": current_raw,
                    }
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        fn_tmp.unlink(missing_ok=True)
        driver_tmp.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    assert out["dirty"] is expect_dirty, (saved_provider, current_provider, out)
    if expect_dirty:
        assert out["bar_display"] != "none", out
    else:
        assert out["bar_display"] == "none", out
