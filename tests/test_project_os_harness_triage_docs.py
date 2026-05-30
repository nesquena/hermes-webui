from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "project-os-harness-triage.md"
TESTS = (REPO_ROOT / "tests" / "test_project_os_extension_regressions.py").read_text(encoding="utf-8")


def test_project_os_harness_triage_doc_exists():
    assert DOC.exists(), "docs/project-os-harness-triage.md must exist for Project OS harness triage"


def test_project_os_harness_triage_doc_covers_false_timeout_stalled_running_and_root_seed():
    content = DOC.read_text(encoding="utf-8")
    for needle in [
        "false timeout",
        "stalled_running",
        "root seed",
        "host timeout",
        "dedicated project session",
    ]:
        assert needle in content, f"triage doc must cover {needle!r} semantics"


def test_project_os_harness_triage_doc_links_to_source_level_regressions():
    content = DOC.read_text(encoding="utf-8")
    expected_tests = [
        "test_project_os_host_timeout_banner_is_not_project_session_truth",
        "test_project_os_submit_lifecycle_marks_zero_message_active_stream_as_stalled_running",
        "test_project_os_workflow_root_seed_is_reference_only_done_anchor",
    ]
    for test_name in expected_tests:
        assert test_name in TESTS, f"sanity: source regression {test_name} must exist"
        assert test_name in content, f"triage doc must point to {test_name}"


def test_testing_plan_links_to_project_os_harness_triage_doc():
    testing = (REPO_ROOT / "TESTING.md").read_text(encoding="utf-8")
    assert "docs/project-os-harness-triage.md" in testing, (
        "TESTING.md must point contributors at the Project OS harness triage guide"
    )


def test_troubleshooting_links_to_project_os_harness_triage_doc():
    troubleshooting = (REPO_ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    assert "project-os-harness-triage.md" in troubleshooting, (
        "docs/troubleshooting.md must point at the Project OS harness triage guide"
    )