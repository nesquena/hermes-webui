"""Focused production-boundary tests for the Agent update adapter."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import agent_update


_REAL_RUN_TRANSACTION = agent_update._run_transaction
_REAL_LOAD_PROCESS_HELPER = agent_update._load_process_helper


def _identity_payload(root, interpreter, *, healthy=True, helper=None, critical=None):
    venv = root / "venv"
    base = root.parent / "base-python"
    modules = tuple(critical or ("hermes_cli.main", "run_agent", "model_tools", "toolsets"))
    return {
        "executable": str(interpreter.resolve()),
        "prefix": str(venv.resolve()),
        "base_prefix": str(base.resolve()),
        "pyvenv": str((venv / "pyvenv.cfg").resolve()),
        "pyvenv_config": {"home": str(base.resolve())},
        "site_roots": [str((venv / "Lib" / "site-packages").resolve())],
        "package": str((root / "hermes_cli").resolve()),
        "project": str(root.resolve()),
        "dependencies": {
            "fastapi": str((venv / "Lib" / "site-packages" / "fastapi.py").resolve()),
        },
        "critical_modules": modules,
        "critical_imports": {name: {"ok": healthy} for name in modules},
        "critical_validation": [healthy, None if healthy else modules[0], None if healthy else "broken"],
        "healthy": healthy,
        "helper": str((helper or root / "hermes_cli" / "_subprocess_compat.py").resolve()),
        "concurrent_exit": 2,
    }


def _target(tmp_path, *, marker=False, lazy_marker=False, healthy=True):
    root = tmp_path / "agent"
    (root / "hermes_cli").mkdir(parents=True)
    interpreter = root / "venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("shim", encoding="utf-8")
    (root / "venv" / "pyvenv.cfg").write_text("home = C:\\Python\\base\n", encoding="utf-8")
    if marker:
        (root / ".update-incomplete").write_text("", encoding="utf-8")
    if lazy_marker:
        (root / ".lazy-refresh-incomplete").write_text("", encoding="utf-8")
    identity = _identity_payload(root, interpreter, healthy=healthy)
    health = agent_update.AgentInstallHealth(
        identity,
        marker,
        lazy_marker,
        tuple(identity["critical_modules"]),
        healthy and all(item["ok"] for item in identity["critical_imports"].values()),
    )
    return agent_update.AgentUpdateTarget(
        root,
        interpreter,
        identity,
        "http://127.0.0.1:8642",
        venv_root=root / "venv",
        environment={"PYTHONNOUSERSITE": "1"},
        health=health,
    )


def _run_result(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def _install_test_runner(monkeypatch, result=None, *, callback=None, timed_out=False, quiescent=True, calls=None):
    calls = calls if calls is not None else []

    def run(args, root, env, timeout, helper):
        calls.append((args, root, env, timeout, helper))
        if callback:
            callback(root)
        return result or _run_result(), timed_out, quiescent

    monkeypatch.setattr(agent_update, "_run_transaction", run)
    return calls


@pytest.fixture(autouse=True)
def _safe_helper_loader(monkeypatch):
    monkeypatch.setattr(
        agent_update,
        "_load_process_helper",
        lambda root: SimpleNamespace(kill_process_tree=lambda proc: proc.kill()),
    )


def test_issue6617_dependency_change_runs_exact_official_transaction(monkeypatch, tmp_path):
    target = _target(tmp_path)
    calls = _install_test_runner(monkeypatch, calls=[])
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "before")
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: target.identity)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "updated"
    assert calls[0][0] == [str(target.interpreter), "-m", "hermes_cli.main", "update", "--yes"]
    assert calls[0][1] == target.source_root
    assert calls[0][2] == target.environment


def test_agent_update_binds_source_command_and_cwd_to_install_a(monkeypatch, tmp_path):
    target = _target(tmp_path)
    install_b = tmp_path / "b"
    (install_b / "hermes_cli").mkdir(parents=True)
    interpreter_b = install_b / "venv" / "Scripts" / "python.exe"
    interpreter_b.parent.mkdir(parents=True)
    interpreter_b.write_text("other", encoding="utf-8")
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: ("http://127.0.0.1:8642", None))
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", str(interpreter_b))
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: target.identity)
    resolved = agent_update._resolve_target()
    assert resolved.interpreter == target.interpreter
    assert resolved.environment["PYTHONNOUSERSITE"] == "1"


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
    interpreter.write_text("shim", encoding="utf-8")
    assert agent_update._candidate(root) == interpreter
    dot_root = tmp_path / "dot-agent"
    (dot_root / "hermes_cli").mkdir(parents=True)
    dot_interpreter = dot_root / ".venv" / "bin" / "python"
    dot_interpreter.parent.mkdir(parents=True)
    dot_interpreter.write_text("shim", encoding="utf-8")
    assert agent_update._candidate(dot_root) is None


def test_identity_rejects_foreign_executable_and_pyvenv_home(monkeypatch, tmp_path):
    target = _target(tmp_path)
    payload = _identity_payload(target.source_root, target.interpreter)
    payload["executable"] = str((tmp_path / "foreign-python.exe").resolve())
    monkeypatch.setattr(agent_update, "_run", lambda *args, **kwargs: _run_result(out=json.dumps(payload)))
    with pytest.raises(RuntimeError, match="executable"):
        agent_update._identity(target.source_root, target.interpreter, env=target.environment)
    payload["executable"] = str(target.interpreter.resolve())
    payload["pyvenv_config"]["home"] = str((tmp_path / "wrong-base").resolve())
    with pytest.raises(RuntimeError, match="home"):
        agent_update._identity(target.source_root, target.interpreter, env=target.environment)


def test_agent_update_exit_zero_with_nonfatal_warnings_is_success(monkeypatch, tmp_path):
    target = _target(tmp_path)
    _install_test_runner(monkeypatch, _run_result(out="warning: optional refresh skipped"))
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "before")
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: target.identity)
    result = agent_update.apply_agent_update()
    assert result["ok"] is True
    assert result["warnings_detail"]


def test_agent_update_zero_exit_with_lazy_marker_fails_closed(monkeypatch, tmp_path):
    target = _target(tmp_path)
    _install_test_runner(monkeypatch, callback=lambda root: (root / ".lazy-refresh-incomplete").write_text("", encoding="utf-8"))
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: target.identity)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "incomplete"
    assert result["reload_eligible"] is False


def test_agent_update_zero_exit_with_unhealthy_critical_import_fails_closed(monkeypatch, tmp_path):
    target = _target(tmp_path)
    bad = _identity_payload(target.source_root, target.interpreter, healthy=False)
    _install_test_runner(monkeypatch)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "changed")
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: bad)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "incomplete"
    assert result["source_before"] == "changed"


def test_agent_update_accepts_changed_official_critical_module_list(monkeypatch, tmp_path):
    target = _target(tmp_path)
    after = dict(target.identity)
    after["critical_modules"] = tuple(target.identity["critical_modules"]) + ("new_agent_module",)
    after["healthy"] = True
    _install_test_runner(monkeypatch)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: after)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "updated"


def test_agent_force_uses_official_transaction_without_force_flags(monkeypatch, tmp_path):
    target = _target(tmp_path)
    calls = _install_test_runner(monkeypatch, calls=[])
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_git_sha", lambda root: "same")
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: target.identity)
    result = agent_update.apply_agent_update(force=True)
    assert result["outcome"] == "updated"
    assert "--force" not in calls[0][0]


@pytest.mark.parametrize("url", ["http://example.test:8642", "https://10.0.0.2"])
def test_remote_gateway_sources_fail_before_local_side_effects(monkeypatch, tmp_path, url):
    target = _target(tmp_path)
    monkeypatch.setattr(agent_update, "_gateway_owner", lambda: (url, "remote gateway owner"))
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    assert agent_update._resolve_target().unsupported_reason == "remote gateway owner"


def test_health_url_cannot_mask_remote_chat_owner(monkeypatch):
    import api.gateway_chat

    monkeypatch.setenv("GATEWAY_HEALTH_URL", "http://127.0.0.1:8642")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "https://remote.example:8642")
    target = api.gateway_chat.resolve_effective_gateway_target({}, dict(os.environ))
    assert target["url"] == "https://remote.example:8642"
    assert target["local"] is False


def test_resolve_target_rejects_remote_chat_before_interpreter_lookup(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.setenv("GATEWAY_HEALTH_URL", "http://127.0.0.1:8642")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "https://remote.example:8642")
    monkeypatch.setattr(agent_update, "_source_root", lambda: target.source_root)
    monkeypatch.setattr(agent_update, "_candidate", lambda root: pytest.fail("candidate lookup must not run"))
    resolved = agent_update._resolve_target()
    assert resolved.unsupported_reason == "remote gateway owner"


def test_transaction_refusal_is_not_git_lock_recovery(monkeypatch, tmp_path):
    target = _target(tmp_path)
    _install_test_runner(monkeypatch, _run_result(2, "still running"))
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    monkeypatch.setattr(agent_update, "_identity", lambda root, exe, env=None: target.identity)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "transaction_in_progress"
    assert result["transaction_in_progress"] is True
    assert "lock_conflict" not in result


def test_agent_update_timeout_is_indeterminate_without_reload(monkeypatch, tmp_path):
    target = _target(tmp_path)
    _install_test_runner(monkeypatch, timed_out=True, quiescent=True)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "indeterminate"
    assert result["reload_eligible"] is False


def test_agent_update_nonquiescent_success_is_indeterminate(monkeypatch, tmp_path):
    target = _target(tmp_path)
    _install_test_runner(monkeypatch, quiescent=False)
    monkeypatch.setattr(agent_update, "_resolve_target", lambda: target)
    result = agent_update.apply_agent_update()
    assert result["outcome"] == "indeterminate"
    assert result["reload_eligible"] is False


def test_process_helper_is_loaded_from_selected_agent_root(monkeypatch, tmp_path):
    target = _target(tmp_path)
    monkeypatch.undo()
    helper = target.source_root / "hermes_cli" / "_subprocess_compat.py"
    helper.write_text("def kill_process_tree(proc): pass\n", encoding="utf-8")
    loaded = _REAL_LOAD_PROCESS_HELPER(target.source_root)
    assert Path(loaded.__file__).resolve() == helper.resolve()


def test_critical_module_list_is_required_before_update_launch(monkeypatch, tmp_path):
    target = _target(tmp_path)
    payload = _identity_payload(target.source_root, target.interpreter)
    payload["critical_modules"] = None
    monkeypatch.setattr(agent_update, "_run", lambda *args, **kwargs: _run_result(out=json.dumps(payload)))
    with pytest.raises(RuntimeError, match="critical module"):
        agent_update._identity(target.source_root, target.interpreter, env=target.environment)


def test_process_runner_terminates_descendants_and_bounds_output(tmp_path):
    if agent_update._load_process_observer() is None:
        pytest.skip("optional process-tree observer is not installed")
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('x'*200000, flush=True); print('y'*200000, file=sys.stderr, flush=True); time.sleep(30)"
    result, timed_out, quiescent = _REAL_RUN_TRANSACTION(
        [sys.executable, "-c", script],
        tmp_path,
        os.environ.copy(),
        timeout=0.3,
        helper=SimpleNamespace(kill_process_tree=lambda proc: proc.kill()),
    )
    assert timed_out is True
    assert quiescent is True
    assert len(result.stdout.encode()) <= agent_update._MAX_OUTPUT
    assert len(result.stderr.encode()) <= agent_update._MAX_OUTPUT


def test_process_runner_without_optional_tree_observer_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_update, "_load_process_observer", lambda: None)
    result, timed_out, quiescent = _REAL_RUN_TRANSACTION(
        [sys.executable, "-c", "print('done')"],
        tmp_path,
        os.environ.copy(),
        timeout=5,
        helper=SimpleNamespace(kill_process_tree=lambda proc: proc.kill()),
    )
    assert timed_out is False
    assert result.returncode == 0
    assert quiescent is False


def test_rolling_diagnostics_are_byte_bounded():
    buffer = agent_update._Rolling(8)
    buffer.add(b"123456")
    buffer.add(b"abcdef")
    assert len(buffer.text().encode()) <= 8
