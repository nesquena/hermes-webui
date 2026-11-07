"""Hermes agent/gateway heartbeat payload helpers (#716, #1879).

The WebUI process is not always paired with a long-running Hermes gateway. Some
setups use WebUI only, while self-hosted messaging deployments run a separate
Hermes gateway daemon that records runtime metadata in the Hermes Agent home.
This module turns those existing safe runtime signals into a small UI-facing
heartbeat without shelling out or adding psutil as a hard dependency.

Cross-container note (#1879): ``gateway.status.get_running_pid()`` uses
``fcntl.flock`` and ``os.kill(pid, 0)``, both of which require the caller to
share a PID namespace with the gateway process. In multi-container deployments
where the WebUI runs separately from ``hermes-agent`` and only a Hermes data
volume is shared, those checks always return ``None`` and the dashboard
incorrectly shows "Gateway not running". To stay accurate without forcing a
``pid: "service:hermes-agent"`` compose workaround, we accept a recent
``updated_at`` timestamp on ``gateway_state.json`` (combined with
``gateway_state == "running"``) as an equivalent live-process signal.  Older
gateway builds do not refresh that file periodically, so a stale
``gateway_state == "running"`` record is treated as inconclusive rather than a
confirmed outage.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger(__name__)

_GATEWAY_PID_FILE = "gateway.pid"
_GATEWAY_RUNTIME_STATUS_FILE = "gateway_state.json"


# Two cron ticks (~60s each). Chosen to avoid false negatives during brief
# gateway restarts while still surfacing a true outage within a couple of
# minutes. Override is intentionally not exposed: keep the check deterministic
# and identical across deployments so support diagnostics are reproducible.
GATEWAY_FRESHNESS_THRESHOLD_S: float = 120.0


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_status_is_fresh(
    runtime_status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    threshold_s: float = GATEWAY_FRESHNESS_THRESHOLD_S,
) -> bool:
    """Return ``True`` when ``gateway_state.json`` looks freshly written.

    "Fresh" means the gateway self-reported ``running`` and the ``updated_at``
    ISO-8601 timestamp is no older than ``threshold_s`` seconds. This is the
    cross-container liveness signal used when ``get_running_pid()`` returns
    ``None`` purely because of PID-namespace isolation (#1879).

    Any unparseable input is treated as "not fresh" — a stale or missing
    timestamp must never report alive.
    """
    if not isinstance(runtime_status, dict):
        return False
    if runtime_status.get("gateway_state") != "running":
        return False

    raw_updated_at = runtime_status.get("updated_at")
    if not isinstance(raw_updated_at, str) or not raw_updated_at:
        return False

    # ``datetime.fromisoformat`` accepts the exact format gateway/status.py
    # writes (``datetime.now(timezone.utc).isoformat()``). We deliberately
    # don't pull in dateutil — keeping this stdlib-only matches the rest of
    # this module.
    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except (TypeError, ValueError):
        return False

    if updated_at.tzinfo is None:
        # A naive timestamp could mean anything across containers / hosts.
        # Refuse to interpret it rather than assume UTC.
        return False

    reference = now if now is not None else datetime.now(timezone.utc)
    age_s = (reference - updated_at).total_seconds()
    if age_s < 0:
        # Clock skew between containers can produce small negatives. A future
        # timestamp is still a "fresh" signal — the gateway clearly wrote it
        # very recently — so accept it. A wildly-future timestamp (> threshold
        # in the future) is rejected to avoid trusting a broken clock.
        return -age_s <= threshold_s
    return age_s <= threshold_s


def _runtime_status_is_stale_stopped(
    runtime_status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    threshold_s: float = GATEWAY_FRESHNESS_THRESHOLD_S,
) -> bool:
    """Return ``True`` for an old clean-stop root gateway state.

    A user may run only profile-scoped gateways while a root
    ``gateway_state.json`` from an older, intentionally stopped gateway remains
    on disk (#1944). Treat that stale stopped file like "no root gateway
    configured" so the heartbeat banner does not keep warning about a service
    the user is not running. Fresh stopped state still reports down.
    """
    if not isinstance(runtime_status, dict):
        return False
    if runtime_status.get("gateway_state") != "stopped":
        return False

    raw_updated_at = runtime_status.get("updated_at")
    if not isinstance(raw_updated_at, str) or not raw_updated_at:
        return False

    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except (TypeError, ValueError):
        return False
    if updated_at.tzinfo is None:
        return False

    reference = now if now is not None else datetime.now(timezone.utc)
    age_s = (reference - updated_at).total_seconds()
    return age_s > threshold_s


def _runtime_status_is_stale_running(
    runtime_status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    threshold_s: float = GATEWAY_FRESHNESS_THRESHOLD_S,
) -> bool:
    """Return ``True`` when the gateway last self-reported running, but stale.

    WebUI often runs in a separate container from the gateway. In that shape PID
    checks can be impossible, and older gateway versions only update
    ``gateway_state.json`` on lifecycle/platform changes. A stale ``running``
    file therefore means "not enough information from WebUI" rather than
    "gateway is down".
    """
    if not isinstance(runtime_status, dict):
        return False
    if runtime_status.get("gateway_state") != "running":
        return False

    raw_updated_at = runtime_status.get("updated_at")
    if not isinstance(raw_updated_at, str) or not raw_updated_at:
        return False

    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except (TypeError, ValueError):
        return False
    if updated_at.tzinfo is None:
        return False

    reference = now if now is not None else datetime.now(timezone.utc)
    age_s = (reference - updated_at).total_seconds()
    return age_s > threshold_s


def _gateway_status_module():
    """Load gateway.status lazily so tests and WebUI-only installs stay isolated."""
    return importlib.import_module("gateway.status")


def _gateway_root_pid_path() -> Path | None:
    """Return the root Hermes gateway PID path.

    Gateway runtime files are root-level singletons.  A profile-scoped WebUI
    process may have HERMES_HOME=<root>/profiles/<name>, but gateway.pid,
    gateway.lock, and gateway_state.json still live under <root>.

    When the root-level gateway.pid is absent (profile-scoped gateway
    deployments write it under <root>/profiles/<name>/), fall back to the
    active profile's directory so the gateway is detected correctly.
    """
    try:
        from hermes_constants import get_default_hermes_root
        root_pid = get_default_hermes_root() / _GATEWAY_PID_FILE
        if root_pid.exists():
            return root_pid
        try:
            from api.profiles import get_active_hermes_home
            profile_pid = Path(get_active_hermes_home()) / _GATEWAY_PID_FILE
            if profile_pid.exists():
                return profile_pid
        except Exception:
            pass
        return root_pid
    except Exception:
        return None


def _read_runtime_status_path(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _read_gateway_runtime_status(gateway_status: Any, pid_path: Path | None) -> dict[str, Any] | None:
    read_runtime_status = gateway_status.read_runtime_status
    if pid_path is not None:
        try:
            return read_runtime_status(pid_path=pid_path)
        except TypeError:
            runtime_status_file = str(
                getattr(gateway_status, "_RUNTIME_STATUS_FILE", _GATEWAY_RUNTIME_STATUS_FILE)
            )
            runtime_status_path = pid_path.with_name(runtime_status_file)
            try:
                return read_runtime_status(runtime_status_path)
            except TypeError:
                if getattr(gateway_status, "__name__", "") == "gateway.status" or hasattr(
                    gateway_status,
                    "_read_json_file",
                ):
                    runtime_status = _read_runtime_status_path(runtime_status_path)
                    if runtime_status is not None:
                        return runtime_status
    return read_runtime_status()


def _gateway_running_pid(gateway_status: Any, pid_path: Path | None) -> int | None:
    get_running_pid = gateway_status.get_running_pid
    if pid_path is not None:
        try:
            return get_running_pid(pid_path=pid_path, cleanup_stale=False)
        except TypeError:
            try:
                return get_running_pid(pid_path, cleanup_stale=False)
            except TypeError:
                pass
    try:
        return get_running_pid(cleanup_stale=False)
    except TypeError:
        # Older agent versions may not expose cleanup_stale. Keep compatibility.
        return get_running_pid()


def _callable_code_declares_parameter(get_running_pid: Any, name: str) -> bool:
    """Return True when a Python callable's own code declares *name*."""
    target = getattr(get_running_pid, "__func__", get_running_pid)
    code = getattr(target, "__code__", None)
    if code is None:
        return False
    declared_count = int(getattr(code, "co_argcount", 0)) + int(
        getattr(code, "co_kwonlyargcount", 0)
    )
    return name in code.co_varnames[:declared_count]


def _declared_pid_path_parameter(
    get_running_pid: Any,
) -> tuple[inspect.Parameter, int, list[inspect.Parameter]] | None:
    try:
        signature = inspect.signature(get_running_pid, follow_wrapped=False)
    except (TypeError, ValueError):
        return None
    params = list(signature.parameters.values())
    for idx, param in enumerate(params):
        if param.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            continue
        name = str(param.name or "").lower()
        if (
            "path" in name
            and "pid" in name
            and _callable_code_declares_parameter(get_running_pid, param.name)
        ):
            positional_index = sum(
                1
                for earlier in params[:idx]
                if earlier.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            )
            return param, positional_index, params
    return None


def _positional_only_pid_args(
    params: list[inspect.Parameter],
    positional_index: int,
    pid_path: Path,
) -> list[Any] | None:
    positional_params = [
        param
        for param in params
        if param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if positional_index >= len(positional_params):
        return None
    args: list[Any] = []
    for param in positional_params[:positional_index]:
        if param.default is inspect.Parameter.empty:
            return None
        args.append(param.default)
    args.append(pid_path)
    return args


def _accepts_cleanup_stale_keyword(params: list[inspect.Parameter]) -> bool:
    return any(
        param.name == "cleanup_stale"
        and param.kind
        in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for param in params
    )


def _gateway_running_pid_strict_path(gateway_status: Any, pid_path: Path) -> int | None:
    """Read a PID from an explicit path only; never fall back to ambient state."""
    get_running_pid = gateway_status.get_running_pid
    pid_path_match = _declared_pid_path_parameter(get_running_pid)
    if pid_path_match is None:
        return None
    pid_path_param, positional_index, params = pid_path_match

    if pid_path_param.kind is inspect.Parameter.KEYWORD_ONLY:
        kwargs = {pid_path_param.name: pid_path}
        try:
            return get_running_pid(**kwargs, cleanup_stale=False)
        except TypeError:
            try:
                return get_running_pid(**kwargs)
            except TypeError:
                return None

    if pid_path_param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
        kwargs = {pid_path_param.name: pid_path}
        try:
            return get_running_pid(**kwargs, cleanup_stale=False)
        except TypeError:
            try:
                return get_running_pid(**kwargs)
            except TypeError:
                return None

    if pid_path_param.kind is inspect.Parameter.POSITIONAL_ONLY:
        args = _positional_only_pid_args(params, positional_index, pid_path)
        if args is None:
            return None
        if _accepts_cleanup_stale_keyword(params):
            try:
                return get_running_pid(*args, cleanup_stale=False)
            except TypeError:
                pass
        try:
            return get_running_pid(*args)
        except TypeError:
            return None
    return None


def get_active_profile_gateway_running_pid(profile: str | None = None) -> int | None:
    """Return the confirmed active-profile PID without state fallbacks.

    Update recovery needs process identity, not just a recent ``running`` state:
    an old gateway can remain alive when the restart CLI fails before executing.
    Use the same active-profile home targeted by the restart helper rather than
    the default-root health path. Keep this fail-closed when status is unavailable.
    """
    try:
        from api.profiles import get_active_hermes_home, get_hermes_home_for_profile

        gateway_status = _gateway_status_module()
        if profile is None:
            gateway_home = get_active_hermes_home()
        else:
            gateway_home = get_hermes_home_for_profile(profile)
        gateway_pid_path = Path(gateway_home) / _GATEWAY_PID_FILE
        return _gateway_running_pid_strict_path(gateway_status, gateway_pid_path)
    except Exception:
        return None


def _runtime_detail_subset(runtime_status: dict[str, Any] | None) -> dict[str, Any]:
    """Return only non-sensitive runtime fields for the browser.

    gateway.status records argv/PID metadata so the CLI can validate process
    identity. The WebUI alert only needs health semantics, never raw command
    lines, paths, environment, or tokens.
    """
    if not isinstance(runtime_status, dict):
        return {}

    details: dict[str, Any] = {}
    gateway_state = runtime_status.get("gateway_state")
    if isinstance(gateway_state, str) and gateway_state:
        details["gateway_state"] = gateway_state

    updated_at = runtime_status.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        details["updated_at"] = updated_at

    try:
        details["active_agents"] = max(0, int(runtime_status.get("active_agents") or 0))
    except (TypeError, ValueError):
        pass

    platforms = runtime_status.get("platforms")
    if isinstance(platforms, dict):
        details["platform_count"] = len(platforms)
        states: dict[str, int] = {}
        for payload in platforms.values():
            if not isinstance(payload, dict):
                continue
            state = payload.get("state")
            if isinstance(state, str) and state:
                states[state] = states.get(state, 0) + 1
        if states:
            details["platform_states"] = states

    return details


# Remote-gateway probe (#3281)
# ------------------------------------------------------------------
# In multi-container Docker deployments the WebUI container does not ship the
# ``gateway`` Python package. The lazy ``importlib.import_module("gateway.status")``
# therefore raises ``ModuleNotFoundError`` and the payload falls through to
# ``gateway_not_configured`` even though ``HERMES_API_URL`` points at a perfectly
# reachable remote gateway. The Tasks/Cron banner then shows a spurious amber
# "Gateway not configured" warning.
#
# When a gateway base URL is set in any supported env var, we treat that as an
# explicit declaration that the gateway lives elsewhere, and probe it over HTTP
# before touching any local filesystem / module signal. The probe result is
# cached briefly so a dashboard rerender that fans out to multiple panels does
# not hammer the gateway.

_REMOTE_PROBE_TIMEOUT_S: float = 2.0
_REMOTE_PROBE_CACHE_TTL_S: float = 5.0
_REMOTE_PROBE_PATHS: tuple[str, ...] = ("/health/detailed", "/health", "/v1/health")
# A gateway health payload is small JSON; cap the 2xx body read so a large or
# slow-trickled remote response can't hang /api/health/agent or balloon memory.
_REMOTE_PROBE_BODY_LIMIT_BYTES: int = 64 * 1024

_remote_probe_lock = threading.Lock()
# Condition wraps the same lock so cache reads/writes and single-flight waits
# share one mutex (mirrors the Condition(_lock) idiom in api/session_lifecycle).
_remote_probe_cond = threading.Condition(_remote_probe_lock)
_remote_probe_cache: dict[str, Any] = {"url": None, "expires_at": 0.0, "result": None}
# base_urls currently being probed by a "leader" thread. Latecomers wait on the
# Condition for the leader's result instead of stampeding the (possibly dead)
# gateway themselves (#5455 dashboard fan-out, #2476).
_remote_probe_inflight: set[str] = set()
def _remote_probe_wait_budget_s() -> float:
    """How long a latecomer waits for the leader before giving up and self-probing.

    The leader can walk every path (each up to ``_REMOTE_PROBE_TIMEOUT_S``), so
    budget for the full walk plus a small margin; timing out is a safety valve
    against a hung leader, never the normal path. Computed on each call (not a
    module-level constant) so a test that monkeypatches the timeout or the path
    list gets a budget consistent with those values instead of a stale one.
    """
    return _REMOTE_PROBE_TIMEOUT_S * len(_REMOTE_PROBE_PATHS) + 1.0


def _remote_gateway_base_url() -> str | None:
    """Return an explicit remote gateway base URL, or None for local-only setups.

    Priority: GATEWAY_HEALTH_URL > HERMES_GATEWAY_HEALTH_URL > HERMES_API_URL
    > HERMES_WEBUI_GATEWAY_BASE_URL.
    Returns ``None`` when no env var is set so the caller falls through to
    local PID/state checks.

    Any of these env vars may legitimately point AT a health endpoint
    (e.g. ``GATEWAY_HEALTH_URL=http://host:8642/health``). Since the probe
    appends ``/health/detailed`` etc. to the returned base, strip a trailing
    health-path suffix first so we don't build ``/health/health/detailed``
    (mirrors the normalization in api/updates.py).
    """
    for var in (
        "GATEWAY_HEALTH_URL",
        "HERMES_GATEWAY_HEALTH_URL",
        "HERMES_API_URL",
        "HERMES_WEBUI_GATEWAY_BASE_URL",
    ):
        val = os.environ.get(var, "").strip()
        if val:
            base = val.rstrip("/")
            for suffix in ("/health/detailed", "/health", "/v1/health", "/status"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)].rstrip("/")
                    break
            return base
    return None


# Gateway lifecycle capability (#8391)
# ------------------------------------------------------------------
# The status path above can monitor a Gateway WebUI does not run (an explicit
# remote health URL), while every lifecycle entrypoint (Settings Start/Stop/
# Restart, the health-banner restart, the Agent-update restart) drives the LOCAL
# ``hermes gateway`` CLI for the active profile. Pairing the two let WebUI offer
# to start a local Gateway next to a healthy remote one, or "restart" a remote
# Gateway by restarting an unrelated local process. This is the one
# server-owned answer to "does the local CLI control the Gateway WebUI is
# monitoring?"; the UI and every lifecycle caller read it instead of inferring
# ownership from status or from the chat-backend selection.

GATEWAY_EXTERNALLY_MANAGED_MESSAGE = (
    "This Gateway is managed outside WebUI. Start, stop, or restart it where it runs."
)


def _is_loopback_base_url(base_url: str) -> bool:
    """Return True only for an http(s) URL whose host is a loopback literal.

    Hostname matching is exact (``localhost`` or a loopback IP), so look-alike
    names such as ``127.0.0.1.example.com`` or ``localhost.example.com`` are not
    treated as local, and no DNS resolution happens on this path.
    """
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"}:
        return False
    host = (parts.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Persisted local-ownership proof for a loopback Gateway URL. A live PID is the
# only way to PROVE that the local CLI owns the Gateway behind a loopback URL,
# but liveness cannot be the ongoing requirement: Stop, a crash, or a restart
# whose replacement failed to launch all remove the PID, and the very next
# action (Start, the restart retry) must still be allowed. So the proof, once
# observed, is recorded in WebUI's own state dir and keyed by the resolved
# profile home + the exact normalized endpoint. It never authorizes a
# non-loopback URL, another endpoint, or another profile home; a different
# configured endpoint for that home drops it (target changed -> prove again).
_GATEWAY_OWNERSHIP_FILENAME = "gateway_lifecycle_ownership.json"
_GATEWAY_OWNERSHIP_VERSION = 1
_gateway_ownership_lock = threading.Lock()


def _gateway_ownership_record_path() -> Path:
    """WebUI-owned record file (never inside an Agent profile home)."""
    from api.config import STATE_DIR

    return Path(STATE_DIR) / _GATEWAY_OWNERSHIP_FILENAME


def _gateway_profile_home_key(profile: str | None) -> str:
    """Resolved home of the profile the lifecycle CLI targets (same resolution
    as ``get_active_profile_gateway_running_pid``)."""
    from api.profiles import get_active_hermes_home, get_hermes_home_for_profile

    home = get_active_hermes_home() if profile is None else get_hermes_home_for_profile(profile)
    return os.path.realpath(os.fspath(home))


def _normalized_gateway_endpoint(base_url: str) -> str | None:
    """``scheme://host:port[/path]`` identity of a monitored Gateway URL.

    Userinfo, query and fragment are dropped so credentials are never
    persisted; the default port is made explicit so ``:80`` and no port match.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").strip().lower()
    if scheme not in {"http", "https"} or not host:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}{parts.path.rstrip('/')}"


def _read_gateway_ownership_owners() -> dict[str, Any]:
    """Return the ``owners`` map; any unreadable/malformed record reads empty."""
    try:
        raw = _gateway_ownership_record_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != _GATEWAY_OWNERSHIP_VERSION:
        return {}
    owners = data.get("owners")
    return owners if isinstance(owners, dict) else {}


def _write_gateway_ownership_owners(owners: dict[str, Any]) -> None:
    """Atomically replace the record (caller holds ``_gateway_ownership_lock``)."""
    import tempfile

    path = _gateway_ownership_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": _GATEWAY_OWNERSHIP_VERSION, "owners": owners},
        indent=2,
        sort_keys=True,
    )
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _record_gateway_ownership(home_key: str, endpoint: str) -> None:
    """Persist proof that ``home_key``'s local Gateway was running behind
    ``endpoint``. Best effort: a failed write only means the next stopped-state
    check fails closed (it never grants anything)."""
    try:
        with _gateway_ownership_lock:
            try:
                owners = dict(_read_gateway_ownership_owners())
            except Exception:
                owners = {}  # malformed record: replace it with a valid one
            current = owners.get(home_key)
            if isinstance(current, dict) and current.get("endpoint") == endpoint:
                return
            owners[home_key] = {"endpoint": endpoint, "proven_at": _checked_at()}
            _write_gateway_ownership_owners(owners)
    except Exception:
        logger.debug("Could not persist Gateway lifecycle ownership proof", exc_info=True)


def _gateway_ownership_proven(home_key: str, endpoint: str) -> bool:
    """True only when the record holds exactly this home + endpoint pair.

    A record for this home naming a different endpoint is dropped: the
    monitored target changed, so ownership must be proven again.
    """
    with _gateway_ownership_lock:
        owners = _read_gateway_ownership_owners()
        current = owners.get(home_key)
        if not isinstance(current, dict):
            return False
        if current.get("endpoint") == endpoint:
            return True
        remaining = {key: value for key, value in owners.items() if key != home_key}
        try:
            _write_gateway_ownership_owners(remaining)
        except Exception:
            logger.debug("Could not drop stale Gateway lifecycle ownership proof", exc_info=True)
        return False


_DEFAULT_GATEWAY_START_COMMAND = "hermes gateway start"


def _gateway_start_command(profile: str | None = None) -> str:
    """The ``hermes gateway start`` command line for the capability's profile.

    Mirrors the profile rule of ``routes._run_gateway_lifecycle_command`` (the
    Settings Start button): the explicit ``profile`` if given, else the active
    profile name; ``--profile <name>`` only when that name is non-empty and not
    ``default``. Only a valid profile id is ever echoed back to the browser;
    anything else (or any error) falls back to the plain command.
    """
    try:
        from api.profiles import _PROFILE_ID_RE, get_active_profile_name

        name = str((profile if profile is not None else get_active_profile_name()) or "").strip()
        if not name or name == "default":
            return _DEFAULT_GATEWAY_START_COMMAND
        if _PROFILE_ID_RE.fullmatch(name):
            return f"hermes --profile {name} gateway start"
    except Exception:
        logger.debug("Could not resolve profile for the Gateway start hint", exc_info=True)
    return _DEFAULT_GATEWAY_START_COMMAND


def gateway_lifecycle_capability(profile: str | None = None) -> dict[str, Any]:
    """Return whether WebUI's local CLI controls the Gateway it monitors.

    ``{"managed": bool, "reason": str}`` (plus ``start_command`` for
    ``local_gateway_unproven``, see below):

    * No remote Gateway URL configured: status comes from this host's
      active-profile Gateway files, which is exactly what ``hermes gateway
      start|stop|restart`` targets -> managed (``local_gateway``).
    * A remote URL on a loopback host, for which this profile home's local
      Gateway has been PROVEN: a confirmed running Gateway process for the
      target profile on this host, observed now or previously behind this exact
      endpoint (persisted proof, see above) -> managed (``local_gateway``). The
      persisted proof keeps Start available after Stop/crash and lets a failed
      restart be retried. A loopback URL alone is not proof: Docker port maps
      and SSH tunnels also answer on 127.0.0.1.
    * A loopback URL with a resolvable endpoint and profile home but no proof
      yet (no live Gateway PID for the profile, no persisted record for this
      home + endpoint) -> not managed (``local_gateway_unproven``). Only the
      reason differs from ``external_gateway``, so the UI can tell a stopped
      Gateway of this machine apart from one that runs elsewhere; every
      lifecycle entrypoint still refuses it. This result alone also carries
      ``start_command``: the exact one-time CLI start for the target profile
      (``hermes gateway start``, or ``hermes --profile <name> gateway start``
      for a named profile, like the Settings Start button runs).
    * Anything else with a remote URL -> not managed (``external_gateway``).

    Fail-closed: any error while proving local ownership, or an endpoint or
    profile home that does not resolve, reports ``external_gateway``.
    """
    remote_base = _remote_gateway_base_url()
    if remote_base is None:
        return {"managed": True, "reason": "local_gateway"}
    try:
        if _is_loopback_base_url(remote_base):
            endpoint = _normalized_gateway_endpoint(remote_base)
            home_key = _gateway_profile_home_key(profile)
            if endpoint and home_key:
                if get_active_profile_gateway_running_pid(profile=profile) is not None:
                    # Record only if the target home did not change while the
                    # PID was read (an active-profile switch mid-check).
                    if _gateway_profile_home_key(profile) == home_key:
                        _record_gateway_ownership(home_key, endpoint)
                    return {"managed": True, "reason": "local_gateway"}
                if _gateway_ownership_proven(home_key, endpoint):
                    return {"managed": True, "reason": "local_gateway"}
                # Loopback, but never seen running for this home + endpoint
                # (e.g. upgraded while the Gateway was stopped). Still
                # unmanaged; the distinct reason only changes the UI copy.
                return {
                    "managed": False,
                    "reason": "local_gateway_unproven",
                    "start_command": _gateway_start_command(profile),
                }
    except Exception:
        pass
    return {"managed": False, "reason": "external_gateway"}


def _remote_gateway_api_key() -> str:
    """Return the Bearer token for authenticated gateway health probes.

    Mirrors ``api.gateway_chat._gateway_api_key``: WebUI containers in
    multi-service deployments must present the same key the agent's API server
    expects on ``/health/detailed`` (#5418).
    """
    return str(
        os.environ.get("HERMES_WEBUI_GATEWAY_API_KEY")
        or os.environ.get("API_SERVER_KEY")
        or ""
    ).strip()


def _http_probe(
    url: str,
    timeout_s: float,
    *,
    api_key: str | None = None,
) -> tuple[bool, int | None, str | None, bytes | None]:
    """GET ``url`` and return (ok, status_code, error_name, body).

    ``ok`` is True only for a 2xx response. 5xx and network errors are not OK.
    4xx is also treated as "responded" (the gateway is up, just answering 404
    on this particular path) so the caller can move on to the next path.
    ``body`` is the raw response bytes for 2xx responses, None otherwise.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib_request.Request(url, method="GET", headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - trusted env var URL
            status = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= int(status) < 300
            # Cap the body read: we only need a small JSON health payload, and an
            # unbounded resp.read() on a large/trickled 2xx body could hang the
            # /api/health/agent handler or balloon memory. Read one byte over the
            # cap so the caller can detect (and skip) an oversized body.
            body = resp.read(_REMOTE_PROBE_BODY_LIMIT_BYTES + 1) if ok else None
            return (ok, int(status), None, body)
    except urllib_error.HTTPError as exc:
        return (False, int(exc.code), "HTTPError", None)
    except Exception as exc:  # urllib_error.URLError, socket.timeout, ssl, etc.
        return (False, None, type(exc).__name__, None)


def _cached_remote_result_locked(base_url: str, current: float) -> dict[str, Any] | None:
    """Return a fresh cached probe result for *base_url*, or None if stale/absent.

    Must be called while holding ``_remote_probe_cond``.
    """
    if (
        _remote_probe_cache.get("url") == base_url
        and _remote_probe_cache.get("expires_at", 0.0) > current
        and _remote_probe_cache.get("result") is not None
    ):
        return _remote_probe_cache["result"]
    return None


def _run_remote_probe(base_url: str) -> dict[str, Any]:
    """Walk the remote gateway health paths and build a payload (no caching/locking).

    This is the expensive, network-bound step: each path can block up to
    ``_REMOTE_PROBE_TIMEOUT_S``. It runs OUTSIDE the probe lock so a single
    "leader" thread does it while latecomers wait for the cached result.
    """
    last_status: int | None = None
    last_error: str | None = None
    gateway_api_key = _remote_gateway_api_key()
    for path in _REMOTE_PROBE_PATHS:
        probe_key = gateway_api_key if path == "/health/detailed" else None
        ok, status, err, body = _http_probe(
            base_url + path,
            _REMOTE_PROBE_TIMEOUT_S,
            api_key=probe_key,
        )
        if ok:
            details: dict[str, Any] = {
                "state": "alive",
                "reason": "remote_gateway",
                "endpoint": base_url + path,
                "status_code": status,
            }
            if body and len(body) <= _REMOTE_PROBE_BODY_LIMIT_BYTES:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict) and "gateway_state" in data:
                        details["gateway_state"] = data["gateway_state"]
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            # An over-cap body (len > limit, i.e. the +1 sentinel byte was read)
            # is treated as "alive but no parseable gateway_state" — we still
            # report the gateway as up, just without the detailed state.
            return {
                "alive": True,
                "checked_at": _checked_at(),
                "details": details,
            }
        # Remember the most informative failure signal we saw.
        if status is not None:
            last_status = status
        if err is not None:
            last_error = err

    details = {
        "state": "down",
        "reason": "remote_gateway_unreachable",
        "endpoint": base_url,
    }
    if last_status is not None:
        details["status_code"] = last_status
    if last_error is not None:
        details["error"] = last_error
    return {
        "alive": False,
        "checked_at": _checked_at(),
        "details": details,
    }


def _probe_remote_gateway(base_url: str, *, now: float | None = None) -> dict[str, Any]:
    """Return an agent-health payload dict for a remote gateway base URL.

    Result is cached for ``_REMOTE_PROBE_CACHE_TTL_S`` seconds per base_url.

    Concurrency (single-flight): when several dashboard panels fan out on a cold
    cache, only the first "leader" thread runs the ~2s-per-path network probe;
    latecomers wait on ``_remote_probe_cond`` for the leader's cached result
    rather than each hammering the (possibly dead) gateway (#5455, #2476). The
    leader always clears the in-flight marker and wakes waiters — even on error —
    so waiters can never deadlock.
    """
    current = time.monotonic() if now is None else now
    with _remote_probe_cond:
        cached = _cached_remote_result_locked(base_url, current)
        if cached is not None:
            # Refresh checked_at so the UI shows a current timestamp without
            # actually re-hitting the gateway.
            return {**cached, "checked_at": _checked_at()}

        # A leader is already probing this base_url: wait for its result instead
        # of starting a duplicate probe.
        if base_url in _remote_probe_inflight:
            deadline = current + _remote_probe_wait_budget_s()
            while base_url in _remote_probe_inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break  # leader stuck; fall through and probe ourselves
                _remote_probe_cond.wait(remaining)
            cached = _cached_remote_result_locked(base_url, time.monotonic())
            if cached is not None:
                return {**cached, "checked_at": _checked_at()}
            # Woke without a usable cached result (leader failed, produced no
            # cacheable result, or we timed out): become a leader ourselves.

        # Become the leader for this base_url.
        _remote_probe_inflight.add(base_url)

    payload: dict[str, Any] | None = None
    try:
        payload = _run_remote_probe(base_url)
    finally:
        with _remote_probe_cond:
            if payload is not None:
                _remote_probe_cache["url"] = base_url
                # Expire from the moment the probe COMPLETES, not from the
                # leader's entry time: walking every path of a hung gateway
                # takes len(paths) * timeout (~6s) which exceeds the 5s TTL, so
                # `current + TTL` would write an already-expired cache line.
                # Waiters woken right after this would then miss the cache and
                # each re-probe the dead gateway — collapsing single-flight and
                # regressing latency to worse-than-serial (#5455, #2476).
                _remote_probe_cache["expires_at"] = (
                    time.monotonic() + _REMOTE_PROBE_CACHE_TTL_S
                )
                _remote_probe_cache["result"] = payload
            # Always release the in-flight marker and wake waiters, even if the
            # probe raised — otherwise latecomers would wait out the full budget.
            _remote_probe_inflight.discard(base_url)
            _remote_probe_cond.notify_all()
    # Only reached when _run_remote_probe returned normally (a raise would have
    # propagated through the finally), so payload is always a dict here. The
    # assert makes that explicit for the type checker (declared -> dict[str, Any]).
    assert payload is not None
    return payload


def _reset_remote_probe_cache_for_tests() -> None:
    """Test hook: clear the in-process remote-probe cache and in-flight state."""
    with _remote_probe_cond:
        _remote_probe_cache["url"] = None
        _remote_probe_cache["expires_at"] = 0.0
        _remote_probe_cache["result"] = None
        _remote_probe_inflight.clear()
        _remote_probe_cond.notify_all()


def build_agent_health_payload() -> dict[str, Any]:
    """Return `{alive, checked_at, details}` for the Hermes gateway/agent.

    `alive` is intentionally tri-state:
      * True: a gateway runtime signal says the process is alive.
      * False: gateway metadata exists, but no live gateway process owns it.
      * None: no gateway metadata/status is available, so this WebUI setup is
        probably not configured with a separate gateway process.
    """
    checked_at = _checked_at()

    # Multi-container deployments (#3281): when HERMES_API_URL is set the
    # gateway lives in another container/host. Probe it over HTTP before
    # touching local module/pid/state-file signals, otherwise a missing
    # ``gateway`` Python package in this image masquerades as
    # "gateway_not_configured" and produces a spurious banner.
    remote_base = _remote_gateway_base_url()
    if remote_base is not None:
        return _probe_remote_gateway(remote_base)

    try:
        gateway_status = _gateway_status_module()
    except Exception as exc:
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_status_unavailable",
                "error": type(exc).__name__,
            },
        }

    gateway_pid_path = _gateway_root_pid_path()

    runtime_status = None
    try:
        runtime_status = _read_gateway_runtime_status(gateway_status, gateway_pid_path)
    except Exception:
        runtime_status = None

    try:
        running_pid = _gateway_running_pid(gateway_status, gateway_pid_path)
    except Exception:
        running_pid = None

    safe_details = _runtime_detail_subset(runtime_status)
    if running_pid is not None:
        return {
            "alive": True,
            "checked_at": checked_at,
            "details": {
                "state": "alive",
                **safe_details,
            },
        }

    # Cross-container fallback (#1879): when ``get_running_pid()`` cannot see
    # the gateway because we're in a different PID namespace, a recent
    # ``updated_at`` on ``gateway_state.json`` is a reliable equivalent signal
    # since the gateway writes it on every tick. We only trust this fallback
    # when the gateway also self-reports ``gateway_state == "running"`` so
    # crash-without-cleanup scenarios still surface as "down".
    if _runtime_status_is_fresh(runtime_status):
        return {
            "alive": True,
            "checked_at": checked_at,
            "details": {
                "state": "alive",
                "reason": "cross_container_freshness",
                **safe_details,
            },
        }

    if _runtime_status_is_stale_stopped(runtime_status):
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_stale_stopped_state",
                **safe_details,
            },
        }

    if _runtime_status_is_stale_running(runtime_status):
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_stale_running_state",
                **safe_details,
            },
        }

    if isinstance(runtime_status, dict):
        return {
            "alive": False,
            "checked_at": checked_at,
            "details": {
                "state": "down",
                "reason": "gateway_not_running",
                **safe_details,
            },
        }

    return {
        "alive": None,
        "checked_at": checked_at,
        "details": {
            "state": "unknown",
            "reason": "gateway_not_configured",
        },
    }
