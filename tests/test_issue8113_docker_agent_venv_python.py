"""Regression tests for issue #8113: the Docker venv was built on the base
image interpreter (3.12), so every agent dependency gated on
``python_version >= '3.14'`` was silently dropped and ``run_agent`` failed to
import with the container still reporting healthy.

The chokepoint is the venv decision in ``docker_init.bash``. The decision logic
lives in ``scripts/docker_venv_python.py`` so it is unit-testable without
Docker; these tests cover the fresh, reused-incompatible, reused-compatible and
no-agent-source paths, plus the ``.deps_installed`` interaction that makes a
restart skip the reinstall entirely.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO / "scripts" / "docker_venv_python.py"

_spec = importlib.util.spec_from_file_location("docker_venv_python", HELPER_PATH)
dvp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dvp)

# A pyproject shaped like the agent that triggered #8113: `requires-python`
# still admits 3.11, but the core dependency set is gated on 3.14.
AGENT_PYPROJECT_314 = """
[project]
name = "hermes-agent"
requires-python = ">=3.11,<3.15"
dependencies = [
  "python-dotenv==1.2.2; python_version >= '3.14'",
  "ruamel.yaml==0.18.16; python_version >= '3.14'",
  "pydantic==2.13.4; python_version >= '3.14'",
  "click>=8.1",
]

[project.optional-dependencies]
all = ["mcp>=2.0", "fastapi>=0.133"]
"""

# The pre-#8113 agent: no 3.14 gates, so any interpreter `requires-python`
# admits is fine and the base image must be left alone.
AGENT_PYPROJECT_LEGACY = """
[project]
name = "hermes-agent"
requires-python = ">=3.11"
dependencies = ["python-dotenv==1.1.0", "pydantic==2.9.0"]
"""


def _make_venv(path: Path, version_info: str) -> Path:
    venv = path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text(
        f"home = /usr/local/bin\nversion_info = {version_info}\n", encoding="utf-8"
    )
    return venv


def _agent_dir(tmp_path: Path, pyproject: str) -> Path:
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    (agent / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return agent


# ── required-version derivation ──────────────────────────────────────────────


def test_required_version_comes_from_the_highest_dependency_gate():
    """The 3.14 gates are the authoritative signal, not `requires-python`.

    `requires-python = ">=3.11,<3.15"` would wrongly admit the base image's
    3.12, which is exactly how the 33 core deps were dropped.
    """
    assert dvp.required_agent_python(AGENT_PYPROJECT_314) == (3, 14)


def test_required_version_is_none_without_any_constraint():
    """No gates and no usable ceiling must not invent a version."""
    pyproject = '[project]\nname="x"\nrequires-python = ">=3.11"\ndependencies = ["click"]\n'
    assert dvp.required_agent_python(pyproject) is None


def test_unparseable_pyproject_fails_closed_to_none():
    """Malformed input must not be guessed at — caller keeps the base image."""
    assert dvp.required_agent_python("this is not toml {{{") is None


# ── venv plan: fresh, reused-incompatible, reused-compatible ─────────────────


def test_fresh_container_selects_the_gate_version_not_the_base_image(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = tmp_path / "venv"

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (3, 14)


def test_reused_incompatible_venv_is_rebuilt(tmp_path):
    """The restart trap: a 3.12 venv with `.deps_installed` must be rebuilt.

    This is the case the maintainer asked for explicitly — a restart otherwise
    reuses the 3.12 venv *and* skips the install because `.deps_installed` is
    present, pinning the broken interpreter forever.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, "3.12.15")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"
    assert plan["python"] == (3, 14)
    assert plan["clear_deps_marker"] is True


