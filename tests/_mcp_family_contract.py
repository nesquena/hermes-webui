"""The declared MCP SDK families, and the CI contract that certifies each one.

`mcp_server.py` supports two SDK families and picks between them at import time
(`_HAS_DECORATOR_HANDLERS`, mcp_server.py:462), so only the installed family's
registration branch ever runs. `.github/workflows/tests.yml` pins one family per
`mcp-family` job to close that, and `tests/test_mcp_server.py` refuses to skip
when `HERMES_MCP_EXPECTED_FAMILY` names one.

The declarations and the workflow assertions live here, not in
`tests/test_mcp_server.py`, for two reasons:

* That module stands down when the optional `mcp` package is absent, and this
  contract has nothing to do with the package being installed.
* A check that a job cannot silently stop certifying must not live inside that
  job. `continue-on-error` on the certifying step would suppress the very
  failure the check reports. `tests/test_mcp_family_ci_contract.py` imports this
  module and runs in the shard matrix instead, which is a required check and a
  different job from the one it inspects.

Usage::

    from tests._mcp_family_contract import MCP_FAMILIES, FAMILY_MAJOR
"""
import pathlib

MCP_FAMILY_DECORATOR = "mcp1"
MCP_FAMILY_CONSTRUCTOR = "mcp2"
MCP_FAMILIES = (MCP_FAMILY_DECORATOR, MCP_FAMILY_CONSTRUCTOR)

# The `mcp` major each family is the SDK API of, so production's hasattr probe
# can be checked against the package metadata it is probing, and so each job's
# constraint can be checked against the family it claims to pin.
FAMILY_MAJOR = {MCP_FAMILY_DECORATOR: 1, MCP_FAMILY_CONSTRUCTOR: 2}

EXPECTED_FAMILY_ENV = "HERMES_MCP_EXPECTED_FAMILY"

_REPO = pathlib.Path(__file__).parent.parent.resolve()
WORKFLOW_PATH = _REPO / ".github" / "workflows" / "tests.yml"
JOB_ID = "mcp-family"

# The one command the certifying step may run, compared exactly.
#
# Blacklisting the ways a pytest invocation can exit 0 without running what it
# names is a losing game: `-ktest_x` attaches with no space so a token check
# misses it, `cmd &` is an asynchronous list that bash reports as success
# immediately, and GitHub's default Linux shell is `bash -e {0}` with no
# pipefail, so `cmd | true` swallows the exit status. An exact match closes all
# of them at once, and makes any genuine change to this command a deliberate
# edit here as well.
CERTIFYING_COMMAND = "pytest tests/test_mcp_server.py -v --timeout=60"

# Selection can also arrive from outside the command. `PYTEST_ADDOPTS` set at
# workflow, job, or step level deselects everything with the `run:` line
# untouched, and an `addopts` in pytest config does the same from the repo.
SELECTION_ENV_VARS = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
)
SELECTION_ADDOPTS_FLAGS = ("-k", "-m", "--deselect", "--ignore", "--collect-only", "--co")
PYTEST_CONFIG_FILES = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")

DOCS_ONLY_GUARD = "needs.changes.outputs.docs_only != 'true'"
JOB_GUARD = "always()"


def load_workflow():
    """The parsed workflow document."""
    import yaml

    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def normalize_condition(value):
    """A GitHub `if:` value with an optional `${{ }}` wrapper removed.

    `if: always()` and `if: ${{ always() }}` mean the same thing, so comparing
    the raw string would fail on an edit that changed nothing.
    """
    text = str(value).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def runnable_lines(run):
    """The command lines of a `run:` block, comments and blanks dropped."""
    return [
        line.strip()
        for line in (run or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def certifying_step(job):
    """The step that runs the registered-boundary matrix under a pinned family."""
    return next(
        step for step in job["steps"]
        if EXPECTED_FAMILY_ENV in (step.get("env") or {})
    )


def constraint_install_step(job):
    """The step that installs the family constraint."""
    return next(
        step for step in job["steps"]
        if any("${{ matrix.constraint }}" in line for line in runnable_lines(step.get("run")))
    )


def assert_family_matrix_contract():
    """Every declared family has a job that actually certifies it.

    Raises AssertionError naming the first breach. Kept as a function rather
    than inlined into a test so the failure reads the same wherever it runs.
    """
    workflow = load_workflow()
    job = workflow["jobs"][JOB_ID]

    entries = job["strategy"]["matrix"]["include"]
    families = {entry["family"] for entry in entries}
    assert families == set(MCP_FAMILIES), (
        f"job matrix covers {sorted(families)} but the declared families are "
        f"{sorted(MCP_FAMILIES)}; a declared family with no job is uncertified"
    )

    # Closed on both ends. An open upper bound would let the next major resolve
    # into a job that claims to be certifying this one.
    for entry in entries:
        major = FAMILY_MAJOR[entry["family"]]
        assert entry["constraint"] == f"mcp>={major},<{major + 1}", (
            f"{entry['family']} pins {entry['constraint']!r}, which is not the "
            f"closed range for major {major}"
        )

    # A skipped job reports success, including when it is a required check, so
    # the job-level condition is part of the contract.
    assert normalize_condition(job.get("if")) == JOB_GUARD, (
        f"job condition is {job.get('if')!r}; anything but {JOB_GUARD} can make "
        f"the job skip and still report success"
    )
    assert job.get("continue-on-error") is None
    assert job["strategy"].get("fail-fast") is False

    install = constraint_install_step(job)
    constraint_line = next(
        line for line in runnable_lines(install["run"])
        if "${{ matrix.constraint }}" in line
    )
    assert "||" not in constraint_line, (
        f"constraint install {constraint_line!r} carries a shell fallback, so a "
        f"resolution failure would not fail the job"
    )

    certify = certifying_step(job)
    assert certify["env"][EXPECTED_FAMILY_ENV] == "${{ matrix.family }}"
    assert certify.get("continue-on-error") is None
    assert normalize_condition(certify.get("if")) == DOCS_ONLY_GUARD, (
        f"certifying step condition is {certify.get('if')!r}; only the shared "
        f"docs-only guard may skip it"
    )

    lines = runnable_lines(certify["run"])
    assert lines == [CERTIFYING_COMMAND], (
        f"certifying step runs {lines!r}; it may run exactly "
        f"[{CERTIFYING_COMMAND!r}]. Any selector, chained command, pipeline, or "
        f"background list here can exit 0 without running the registered-boundary cases"
    )
    # The default shell decides whether a pipeline's exit status even reaches
    # the runner, so an override is part of what the command means.
    assert "shell" not in certify, f"certifying step overrides the shell: {certify.get('shell')!r}"
    assert job.get("defaults") is None
    assert workflow.get("defaults") is None

    assert_no_external_test_selection(workflow)


def assert_no_external_test_selection(workflow=None):
    """Nothing outside the command may deselect the certification.

    `PYTEST_ADDOPTS` in the workflow, or an `addopts` carrying a selector in the
    repo's pytest config, reaches the run with the `run:` line untouched.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    for name in SELECTION_ENV_VARS:
        assert name not in text, (
            f"{name} appears in {WORKFLOW_PATH.name}; it can deselect the "
            f"certification without touching the command"
        )

    for filename in PYTEST_CONFIG_FILES:
        path = _REPO / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("addopts"):
                continue
            offenders = [flag for flag in SELECTION_ADDOPTS_FLAGS if flag in stripped]
            assert not offenders, (
                f"{filename} sets {stripped!r}, carrying {offenders}, which "
                f"deselects tests for every run including the certification"
            )
