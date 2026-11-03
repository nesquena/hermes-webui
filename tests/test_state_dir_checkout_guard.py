"""The test state dir must never resolve to the checkout.

`HERMES_WEBUI_TEST_STATE_DIR` is read with `os.getenv(name, default)`, which
returns `""` (not the default) when the variable is exported but empty — and
`Path("").resolve()` is the CURRENT DIRECTORY. A bare `HERMES_WEBUI_TEST_STATE_DIR=`
run from the repo root therefore pointed the session-teardown `rmtree` at the
checkout and deleted it.

Both cases are exercised in a subprocess that only imports `tests/conftest.py`:
module import computes the dir but never removes it, so a regression fails the
assertion instead of destroying the repo running the test.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFTEST = REPO_ROOT / "tests" / "conftest.py"

_PROBE = (
    "import importlib.util, sys, pathlib;"
    "spec = importlib.util.spec_from_file_location('webui_conftest', sys.argv[1]);"
    "mod = importlib.util.module_from_spec(spec);"
    "spec.loader.exec_module(mod);"
    "print(mod.TEST_STATE_DIR)"
)


def _run_probe(tmp_path: Path, state_dir_value: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HERMES_WEBUI_TEST_STATE_DIR"] = state_dir_value
    # A runner-exported state ROOT would re-anchor the fallback outside tmp_path.
    env.pop("HERMES_WEBUI_TEST_STATE_ROOT", None)
    # Keep the default under a temp root that is outside the real home tree, and
    # out of this machine's TMPDIR (which may itself live under ~/.hermes).
    env["TMPDIR"] = str(tmp_path / "tmp")
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "home").mkdir()
    return subprocess.run(
        [sys.executable, "-c", _PROBE, str(CONFTEST)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_empty_state_dir_env_falls_back_instead_of_the_checkout(tmp_path):
    out = _run_probe(tmp_path, "")
    assert out.returncode == 0, out.stderr
    resolved = Path(out.stdout.strip()).resolve()
    assert resolved != REPO_ROOT
    assert resolved not in REPO_ROOT.parents
    assert (tmp_path / "tmp").resolve() in resolved.parents


def test_state_dir_pointing_at_the_checkout_is_refused(tmp_path):
    out = _run_probe(tmp_path, str(REPO_ROOT))
    assert out.returncode != 0
    assert "REFUSING TO RUN" in out.stderr


# Maintainer follow-up on #8414 (release review): the session fixture rmtrees the
# state dir at setup and teardown, so a value INSIDE the checkout (``tests``,
# ``static``, a relative path) wiped that directory on master. Refuse it too.
@pytest.mark.parametrize("value", ["tests", "static", "./tests", "tests/state-inside"])
def test_state_dir_inside_the_checkout_is_refused(tmp_path, value):
    out = _run_probe(tmp_path, value)
    assert out.returncode != 0
    assert "REFUSING TO RUN" in out.stderr


def test_absolute_path_inside_the_checkout_is_refused(tmp_path):
    out = _run_probe(tmp_path, str(REPO_ROOT / "static"))
    assert out.returncode != 0
    assert "REFUSING TO RUN" in out.stderr


def test_whitespace_only_state_dir_env_falls_back(tmp_path):
    out = _run_probe(tmp_path, "   ")
    assert out.returncode == 0, out.stderr
    resolved = Path(out.stdout.strip()).resolve()
    assert REPO_ROOT not in resolved.parents and resolved != REPO_ROOT
    assert (tmp_path / "tmp").resolve() in resolved.parents


def test_explicit_state_dir_outside_the_checkout_is_honoured(tmp_path):
    target = tmp_path / "explicit-state"
    out = _run_probe(tmp_path, str(target))
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).resolve() == target.resolve()


def test_state_dir_ending_in_a_space_named_directory_is_kept_exactly(tmp_path):
    # A non-blank value must not be stripped: "<dir>/ " would otherwise select
    # <dir> itself, and the session cleanup would delete its other contents.
    target = tmp_path / "parent" / " "
    out = _run_probe(tmp_path, str(target))
    assert out.returncode == 0, out.stderr
    assert out.stdout.rstrip("\n") == str(target.resolve())
