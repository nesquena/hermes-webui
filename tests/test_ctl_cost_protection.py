import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_ctl_cost_protection(tmp_path, action):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path / "state")
    return subprocess.run(
        ["bash", str(ROOT / "ctl.sh"), "cost-protection", action],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_ctl_cost_protection_enable_disable_status_uses_webui_settings(tmp_path):
    enabled = _run_ctl_cost_protection(tmp_path, "enable")
    settings_file = tmp_path / "state" / "settings.json"

    assert "Cost Protection: enabled" in enabled.stdout
    assert settings_file.exists()
    assert json.loads(settings_file.read_text())["cost_protection_enabled"] is True

    status = _run_ctl_cost_protection(tmp_path, "status")
    assert "Cost Protection: enabled" in status.stdout
    assert "Background runaway alerts are not implemented yet" in status.stdout

    disabled = _run_ctl_cost_protection(tmp_path, "disable")
    assert "Cost Protection: disabled" in disabled.stdout
    assert json.loads(settings_file.read_text())["cost_protection_enabled"] is False


def test_ctl_help_lists_cost_protection_command():
    result = subprocess.run(
        ["bash", str(ROOT / "ctl.sh"), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "cost-protection <command>" in result.stdout
    assert "runaway-cost guards" in result.stdout
