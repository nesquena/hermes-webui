"""The per-family MCP job cannot quietly stop certifying.

This lives outside `tests/test_mcp_server.py` on purpose. That module stands
down when the optional `mcp` package is absent, and more importantly it runs
*inside* the job these assertions describe, so `continue-on-error` on the
certifying step would suppress the failure they report. This file imports no
SDK, so it runs in every shard of the required `test` matrix, which is a
different job from the one it inspects.

See `tests/_mcp_family_contract.py` for the declarations and the contract.
"""
import pytest

from tests._mcp_family_contract import (
    DOCS_ONLY_GUARD,
    EXPECTED_FAMILY_ENV,
    FAMILY_MAJOR,
    JOB_GUARD,
    JOB_ID,
    MCP_FAMILIES,
    NON_EXECUTING_PYTEST_FLAGS,
    assert_family_matrix_contract,
    certifying_step,
    constraint_install_step,
    load_workflow,
    normalize_condition,
    runnable_lines,
)


def test_every_declared_family_has_a_job_that_certifies_it():
    """The whole contract, in the words it fails with."""
    assert_family_matrix_contract()


def test_matrix_families_match_the_declaration():
    job = load_workflow()["jobs"][JOB_ID]
    entries = job["strategy"]["matrix"]["include"]
    assert {entry["family"] for entry in entries} == set(MCP_FAMILIES)
    for entry in entries:
        major = FAMILY_MAJOR[entry["family"]]
        assert entry["constraint"] == f"mcp>={major},<{major + 1}"


def test_job_cannot_skip_or_tolerate_failure():
    """A skipped job reports success, including as a required check."""
    job = load_workflow()["jobs"][JOB_ID]
    assert normalize_condition(job.get("if")) == JOB_GUARD
    assert job.get("continue-on-error") is None
    assert job["strategy"].get("fail-fast") is False


def test_constraint_install_has_no_fallback():
    job = load_workflow()["jobs"][JOB_ID]
    install = constraint_install_step(job)
    line = next(
        candidate for candidate in runnable_lines(install["run"])
        if "${{ matrix.constraint }}" in candidate
    )
    assert "||" not in line


@pytest.mark.parametrize("flag", sorted(NON_EXECUTING_PYTEST_FLAGS))
def test_certifying_command_carries_no_execution_selector(flag):
    """Each way a pytest run can exit 0 without running what it names."""
    job = load_workflow()["jobs"][JOB_ID]
    command = runnable_lines(certifying_step(job)["run"])[0]
    assert flag not in command.split()


def test_certifying_step_runs_one_unchained_pytest_command():
    """`pytest … || true` exits 0 under GitHub's `bash -e -o pipefail`."""
    job = load_workflow()["jobs"][JOB_ID]
    certify = certifying_step(job)
    lines = runnable_lines(certify["run"])
    assert len(lines) == 1
    tokens = lines[0].split()
    assert tokens[0] == "pytest"
    assert "tests/test_mcp_server.py" in tokens
    assert "||" not in lines[0]
    assert "&&" not in lines[0]
    assert ";" not in lines[0]


def test_certifying_step_is_wired_to_the_matrix_family_and_only_docs_gated():
    job = load_workflow()["jobs"][JOB_ID]
    certify = certifying_step(job)
    assert certify["env"][EXPECTED_FAMILY_ENV] == "${{ matrix.family }}"
    assert certify.get("continue-on-error") is None
    assert normalize_condition(certify.get("if")) == DOCS_ONLY_GUARD