def test_reused_compatible_venv_still_takes_the_fast_path(tmp_path):
    """A venv already on the required version must not be rebuilt.

    Guards against a fix that rebuilds on every restart and makes container
    restarts pay a full 100+ package reinstall.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, "3.14.8")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"
    assert plan["clear_deps_marker"] is False


def test_reused_venv_above_the_exclusive_upper_bound_is_rebuilt(tmp_path):
    """`<3.15` is exclusive: a 3.15 venv is outside what the agent accepts.

    Clearing the 3.14 dependency gate is necessary, not sufficient — pip would
    refuse the agent itself on 3.15 (`requires-python` mismatch), so the venv
    has to move back inside the declared range.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, "3.15.1")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"
    assert plan["python"] == (3, 14)
    assert plan["clear_deps_marker"] is True


def test_reused_venv_inside_the_range_and_above_the_gate_is_kept(tmp_path):
    """Only an interpreter outside the admissible set forces a rebuild.

    With the range widened to `<3.16`, a 3.15 venv clears both the gate and the
    declared bounds, so an operator's higher pin must not be torn down just
    because it is not the exact interpreter a fresh container would pick.
    """
    pyproject = AGENT_PYPROJECT_314.replace('">=3.11,<3.15"', '">=3.11,<3.16"')
    assert ',<3.16"' in pyproject
    agent = _agent_dir(tmp_path, pyproject)
    venv_dir = _make_venv(tmp_path, "3.15.1")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"
    assert plan["clear_deps_marker"] is False


