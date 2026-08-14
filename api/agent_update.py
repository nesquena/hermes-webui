"""Single-install adapter for the Hermes Agent's official update transaction."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


_PROBE = """
import importlib
import json
import pathlib

import hermes_cli
import hermes_cli.main as main

missing = []
for module_name in ("fastapi", "uvicorn", "pydantic", "openai", "yaml"):
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        missing.append(f"{module_name}: {exc}")

print(json.dumps({
    "package": str(pathlib.Path(hermes_cli.__file__).resolve().parent),
    "project": str(pathlib.Path(main.PROJECT_ROOT).resolve()),
    "healthy": not missing,
    "health_detail": "; ".join(missing[:4]),
}))
"""
_MAX_DETAIL = 600
_CREDENTIAL_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s'\"]+)@")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_QUERY_SECRET_RE = re.compile(r"([?&](?:access_token|oauth_token|private_token|client_secret|app_secret|api[_-]?key|token|password|secret|auth|key)=)[^&\s'\"]+", re.IGNORECASE)


@dataclass(frozen=True)
class AgentUpdateTarget:
    source_root: Path | None
    interpreter: Path | None
    identity: dict | None = None
    gateway_owner: str | None = None
    unsupported_reason: str | None = None


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

    def as_dict(self) -> dict:
        payload = {
            "ok": self.outcome in {"updated", "repaired", "noop"},
            "target": "agent",
            "outcome": self.outcome,
            "message": self.detail or f"Agent update {self.outcome}",
            "reload_eligible": self.reload_eligible,
            "restart_scheduled": self.reload_eligible,
        }
        for key in ("exit_code", "source_before", "source_after", "warnings_detail"):
            value = getattr(self, key)
            if value is not None and value != "":
                payload[key] = value
        if self.lock_conflict:
            payload["lock_conflict"] = True
        if self.outcome in {"unsupported", "indeterminate", "failed", "incomplete"}:
            payload["ok"] = False
        return payload


def _bounded(value: object) -> str:
    text = str(value or "").replace("\x00", "")
    updates = sys.modules.get("api.updates")
    if updates is not None:
        return updates._sanitize_git_diagnostic(text, limit=_MAX_DETAIL)
    text = _CREDENTIAL_IN_URL_RE.sub(r"\1<redacted>@", text)
    text = _GITHUB_TOKEN_RE.sub("<redacted>", text)
    text = _QUERY_SECRET_RE.sub(r"\1<redacted>", text)
    return text.strip()[:_MAX_DETAIL]


def _local_gateway(url: str | None) -> bool:
    if not url:
        return True
    try:
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"}:
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _gateway_owner() -> tuple[str | None, str | None]:
    from api.agent_health import _remote_gateway_base_url

    remote = _remote_gateway_base_url()
    if remote:
        return remote, None if _local_gateway(remote) else "remote gateway owner"
    from api.config import get_config
    from api.gateway_chat import _gateway_base_url

    configured = _gateway_base_url(get_config())
    return configured, None if _local_gateway(configured) else "remote gateway owner"


def _source_root() -> Path | None:
    from api.config import _AGENT_DIR

    if _AGENT_DIR is None:
        return None
    try:
        root = Path(_AGENT_DIR).resolve()
        return root if root.is_dir() else None
    except OSError:
        return None


def _managed_install(root: Path) -> bool:
    if os.getenv("HERMES_MANAGED") or os.getenv("HERMES_DOCKER"):
        return True
    try:
        method = (root / ".install_method").read_text(encoding="utf-8").strip().lower()
        if method in {"docker", "nixos", "homebrew", "brew"}:
            return True
    except OSError:
        pass
    homes = [root.parent]
    configured_home = os.getenv("HERMES_HOME", "").strip()
    if configured_home:
        homes.append(Path(configured_home).expanduser())
    return any((home / ".managed").exists() for home in homes)


def _candidate(root: Path) -> Path | None:
    # The Agent transaction repairs the install-root venv, not a dev .venv.
    names = ("venv/bin/python", "venv/Scripts/python.exe")
    for name in names:
        candidate = root / name
        try:
            if candidate.is_file() and candidate.absolute().is_relative_to(root):
                return candidate
        except OSError:
            continue
    return None


def _flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _run(args: list[str], root: Path, timeout: float):
    return subprocess.run(
        args,
        cwd=str(root),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_flags(),
    )


def _git_sha(root: Path) -> str | None:
    try:
        result = _run(["git", "rev-parse", "HEAD"], root, 10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _marker(root: Path) -> bool:
    return (root / ".update-incomplete").exists()


def _identity(root: Path, interpreter: Path) -> dict:
    result = _run([str(interpreter), "-c", _PROBE], root, 30)
    if result.returncode != 0:
        raise RuntimeError(_bounded(result.stderr or result.stdout or "identity probe failed"))
    import json

    value = json.loads(result.stdout)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("healthy"), bool)
        or not isinstance(value.get("package"), str)
        or not isinstance(value.get("project"), str)
        or not isinstance(value.get("health_detail"), str)
    ):
        raise RuntimeError("Agent identity probe returned malformed health state")
    if Path(value["package"]).resolve() != root / "hermes_cli" or Path(value["project"]).resolve() != root:
        raise RuntimeError("Agent interpreter identity does not match source root")
    return value


def _resolve_target() -> AgentUpdateTarget:
    owner, remote_reason = _gateway_owner()
    root = _source_root()
    if remote_reason:
        return AgentUpdateTarget(root, None, gateway_owner=owner, unsupported_reason=remote_reason)
    if root is None:
        return AgentUpdateTarget(None, None, gateway_owner=owner, unsupported_reason="Agent source is unavailable")
    if _managed_install(root):
        return AgentUpdateTarget(root, None, gateway_owner=owner, unsupported_reason="managed or Docker Agent installation")
    interpreter = _candidate(root)
    if interpreter is None:
        return AgentUpdateTarget(root, None, gateway_owner=owner, unsupported_reason="Agent venv interpreter is unavailable")
    return AgentUpdateTarget(root, interpreter, gateway_owner=owner)


def apply_agent_update(*, force: bool = False) -> dict:
    target = _resolve_target()
    if target.unsupported_reason:
        return AgentUpdateResult("unsupported", detail=target.unsupported_reason).as_dict()
    assert target.source_root is not None and target.interpreter is not None
    root, interpreter = target.source_root, target.interpreter
    before = _git_sha(root)
    marker_before = _marker(root)
    try:
        identity = _identity(root, interpreter)
    except (OSError, TypeError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        return AgentUpdateResult("unsupported", detail=f"Agent identity rejected: {_bounded(exc)}").as_dict()
    if not identity.get("healthy"):
        before_probe = False
    else:
        before_probe = True
    try:
        result = _run([str(interpreter), "-m", "hermes_cli.main", "update", "--yes"], root, 1800)
    except subprocess.TimeoutExpired as exc:
        return AgentUpdateResult("indeterminate", detail=f"Agent update timed out: {_bounded(exc)}").as_dict()
    except OSError as exc:
        return AgentUpdateResult("indeterminate", detail=f"Agent update could not start: {_bounded(exc)}").as_dict()
    output = _bounded((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        lock = "update is already running" in output.lower() or ".hermes-update-in-progress" in output.lower()
        return AgentUpdateResult("failed", result.returncode, output, lock_conflict=lock).as_dict()
    try:
        after_identity = _identity(root, interpreter)
    except (OSError, TypeError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        return AgentUpdateResult("failed", 0, f"Post-update Agent health probe failed: {_bounded(exc)}").as_dict()
    after = _git_sha(root)
    marker_after = _marker(root)
    if marker_after:
        return AgentUpdateResult("incomplete", 0, "Agent update remains incomplete; rerun the official update transaction", source_before=before, source_after=after, marker_before=marker_before, marker_after=True).as_dict()
    if not after_identity.get("healthy"):
        return AgentUpdateResult("failed", 0, "Agent post-update import health is unhealthy", source_before=before, source_after=after).as_dict()
    if marker_before and not marker_after:
        outcome = "repaired"
    elif not before_probe:
        outcome = "repaired"
    elif before is None or after is None:
        outcome = "updated"
    else:
        # Equal Git HEAD can still hide dependency or generated-install changes.
        outcome = "updated"
    warning = output if any(token in output.lower() for token in ("warning", "warn", "failed to refresh")) else ""
    return AgentUpdateResult(outcome, 0, output or f"Agent update {outcome}", warning, before, after, marker_before, marker_after, reload_eligible=outcome in {"updated", "repaired"}).as_dict()


run_agent_update = apply_agent_update
