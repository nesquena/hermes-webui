#!/usr/bin/env python3
"""One-shot bootstrap launcher for Hermes Web UI."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path


INSTALLER_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"
REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("HERMES_WEBUI_PORT", "8787"))
# Set HERMES_WEBUI_SKIP_ONBOARDING=1 to bypass the first-run wizard when
# the environment is already fully configured (e.g. managed hosting).


def info(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    release = platform.release().lower()
    return (
        "microsoft" in release or "wsl" in release or bool(os.getenv("WSL_DISTRO_NAME"))
    )


def ensure_supported_platform() -> None:
    if platform.system() == "Windows" and not is_wsl():
        raise RuntimeError(
            "Native Windows is not supported for this bootstrap yet. "
            "Please run it from Linux, macOS, or inside WSL2."
        )


def discover_agent_dir() -> Path | None:
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    candidates = [
        os.getenv("HERMES_WEBUI_AGENT_DIR", ""),
        str(home / "hermes-agent"),
        str(REPO_ROOT.parent / "hermes-agent"),
        str(Path.home() / ".hermes" / "hermes-agent"),
        str(Path.home() / "hermes-agent"),
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve()
        if candidate.exists() and (candidate / "run_agent.py").exists():
            return candidate
    return None


def discover_launcher_python(agent_dir: Path | None) -> str:
    env_python = os.getenv("HERMES_WEBUI_PYTHON")
    if env_python:
        return env_python
    if agent_dir:
        for rel in ("venv/bin/python", "venv/Scripts/python.exe"):
            candidate = agent_dir / rel
            if candidate.exists():
                return str(candidate)
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        candidate = REPO_ROOT / rel
        if candidate.exists():
            return str(candidate)
    return shutil.which("python3") or shutil.which("python") or sys.executable


def ensure_python_has_webui_deps(python_exe: str) -> str:
    check = subprocess.run(
        [python_exe, "-c", "import yaml"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return python_exe

    venv_dir = REPO_ROOT / ".venv"
    venv_python = venv_dir / (
        "Scripts/python.exe" if platform.system() == "Windows" else "bin/python"
    )
    if not venv_python.exists():
        info(f"Creating local virtualenv at {venv_dir}")
        venv.EnvBuilder(with_pip=True).create(venv_dir)

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
    return str(venv_python)


def hermes_command_exists() -> bool:
    return shutil.which("hermes") is not None


def install_hermes_agent() -> None:
    info(f"Hermes Agent not found. Attempting install via {INSTALLER_URL}")
    subprocess.run(
        ["/bin/bash", "-lc", f"curl -fsSL {INSTALLER_URL} | bash"], check=True
    )


def wait_for_health(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if b'"status": "ok"' in response.read():
                    return True
        except Exception:
            time.sleep(0.4)
    return False


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_supported_platform()

    agent_dir = discover_agent_dir()
    if not agent_dir and not hermes_command_exists():
        if args.skip_agent_install:
            raise RuntimeError(
                "Hermes Agent was not found and auto-install was disabled."
            )
        install_hermes_agent()
        agent_dir = discover_agent_dir()

    python_exe = ensure_python_has_webui_deps(discover_launcher_python(agent_dir))
    state_dir = Path(
        os.getenv("HERMES_WEBUI_STATE_DIR", str(Path.home() / ".hermes" / "webui"))
    ).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / f"bootstrap-{args.port}.log"

    env = os.environ.copy()
    env["HERMES_WEBUI_HOST"] = args.host
    env["HERMES_WEBUI_PORT"] = str(args.port)
    env.setdefault("HERMES_WEBUI_STATE_DIR", str(state_dir))
    if agent_dir:
        env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)

    info(f"Starting Hermes Web UI on http://{args.host}:{args.port}")
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(
            [python_exe, str(REPO_ROOT / "server.py")],
            cwd=str(agent_dir or REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    health_url = f"http://{args.host}:{args.port}/health"
    if not wait_for_health(health_url):
        raise RuntimeError(
            f"Web UI did not become healthy at {health_url}. "
            f"Check the log at {log_path}. Server PID: {proc.pid}"
        )

    app_url = (
        f"http://localhost:{args.port}"
        if args.host in ("127.0.0.1", "localhost")
        else f"http://{args.host}:{args.port}"
    )
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
