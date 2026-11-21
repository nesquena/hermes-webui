"""The test home's skills must never reach the developer's real Hermes home.

The session test-server fixture used to seed ``TEST_STATE_DIR/skills`` by
symlinking the real ``~/.hermes/skills`` into it. The test server writes skills
into that directory (skill save/delete tests), so on any machine with real
skills those writes landed in production (#8929). CI runners have no
``~/.hermes``, so the regression is driven here in a nested pytest session whose
HOME is a throwaway directory holding a "real" skills dir that must stay
untouched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import tests.conftest as conftest

REPO_ROOT = Path(__file__).resolve().parents[1]
_PROBE_ENV = "HERMES_WEBUI_SKILLS_ISOLATION_PROBE"
_PROBE_NODE = "tests/test_conftest_skills_isolation.py::test_probe_saves_and_deletes_a_skill"


def test_test_home_skills_are_not_linked_to_the_real_hermes_home():
    skills = conftest.TEST_STATE_DIR / "skills"

    # TEST_STATE_DIR itself is refused inside ~/.hermes at conftest import, so
    # a link is the only way the test home's skills can reach the real home.
    assert not skills.is_symlink(), f"{skills} links to {skills.resolve()}"


@pytest.mark.skipif(os.environ.get(_PROBE_ENV) != "1", reason="runs only inside the nested session below")
def test_probe_saves_and_deletes_a_skill():
    from tests._pytest_port import BASE

    def post(path: str, body: dict) -> int:
        request = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    name = "isolation-probe-skill"
    content = f"---\nname: {name}\ndescription: isolation probe\n---\n\n# Probe\n"
    assert post("/api/skills/save", {"name": name, "content": content}) == 200
    assert post("/api/skills/delete", {"name": name}) == 200


def _agent_dir_for_child() -> str | None:
    agent = conftest.HERMES_AGENT
    return str(agent) if agent else None


@pytest.mark.parametrize("with_agent", [False, True], ids=["no-agent", "agent"])
def test_skill_writes_never_touch_the_real_skills_dir(tmp_path, with_agent):
    agent_dir = _agent_dir_for_child()
    if with_agent and agent_dir is None:
        pytest.skip("requires hermes-agent")

    home = tmp_path / "home"
    real_skills = home / ".hermes" / "skills"
    (real_skills / "user-skill").mkdir(parents=True)
    (real_skills / "user-skill" / "SKILL.md").write_text("# Mine\n", encoding="utf-8")
    before = (sorted(p.name for p in real_skills.iterdir()), real_skills.stat().st_mtime_ns)

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "PYTEST_"))
    }
    env.update(
        HOME=str(home),
        HERMES_WEBUI_TEST_STATE_ROOT=str(tmp_path / "state"),
        HERMES_WEBUI_AGENT_DIR=agent_dir if with_agent else str(tmp_path / "no-agent"),
        **{_PROBE_ENV: "1"},
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", _PROBE_NODE,
            "-q", "-p", "no:cacheprovider", "--no-header", "-rfEs",
            f"--basetemp={tmp_path / 'bt'}",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "1 passed" in result.stdout, result.stdout[-3000:]
    after = (sorted(p.name for p in real_skills.iterdir()), real_skills.stat().st_mtime_ns)
    assert after == before, f"real skills dir changed: {before} -> {after}"
