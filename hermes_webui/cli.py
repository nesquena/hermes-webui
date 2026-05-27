"""Command line interface for Hermes WebUI."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent if (PACKAGE_ROOT.parent / "pyproject.toml").exists() else PACKAGE_ROOT


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def _state_dir() -> Path:
    return Path(
        os.getenv("HERMES_WEBUI_STATE_DIR", str(_hermes_home() / "webui"))
    ).expanduser()


def _pid_file() -> Path:
    return Path(
        os.getenv("HERMES_WEBUI_PID_FILE", str(_hermes_home() / "webui.pid"))
    ).expanduser()


def _log_file() -> Path:
    return Path(
        os.getenv("HERMES_WEBUI_LOG_FILE", str(_hermes_home() / "webui.log"))
    ).expanduser()


def _ctl_state_file() -> Path:
    return Path(
        os.getenv("HERMES_WEBUI_CTL_STATE_FILE", str(_hermes_home() / "webui.ctl.env"))
    ).expanduser()


def _ensure_dirs() -> None:
    _hermes_home().mkdir(parents=True, exist_ok=True)
    _state_dir().mkdir(parents=True, exist_ok=True)


def _read_pid() -> int | None:
    try:
        raw = _pid_file().read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_state() -> dict[str, str]:
    path = _ctl_state_file()
    if not path.exists():
        return {}
    state: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            state[key] = value
    except OSError:
        return {}
    return state


def _write_state(pid: int, host: str, port: int) -> None:
    state = {
        "PID": str(pid),
        "HOST": host,
        "PORT": str(port),
        "LOG_FILE": str(_log_file()),
        "STATE_DIR": str(_state_dir()),
        "STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _ctl_state_file().write_text(
        "".join(f"{key}={value}\n" for key, value in state.items()),
        encoding="utf-8",
    )


def _clear_pid_state() -> None:
    for path in (_pid_file(), _ctl_state_file()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _run_bootstrap(argv: list[str]) -> int:
    from hermes_webui import bootstrap

    return bootstrap.main(argv)


def _cli_version() -> str:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    version_file = PACKAGE_ROOT / "api" / "_version.py"
    if version_file.exists():
        try:
            match = re.search(
                r"""__version__\s*=\s*['"]([^'"]+)['"]""",
                version_file.read_text(encoding="utf-8"),
            )
            if match:
                return match.group(1)
        except OSError:
            pass

    try:
        return version("hermes-webui")
    except PackageNotFoundError:
        return "unknown"


def _webui_pythonpath() -> str:
    return str(PACKAGE_ROOT.parent)


def _prepend_pythonpath(env: dict[str, str], path: str) -> None:
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if path not in parts:
        parts.insert(0, path)
    env["PYTHONPATH"] = os.pathsep.join(parts)


def _resolve_server_python() -> tuple[str, Path | None]:
    from hermes_webui import bootstrap

    agent_dir = bootstrap.discover_agent_dir()
    if not agent_dir:
        return sys.executable, None
    return bootstrap.discover_launcher_python(agent_dir), agent_dir


def _server_process_env(
    host: str,
    port: int,
    python_exe: str,
    agent_dir: Path | None,
    *,
    expose_python: bool = True,
) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_WEBUI_HOST"] = host
    env["HERMES_WEBUI_PORT"] = str(port)
    env.setdefault("HERMES_WEBUI_STATE_DIR", str(_state_dir()))
    if expose_python:
        env["HERMES_WEBUI_PYTHON"] = python_exe
    if agent_dir:
        env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    _prepend_pythonpath(env, _webui_pythonpath())
    return env


def _maybe_reexec_server(args: argparse.Namespace) -> None:
    if os.environ.get("HERMES_WEBUI_SERVER_REEXECED") == "1":
        return
    python_exe, agent_dir = _resolve_server_python()
    if not agent_dir:
        return
    if Path(python_exe).resolve() == Path(sys.executable).resolve():
        return

    env = _server_process_env(args.host, args.port, python_exe, agent_dir)
    env["HERMES_WEBUI_SERVER_REEXECED"] = "1"
    os.execvpe(
        python_exe,
        [
            python_exe,
            "-m",
            "hermes_webui.cli",
            "serve",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        env,
    )


def _web(args: argparse.Namespace) -> int:
    argv = []
    if args.no_browser:
        argv.append("--no-browser")
    if args.skip_agent_install:
        argv.append("--skip-agent-install")
    argv.extend(["--host", args.host, str(args.port)])
    return _run_bootstrap(argv)


def _serve(args: argparse.Namespace) -> int:
    _maybe_reexec_server(args)
    os.environ["HERMES_WEBUI_HOST"] = args.host
    os.environ["HERMES_WEBUI_PORT"] = str(args.port)
    os.environ.setdefault("HERMES_WEBUI_STATE_DIR", str(_state_dir()))
    from hermes_webui import server

    server.main()
    return 0


def _daemon_start(args: argparse.Namespace) -> int:
    from hermes_webui import bootstrap as _bootstrap

    _bootstrap._load_repo_dotenv()
    _ensure_dirs()
    existing = _read_pid()
    if existing and _is_alive(existing):
        print(f"[hermes-webui] already running (PID {existing})")
        return 0
    _clear_pid_state()

    host = args.host or os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
    port = args.port or int(os.getenv("HERMES_WEBUI_PORT", "8787"))
    python_exe, agent_dir = _resolve_server_python()
    env = _server_process_env(
        host,
        port,
        python_exe,
        agent_dir,
        expose_python=agent_dir is not None,
    )

    log_path = _log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    proc = subprocess.Popen(
        [
            python_exe,
            "-m",
            "hermes_webui.server",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    log_file.close()

    _pid_file().write_text(f"{proc.pid}\n", encoding="utf-8")
    _write_state(proc.pid, host, port)
    time.sleep(0.2)
    if not _is_alive(proc.pid):
        _clear_pid_state()
        print(f"[hermes-webui] failed to stay running. Log: {log_path}", file=sys.stderr)
        return 1
    print(f"[hermes-webui] started (PID {proc.pid})")
    print(f"[hermes-webui] bound: {host}:{port}")
    print(f"[hermes-webui] log: {log_path}")
    return 0


def _daemon_stop(_args: argparse.Namespace) -> int:
    _ensure_dirs()
    pid = _read_pid()
    if not pid:
        _clear_pid_state()
        print("[hermes-webui] stopped")
        return 0
    if not _is_alive(pid):
        _clear_pid_state()
        print("[hermes-webui] removed stale PID file")
        return 0

    print(f"[hermes-webui] stopping (PID {pid})")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid_state()
        print("[hermes-webui] stopped")
        return 0

    deadline = time.time() + 5
    while time.time() < deadline:
        if not _is_alive(pid):
            _clear_pid_state()
            print("[hermes-webui] stopped")
            return 0
        time.sleep(0.1)

    print("[hermes-webui] process did not exit after SIGTERM; sending SIGKILL", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _clear_pid_state()
    return 0


def _health_line(host: str, port: str) -> str:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310
            payload = response.read().decode("utf-8", "replace")
        data = json.loads(payload)
        if data.get("status") == "ok":
            return f"ok ({data.get('sessions', data.get('session_count', '?'))} sessions, {data.get('active_streams', '?')} active streams)"
        return str(data.get("status") or "reachable")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return f"unreachable ({url})"


def _daemon_status(_args: argparse.Namespace) -> int:
    _ensure_dirs()
    state = _read_state()
    host = state.get("HOST") or os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
    port = state.get("PORT") or os.getenv("HERMES_WEBUI_PORT", "8787")
    log_path = state.get("LOG_FILE") or str(_log_file())
    pid = _read_pid()
    if pid and _is_alive(pid):
        print("* hermes-webui - running")
        print(f"  PID:     {pid}")
        print(f"  Bound:   {host}:{port}")
        print(f"  Log:     {log_path}")
        print(f"  Health:  {_health_line(host, port)}")
    else:
        _clear_pid_state()
        print("* hermes-webui - stopped")
        print("  PID:     -")
        print(f"  Bound:   {host}:{port}")
        print(f"  Log:     {log_path}")
        print("  Health:  not checked")
    return 0


def _daemon_logs(args: argparse.Namespace) -> int:
    path = _log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError as exc:
        print(f"[hermes-webui] cannot open log file {path}: {exc}", file=sys.stderr)
        return 1
    cmd = ["tail", "-n", str(args.lines)]
    if args.follow:
        cmd.append("-f")
    cmd.append(str(path))
    return subprocess.call(cmd)


def _daemon_restart(args: argparse.Namespace) -> int:
    rc = _daemon_stop(args)
    if rc:
        return rc
    return _daemon_start(args)


def _mcp(args: argparse.Namespace) -> int:
    import asyncio

    try:
        from hermes_webui import mcp_server
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            print(
                "[hermes-webui] MCP support is not installed. "
                "Install with `python3 -m pip install -e .[mcp]` "
                "or run `uv run --extra mcp hermes-webui mcp`.",
                file=sys.stderr,
            )
            return 1
        raise

    if args.profile:
        mcp_server._profile_arg = args.profile
        import api.profiles as _profiles

        _profiles._active_profile = args.profile
    asyncio.run(mcp_server.main())
    return 0


def _add_host_port(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=os.getenv("HERMES_WEBUI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_WEBUI_PORT", "8787")))


def build_parser() -> argparse.ArgumentParser:
    from hermes_webui import bootstrap as _bootstrap

    _bootstrap._load_repo_dotenv()
    parser = argparse.ArgumentParser(prog="hermes-webui")
    parser.add_argument("--version", action="store_true", help="show version and exit")
    sub = parser.add_subparsers(dest="command")

    web = sub.add_parser("web", help="bootstrap and open the browser interface")
    _add_host_port(web)
    web.add_argument("--no-browser", action="store_true")
    web.add_argument("--skip-agent-install", action="store_true")
    web.set_defaults(func=_web)

    serve = sub.add_parser("serve", help="start the foreground headless server")
    _add_host_port(serve)
    serve.set_defaults(func=_serve)

    start = sub.add_parser("start", help="start Hermes WebUI as a background daemon")
    start.add_argument("--host", default=None)
    start.add_argument("--port", type=int, default=None)
    start.add_argument("bootstrap_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    start.set_defaults(func=_daemon_start)

    stop = sub.add_parser("stop", help="stop the background daemon")
    stop.set_defaults(func=_daemon_stop)

    restart = sub.add_parser("restart", help="restart the background daemon")
    restart.add_argument("--host", default=None)
    restart.add_argument("--port", type=int, default=None)
    restart.add_argument("bootstrap_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    restart.set_defaults(func=_daemon_restart)

    status = sub.add_parser("status", help="show daemon status")
    status.set_defaults(func=_daemon_status)

    logs = sub.add_parser("logs", help="show daemon logs")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", dest="follow", action="store_true", default=True)
    logs.add_argument("--no-follow", dest="follow", action="store_false")
    logs.set_defaults(func=_daemon_logs)

    mcp = sub.add_parser("mcp", help="start the MCP stdio server")
    mcp.add_argument("--profile", default=None)
    mcp.set_defaults(func=_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(_cli_version())
        return 0
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
