"""Behavioural checks for #5001 active-profile recovery."""

import json
import subprocess
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
BOOT_JS = ROOT / "static" / "boot.js"
NODE = shutil.which("node")


pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute boot.js recovery-path behavior",
)


_BOOT_DRIVER = r"""
const fs = require('fs');
const bootSrc = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3] || '{}');

function extractFunction(source, name) {
  const marker = `async function ${name}`;
  const start = source.indexOf(marker);
  if (start < 0) {
    throw new Error(`missing function: ${name}`);
  }
  const end = source.indexOf('\n  // Fetch active profile', start);
  if (end < 0) {
    throw new Error(`missing marker following function: ${name}`);
  }
  return source.slice(start, end);
}

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

  snapshot() {
    return { ...this.store };
  }
}

function makeAttempt(attempt) {
  return async function () {
    if (attempt.type === 'success') {
      const payload = attempt.payload || {name: 'default', is_default: true};
      return payload;
    }
    if (attempt.type === 'undefined') {
      return undefined;
    }
    const error = new Error(attempt.message || 'active profile bootstrap failure');
    if (attempt.status !== undefined) error.status = attempt.status;
    throw error;
  };
}

eval(extractFunction(bootSrc, '_resolveActiveProfileBootstrapState'));

(async () => {
  const attempts = Array.isArray(scenario.attempts) ? scenario.attempts : [];
  const markerKey =
    scenario.markerKey || 'hermes-webui-active-profile-bootstrap-401';
  const storage = new FakeStorage(scenario.initialStorage || {});
  const redirectUrls = [];
  const results = [];
  const storageHistory = [];

  let applyBotNameCalls = 0;
  let bootProfile = null;
  let bootIsDefault = null;

  for (const attempt of attempts) {
    const state = await _resolveActiveProfileBootstrapState({
      loadActiveProfile: makeAttempt(attempt),
      markerStorage: storage,
      markerKey,
      getNextUrl: () => attempt.nextUrl || '/',
      redirectToLogin: (nextUrl) => {
        redirectUrls.push(`login?next=${encodeURIComponent(nextUrl)}`);
      },
    });

    results.push(state);
    storageHistory.push(storage.snapshot());

    if (scenario.simulateBootContinue && state.status === 'resolved') {
      bootProfile = state.profile;
      bootIsDefault = state.isDefault;
      applyBotNameCalls += 1;
    }
  }

  console.log(
    JSON.stringify({
      attempts: results,
      redirects: redirectUrls,
      storageHistory,
      storageSnapshot: storage.snapshot(),
      loadCalls: attempts.length,
      bootProfile,
      bootIsDefault,
      applyBotNameCalls,
    })
  );
})();
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("boot-profile-driver") / "boot_profile_driver.js"
    path.write_text(_BOOT_DRIVER, encoding="utf-8")
    return str(path)


def _run_boot_profile_scenario(driver_path, scenario):
    process = subprocess.run(
        [NODE, driver_path, str(BOOT_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"node profile driver failed: {process.stderr.strip()}")
    return json.loads(process.stdout)


def test_active_profile_boot_recovery_is_one_shot_and_bounded(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery",
            "attempts": [
                {"type": "undefined", "nextUrl": "/"},
                {"type": "undefined", "nextUrl": "/"},
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "recovery-redirect"
    assert payload["attempts"][1]["status"] == "fallback"
    assert payload["attempts"][1]["profile"] == "default"
    assert payload["attempts"][1]["isDefault"] is True
    assert payload["loadCalls"] == 2
    assert payload["redirects"] == ["login?next=%2F"]
    assert payload["storageHistory"][0].get("test-5001-active-profile-recovery") == "1"
    assert payload["storageHistory"][1].get("test-5001-active-profile-recovery") is None
    assert payload["storageSnapshot"] == {}


def test_active_profile_boot_recovery_handles_loader_thrown_401s(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery-throws",
            "attempts": [
                {"type": "error", "status": 401, "nextUrl": "/"},
                {"type": "error", "status": 401, "nextUrl": "/"},
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "recovery-redirect"
    assert payload["attempts"][1]["status"] == "fallback"
    assert payload["attempts"][1]["profile"] == "default"
    assert payload["attempts"][1]["isDefault"] is True
    assert payload["redirects"] == ["login?next=%2F"]
    assert payload["storageHistory"][0].get("test-5001-active-profile-recovery-throws") == "1"
    assert payload["storageHistory"][1].get("test-5001-active-profile-recovery-throws") is None
    assert payload["storageSnapshot"] == {}


def test_active_profile_success_path_applies_boot_state_and_continues(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery-success",
            "simulateBootContinue": True,
            "attempts": [
                {
                    "type": "success",
                    "payload": {"name": "team-profile", "is_default": False},
                }
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "resolved"
    assert payload["attempts"][0]["profile"] == "team-profile"
    assert payload["attempts"][0]["isDefault"] is False
    assert payload["bootProfile"] == "team-profile"
    assert payload["bootIsDefault"] is False
    assert payload["applyBotNameCalls"] == 1
    assert payload["redirects"] == []
    assert payload["storageHistory"][0].get("test-5001-active-profile-recovery-success") is None
    assert payload["storageSnapshot"] == {}
