#!/usr/bin/env python3
"""One-shot bootstrap launcher for Hermes Web UI."""

from __future__ import annotations

import argparse
import errno
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
import webbrowser
from pathlib import Path


INSTALLER_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"
REPO_ROOT = Path(__file__).resolve().parent


def _load_repo_dotenv() -> None:
    """Load REPO_ROOT/.env into os.environ.

    Mirrors what start.sh does via ``set -a; source .env`` so that running
    ``python3 bootstrap.py`` directly behaves identically to ``./start.sh``.
    Variables are set unconditionally (matching shell source semantics), so a
    value in .env overrides one already present in the shell environment.
    ``ctl.sh`` sets HERMES_WEBUI_PRESERVE_ENV=1 when it has already resolved
    launcher-specific values such as HERMES_HOME or HERMES_WEBUI_STATE_DIR.

    Only loads the webui repo .env — not ~/.hermes/.env, which the server
    loads independently at startup for provider credentials.

    Note: does not handle the ``export FOO=bar`` prefix — strip ``export``
    from .env values if copy-pasting from a shell rc file.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        preserve_existing = os.getenv("HERMES_WEBUI_PRESERVE_ENV", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            # Strip optional 'export' prefix (common in copy-pasted shell snippets)
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip().strip('"').strip("'")
            if k:
                if preserve_existing and k in os.environ:
                    continue
                os.environ[k] = v
    except Exception as exc:
        import sys as _sys
        print(f"[bootstrap] Warning: could not load .env — {exc}", file=_sys.stderr)


# Side effect: loads REPO_ROOT/.env into os.environ on import.
# Must run before DEFAULT_HOST / DEFAULT_PORT so os.getenv() picks up
# values from .env even when bootstrap.py is invoked directly (not via start.sh).
_load_repo_dotenv()

DEFAULT_HOST = os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("HERMES_WEBUI_PORT", "8787"))
# Set HERMES_WEBUI_SKIP_ONBOARDING=1 to bypass the first-run wizard when
# the environment is already fully configured (e.g. managed hosting).


def info(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[bootstrap] [warn] {msg}", file=sys.stderr, flush=True)


def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    release = platform.release().lower()
    return (
        "microsoft" in release or "wsl" in release or bool(os.getenv("WSL_DISTRO_NAME"))
    )


def ensure_supported_platform() -> None:
    if platform.system() == "Windows" and not is_wsl():
        info(
            "Warning: Native Windows bootstrap is experimental. "
            "Embedded terminal and auto-install are not supported."
        )


def _walk_up_for_run_agent(start: Path) -> Path | None:
    """Walk up the parents of ``start`` and return the first dir with run_agent.py."""
    for parent in start.parents:
        if (parent / "run_agent.py").exists():
            return parent.resolve()
    return None


def _agent_dir_from_hermes_cli() -> Path | None:
    """Resolve the agent install root by inspecting the `hermes` CLI launcher.

    The Hermes Agent installer drops a `hermes` launcher in the user's PATH.
    It comes in two shapes depending on installer version:

    1. A Python console-script whose shebang points at the agent's venv::

           #!/path/to/hermes-agent/venv/bin/python3

    2. A small POSIX shell wrapper that ``exec``s the real venv entrypoint
       (the current installer shape — clears PYTHONPATH/PYTHONHOME first)::

           #!/usr/bin/env bash
           exec "/path/to/hermes-agent/venv/bin/hermes" "$@"

    In both cases an absolute path inside the launcher points into the agent's
    venv. Walking up its parents until we find a directory containing
    `run_agent.py` recovers the install root regardless of where the agent
    lives — e.g. the root-on-Linux FHS layout (`/usr/local/lib/hermes-agent`)
    or a custom clone (`~/Projects/GitHub/hermes-agent`) — neither of which the
    hard-coded candidate list in :func:`discover_agent_dir` can know about.

    Last-resort only: this is invoked after every explicit candidate
    (`HERMES_WEBUI_AGENT_DIR`, `$HERMES_HOME/hermes-agent`, etc.) has missed.
    A stale clone in a known location still wins over the live `hermes` CLI
    — that's intentional, since the candidate list is treated as
    authoritative when present, and matches existing behavior.
    """
    hermes_path = shutil.which("hermes")
    if not hermes_path:
        return None
    try:
        # The launcher is tiny; read a bounded prefix so we never slurp a huge
        # file if `hermes` resolves to something unexpected.
        with open(hermes_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [f.readline() for _ in range(20)]
    except OSError:
        return None
    if not lines or not lines[0].startswith("#!"):
        return None

    # Collect every absolute path the launcher references — the shebang
    # interpreter (Python-console-script shape) plus any quoted path in an
    # `exec`/wrapper line (shell-wrapper shape). A `#!/usr/bin/env bash`
    # shebang yields a useless `/usr/bin/env`, so the wrapper's exec target is
    # what actually points at the agent venv.
    candidate_paths: list[Path] = []

    shebang_field = lines[0][2:].strip().split(None, 1)
    if shebang_field:
        interp = Path(shebang_field[0])
        # Skip env-style indirection (`/usr/bin/env bash`) — env itself is not
        # in the agent tree; the real target is the wrapped exec line below.
        if interp.is_absolute() and interp.name != "env":
            candidate_paths.append(interp)

    for line in lines[1:]:
        for match in re.findall(r"""['"](/[^'"]+)['"]""", line):
            candidate_paths.append(Path(match))

    for candidate in candidate_paths:
        if not candidate.is_absolute():
            continue
        found = _walk_up_for_run_agent(candidate)
        if found:
            return found
    return None


def _agent_dir_from_python(python_exe: str) -> Path | None:
    script = (
        'import importlib.util\n'
        'spec = importlib.util.find_spec("run_agent")\n'
        'print(spec.origin if spec else "")\n'
    )
    try:
        check = subprocess.run(
            [python_exe, "-c", script],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if check.returncode != 0:
        return None
    lines = check.stdout.splitlines()
    if not lines:
        return None
    origin = Path(lines[0].strip())
    if not origin.is_absolute() or origin.name != "run_agent.py" or not origin.is_file():
        return None
    return origin.parent.resolve()


def discover_agent_dir() -> Path | None:
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    candidates = [
        os.getenv("HERMES_WEBUI_AGENT_DIR", ""),
        str(home / "hermes-agent"),
        str(REPO_ROOT.parent / "hermes-agent"),
        str(Path.home() / ".hermes" / "hermes-agent"),
        str(Path.home() / "hermes-agent"),
        # Root-on-Linux FHS layout: the installer puts agent code under
        # /usr/local/lib and links the CLI into /usr/local/bin (matches
        # Claude Code / Codex). HERMES_HOME stays at /root/.hermes, so the
        # `home / "hermes-agent"` candidate above does NOT cover this case.
        "/usr/local/lib/hermes-agent",
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve()
        if candidate.exists() and (candidate / "run_agent.py").exists():
            return candidate
    agent_dir = _agent_dir_from_hermes_cli()
    if agent_dir:
        return agent_dir
    return _agent_dir_from_python(discover_launcher_python(None))


def discover_launcher_python(agent_dir: Path | None) -> str:
    env_python = os.getenv("HERMES_WEBUI_PYTHON")
    if env_python:
        return env_python
    if agent_dir:
        for rel in ("venv/bin/python", "venv/Scripts/python.exe", ".venv/bin/python", ".venv/Scripts/python.exe"):
            candidate = agent_dir / rel
            if candidate.exists():
                return str(candidate)
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        candidate = REPO_ROOT / rel
        if candidate.exists():
            return str(candidate)
    return shutil.which("python3") or shutil.which("python") or sys.executable


def _python_can_run_webui_and_agent(python_exe: str, agent_dir: Path | None = None) -> bool:
    # Agent first: it may relaunch into its managed runtime, which ships ruamel.yaml only.
    script = (
        "from run_agent import AIAgent\n"
        "try:\n    import yaml\nexcept ImportError:\n    import ruamel.yaml\n"
    )
    env = os.environ.copy()
    if agent_dir:
        # PREPEND agent_dir to PYTHONPATH so an `agent_dir/run_agent.py` wins
        # over any stale `run_agent` package in system site-packages (sys.path
        # order: script-dir → PYTHONPATH entries → site-packages). The
        # "if PYTHONPATH unset" branch avoids a leading os.pathsep, which
        # CPython would interpret as "current directory" — a footgun.
        env["PYTHONPATH"] = (
            str(agent_dir)
            if not env.get("PYTHONPATH")
            else f"{agent_dir}{os.pathsep}{env['PYTHONPATH']}"
        )
    check = subprocess.run(
        [python_exe, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    return check.returncode == 0


def ensure_python_has_webui_deps(python_exe: str, agent_dir: Path | None = None) -> str:
    """Return a Python executable that can run both WebUI and Hermes Agent.

    The WebUI can be launched directly with its local .venv. That venv has the
    WebUI dependencies (for example PyYAML), but may not have Hermes Agent on its
    import path. In that case the server starts healthy, then chat fails later
    with "AIAgent not available". Prefer the agent venv when it is usable, and
    validate the final interpreter before starting the server.
    """
    if _python_can_run_webui_and_agent(python_exe, agent_dir):
        return python_exe

    agent_candidates: list[Path] = []
    if agent_dir:
        for rel in (
            "venv/bin/python",
            "venv/Scripts/python.exe",
            ".venv/bin/python",
            ".venv/Scripts/python.exe",
        ):
            agent_candidates.append(agent_dir / rel)
        for candidate in agent_candidates:
            if str(candidate) != python_exe and candidate.exists():
                if _python_can_run_webui_and_agent(str(candidate), agent_dir):
                    return str(candidate)

    if (os.getenv("HERMES_WEBUI_DISABLE_LOCAL_VENV") or "").strip().lower() in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "Python environment cannot import both WebUI dependencies and Hermes Agent, "
            "and local .venv creation is disabled for this packaged launch. Set "
            "HERMES_WEBUI_PYTHON to an interpreter with Hermes Agent dependencies."
        )

    venv_dir = REPO_ROOT / ".venv"
    venv_python = venv_dir / (
        "Scripts/python.exe" if platform.system() == "Windows" else "bin/python"
    )
    if not venv_python.exists():
        info(f"Creating local virtualenv at {venv_dir}")
        # symlinks=True: some Python builds (notably mise/asdf shared-library
        # installs on macOS) default venv to copy mode. The copied binary still
        # uses @executable_path/../lib/libpython3.X.dylib for its load command,
        # so the venv binary aborts with SIGABRT on first import because the
        # dylib never gets copied into .venv/lib. Symlinking the interpreter
        # keeps @executable_path resolving back to the original install.
        # CPython's venv falls back to copy mode automatically when symlink
        # creation fails (e.g. older Windows without SeCreateSymbolicLinkPrivilege),
        # so this is safe to set unconditionally.
        venv.EnvBuilder(with_pip=True, symlinks=True).create(venv_dir)

    info("Installing WebUI dependencies into local virtualenv")
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=True,
    )
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "-r",
            str(REPO_ROOT / "requirements.txt"),
        ],
        check=True,
    )
    if _python_can_run_webui_and_agent(str(venv_python), agent_dir):
        return str(venv_python)
    raise RuntimeError(
        "Python environment cannot import both WebUI dependencies and Hermes Agent. "
        "Set HERMES_WEBUI_PYTHON to the Hermes Agent venv Python or install the "
        "WebUI requirements into that environment."
    )


def hermes_command_exists() -> bool:
    return shutil.which("hermes") is not None


def install_hermes_agent() -> None:
    if platform.system() == "Windows" and not is_wsl():
        raise RuntimeError(
            "Auto-install is not supported on native Windows. "
            "Install hermes-agent manually first."
        )
    info(f"Hermes Agent not found. Attempting install via {INSTALLER_URL}")
    subprocess.run(
        ["/bin/bash", "-lc", f"curl -fsSL {INSTALLER_URL} | bash"], check=True
    )


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _tls_probe_enabled() -> bool:
    """Mirror api.config.TLS_ENABLED: HTTPS when both cert and key are set."""
    return bool(os.getenv("HERMES_WEBUI_TLS_CERT", "").strip()) and bool(
        os.getenv("HERMES_WEBUI_TLS_KEY", "").strip()
    )


_STATUS_OK_MARKER = b'"status": "ok"'

# Fields only the WebUI's own /health answer carries (api/routes.py
# ``_handle_health``). Used to tell OUR server apart from an unrelated
# service that merely answers 200 with a generic ``{"status": "ok"}`` on
# the same port.
_HERMES_HEALTH_MARKERS = (b'"server_started_at"', b'"sessions"')


def _health_ok(
    url: str,
    verify: bool = True,
    markers: tuple[bytes, ...] = (_STATUS_OK_MARKER,),
    accept_degraded: bool = False,
) -> bool:
    """Single /health request. True iff the body carries every marker.

    The default marker is the ``"status": "ok"`` field every WebUI answer
    has. Callers that must not mistake a foreign listener for the WebUI pass
    ``markers=_HERMES_HEALTH_MARKERS``: a generic ok without the Hermes
    fields is then rejected.

    ``accept_degraded=True`` (the occupied-port probe) also reads the body of
    a non-2xx answer: an own WebUI that is up but degraded replies 503 with the
    same payload, and it is still OUR instance - the one that must not be
    advised against as "another service". Callers waiting for a server to
    become healthy leave it False, so a degraded answer never counts as ready.

    ``verify=False`` disables TLS certificate verification (self-signed certs).
    """
    # Validate URL scheme to prevent file:// and other dangerous schemes
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid health check URL: {url}")
    context = None
    if url.startswith("https://") and not verify:
        import ssl

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=2, context=context) as response:  # nosec B310
            body = response.read()
    except urllib.error.HTTPError as exc:
        # 503 is how an own WebUI answers while degraded
        # (api/routes.py::_handle_health) - the markers still tell it apart
        # from a foreign listener, so the occupied-port probe accepts it.
        if not accept_degraded:
            return False
        try:
            body = exc.read()
        except Exception:
            return False
    except Exception:
        return False
    return all(marker in body for marker in markers)


def wait_for_health(
    url: str,
    timeout: float = 25.0,
    markers: tuple[bytes, ...] = (),
    accept_degraded: bool = False,
    tls_unknown: bool = False,
) -> str:
    """Poll /health until the server answers ok or the timeout elapses.

    ``markers`` overrides which fields the body must carry (the default is
    the ``"status": "ok"`` field alone). Bootstrap's own probe of an
    occupied port passes ``_HERMES_HEALTH_MARKERS`` so a foreign
    ok-answering service is never mistaken for the WebUI, plus
    ``accept_degraded=True`` so a degraded own instance is not mistaken for a
    foreign one either.

    Returns the scheme that actually answered ("https" or "http") on success,
    or "" on timeout. The scheme string is truthy on success, so existing
    ``if not wait_for_health(...)`` / ``assert wait_for_health(...)`` callers
    keep working unchanged.

    TLS-aware: when TLS is configured (HERMES_WEBUI_TLS_CERT/KEY set) the server
    serves HTTPS, so probe HTTPS first. Self-signed certs are handled by a
    second, unverified attempt (with a one-line warning). server.py falls back
    to plain HTTP when the cert/key are unloadable
    (tests/test_tls_support.py::test_tls_startup_failure_fallback_to_http), so
    HTTP is probed last to honor that contract instead of polling HTTPS forever.
    The returned scheme reflects that fallback, so callers print the URL the
    server is actually reachable on.

    HERMES_WEBUI_TLS_INSECURE_PROBE=1 is an explicit opt-in that skips the
    verified attempt and stays silent by contract.

    ``tls_unknown=True`` is for a port whose server we did not start (the
    occupied-port check): its TLS settings may live in another launcher or an
    earlier shell, so HTTPS is tried first WITHOUT certificate verification -
    silently, since a local self-signed certificate is expected - and HTTP
    second, instead of letting the local TLS env decision pick one scheme.
    """
    # Validate URL scheme to prevent file:// and other dangerous schemes
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid health check URL: {url}")
    required = markers or (_STATUS_OK_MARKER,)

    def probe(url_: str, verify: bool = True) -> bool:
        return _health_ok(
            url_,
            verify=verify,
            markers=required,
            accept_degraded=accept_degraded,
        )

    deadline = time.time() + timeout
    https = _tls_probe_enabled() or tls_unknown
    insecure_optin = (
        _truthy(os.getenv("HERMES_WEBUI_TLS_INSECURE_PROBE")) or tls_unknown
    )
    # Derive host:port/path from the passed URL, then build scheme-correct URLs.
    parsed = urllib.parse.urlsplit(url)
    authority = parsed.netloc
    path = parsed.path or "/health"
    https_url = f"https://{authority}{path}"
    http_url = f"http://{authority}{path}"
    warned = False
    while time.time() < deadline:
        if not https:
            if probe(http_url, verify=True):
                return "http"
        else:
            if insecure_optin:
                if probe(https_url, verify=False):
                    return "https"
            else:
                if probe(https_url, verify=True):
                    return "https"
                if probe(https_url, verify=False):
                    if not warned:
                        warned = True
                        warn(
                            f"Health probe: TLS certificate at {https_url} is "
                            "self-signed or not trusted; proceeding without "
                            "verification."
                        )
                    return "https"
            # server.py may have fallen back to plain HTTP (cert/key unloadable).
            if probe(http_url, verify=True):
                return "http"
        time.sleep(0.4)
    return ""


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        info(f"Could not open browser automatically: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Hermes Web UI onboarding.")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser tab automatically.",
    )
    parser.add_argument(
        "--skip-agent-install",
        action="store_true",
        help="Fail instead of attempting the official Hermes installer.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Run server.py in this process (via os.execv on POSIX; via a "
            "Popen child + exit on Windows, where execv can't replace the "
            "process image) instead of spawning a detached child. Use this "
            "under launchd / systemd / supervisord so the "
            "supervisor sees the long-lived server as the original child. "
            "Implies --no-browser. Skips the post-launch health probe — the "
            "supervisor's own KeepAlive / Restart=on-failure handles liveness."
        ),
    )
    return parser.parse_args()


# Env vars whose presence indicates this process was launched by a supervisor
# that wants to manage the server's lifecycle (KeepAlive, Restart=always, etc.).
# When any is set, we auto-promote to --foreground so we don't double-fork.
#
# - INVOCATION_ID            systemd (set on every service activation)
# - JOURNAL_STREAM           systemd (set when stdio is wired to the journal)
# - NOTIFY_SOCKET            systemd Type=notify, s6 sd_notify-style
# - XPC_SERVICE_NAME         launchd (set to the Label of the running plist)
# - SUPERVISOR_ENABLED       supervisord
# - HERMES_WEBUI_FOREGROUND  explicit user opt-in (=1 / true / yes / on)
#
# Note on XPC_SERVICE_NAME: macOS launchd sets this in EVERY Terminal-launched
# shell too — typical values include "0" (truthy in Python!) and
# "application.com.apple.Terminal.<UUID>". A bare existence check would
# false-positive on every Mac dev machine running ./start.sh interactively.
# We narrow to launchd Label-style names (com.<reverse-dns>.<svc>) — those
# are real services. Verified with `launchctl getenv XPC_SERVICE_NAME` and
# Apple's documented launchd behavior.
_SUPERVISOR_ENV_VARS = (
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "NOTIFY_SOCKET",
    "XPC_SERVICE_NAME",
    "SUPERVISOR_ENABLED",
)


def _is_real_supervisor_value(name: str, value: str) -> bool:
    """Filter out known-noise env-var values that aren't actual supervisors.

    Most env vars in _SUPERVISOR_ENV_VARS are only set by the supervisor we
    care about, so any non-empty value is meaningful. XPC_SERVICE_NAME is the
    exception: macOS launchd sets it in every Terminal-spawned shell with
    values like "0" or "application.com.apple.Terminal.<UUID>". A real
    launchd-managed service has a reverse-DNS Label like "com.example.foo".
    """
    if not value:
        return False
    if name == "XPC_SERVICE_NAME":
        # Reject Apple's noise values; accept Label-style names.
        if value == "0":
            return False
        if value.startswith("application."):
            return False
    return True


def _detect_supervisor() -> str | None:
    """Return the name of the detected supervisor env var, or None.

    Pure inspection of os.environ — no side effects. Returned name is the env
    var that triggered detection, useful for log messages and for tests.
    """
    explicit = os.environ.get("HERMES_WEBUI_FOREGROUND", "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return "HERMES_WEBUI_FOREGROUND"
    for name in _SUPERVISOR_ENV_VARS:
        value = os.environ.get(name, "")
        if _is_real_supervisor_value(name, value):
            return name
    return None


def _bind_host_for_check(host: str) -> str:
    """Normalize a configured host into the address used for the bind check.

    Wildcard binds ("", "0.0.0.0", "::", "[::]") are probed on the wildcard
    address itself, so another service holding the port on a single interface
    address is still reported as a conflict. Substituting loopback is right for
    reaching an already-running server (server.py's
    _abort_if_already_serving) but not for deciding whether the server can
    bind. Everything else is used verbatim, so a specific interface address is
    checked as-is.
    """
    if host in ("", "0.0.0.0"):
        return "0.0.0.0"
    if host in ("::", "[::]"):
        return "::"
    return host


def _apply_bind_flags(sock: socket.socket) -> None:
    """Give a check socket the same options as the server's own bind.

    Windows needs ``SO_EXCLUSIVEADDRUSE`` and no address reuse: with
    ``SO_REUSEADDR`` a second socket may bind a port another listener holds
    without exclusive use, so a busy port would pass the check and the real
    bind would only fail later, after dependencies and state exist (review
    #8112). ``server.py::QuietHTTPServer.server_bind`` makes this exact choice
    for the binding socket; the check must not be laxer than the bind it
    predicts.
    """
    # server.py keys its own bind on sys.platform == "win32"; resolved per
    # call, so a test can simulate either platform.
    windows = sys.platform == "win32"
    if windows:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.setsockopt(
            socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1
        )
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _port_is_available(host: str, port: int) -> bool:
    """Return True iff a TCP socket can bind host:port right now.

    Point-in-time check only: a successful bind here does not guarantee the
    server's later bind will succeed (another process may grab the port in
    between). Actual bind failures remain authoritative.

    Binds with ``_apply_bind_flags``, so the check fails where the real
    bind does (Windows in particular).
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        _apply_bind_flags(sock)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_free_port(host: str, start_port: int, attempts: int = 20) -> int | None:
    """Return the first available port at or above start_port, or None."""
    for candidate in range(start_port + 1, start_port + 1 + attempts):
        if candidate > 65535:
            break
        if _port_is_available(host, candidate):
            return candidate
    return None


