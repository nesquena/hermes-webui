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
    CERTIFYING_COMMAND,
    DOCS_ONLY_GUARD,
    EXPECTED_FAMILY_ENV,
    FAMILY_MAJOR,
    JOB_GUARD,
    JOB_ID,
    MCP_FAMILIES,
    SELECTION_ENV_VARS,
    WORKFLOW_PATH,
    assert_family_matrix_contract,
    assert_no_external_test_selection,
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


def test_certifying_step_runs_exactly_the_contract_command():
    """One exact command, because the bypasses are not an enumerable set.

    `-ktest_x` attaches with no space, `cmd &` is an async list bash calls
    successful straight away, and GitHub's default Linux shell is `bash -e {0}`
    with no pipefail, so `cmd | true` swallows pytest's status.
    """
    job = load_workflow()["jobs"][JOB_ID]
    assert runnable_lines(certifying_step(job)["run"]) == [CERTIFYING_COMMAND]


def test_certifying_step_does_not_override_the_shell():
    workflow = load_workflow()
    job = workflow["jobs"][JOB_ID]
    assert "shell" not in certifying_step(job)
    assert job.get("defaults") is None
    assert workflow.get("defaults") is None


@pytest.mark.parametrize("name", SELECTION_ENV_VARS)
def test_workflow_sets_no_pytest_selection_env(name):
    """`PYTEST_ADDOPTS` deselects everything with the `run:` line untouched."""
    assert name not in WORKFLOW_PATH.read_text(encoding="utf-8")


def test_repo_pytest_config_deselects_nothing():
    assert_no_external_test_selection()


def test_certifying_step_is_wired_to_the_matrix_family_and_only_docs_gated():
    job = load_workflow()["jobs"][JOB_ID]
    certify = certifying_step(job)
    assert certify["env"][EXPECTED_FAMILY_ENV] == "${{ matrix.family }}"
    assert certify.get("continue-on-error") is None
    assert normalize_condition(certify.get("if")) == DOCS_ONLY_GUARD
