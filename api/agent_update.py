"""Narrow adapter for the Hermes Agent's official update transaction."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


_MAX_DETAIL = 600
_MAX_OUTPUT = 64 * 1024
_PROBE = r'''
import importlib, importlib.util, json, pathlib, site, subprocess, sys
root = pathlib.Path(__ROOT__).resolve()
venv = root / "venv"
cfg_path = venv / "pyvenv.cfg"
cfg = {}
if cfg_path.is_file():
    for line in cfg_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1); cfg[k.strip().lower()] = v.strip()
critical = None
critical_error = ""
critical_validation = None
try:
    owner = importlib.import_module("hermes_cli.update_cmd")
    critical = tuple(owner._UPDATE_CRITICAL_MODULES)
    if not critical or any(not isinstance(x, str) or not x for x in critical):
        raise ValueError("malformed official critical module list")
    validator_run = {"called": False, "returncodes": [], "error": ""}
    real_run = subprocess.run
    def observe_validator_run(*args, **kwargs):
        validator_run["called"] = True
        try:
            result = real_run(*args, **kwargs)
        except BaseException as exc:
            validator_run["error"] = str(exc)
            raise
        validator_run["returncodes"].append(getattr(result, "returncode", None))
        return result
    subprocess.run = observe_validator_run
    try:
        critical_validation = owner._validate_critical_modules_import(root)
    finally:
        subprocess.run = real_run
    if not validator_run["called"] or validator_run["error"] or any(code != 0 for code in validator_run["returncodes"]):
        critical_validation = (False, None, validator_run["error"] or "official critical-module validator was unavailable")
except Exception as exc:
    critical_error = str(exc)
deps = {}
for name in ("fastapi", "uvicorn", "pydantic", "openai", "yaml"):
    try:
        mod = importlib.import_module(name)
        deps[name] = str(pathlib.Path(mod.__file__).resolve())
    except Exception as exc:
        deps[name] = "!" + str(exc)
try:
    helper = importlib.util.find_spec("hermes_cli._subprocess_compat")
    helper_file = str(pathlib.Path(helper.origin).resolve()) if helper and helper.origin else ""
except Exception:
    helper_file = ""
try:
    lock = importlib.import_module("hermes_cli.update_lock")
    concurrent = int(lock.UPDATE_EXIT_CONCURRENT)
except Exception:
    concurrent = None
try:
    package = str(pathlib.Path(importlib.import_module("hermes_cli").__file__).resolve().parent)
    project = str(pathlib.Path(importlib.import_module("hermes_cli.main").PROJECT_ROOT).resolve())
except Exception as exc:
    package, project = "", ""
site_roots = [str(pathlib.Path(x).resolve()) for x in site.getsitepackages()]
try: site_roots.append(str(pathlib.Path(site.getusersitepackages()).resolve()))
except Exception: pass
print(json.dumps({"executable": str(pathlib.Path(sys.executable).resolve()),
 "prefix": str(pathlib.Path(sys.prefix).resolve()),
 "base_prefix": str(pathlib.Path(sys.base_prefix).resolve()),
 "pyvenv": str(cfg_path), "pyvenv_config": cfg, "site_roots": site_roots,
 "package": package, "project": project, "dependencies": deps,
 "critical_modules": critical, "critical_validation": critical_validation,
 "critical_error": critical_error, "helper": helper_file,
 "concurrent_exit": concurrent}))
'''
_CREDENTIAL_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s'\"]+)@")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_QUERY_SECRET_RE = re.compile(r"([?&](?:access_token|oauth_token|private_token|client_secret|app_secret|api[_-]?key|token|password|secret|auth|key)=)[^&\s'\"]+", re.I)


@dataclass(frozen=True)
class AgentInstallHealth:
    identity: dict
    update_incomplete: bool
    lazy_refresh_incomplete: bool
    critical_modules: tuple[str, ...] = ()
    healthy: bool = False

    @property
    def incomplete(self) -> bool:
        return self.update_incomplete or self.lazy_refresh_incomplete


@dataclass(frozen=True)
class AgentUpdateTarget:
    source_root: Path | None
    interpreter: Path | None
    identity: dict | None = None
    gateway_owner: str | None = None
    unsupported_reason: str | None = None
    venv_root: Path | None = None
    environment: dict[str, str] | None = field(default=None, repr=False, compare=False)
    health: AgentInstallHealth | None = None


@dataclass(frozen=True)
class AgentUpdateResult:
    outcome: str
    exit_code: int | None = None
    detail: str = ""
    warnings_detail: str = ""
    source_before: str | None = None
    source_after: str | None = None
    marker_before: bool = False
    marker_after: bool = False
    lock_conflict: bool = False
    reload_eligible: bool = False
    transaction_in_progress: bool = False
    quiescent: bool | None = None

    def as_dict(self) -> dict:
        p = {"ok": self.outcome in {"updated", "repaired", "noop"}, "target": "agent", "outcome": self.outcome,
             "message": self.detail or f"Agent update {self.outcome}", "reload_eligible": self.reload_eligible,
             "restart_scheduled": self.reload_eligible}
        for k in ("exit_code", "source_before", "source_after", "warnings_detail"):
            v = getattr(self, k)
            if v is not None and v != "": p[k] = v
        if self.marker_before: p["marker_before"] = True
        if self.marker_after: p["marker_after"] = True
        if self.lock_conflict: p["lock_conflict"] = True
        if self.transaction_in_progress: p["transaction_in_progress"] = True
        if self.quiescent is not None: p["quiescent"] = self.quiescent
        if self.outcome in {"unsupported", "indeterminate", "failed", "incomplete", "transaction_in_progress"}: p["ok"] = False
        return p


def _bounded(value: object) -> str:
    text = str(value or "").replace("\x00", "")
    updates = sys.modules.get("api.updates")
    if updates is not None: return updates._sanitize_git_diagnostic(text, limit=_MAX_DETAIL)
    return _QUERY_SECRET_RE.sub(r"\1<redacted>", _GITHUB_TOKEN_RE.sub("<redacted>", _CREDENTIAL_IN_URL_RE.sub(r"\1<redacted>@", text))).strip()[:_MAX_DETAIL]


def _gateway_owner() -> tuple[str | None, str | None]:
    from api.config import get_config
    from api.gateway_chat import resolve_effective_gateway_target
    target = resolve_effective_gateway_target(get_config())
    url = target["url"]
    return url, None if target["local"] else "remote gateway owner"


def _source_root() -> Path | None:
    from api.config import _AGENT_DIR
    try:
        root = Path(_AGENT_DIR).resolve() if _AGENT_DIR is not None else None
        return root if root and root.is_dir() else None
    except OSError: return None


def _managed_install(root: Path) -> bool:
    if os.getenv("HERMES_MANAGED") or os.getenv("HERMES_DOCKER"): return True
    try:
        if (root / ".install_method").read_text(encoding="utf-8").strip().lower() in {"docker", "nixos", "homebrew", "brew"}: return True
    except OSError: pass
    homes = [root.parent]
    if os.getenv("HERMES_HOME", "").strip(): homes.append(Path(os.environ["HERMES_HOME"]).expanduser())
    return any((h / ".managed").exists() for h in homes)


def _candidate(root: Path) -> Path | None:
    for rel in ("venv/bin/python", "venv/Scripts/python.exe"):
        p = root / rel
        try:
            if p.is_file() and p.absolute().is_relative_to(root): return p
        except OSError: pass
    return None


def _flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _transaction_flags() -> int:
    flags = _flags()
    if os.name == "nt":
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags


def _run(args: list[str], root: Path, timeout: float, *, env=None):
    return subprocess.run(args, cwd=str(root), shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=_flags(), env=env)


def _git_sha(root: Path) -> str | None:
    try:
        r = _run(["git", "rev-parse", "HEAD"], root, 10); return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError): return None


def _identity(root: Path, interpreter: Path, env=None) -> dict:
    probe = _PROBE.replace("__ROOT__", repr(str(root)))
    result = _run([str(interpreter), "-c", probe], root, 30, env=env)
    if result.returncode != 0: raise RuntimeError(_bounded(result.stderr or result.stdout or "identity probe failed"))
    try: value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc: raise RuntimeError("Agent identity probe returned malformed health state") from exc
    required = ("executable", "prefix", "base_prefix", "pyvenv", "pyvenv_config", "site_roots", "package", "project", "dependencies", "critical_modules", "critical_validation", "helper", "concurrent_exit")
    if not isinstance(value, dict) or any(k not in value for k in required): raise RuntimeError("Agent identity probe returned malformed health state")
    if not value["pyvenv_config"] or not isinstance(value["pyvenv_config"], dict) or "home" not in value["pyvenv_config"]: raise RuntimeError("Agent venv pyvenv.cfg metadata is malformed")
    venv = (root / "venv").resolve()
    runtime_executable = Path(value["executable"]).resolve()
    selected_executable = Path(interpreter).resolve()
    if runtime_executable != selected_executable: raise RuntimeError("Agent interpreter executable does not match selected venv launcher")
    if Path(value["prefix"]).resolve() != venv or Path(value["base_prefix"]).resolve() == venv or Path(value["pyvenv"]).resolve() != venv / "pyvenv.cfg": raise RuntimeError("Agent interpreter prefix does not match selected venv")
    if Path(value["pyvenv_config"]["home"]).resolve() != Path(value["base_prefix"]).resolve(): raise RuntimeError("Agent venv home does not match runtime base prefix")
    if Path(value["package"]).resolve() != root / "hermes_cli" or Path(value["project"]).resolve() != root: raise RuntimeError("Agent interpreter identity does not match source root")
    roots = [Path(x).resolve() for x in value["site_roots"] if isinstance(x, str)]
    owned_roots = [r for r in roots if r.is_relative_to(venv)]
    if not owned_roots: raise RuntimeError("Agent venv site-packages are unavailable")
    dependency_healthy = True
    for origin in value["dependencies"].values():
        if not isinstance(origin, str) or not origin:
            raise RuntimeError("Agent dependency origin is malformed")
        if origin.startswith("!"):
            dependency_healthy = False
            continue
        if not any(Path(origin).resolve().is_relative_to(r) for r in owned_roots):
            raise RuntimeError("Agent dependencies are not owned by selected venv")
    if not isinstance(value["critical_modules"], (list, tuple)) or not value["critical_modules"] or any(not isinstance(x, str) or not x for x in value["critical_modules"]): raise RuntimeError("Agent critical module list is malformed")
    validation = value["critical_validation"]
    if not isinstance(validation, (list, tuple)) or len(validation) != 3 or not isinstance(validation[0], bool): raise RuntimeError("Agent critical module validation is malformed")
    value["healthy"] = dependency_healthy and validation[0]
    if Path(value["helper"]).resolve() != (root / "hermes_cli" / "_subprocess_compat.py").resolve() or value["concurrent_exit"] != 2: raise RuntimeError("Agent official helper or transaction contract is unavailable")
    if not (root / "venv" / "pyvenv.cfg").is_file(): raise RuntimeError("Agent venv pyvenv.cfg is unavailable")
    return value


def _controlled_env(venv: Path) -> dict[str, str]:
    env = dict(os.environ); env.pop("PYTHONHOME", None); env.pop("PYTHONPATH", None); env["PYTHONNOUSERSITE"] = "1"; env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = str(venv / ("Scripts" if os.name == "nt" else "bin")) + os.pathsep + env.get("PATH", "")
    return env


class _Rolling:
    def __init__(self, limit=_MAX_OUTPUT): self.data = deque(); self.size = 0; self.limit = limit
    def add(self, chunk):
        self.data.append(chunk); self.size += len(chunk)
        while self.size > self.limit: self.size -= len(self.data.popleft())
    def text(self): return b"".join(self.data).decode("utf-8", "replace")


def _load_process_helper(root: Path):
    path = (root / "hermes_cli" / "_subprocess_compat.py").resolve()
    if not path.is_file() or not path.is_relative_to(root.resolve()):
        raise RuntimeError("Agent process helper is outside the selected source root")
    spec = importlib.util.spec_from_file_location("_hermes_selected_subprocess_compat", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Agent process helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RuntimeError("Agent process helper cannot be imported") from exc
    if not callable(getattr(module, "kill_process_tree", None)):
        raise RuntimeError("Agent process helper is malformed")
    return module


def _load_process_observer():
    try:
        return importlib.import_module("psutil")
    except Exception:
        return None


def _record_descendants(observer, proc, known: dict[int, float]) -> None:
    if observer is None:
        return
    try:
        children = observer.Process(proc.pid).children(recursive=True)
    except Exception:
        return
    for child in children:
        try:
            known[child.pid] = child.create_time()
        except Exception:
            continue


def _live_owned_descendants(observer, known: dict[int, float]) -> list[object] | None:
    if observer is None:
        return None
    live = []
    for pid, created in known.items():
        try:
            child = observer.Process(pid)
            if abs(child.create_time() - created) < 0.01 and child.is_running() and child.status() != observer.STATUS_ZOMBIE:
                live.append(child)
        except Exception:
            continue
    return live


def _stop_owned_descendants(observer, known: dict[int, float]) -> None:
    live = _live_owned_descendants(observer, known) or []
    for child in live:
        try:
            child.terminate()
        except Exception:
            continue
    deadline = time.monotonic() + 2
    while live and time.monotonic() < deadline:
        time.sleep(0.05)
        live = _live_owned_descendants(observer, known) or []
    for child in live:
        try:
            child.kill()
        except Exception:
            continue


def _run_transaction(args, root, env, timeout=1800, helper=None):
    kwargs = {"cwd": str(root), "env": env, "shell": False, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "creationflags": _transaction_flags()}
    if os.name != "nt": kwargs["process_group"] = 0
    proc = subprocess.Popen(args, **kwargs)
    out, err = _Rolling(), _Rolling()
    observer = _load_process_observer()
    known_descendants: dict[int, float] = {}
    def drain(stream, buf):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk: break
                buf.add(chunk)
        finally: stream.close()
    threads = [threading.Thread(target=drain, args=(proc.stdout, out), daemon=True), threading.Thread(target=drain, args=(proc.stderr, err), daemon=True)]
    for t in threads: t.start()
    deadline = time.monotonic() + timeout; timed_out = False
    while proc.poll() is None and time.monotonic() < deadline:
        _record_descendants(observer, proc, known_descendants)
        time.sleep(.05)
    if proc.poll() is None:
        timed_out = True
        try:
            helper = helper or _load_process_helper(root)
            helper.kill_process_tree(proc)
        except Exception:
            try: proc.kill()
            except OSError: pass
        _stop_owned_descendants(observer, known_descendants)
    try: proc.wait(timeout=3)
    except subprocess.TimeoutExpired: pass
    for t in threads: t.join(timeout=2)
    _record_descendants(observer, proc, known_descendants)
    descendants_quiescent = observer is not None and _live_owned_descendants(observer, known_descendants) == []
    if observer is not None and not descendants_quiescent:
        _stop_owned_descendants(observer, known_descendants)
        descendants_quiescent = _live_owned_descendants(observer, known_descendants) == []
    quiescent = all(not t.is_alive() for t in threads) and proc.poll() is not None and descendants_quiescent
    return subprocess.CompletedProcess(args, proc.returncode, out.text(), err.text()), timed_out, quiescent


def _resolve_target() -> AgentUpdateTarget:
    owner, remote = _gateway_owner(); root = _source_root()
    if remote: return AgentUpdateTarget(root, None, gateway_owner=owner, unsupported_reason=remote)
    if root is None: return AgentUpdateTarget(None, None, gateway_owner=owner, unsupported_reason="Agent source is unavailable")
    if _managed_install(root): return AgentUpdateTarget(root, None, gateway_owner=owner, unsupported_reason="managed or Docker Agent installation")
    interpreter = _candidate(root)
    if interpreter is None: return AgentUpdateTarget(root, None, gateway_owner=owner, unsupported_reason="Agent venv interpreter is unavailable")
    venv = root / "venv"; env = _controlled_env(venv)
    try: identity = _identity(root, interpreter, env=env)
    except Exception as exc: return AgentUpdateTarget(root, interpreter, gateway_owner=owner, venv_root=venv, environment=env, unsupported_reason=f"Agent identity rejected: {_bounded(exc)}")
    health = AgentInstallHealth(identity, (root / ".update-incomplete").exists(), (root / ".lazy-refresh-incomplete").exists(), tuple(identity["critical_modules"]), bool(identity.get("healthy")))
    return AgentUpdateTarget(root, interpreter, identity, owner, venv_root=venv, environment=env, health=health)


def apply_agent_update(*, force: bool = False) -> dict:
    target = _resolve_target()
    if target.unsupported_reason: return AgentUpdateResult("unsupported", detail=target.unsupported_reason).as_dict()
    assert target.source_root and target.interpreter
    root, interpreter = target.source_root, target.interpreter
    before = _git_sha(root)
    pre = target.health; marker_before = pre.incomplete if pre else False
    try:
        helper = _load_process_helper(root)
        result, timed_out, quiescent = _run_transaction([str(interpreter), "-m", "hermes_cli.main", "update", "--yes"], root, target.environment or {}, 1800, helper)
    except RuntimeError as exc:
        return AgentUpdateResult("unsupported", detail=f"Agent process helper rejected: {_bounded(exc)}").as_dict()
    except OSError as exc: return AgentUpdateResult("indeterminate", detail=f"Agent update could not start: {_bounded(exc)}").as_dict()
    output = _bounded((result.stdout or "") + (result.stderr or ""))
    if timed_out: return AgentUpdateResult("indeterminate", result.returncode, f"Agent update timed out; process cleanup quiescence={quiescent}: {output}", quiescent=quiescent).as_dict()
    if not quiescent:
        return AgentUpdateResult("indeterminate", result.returncode, f"Agent update process tree did not reach quiescence: {output}", quiescent=False).as_dict()
    if result.returncode == (pre.identity.get("concurrent_exit") if pre else 2): return AgentUpdateResult("transaction_in_progress", result.returncode, "Another Agent update is in progress; wait and retry later", transaction_in_progress=True, quiescent=quiescent).as_dict()
    if result.returncode != 0: return AgentUpdateResult("failed", result.returncode, output, quiescent=quiescent).as_dict()
    try: after_id = _identity(root, interpreter, env=target.environment)
    except Exception as exc: return AgentUpdateResult("failed", 0, f"Post-update Agent health probe failed: {_bounded(exc)}", quiescent=quiescent).as_dict()
    after = AgentInstallHealth(after_id, (root / ".update-incomplete").exists(), (root / ".lazy-refresh-incomplete").exists(), tuple(after_id["critical_modules"]), bool(after_id.get("healthy")))
    if after.incomplete or not after.healthy:
        return AgentUpdateResult(
            "incomplete",
            exit_code=0,
            detail="Agent update remains incomplete or critical imports are unhealthy; rerun the official update transaction",
            source_before=before,
            source_after=_git_sha(root),
            marker_before=marker_before,
            marker_after=after.incomplete,
            quiescent=quiescent,
        ).as_dict()
    outcome = "repaired" if marker_before or not pre.healthy else "updated"
    warning = output if any(x in output.lower() for x in ("warning", "warn", "failed to refresh")) else ""
    return AgentUpdateResult(
        outcome,
        exit_code=0,
        detail=output or f"Agent update {outcome}",
        warnings_detail=warning,
        source_before=before,
        source_after=_git_sha(root),
        marker_before=marker_before,
        marker_after=after.incomplete,
        reload_eligible=True,
        quiescent=quiescent,
    ).as_dict()


run_agent_update = apply_agent_update