class PortInUseError(RuntimeError):
    """The configured port is already bound by another listener.

    A distinct type because only THIS failure may be our own WebUI already
    running: an invalid or unreachable bind address (EADDRNOTAVAIL and
    friends) must stay a hard error instead of being probed for a server.
    """


def _check_port_available(host: str, port: int) -> None:
    """Preflight bind check. Raise with an actionable message.

    Distinguishes a port conflict (address in use) from an invalid bind
    address or other socket error, and suggests a free alternative without
    silently switching ports or modifying persistent configuration. A
    conflict raises ``PortInUseError``; every other bind failure raises a
    plain ``RuntimeError`` so its cause stays visible.
    """
    check_host = _bind_host_for_check(host)
    family = socket.AF_INET6 if ":" in check_host else socket.AF_INET
    # check_host is the address the server itself would bind, so an occupied
    # port on any interface is caught here instead of after installation.
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        _apply_bind_flags(sock)
        sock.bind((check_host, port))
        return
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 48, 10048):
            alternative = _find_free_port(check_host, port)
            lines = [
                f"Port {port} on {host} is already in use by another service.",
            ]
            if alternative is not None:
                lines.append(
                    f"Try an available port instead: ./start.sh {alternative}"
                )
                lines.append(
                    f"Or set HERMES_WEBUI_PORT={alternative} in {REPO_ROOT / '.env'}."
                )
            else:
                lines.append(
                    "No free alternative port was found nearby; choose one manually."
                )
            lines.append(
                "The existing service was left untouched; no configuration was changed."
            )
            raise PortInUseError(" ".join(lines)) from exc
        raise RuntimeError(
            f"Cannot bind {host}:{port} — {exc}. "
            "Check that the host address is valid and available on this machine."
        ) from exc
    finally:
        sock.close()


