"""Focused contract tests for the single-install Agent update adapter."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from api import agent_update


@pytest.fixture(autouse=True)
def _block_real_subprocess_and_restart(monkeypatch):
    monkeypatch.setattr(agent_update.subprocess, "Popen", lambda *a, **k: pytest.fail("Popen is forbidden"))


def _target(tmp_path):
    root = tmp_path / "a"
    (root / "hermes_cli").mkdir(parents=True)
    interpreter = root / "venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    return agent_update.AgentUpdateTarget(root, interpreter, {"healthy": True}, "http://127.0.0.1:8642")


def _run_result(code=0, out=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr="")


def test_issue6617_dependency_change_runs_exact_official_transaction(monkeypatch, tmp_path):
    target = _target(tmp_path)
    calls = []
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "before" if not calls else "after")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_run", lambda args, root, timeout: calls.append((args, root, timeout)) or _run_result())
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "updated"
    assert calls[0][0] == [str(target.interpreter), "-m", "hermes_cli.main", "update", "--yes"]
    assert calls[0][1] == target.source_root


def test_agent_update_binds_source_command_and_cwd_to_install_a(monkeypatch, tmp_path):
    target = _target(tmp_path)
    install_b = tmp_path / "b"
    (install_b / "hermes_cli").mkdir(parents=True)
    interpreter_b = install_b / "venv" / "Scripts" / "python.exe"
    interpreter_b.parent.mkdir(parents=True)
    interpreter_b.write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: ("http://127.0.0.1:8642", None))
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", str(interpreter_b))
    resolved = agent_update._resolve_target()
    assert resolved.interpreter == target.interpreter
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: resolved)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_run", lambda args, root, timeout: calls.append((args, root)) or _run_result())
    agent_update.apply_agent_update()
    assert all(call[1] == target.source_root for call in calls)
    assert all(call[0][0] == str(target.interpreter) for call in calls)


def test_agent_managed_marker_fails_before_interpreter_lookup(monkeypatch, tmp_path):
    target = _target(tmp_path)
    (target.source_root.parent / ".managed").write_text("", encoding="utf-8")
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: ("http://127.0.0.1:8642", None))
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    assert agent_update._resolve_target().unsupported_reason == "managed or Docker Agent installation"


@pytest.mark.parametrize("relative", ["venv/bin/python", "venv/Scripts/python.exe"])
def test_agent_candidate_uses_only_the_official_venv(tmp_path, relative):
    root = tmp_path / "agent"
    (root / "hermes_cli").mkdir(parents=True)
    interpreter = root / relative
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    assert agent_update._candidate(root) == interpreter

    dot_root = tmp_path / "dot-agent"
    (dot_root / "hermes_cli").mkdir(parents=True)
    dot_interpreter = dot_root / ".venv" / "bin" / "python"
    dot_interpreter.parent.mkdir(parents=True)
    dot_interpreter.write_text("", encoding="utf-8")
    assert agent_update._candidate(dot_root) is None


def test_agent_update_exit_zero_with_nonfatal_warnings_is_success(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "before")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result(out="warning: optional refresh skipped"))
    result = agent_update.apply_agent_update()
    assert result["ok"] is True
    assert result["warnings_detail"]


def test_agent_update_rejects_missing_or_mismatched_identity_before_update(monkeypatch, tmp_path):
    target = _target(tmp_path)
    called = []
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda *a: (_ for _ in ()).throw(RuntimeError("mismatch")))
    monkeypatch.setattr(agent_update, "_run", lambda *a: called.append(a) or _run_result())
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "unsupported"
    assert not any("hermes_cli.main" in call[0] for call in called)


def test_agent_update_rejects_malformed_identity_health_before_update(monkeypatch, tmp_path):
    target = _target(tmp_path)
    called = []
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda *a: (_ for _ in ()).throw(RuntimeError("malformed health state")))
    monkeypatch.setattr(agent_update, "_run", lambda *a: called.append(a) or _run_result())
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "unsupported"
    assert not any("hermes_cli.main" in call[0] for call in called)


def test_identity_probe_rejects_null_identity_paths(monkeypatch, tmp_path):
    target = _target(tmp_path)
    malformed = json.dumps({"package": None, "project": str(target.source_root), "healthy": True})
    monkeypatch.setattr(
        agent_update,
        "_run",
        lambda *a: _run_result(out=malformed),
    )
    with pytest.raises(RuntimeError, match="malformed health state"):
        agent_update._identity(target.source_root, target.interpreter)


def test_agent_update_nonzero_timeout_and_launch_failure_never_claim_success(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result(1, "failed"))
    assert agent_update.apply_agent_update()["ok"] is False


def test_agent_update_zero_exit_with_incomplete_marker_fails_closed(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_marker", lambda root: True)
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    assert agent_update.apply_agent_update()["outcome"] == "incomplete"


def test_agent_force_preserves_agent_process_guards(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "before")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    seen = []
    monkeypatch.setattr(agent_update, "_run", lambda args, root, timeout: seen.append(args) or _run_result())
    result = agent_update.apply_agent_update(force=True)
    assert result["outcome"] == "updated"
    assert all("--force" not in args for args in seen)


@pytest.mark.parametrize("url", ["http://example.test:8642", "https://10.0.0.2"])
def test_remote_gateway_sources_fail_before_local_side_effects(monkeypatch, tmp_path, url):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: (url, "remote gateway owner"))
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    assert agent_update._resolve_target().unsupported_reason == "remote gateway owner"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8642", "http://localhost:8642", "http://[::1]:8642"])
def test_loopback_gateway_sources_remain_local(monkeypatch, tmp_path, url):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: (url, None))
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: target.interpreter)
    assert agent_update._resolve_target().unsupported_reason is None


def test_agent_update_zero_exit_with_unhealthy_post_probe_fails_closed(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "changed")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    states = iter(({"healthy": True}, {"healthy": False}))
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: next(states))
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    assert agent_update.apply_agent_update()["outcome"] == "failed"


def test_agent_update_repairs_unhealthy_pre_probe(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    states = iter(({"healthy": False}, {"healthy": True}))
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: next(states))
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "repaired"
    assert result["reload_eligible"] is True


def test_agent_update_clearing_incomplete_marker_is_repaired(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    markers = iter((True, False))
    monkeypatch.setattr(agent_update, "_marker", lambda root: next(markers))
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "repaired"
    assert result["reload_eligible"] is True


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["python"], 1), OSError("missing")])
def test_agent_update_timeout_and_launch_failure_are_indeterminate(monkeypatch, tmp_path, failure):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: None)
    monkeypatch.setattr(agent_update, "_run", lambda *a: (_ for _ in ()).throw(failure))
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "indeterminate"
    assert result["ok"] is False


@pytest.mark.parametrize("variable", ["HERMES_MANAGED", "HERMES_DOCKER"])
def test_managed_and_docker_targets_fail_before_interpreter_lookup(monkeypatch, tmp_path, variable):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: ("http://127.0.0.1:8642", None))
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    monkeypatch.setenv(variable, "1")
    assert agent_update._resolve_target().unsupported_reason == "managed or Docker Agent installation"


@pytest.mark.parametrize("method", ["docker", "nixos", "homebrew", "brew"])
def test_install_method_markers_fail_before_interpreter_lookup(monkeypatch, tmp_path, method):
    target = _target(tmp_path)
    (target.source_root / ".install_method").write_text(method, encoding="utf-8")
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: ("http://127.0.0.1:8642", None))
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    assert agent_update._resolve_target().unsupported_reason == "managed or Docker Agent installation"


def test_configured_remote_gateway_fails_before_interpreter_lookup(monkeypatch, tmp_path):
    import api.agent_health
    import api.config
    import api.gateway_chat

    target = _target(tmp_path)
    for variable in ("GATEWAY_HEALTH_URL", "HERMES_GATEWAY_HEALTH_URL", "HERMES_API_URL", "HERMES_WEBUI_GATEWAY_BASE_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(api.agent_health, "_remote_gateway_base_url", lambda: None)
    monkeypatch.setattr(api.config, "get_config", lambda: {"webui_gateway_base_url": "https://remote.example:8642"})
    monkeypatch.setattr(api.gateway_chat, "_gateway_base_url", lambda config_data=None, environ=None: "https://remote.example:8642")
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    assert agent_update._resolve_target().unsupported_reason == "remote gateway owner"


@pytest.mark.parametrize("variable", ["GATEWAY_HEALTH_URL", "HERMES_GATEWAY_HEALTH_URL", "HERMES_API_URL", "HERMES_WEBUI_GATEWAY_BASE_URL"])
def test_supported_remote_gateway_environment_sources_fail_closed(monkeypatch, tmp_path, variable):
    target = _target(tmp_path)
    for name in ("GATEWAY_HEALTH_URL", "HERMES_GATEWAY_HEALTH_URL", "HERMES_API_URL", "HERMES_WEBUI_GATEWAY_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "https://remote.example:8642")
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    assert agent_update._resolve_target().unsupported_reason == "remote gateway owner"


def test_agent_update_same_sha_success_still_reloads_for_dependency_changes(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "updated"
    assert result["reload_eligible"] is True


def test_agent_zip_install_uses_conservative_success(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: None)
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "updated"
    assert result["reload_eligible"] is True


def test_agent_update_missing_sha_is_safe(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result())
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: None)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "updated"
    assert result["reload_eligible"] is True


def test_agent_update_lock_conflict_and_diagnostic_redaction(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe: {"healthy": True})
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_marker", lambda root: False)
    token = "ghp_" + "A" * 24
    output = f"https://user:password@example.test/update?token={token}; another update is already running"
    monkeypatch.setattr(agent_update, "_run", lambda *a: _run_result(1, output))
    result = agent_update.apply_agent_update()
    assert result["lock_conflict"] is True
    assert "password" not in result["message"]
    assert token not in result["message"]
