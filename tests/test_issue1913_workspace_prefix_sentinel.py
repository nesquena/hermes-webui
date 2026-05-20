from api.models import title_from
from api.streaming import (
    _fallback_title_from_exchange,
    _hermes_webui_context_prefix,
    _strip_workspace_prefix,
    _webui_message_context_prefix,
    _workspace_context_prefix,
)


def test_workspace_prefix_strips_only_versioned_sentinel():
    assert _strip_workspace_prefix("[Workspace::v1: /tmp/project]\nHello") == "Hello"
    assert _strip_workspace_prefix("[Workspace: /tmp/project]\nHello") == "[Workspace: /tmp/project]\nHello"


def test_hermes_webui_context_prefix_uses_json_strings_and_literal_nulls():
    prefix = _hermes_webui_context_prefix(
        project_id='proj_abc123',
        project_name='Initial "Hermes" setup',
        workspace='/tmp/project',
    )

    assert prefix == (
        '[HermesWebUIContext::v1\n'
        'project_id: "proj_abc123"\n'
        'project_name: "Initial \\"Hermes\\" setup"\n'
        'workspace: "/tmp/project"\n'
        ']\n'
        '[Workspace::v1: /tmp/project]\n'
    )

    unassigned = _hermes_webui_context_prefix(project_id=None, project_name=None, workspace='/tmp/project')
    assert 'project_id: null\n' in unassigned
    assert 'project_name: null\n' in unassigned
    assert 'workspace: "/tmp/project"\n' in unassigned


def test_webui_message_context_prefix_preserves_workspace_sentinel_for_unassigned_session():
    class DummySession:
        workspace = '/tmp/project'
        project_id = None

    prefix = _webui_message_context_prefix(DummySession())

    assert prefix == (
        '[HermesWebUIContext::v1\n'
        'project_id: null\n'
        'project_name: null\n'
        'workspace: "/tmp/project"\n'
        ']\n'
        '[Workspace::v1: /tmp/project]\n'
    )
    assert _strip_workspace_prefix(prefix + 'Hello') == 'Hello'


def test_webui_message_context_prefix_includes_assigned_project_metadata(monkeypatch):
    import api.streaming as streaming

    class DummySession:
        workspace = '/tmp/project'
        project_id = 'proj_abc123'

    monkeypatch.setattr(streaming, '_project_name_for_id', lambda project_id: 'Initial Hermes setup')

    prefix = _webui_message_context_prefix(DummySession())

    assert prefix == (
        '[HermesWebUIContext::v1\n'
        'project_id: "proj_abc123"\n'
        'project_name: "Initial Hermes setup"\n'
        'workspace: "/tmp/project"\n'
        ']\n'
        '[Workspace::v1: /tmp/project]\n'
    )
    assert _strip_workspace_prefix(prefix + 'Can you see my project metadata?') == 'Can you see my project metadata?'


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


def test_workspace_prefix_escapes_paths_with_closing_brackets():
    prefix = _workspace_context_prefix("/tmp/proj-[wip]/src")

    assert prefix == "[Workspace::v1: /tmp/proj-[wip\\]/src]\n"
    assert _strip_workspace_prefix(f"{prefix}Continue") == "Continue"


def test_title_from_strips_project_context_prefix():
    prefix = _hermes_webui_context_prefix(
        project_id='proj_123',
        project_name='Project One',
        workspace='/workspace',
    )

    assert title_from([{'role': 'user', 'content': prefix + 'Summarize the plan'}]) == 'Summarize the plan'


def test_legacy_workspace_prefix_only_strips_for_compatibility_callers():
    legacy = "[Workspace: /tmp/project]\nContinue"

    assert _strip_workspace_prefix(legacy) == legacy
    assert _strip_workspace_prefix(legacy, include_legacy=True) == "Continue"


def test_user_typed_legacy_workspace_prefix_survives_fallback_title():
    title = _fallback_title_from_exchange(
        "[Workspace: /tmp/project]\nExplain this literal prefix",
        "Sure",
    )

    assert title is not None
    assert title.startswith("Workspace tmp/project")