def _url_host(host: str) -> str:
    """A configured host as it must appear inside a URL.

    Wildcard binds ("", "0.0.0.0", "::", "[::]") mean "every interface",
    which is not a reachable URL host, so they map to localhost - the address
    a wildcard-bound server answers on. Loopback keeps that name too: the
    browser origin - and with it the passkey rpId and the saved session in
    localStorage - is bound to the hostname the UI was opened on, so an
    explicit ``--host 127.0.0.1`` must not move it to the numeric address.
    IPv6 literals need brackets: ``http://::1:8787`` parses the last address
    group as the port, so the health probe, the printed URL and the browser
    open all miss the running server (review #8112).
    """
    if host in ("", "0.0.0.0", "::", "[::]", "127.0.0.1", "localhost"):
        return "localhost"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _probe_host(host: str) -> str:
    """The configured address, as a health-check URL must spell it.

    Deliberately NOT ``_url_host``: that one keeps the browser on ``localhost``
    so the saved session and the passkey rpId keep their origin. Health checks
    must instead stay on the address the user configured. With ``HTTP_PROXY``
    set and ``NO_PROXY=127.0.0.1``, a probe to ``localhost`` is sent through the
    proxy while ``127.0.0.1`` bypasses it, so a proxy that refuses local requests
    makes bootstrap report a failure - or an existing WebUI as a port conflict -
    for a server that is running (review #8112).

    Wildcard binds have no reachable URL host of their own: they are probed on
    loopback of the same family (``""``/``0.0.0.0`` on 127.0.0.1, ``::`` on
    [::1]). IPv6 literals need brackets, or the last group parses as the port.
    """
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host in ("::", "[::]"):
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _already_serving_scheme(host: str, port: int) -> str:
    """Scheme of a healthy Hermes WebUI already answering on host:port, else "".

    Bounded, TLS-aware probe (``wait_for_health``, one second) of the port the
    bind preflight just found occupied. The listener there may be OUR OWN
    healthy WebUI: re-running bootstrap, or ``start.sh`` falling through to
    bootstrap when neither curl nor wget is installed. Advising a second
    instance on the same state dir in that case is wrong. Wildcard binds are
    probed on loopback of their own family, where a server bound to 0.0.0.0
    (or ::) answers.

    Only the WebUI's own payload counts (``_HERMES_HEALTH_MARKERS``): any
    other service that happens to answer a generic ``{"status": "ok"}`` on
    that port is a foreign listener, so bootstrap keeps reporting the
    conflict. A degraded own WebUI answers 503 with the same payload and is
    still that instance, so the probe accepts any status here
    (``accept_degraded=True``). Both schemes are tried (``tls_unknown=True``):
    the running instance may serve HTTPS with settings from another launcher
    or an earlier shell, which this process cannot see in its own env.
    """
    probe_hosts = [_probe_host(host)]
    # On a dual-stack host, binding ``::`` also conflicts with an IPv4-only
    # listener on 127.0.0.1. That listener may be our own WebUI, which [::1]
    # never reaches, so probe IPv4 loopback too before calling it foreign.
    if host in ("::", "[::]"):
        probe_hosts.append("127.0.0.1")
    for probe_host in probe_hosts:
        scheme = wait_for_health(
            f"http://{probe_host}:{port}/health",
            timeout=1.0,
            markers=_HERMES_HEALTH_MARKERS,
            accept_degraded=True,
            tls_unknown=True,
        )
        if scheme:
            return scheme
    return ""