def test_legacy_agent_leaves_the_base_image_alone(tmp_path):
    """No 3.14 gate must not pin an interpreter — avoid a needless 3.14 fetch."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_LEGACY)
    venv_dir = _make_venv(tmp_path, "3.12.15")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["python"] is None
    assert plan["action"] == "reuse"


def test_missing_agent_source_falls_back_to_the_base_image(tmp_path):
    """No staged agent (single-container / dev) must keep today's behaviour."""
    venv_dir = _make_venv(tmp_path, "3.12.15")

    plan = dvp.plan_agent_venv(tmp_path / "nope" / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["python"] is None
    assert plan["action"] == "reuse"


def test_no_venv_yet_creates_on_the_gate_version(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (3, 14)


def test_venv_without_pyvenv_cfg_is_treated_as_incompatible(tmp_path):
    """A structurally broken venv must be rebuilt, not reused and crash later."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = tmp_path / "venv"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"


# ── the complete requires-python range, on creation and on reuse ─────────────

# `requires-python` alone carries the floor here: there are no dependency gates.
AGENT_PYPROJECT_LOWER_BOUND_ONLY = """
[project]
name = "hermes-agent"
requires-python = ">=3.14"
dependencies = ["python-dotenv==1.2.2", "pydantic==2.13.4"]
"""

# The dependency gate asks for 3.14 but the declared range stops below it, so
# no interpreter satisfies both — there is nothing safe to rebuild onto.
AGENT_PYPROJECT_UNSATISFIABLE = """
[project]
name = "hermes-agent"
requires-python = ">=3.11,<3.14"
dependencies = ["python-dotenv==1.2.2; python_version >= '3.14'"]
"""


def _range_pyproject(requires_python: str) -> str:
    return f'[project]\nname="x"\nrequires-python = "{requires_python}"\ndependencies = ["click"]\n'


def test_lower_bound_only_range_rebuilds_an_older_venv(tmp_path):
    """`>=3.14` with no upper bound is still a constraint a 3.12 venv fails."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_LOWER_BOUND_ONLY)
    venv_dir = _make_venv(tmp_path, "3.12.15")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"
    assert plan["python"] == (3, 14)
    assert plan["clear_deps_marker"] is True


def test_lower_bound_only_range_creates_on_an_admissible_interpreter(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_LOWER_BOUND_ONLY)

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (3, 14)


def test_supported_existing_interpreter_is_kept_below_an_upper_bound(tmp_path):
    """An upper bound is not a target: a 3.12 venv inside `<3.14` stays put.

    Treating the highest admitted minor (3.13) as a floor would tear down a
    venv the agent already accepts and pay a full reinstall on every upgrade.
    """
    agent = _agent_dir(tmp_path, _range_pyproject(">=3.11,<3.14"))
    venv_dir = _make_venv(tmp_path, "3.12.15")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"
    assert plan["python"] is None
    assert plan["clear_deps_marker"] is False


def test_existing_venv_above_the_upper_bound_is_rebuilt_on_the_admissible_base(tmp_path):
    """The rebuild names its interpreter explicitly, even when it is the base.

    Leaving it to uv's default discovery could re-select the very interpreter
    being replaced (a managed 3.14 fetched by an earlier plan).
    """
    agent = _agent_dir(tmp_path, _range_pyproject(">=3.11,<3.14"))
    venv_dir = _make_venv(tmp_path, "3.14.2")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"
    assert plan["python"] == (3, 12)


def test_fresh_container_honours_an_upper_bound_below_the_base(tmp_path):
    agent = _agent_dir(tmp_path, _range_pyproject(">=3.10,<3.12"))

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (3, 11)


# A range that names a major the base image does not ship. The base image
# carries 3.12; this agent declares ">=4.0,<4.2" and its dependencies carry no
# python_version gate — the #8113 shape across a major boundary (the agent
# needs an interpreter the image cannot provide, and nothing else states it).
AGENT_PYPROJECT_NEXT_MAJOR = """
[project]
name = "hermes-agent"
requires-python = ">=4.0,<4.2"
dependencies = [
  "python-dotenv==1.2.2",
]
"""


def test_cross_major_range_creates_on_an_admissible_interpreter(tmp_path):
    """A declared major the base image lacks must still be reachable.

    The candidate search used to stop at the base image's major, so
    ">=4.0,<4.2" had no candidate at all and the plan fell back to the base
    interpreter — exactly the #8113 signature: a venv on 3.12 with the
    agent's 4.x dependencies silently dropped.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_NEXT_MAJOR)

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (4, 0)
    assert plan["required"] == (4, 0)


def test_cross_major_range_rebuilds_a_venv_on_the_base_major(tmp_path):
    """A 3.12 venv cannot satisfy a ">=4.0,<4.2" agent; it must be rebuilt.

    Keeping it would pin a venv the agent cannot be installed into, so every
    restart would report healthy while `run_agent` fails to import.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_NEXT_MAJOR)
    venv_dir = _make_venv(tmp_path, "3.12.15")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"
    assert plan["python"] == (4, 0)
    assert plan["clear_deps_marker"] is True


def test_a_lower_declared_major_is_not_selected_below_the_base_image(tmp_path):
    """The image's own major is the floor: never build a venv below it.

    The base image is 3.12; a "<3" range admits nothing the image can run, so
    there is no candidate and the base interpreter is left in place.
    """
    agent = _agent_dir(tmp_path, _range_pyproject(">=2.7,<3"))

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] is None


