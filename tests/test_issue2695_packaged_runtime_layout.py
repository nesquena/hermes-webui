from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _copy_repo_without_heavy_dirs(dst: Path) -> Path:
    repo_copy = dst / "repo"
    shutil.copytree(
        ROOT,
        repo_copy,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "node_modules",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
        ),
    )
    return repo_copy


# pip's isolated PEP 517 build installs the build backend (setuptools,
# setuptools-scm) from an index first. Without network access (a sandboxed
# suite run has no network namespace egress) that step fails before the
# project is ever built, which says nothing about the packaged layout.
_BUILD_DEPS_STEP_FAILED = "installing build dependencies did not run successfully"
_NO_INDEX_MARKERS = (
    "Temporary failure in name resolution",
    "Name or service not known",
    "NameResolutionError",
    "Network is unreachable",
    "Failed to establish a new connection",
    "No matching distribution found for setuptools",
)


def _index_reachable(timeout: float = 5.0) -> bool:
    """Can a child process reach the package index at all (proxies honoured)?

    The skip is for a run with NO index, never for a pin the index cannot
    satisfy: pip ends both with "No matching distribution found for setuptools".
    Probed in a subprocess, like pip itself, so in-process test guards on
    sockets do not answer for it. Any HTTP status proves the index is reachable.
    """
    url = os.environ.get("PIP_INDEX_URL") or "https://pypi.org/simple/"
    probe = (
        "import sys, urllib.error, urllib.request\n"
        "try:\n"
        f"    urllib.request.urlopen({url!r}, timeout={timeout!r}).close()\n"
        "except urllib.error.HTTPError:\n"
        "    pass\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )
    try:
        return subprocess.run([sys.executable, "-c", probe], timeout=timeout + 5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _build_backend_importable() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("setuptools", "setuptools_scm"))


def _build_wheel(repo_copy: Path) -> Path:
    dist_dir = repo_copy / "dist"
    env = os.environ.copy()
    env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HERMES_WEBUI"] = "0.52.2695"
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        ".",
        "--no-deps",
        "--wheel-dir",
        str(dist_dir),
    ]

    def _pip_wheel(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, *extra],
            cwd=repo_copy,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    result = _pip_wheel()
    output = result.stdout + result.stderr
    if (
        result.returncode != 0
        and _BUILD_DEPS_STEP_FAILED in output
        and any(marker in output for marker in _NO_INDEX_MARKERS)
        and not _index_reachable()
    ):
        if not _build_backend_importable():
            pytest.skip(
                "pip cannot provision the PEP 517 build backend (setuptools, "
                "setuptools-scm) without a reachable package index; the packaged "
                "layout is checked wherever one is (CI)"
            )
        result = _pip_wheel("--no-build-isolation")
        output = result.stdout + result.stderr
    assert result.returncode == 0, f"pip wheel failed ({result.returncode}):\n{output}"
    wheels = sorted(dist_dir.glob("hermes_webui-*.whl"))
    assert wheels, "wheel build must produce a hermes_webui wheel"
    return wheels[0]


@pytest.fixture(scope="module")
def extracted_wheel(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("packaged-runtime")
    repo_copy = _copy_repo_without_heavy_dirs(tmp)
    wheel = _build_wheel(repo_copy)
    extract_dir = tmp / "wheel"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(extract_dir)
    return wheel, extract_dir


def test_wheel_build_contains_runtime_tree(extracted_wheel):
    wheel, _ = extracted_wheel
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        assert "bootstrap.py" in names
        assert "server.py" in names
        assert "mcp_server.py" in names
        assert "api/config.py" in names
        assert "api/_scm_version.py" in names
        assert "static/__init__.py" in names
        assert "static/index.html" in names
        assert "static/ui.js" in names
        assert "static/style.css" in names
        assert "static/vendor/smd.min.js" in names


def test_extracted_wheel_resolves_static_root_without_console_entrypoint_contract_change(extracted_wheel):
    _, extract_dir = extracted_wheel
    script = """
import api.config as api_config
from api.updates import WEBUI_VERSION
print(api_config.__file__)
print(api_config.get_static_root())
print(api_config.get_index_html_path())
print(WEBUI_VERSION)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(extract_dir)
    env["GIT_CEILING_DIRECTORIES"] = str(extract_dir.parent)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=extract_dir,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines == [
        str((extract_dir / "api" / "config.py").resolve()),
        str((extract_dir / "static").resolve()),
        str((extract_dir / "static" / "index.html").resolve()),
        "v0.52.2695",
    ]
    assert Path(lines[0]).exists()
    assert Path(lines[1]).is_dir()
    assert Path(lines[2]).is_file()


def test_checkout_static_root_stays_repo_relative():
    import api.config as api_config

    assert api_config.get_static_root() == ROOT / "static"
    assert api_config.get_index_html_path() == ROOT / "static" / "index.html"