def main() -> int:
    args = parse_args()
    ensure_supported_platform()

    # Preflight: fail fast on an occupied port before installing the agent,
    # setting up dependencies, creating state, or launching the server.
    try:
        _check_port_available(args.host, args.port)
    except PortInUseError:
        # 10.10 (review #8112, fix 1): an occupied port is not automatically a
        # foreign service. If an own WebUI already answers there, take the
        # already-running path - report it ready and leave it untouched. Only a
        # port conflict is recovered here (review #8112, fix 2): a bind-address
        # error stays a hard error, otherwise a remote --host would be reported
        # as running while nothing local ever started.
        _scheme = _already_serving_scheme(args.host, args.port)
        if not _scheme:
            raise
        _url = f"{_scheme}://{_url_host(args.host)}:{args.port}"
        # A foreground/supervisor launch would be a second server on the same
        # port, so it still fails - with advice about its OWN instance
        # (review #8112, should-fix): the port-conflict text ("try
        # ./start.sh <other port>") starts a second WebUI on the same state
        # dir, the outcome #8111 exists to prevent.
        if args.foreground or _detect_supervisor():
            raise PortInUseError(
                f"Hermes WebUI is already running at {_url}. Stop that instance first."
            ) from None
        info(f"Web UI is already running: {_url}")
        if not args.no_browser:
            open_browser(_url)
        return 0

    agent_dir = discover_agent_dir()
    if not agent_dir and not hermes_command_exists():
        if args.skip_agent_install:
            raise RuntimeError(
                "Hermes Agent was not found and auto-install was disabled."
            )
        install_hermes_agent()
        agent_dir = discover_agent_dir()

    python_exe = ensure_python_has_webui_deps(discover_launcher_python(agent_dir), agent_dir)
    state_dir = Path(
        os.getenv("HERMES_WEBUI_STATE_DIR")
        or Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes")) / "webui"
    ).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    # Mutate os.environ so child (or post-execv) inherits the resolved values.
    os.environ["HERMES_WEBUI_HOST"] = args.host
    os.environ["HERMES_WEBUI_PORT"] = str(args.port)
    os.environ.setdefault("HERMES_WEBUI_STATE_DIR", str(state_dir))
    if agent_dir:
        os.environ["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)

    # Let operators move fallback relative writes out of a read-only agent dir.
    server_cwd = os.environ.get("HERMES_WEBUI_SERVER_CWD", "").strip() or str(agent_dir or REPO_ROOT)
    server_path = str(REPO_ROOT / "server.py")
    # Scheme the server will advertise (HTTPS when TLS cert+key are configured).
    scheme = "https" if _tls_probe_enabled() else "http"

    # --foreground (or auto-detected supervisor): replace this process with the
    # server. The supervisor sees the long-lived server as the original child,
    # so KeepAlive / Restart=always / autorestart=true work correctly. No
    # health probe — the supervisor's own restart-on-exit handles liveness.
    foreground_reason = "--foreground" if args.foreground else _detect_supervisor()
    if foreground_reason:
        info(
            f"Starting Hermes Web UI on {scheme}://{_url_host(args.host)}:{args.port} "
            f"(foreground mode: {foreground_reason})"
        )
        try:
            os.chdir(server_cwd)
        except OSError as exc:
            raise RuntimeError(
                f"Could not chdir to {server_cwd!r} before exec: {exc}"
            ) from exc
        # Defensive check: if python_exe is missing or non-executable, execv
        # raises OSError, the wrapper catches and SystemExit(1)s, and the
        # supervisor restarts — looping forever, exactly the failure mode this
        # PR is meant to eliminate. Convert to a single visible error.
        if not os.access(python_exe, os.X_OK):
            raise RuntimeError(
                f"Python interpreter at {python_exe!r} is not executable. "
                f"Set HERMES_WEBUI_PYTHON to a working interpreter or fix "
                f"the agent venv at {agent_dir}."
            )
        # os.execv replaces the current process image. On Windows, execv
        # spawns a new process instead of replacing (Python calls CreateProcess),
        # orphaning it from any supervisor. Use Popen + exit there instead.
        if sys.platform == "win32":
            # Mirror the robust pattern from api/updates._schedule_restart:
            # 1. Prefer pythonw.exe (windowless subsystem) over python.exe
            #    so the restarted server never creates a visible console window.
            # 2. DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            #    suppresses the brief console flash even when python.exe is used.
            _exe = str(python_exe)
            if _exe.lower().endswith("python.exe"):
                _w = _exe[:-4] + "w.exe"  # python.exe -> pythonw.exe
                if os.path.isfile(_w):
                    _exe = _w
            _flags = 0
            for _attr in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",
                          "CREATE_NO_WINDOW"):
                _flags |= getattr(subprocess, _attr, 0)
            # Redirect the windowless child's stdout/stderr to a real log file
            # (not DEVNULL): server.py writes startup/request/error diagnostics
            # to stdout/stderr, and with no console (pythonw + CREATE_NO_WINDOW)
            # there is nowhere else for them to go — DEVNULL would silently drop
            # all Windows server logs after a supervisor restart. Mirror the
            # default-path log sink (state_dir/bootstrap-<port>.log).
            _win_log_path = state_dir / f"bootstrap-{args.port}.log"
            _win_log = _win_log_path.open("ab")
            try:
                subprocess.Popen(
                    [_exe, str(server_path)],
                    cwd=str(server_cwd),
                    env=os.environ.copy(),
                    creationflags=_flags,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=_win_log,
                    stderr=subprocess.STDOUT,
                )
            finally:
                _win_log.close()
            sys.exit(0)
        os.execv(python_exe, [python_exe, server_path])
        # Unreachable — execv either replaces the process or raises.
        raise RuntimeError("os.execv returned unexpectedly")

    # Default (legacy) path: spawn the server as a detached child, probe
    # /health, then return. Suitable for an interactive `bash start.sh` run.
    log_path = state_dir / f"bootstrap-{args.port}.log"

    info(f"Starting Hermes Web UI on {scheme}://{_url_host(args.host)}:{args.port}")
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(
            [python_exe, server_path],
            cwd=server_cwd,
            env=os.environ.copy(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    health_url = f"{scheme}://{_probe_host(args.host)}:{args.port}/health"
    healthy_scheme = wait_for_health(health_url)
    if not healthy_scheme:
        raise RuntimeError(
            f"Web UI did not become healthy at {health_url}. "
            f"Check the log at {log_path}. Server PID: {proc.pid}"
        )

    # server.py falls back to plain HTTP when the cert/key are unloadable, so the
    # scheme that actually answered the probe is the one the server is reachable
    # on — use it for the ready URL and browser-open, not the configured scheme.
    ready_scheme = healthy_scheme or scheme
    app_url = f"{ready_scheme}://{_url_host(args.host)}:{args.port}"
    info(f"Web UI is ready: {app_url}")
    info(f"Log file: {log_path}")
    if not args.no_browser:
        open_browser(app_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