def test_unsatisfiable_constraints_keep_the_existing_venv(tmp_path):
    """No admissible interpreter: do not delete a working environment first.

    Rebuilding onto the gate would violate `requires-python` (pip refuses the
    agent) and rebuilding onto anything else cannot satisfy the gate, so the
    only outcome of `rm -rf /app/venv` would be a slower, equally broken boot.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_UNSATISFIABLE)
    venv_dir = _make_venv(tmp_path, "3.12.15")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"
    assert plan["python"] is None
    assert plan["clear_deps_marker"] is False


def test_unsatisfiable_constraints_create_on_the_base_image(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_UNSATISFIABLE)

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] is None


def test_unparseable_requires_python_fails_closed_to_reuse(tmp_path):
    """An unreadable range cannot be respected, so it must not drive a rebuild."""
    pyproject = AGENT_PYPROJECT_314.replace('">=3.11,<3.15"', '"about 3.14"')
    assert '"about 3.14"' in pyproject
    agent = _agent_dir(tmp_path, pyproject)
    venv_dir = _make_venv(tmp_path, "3.12.15")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"
    assert plan["python"] is None


# The helper runs on the base image's bare interpreter before any venv exists,
# where `packaging` is not guaranteed to be importable, so it carries its own
# comparator. Pin it to packaging's SpecifierSet over the shapes interpreter
# bounds actually take.
_ORACLE_SPECS = [
    ">=3.11,<3.15",
    ">=3.14",
    "<3.14",
    "<=3.13",
    ">3.12",
    ">3.14.0",
    "<3.14.2",
    "==3.12",
    "==3.14.0",
    "!=3.12",
    "==3.14.*",
    "==3.*",
    "!=3.13.*",
    "~=3.11",
    "~=3.11.2",
    ">=3",
    ">=3.11, !=3.12.*, <3.15",
    " >= 3.11 , < 3.15 ",
    ">=3.11,",
    "",
]
_ORACLE_VERSIONS = [
    (3, 9), (3, 10), (3, 11), (3, 11, 2), (3, 11, 5), (3, 12), (3, 12, 0), (3, 12, 1),
    (3, 13), (3, 14), (3, 14, 1), (3, 14, 2), (3, 15), (3, 16), (4, 0),
]


@pytest.mark.parametrize("spec", _ORACLE_SPECS)
def test_requires_python_comparator_matches_packaging(spec):
    specifiers = pytest.importorskip("packaging.specifiers")
    oracle = specifiers.SpecifierSet(spec)
    for version in _ORACLE_VERSIONS:
        text = ".".join(str(part) for part in version)
        assert dvp.requires_python_admits(spec, version) is oracle.contains(text), (spec, text)


@pytest.mark.parametrize("spec", ["===3.14", ">=3.14.0b1", "3.14", ">=3.*", "~=3", ">=3.14;", "about 3.14"])
def test_requires_python_syntax_outside_the_comparator_is_unparseable(spec):
    """Anything the comparator cannot evaluate exactly is reported, not guessed."""
    assert dvp.requires_python_admits(spec, (3, 14)) is None


# ── the CLI docker_init.bash actually calls ──────────────────────────────────


def test_cli_prints_the_planned_python_for_the_shell(tmp_path):
    """`docker_init.bash` consumes stdout, so the contract is a real value."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    (venv_dir / "bin").mkdir()

    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), str(agent / "pyproject.toml"), str(venv_dir), "3.12"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "create 3.14"


def test_cli_is_silent_when_no_agent_is_staged(tmp_path):
    """A missing agent must not fail the container — plain no-version output."""
    venv_dir = _make_venv(tmp_path, "3.12.15")

    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), str(tmp_path / "nope.toml"), str(venv_dir), "3.12"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reuse -"


def _cli_plan(pyproject: Path, venv_dir: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), str(pyproject), str(venv_dir), "3.12"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_cli_rebuilds_a_venv_below_a_lower_bound_only_range(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_LOWER_BOUND_ONLY)
    venv_dir = _make_venv(tmp_path, "3.12.15")

    assert _cli_plan(agent / "pyproject.toml", venv_dir) == "rebuild 3.14"


