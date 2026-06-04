from api.models import title_from
from api.streaming import (
    _fallback_title_from_exchange,
    _hermes_webui_context_prefix,
    _strip_workspace_prefix,
    _workspace_context_prefix,
)


def test_workspace_prefix_strips_only_versioned_sentinel():
    assert _strip_workspace_prefix("[Workspace::v1: /tmp/project]\nHello") == "Hello"
    assert _strip_workspace_prefix("[Workspace: /tmp/project]\nHello") == "[Workspace: /tmp/project]\nHello"


def test_workspace_prefix_escapes_paths_with_closing_brackets():
    prefix = _workspace_context_prefix("/tmp/proj-[wip]/src")

    assert prefix == "[Workspace::v1: /tmp/proj-[wip\\]/src]\n"
    assert _strip_workspace_prefix(f"{prefix}Continue") == "Continue"


def test_project_context_prefix_assigned_precedes_workspace_sentinel():
    prefix = _hermes_webui_context_prefix(
        project_id='proj_123',
        project_name='Project "One"',
        workspace='/tmp/proj-[wip]/src',
    )

    assert prefix == (
        '[HermesWebUIContext::v1\n'
        'project_id: "proj_123"\n'
        'project_name: "Project \\"One\\""\n'
        'workspace: "/tmp/proj-[wip]/src"\n'
        ']\n'
        '[Workspace::v1: /tmp/proj-[wip\\]/src]\n'
    )
    assert _strip_workspace_prefix(prefix + 'Continue') == 'Continue'


def test_project_context_prefix_unassigned_uses_literal_nulls():
    prefix = _hermes_webui_context_prefix(workspace='/workspace')

    assert prefix == (
        '[HermesWebUIContext::v1\n'
        'project_id: null\n'
        'project_name: null\n'
        'workspace: "/workspace"\n'
        ']\n'
        '[Workspace::v1: /workspace]\n'
    )
    assert _strip_workspace_prefix(prefix + 'Hello') == 'Hello'


def test_legacy_workspace_prefix_only_strips_for_compatibility_callers():
    legacy = "[Workspace: /tmp/project]\nContinue"

    assert _strip_workspace_prefix(legacy) == legacy
    assert _strip_workspace_prefix(legacy, include_legacy=True) == "Continue"


def test_title_from_strips_project_context_prefix():
    prefix = _hermes_webui_context_prefix(
        project_id='proj_123',
        project_name='Project One',
        workspace='/workspace',
    )

    assert title_from([{'role': 'user', 'content': prefix + 'Summarize the plan'}]) == 'Summarize the plan'


def test_user_typed_legacy_workspace_prefix_survives_fallback_title():
    title = _fallback_title_from_exchange(
        "[Workspace: /tmp/project]\nExplain this literal prefix",
        "Sure",
    )

    assert title is not None
    assert title.startswith("Workspace tmp/project")
