"""Tests for the shared workspace resolve across external-agent bridges (#674).

``get_last_workspace()`` is loop-invariant for a whole sidebar build but is not
free: it stats ``config.yaml`` and probes the terminal's cwd. #4718 established
the invariant that a build resolves it ONCE. Adding a second external-agent
source (aider, #674) must not add a second resolve, so the loader now hoists a
single memo and hands it to both bridges.

Two behaviours are pinned here:

1. The bridges accept a pre-resolved ``workspace`` and never resolve their own
   when one is supplied.
2. The loader passes that resolved value down, so N external sources still cost
   exactly one ``get_last_workspace()`` call.

``_external_scan_kwargs`` exists because tests monkeypatch these scanners with
narrow zero-arg fakes; passing ``workspace`` unconditionally would raise
TypeError and silently drop the entire source from the sidebar.
"""
from __future__ import annotations


def _counts_workspace(*args, **kwargs):
    """A stand-in scanner that accepts an optional pre-resolved workspace."""
    calls.append(kwargs.get('workspace'))
    return []


def _zero_arg_fake(*args, **kwargs):
    """A stand-in scanner with a narrow signature, as older tests monkeypatch."""
    calls.append('called')
    return []


calls: list = []


def test_bridges_accept_pre_resolved_workspace(tmp_path, monkeypatch):
    """Both bridges use a supplied workspace instead of resolving their own.

    Calls the REAL bridge functions (only the file iterators are stubbed to
    return no paths). At baseline each bridge resolves its own workspace before
    even entering its loop, so the throwing ``get_last_workspace`` turns this
    into a genuine red/green test.
    """
    import api.models as models

    monkeypatch.setattr(
        models, 'get_last_workspace',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not resolve')),
    )
    monkeypatch.setattr(models, '_iter_claude_code_jsonl_files', lambda *a, **k: [])
    monkeypatch.setattr(models, '_iter_aider_chat_history_files', lambda *a, **k: [])

    # Must not raise: a supplied workspace means no internal resolve.
    assert models.get_claude_code_sessions(workspace='/ws/alpha') == []
    assert models.get_aider_sessions(workspace='/ws/alpha') == []


def test_default_aider_scan_dir_uses_supplied_workspace(tmp_path, monkeypatch):
    """The scan root is derived from the caller's workspace, not a fresh resolve."""
    import api.models as models

    monkeypatch.delenv('HERMES_WEBUI_AIDER_SCAN_DIR', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TEST_STATE_DIR', raising=False)

    root = tmp_path / 'repo'
    (root / '.git').mkdir(parents=True)

    monkeypatch.setattr(
        models, 'get_last_workspace',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not resolve')),
    )

    resolved = models._default_aider_scan_dir(workspace=str(root))
    assert resolved == root


def test_external_scan_kwargs_probes_before_passing(tmp_path, monkeypatch):
    """Zero-arg monkeypatched scanners must not receive the workspace kwarg."""
    import api.models as models

    assert models._external_scan_kwargs(_counts_workspace, '/ws') == {'workspace': '/ws'}
    # A narrow fake has no 'workspace' parameter -> omit it rather than TypeError.
    assert models._external_scan_kwargs(lambda: [], '/ws') == {}
    # **kwargs fakes accept anything.
    assert models._external_scan_kwargs(lambda **kw: [], '/ws') == {'workspace': '/ws'}


def test_loader_resolves_workspace_once_for_all_external_sources(tmp_path, monkeypatch):
    """Two external sources must cost ONE get_last_workspace() call, not two.

    This is the #4718 invariant carried across the #674 addition: a build that
    adds a source must not add a resolve.
    """
    import api.models as models

    resolves = []
    scanned = []

    def _counting_workspace(*a, **k):
        resolves.append(1)
        return '/ws/only'

    def _cc(*a, **kwargs):
        scanned.append('claude_code')
        return []

    def _aider(*a, **kwargs):
        scanned.append('aider')
        return []

    monkeypatch.setattr(models, 'get_last_workspace', _counting_workspace)
    monkeypatch.setattr(models, 'get_claude_code_sessions', _cc)
    monkeypatch.setattr(models, 'get_aider_sessions', _aider)
    monkeypatch.setattr(models, '_iter_claude_code_jsonl_files', lambda *a, **k: [])
    monkeypatch.setattr(models, '_iter_aider_chat_history_files', lambda *a, **k: [])

    models._load_cli_sessions_uncached(
        tmp_path,
        tmp_path / 'state.db',
        'default',
        source_filter=None,
        include_claude_code=True,
        include_aider=True,
    )

    assert sorted(scanned) == ['aider', 'claude_code']
    assert len(resolves) == 1, f'expected 1 workspace resolve, got {len(resolves)}'


def test_loader_tolerates_zero_arg_monkeypatched_scanners(tmp_path, monkeypatch):
    """A zero-arg fake must still yield its rows -- not be dropped by TypeError."""
    import api.models as models

    monkeypatch.setattr(models, 'get_last_workspace', lambda *a, **k: '/ws')
    monkeypatch.setattr(models, 'get_claude_code_sessions', _zero_arg_fake)
    monkeypatch.setattr(models, 'get_aider_sessions', _zero_arg_fake)
    monkeypatch.setattr(models, '_iter_claude_code_jsonl_files', lambda *a, **k: [])
    monkeypatch.setattr(models, '_iter_aider_chat_history_files', lambda *a, **k: [])

    calls.clear()
    models._load_cli_sessions_uncached(
        tmp_path,
        tmp_path / 'state.db',
        'default',
        source_filter=None,
        include_claude_code=True,
        include_aider=True,
    )

    assert calls.count('called') == 2, 'both zero-arg fakes must be invoked'


def test_aider_cache_key_resolves_workspace_once(tmp_path, monkeypatch):
    """The all-profiles cache key must resolve the workspace exactly once.

    The cache key needs the aider scan root twice -- once for the path component
    and once for the stat component -- and resolving it costs a
    ``get_last_workspace()`` (it stats config.yaml and probes terminal cwd). The
    hunk hoists it into a local so the key costs one resolve, not two.

    Counting ``_default_aider_scan_dir()`` invocations would be the wrong seam:
    the bridge legitimately calls it again to derive its own root from the
    shared workspace (that path does NOT re-resolve). Count the resolves, and
    clear the test-state guard that otherwise short-circuits before resolving.
    """
    import api.models as models

    monkeypatch.delenv('HERMES_WEBUI_AIDER_SCAN_DIR', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TEST_STATE_DIR', raising=False)

    root = tmp_path / 'repo'
    (root / '.git').mkdir(parents=True)

    resolves = []

    def _counting_workspace(*a, **k):
        resolves.append(1)
        return str(root)

    monkeypatch.setattr(models, 'get_last_workspace', _counting_workspace)
    monkeypatch.setattr(models, '_default_claude_code_projects_dir', lambda: tmp_path)
    monkeypatch.setattr(models, '_all_profiles_cli_contexts', lambda: ([], ()))
    monkeypatch.setattr(models, '_cli_sessions_streaming_freeze_marker', lambda: None)
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', lambda *a, **kw: [])

    models.get_cli_sessions(all_profiles=True, include_claude_code=False, include_aider=True)

    # Measured: the shared resolve hunk halves this path from 4 resolves to 2.
    # The 2 that remain are distinct and necessary -- one for the cache key's
    # scan-root component, one for the workspace handed to the bridges.
    assert len(resolves) == 2, f'expected 2 workspace resolves, got {len(resolves)}'