def test_cli_keeps_the_venv_when_no_interpreter_is_admissible(tmp_path):
    """`reuse -` is the shell contract for \"leave /app/venv exactly as found\"."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_UNSATISFIABLE)
    venv_dir = _make_venv(tmp_path, "3.12.15")

    assert _cli_plan(agent / "pyproject.toml", venv_dir) == "reuse -"


# ── docker_init.bash wiring ──────────────────────────────────────────────────

INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")


def test_docker_init_resolves_the_venv_plan_before_creating_the_venv():
    """The decision must precede `uv venv`, or the base image wins by default."""
    plan_pos = INIT_SH.find("docker_venv_python.py")
    # Match the real invocation, not the explanatory comment that also names it.
    create_pos = INIT_SH.find("uv venv venv")
    assert plan_pos != -1, "docker_init.bash must consult scripts/docker_venv_python.py"
    assert create_pos != -1, "uv venv invocation not found"
    assert plan_pos < create_pos, "interpreter selection must run before venv creation"


def test_docker_init_passes_the_requested_version_to_uv():
    """`uv venv` alone cannot pick a non-base interpreter."""
    assert "UV_PYTHON=" in INIT_SH or "--python" in INIT_SH


def test_docker_init_rebuild_path_drops_the_deps_marker():
    """Without dropping `.deps_installed` the rebuild skips its own install.

    The rebuild now creates a fresh venv through `rebuild_agent_venv` (which
    keeps the old one until the new one exists), so the marker is absent from
    the rebuilt venv by construction; the bash behaviour is pinned in
    tests/test_issue8113_venv_rebuild_keeps_previous.py.
    """
    plan_pos = INIT_SH.find("docker_venv_python.py")
    rebuild_pos = INIT_SH.find('if [ "$_venv_action" = "rebuild" ]; then')
    call_pos = INIT_SH.find('rebuild_agent_venv "$_venv_python"', rebuild_pos)
    install_guard_pos = INIT_SH.find("if [ -f /app/venv/.deps_installed ]; then")
    assert rebuild_pos != -1, "no rebuild branch — a reused 3.12 venv would stay broken"
    assert call_pos != -1, "the rebuild branch must replace the venv with a fresh one"
    assert plan_pos < rebuild_pos, "the plan must be computed before the rebuild branch"
    assert rebuild_pos < call_pos < install_guard_pos, "the venv must be rebuilt before the fast-restart guard"

# ── #8360 gate: the micro version decides `requires-python` ─────────────────

PATCH_FLOOR_PYPROJECT = """
[project]
name = "hermes-agent"
requires-python = ">=3.12.1,<3.15"
dependencies = ["click>=8.1"]
"""


def test_venv_version_keeps_the_micro_release(tmp_path):
    assert dvp.venv_python_version(_make_venv(tmp_path / "a", "3.12.13.final.0")) == (3, 12, 13)
    plain = tmp_path / "b" / "venv"
    (plain / "bin").mkdir(parents=True)
    (plain / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.13\n", encoding="utf-8")
    assert dvp.venv_python_version(plain) == (3, 12, 13)


def test_an_admissible_patch_release_is_reused_not_rebuilt(tmp_path):
    """Python 3.12.13 satisfies `>=3.12.1`; reading it as 3.12.0 rebuilt it onto 3.13."""
    agent = _agent_dir(tmp_path, PATCH_FLOOR_PYPROJECT)
    venv_dir = _make_venv(tmp_path, "3.12.13.final.0")
    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12, 13))
    assert plan["action"] == "reuse"
    assert plan["python"] is None


def test_an_admissible_base_patch_release_is_selected(tmp_path):
    constraints = dvp.agent_python_constraints(PATCH_FLOOR_PYPROJECT)
    assert dvp.select_agent_python(constraints, (3, 12, 13)) == (3, 12, 13)
    # A base below the floor is not admissible; the nearest minor that is wins.
    assert dvp.select_agent_python(constraints, (3, 12, 0)) == (3, 13)


def test_cli_keeps_the_base_micro_release(tmp_path):
    agent = _agent_dir(tmp_path, PATCH_FLOOR_PYPROJECT)
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), str(agent / "pyproject.toml"), str(tmp_path / "absent"), "3.12.13"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "create -", "the base 3.12.13 is admissible: no interpreter to pin"


@pytest.mark.parametrize(
    "version_info,expected",
    [("3.14.2.final.0", 0), ("3.13.9.final.0", 1), ("3.15.0.final.0", 1)],
)
def test_cli_admits_checks_the_interpreter_uv_installed(tmp_path, version_info, expected):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, version_info)
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--admits", str(agent / "pyproject.toml"), str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected, result.stderr


def test_cli_admits_anything_when_no_agent_is_staged(tmp_path):
    venv_dir = _make_venv(tmp_path, "3.12.13.final.0")
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--admits", str(tmp_path / "nope.toml"), str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
