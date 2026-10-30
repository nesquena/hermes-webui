"""Ownership rules for the frequent-model preference consumer.

The picker feature counts "usage" and stores pins per profile owner. Two
ownership cases decide whether that data survives real user behaviour:

1. Native writable forks. ``/api/session/branch`` deliberately creates an
   ordinary writable WebUI session that carries ``parent_session_id`` plus
   ``session_source="fork"`` (api/routes.py); the compression-recovery focused
   continuation does the same. A parent link is lineage provenance, NOT
   automation provenance, so a fresh user send inside such a fork must count as
   usage — while sessions that DO carry a parent link but are read-only, CLI or
   automation-owned (imported subagent children, CLI imports, foreign channels)
   stay excluded.

2. Renamed root profiles. api.profiles treats a renamed root and the legacy
   ``default`` alias as the same owner (``_is_root_profile`` /
   ``_profiles_match``), so the pin store and BOTH usage filters must resolve
   that alias instead of comparing raw profile strings — otherwise renaming the
   root profile orphans every pin and every usage row the user already had.
   The response still reports the REQUESTED display name (the frontend uses it
   as a mid-flight freshness guard), and a genuinely separate named profile
   stays isolated from the root alias in both directions.

The fork cases replay the production shape end to end: real ``Session.save()``
-> compact/index publication -> ``append_turn_journal_event()`` ->
``get_preferences()``.
"""

import time

import pytest
from api import model_preferences as mp
from api import models as api_models
from api.turn_journal import append_turn_journal_event

PROVIDER = 'prov'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, '_state_dir', lambda: tmp_path)
    monkeypatch.setattr(mp, '_request_profile', lambda: 'default')
    mp._scan_cache.clear()
    return tmp_path


def _usage_map():
    return {(r['model'], r['provider']): r['count'] for r in mp.get_preferences()['usage']}


def _pin_models():
    return {p['model'] for p in mp.get_preferences()['pins']}


def _publish_sessions(session_dir, specs):
    """Persist real sessions so save() publishes their compact index rows."""
    session_dir.mkdir(parents=True, exist_ok=True)
    saved_dir, saved_index = api_models.SESSION_DIR, api_models.SESSION_INDEX_FILE
    api_models.SESSION_DIR = session_dir
    api_models.SESSION_INDEX_FILE = session_dir / '_index.json'
    api_models.SESSIONS.clear()
    try:
        for spec in specs:
            sid = spec['session_id']
            api_models.Session(
                session_id=sid, title=sid, messages=[],
                **{k: v for k, v in spec.items() if k != 'session_id'},
            ).save()
    finally:
        api_models.SESSION_DIR, api_models.SESSION_INDEX_FILE = saved_dir, saved_index
        api_models.SESSIONS.clear()


def _journal_send(session_dir, sid, model, *, profile=None, when=None):
    event = {'event': 'submitted', 'stream_id': f'{sid}-s1', 'model': model,
             'model_provider': PROVIDER, 'created_at': when if when is not None else time.time() - 120}
    if profile is not None:
        event['profile'] = profile
    return append_turn_journal_event(sid, event, session_dir=session_dir)


def _seed_root_names(monkeypatch, *names):
    """Seed canonical renamed-root membership in this disposable process."""
    import api.profiles as profiles
    monkeypatch.setattr(profiles, '_root_profile_name_cache', {'default', *names})
    monkeypatch.setattr(profiles, '_root_profile_name_cache_loaded', True)


# ── Finding 1: a parent link is not automation provenance ──────────────────

def test_fresh_send_in_native_writable_fork_counts_as_usage(store):
    session_dir = store / 'sessions'
    _publish_sessions(session_dir, [
        # Ordinary session: the control that must always count.
        {'session_id': 'ownordinary'},
        # The production fork shape: a normal writable WebUI session whose
        # only difference is the parent link + session_source="fork".
        {'session_id': 'ownfork0001', 'parent_session_id': 'ownordinary',
         'session_source': 'fork'},
    ])
    _journal_send(session_dir, 'ownordinary', 'ordinary-model')
    _journal_send(session_dir, 'ownfork0001', 'fork-model')
    assert _usage_map() == {('ordinary-model', PROVIDER): 1, ('fork-model', PROVIDER): 1}


def test_parent_linked_readonly_cli_and_automation_sessions_stay_excluded(store):
    session_dir = store / 'sessions'
    _publish_sessions(session_dir, [
        # Native writable fork: counts.
        {'session_id': 'ownfork0002', 'parent_session_id': 'ownfork0001',
         'session_source': 'fork'},
        # Recovered subagent child: parent link, subagent provenance, view-only.
        {'session_id': 'ownsub00001', 'parent_session_id': 'ownfork0001',
         'session_source': 'subagent', 'source_tag': 'subagent', 'read_only': True},
        # CLI import that carries a parent link.
        {'session_id': 'owncli00001', 'parent_session_id': 'ownfork0001',
         'session_source': 'cli', 'source_tag': 'cli', 'raw_source': 'cli',
         'is_cli_session': True, 'read_only': True},
        # Foreign channel session shaped like a fork (parent + writable).
        {'session_id': 'ownfeishu01', 'parent_session_id': 'ownfork0001',
         'session_source': 'other', 'source_tag': 'feishu', 'raw_source': 'feishu'},
    ])
    for sid, model in (('ownfork0002', 'fork-model'), ('ownsub00001', 'subagent-model'),
                       ('owncli00001', 'cli-model'), ('ownfeishu01', 'feishu-model')):
        _journal_send(session_dir, sid, model)
    assert _usage_map() == {('fork-model', PROVIDER): 1}


# ── Finding 2: renamed root is the same owner as 'default' ─────────────────

def test_renamed_root_keeps_default_owned_pins_and_usage(store, monkeypatch):
    mp.apply_preferences('legacy-pinned', PROVIDER, True)
    session_dir = store / 'sessions'
    _publish_sessions(session_dir, [{'session_id': 'ownlegacy01', 'profile': 'default'}])
    _journal_send(session_dir, 'ownlegacy01', 'legacy-usage')

    _seed_root_names(monkeypatch, 'kinni')
    monkeypatch.setattr(mp, '_request_profile', lambda: 'kinni')
    mp._scan_cache.clear()
    data = mp.get_preferences()
    # The requested display profile is what the frontend freshness guard reads.
    assert data['profile'] == 'kinni'
    assert data['pins'] == [{'model': 'legacy-pinned', 'provider': PROVIDER}]
    assert _usage_map() == {('legacy-usage', PROVIDER): 1}
    # New writes resolve to the SAME legacy store, not a fresh renamed one.
    assert mp._pins_file(store) == store / 'model_preferences.json'
    mp.apply_preferences('renamed-pinned', PROVIDER, True)
    assert _pin_models() == {'legacy-pinned', 'renamed-pinned'}


def test_default_profile_keeps_data_written_under_the_renamed_root(store, monkeypatch):
    _seed_root_names(monkeypatch, 'kinni')
    monkeypatch.setattr(mp, '_request_profile', lambda: 'kinni')
    mp.apply_preferences('written-as-kinni', PROVIDER, True)
    session_dir = store / 'sessions'
    _publish_sessions(session_dir, [{'session_id': 'ownkin00001', 'profile': 'kinni'}])
    _journal_send(session_dir, 'ownkin00001', 'kinni-usage', profile='kinni')

    monkeypatch.setattr(mp, '_request_profile', lambda: 'default')
    mp._scan_cache.clear()
    data = mp.get_preferences()
    assert data['profile'] == 'default'
    assert 'written-as-kinni' in _pin_models()
    assert _usage_map() == {('kinni-usage', PROVIDER): 1}


def test_separate_named_profile_stays_isolated_from_the_root_alias(store, monkeypatch):
    mp.apply_preferences('root-pinned', PROVIDER, True)
    session_dir = store / 'sessions'
    _publish_sessions(session_dir, [
        {'session_id': 'ownroot0001', 'profile': 'default'},
        {'session_id': 'ownwork0001', 'profile': 'work'},
    ])
    _journal_send(session_dir, 'ownroot0001', 'root-usage', profile='default')
    _journal_send(session_dir, 'ownwork0001', 'work-usage', profile='work')

    monkeypatch.setattr(mp, '_request_profile', lambda: 'work')
    mp._scan_cache.clear()
    # 'work' sees its own row and none of the root-owned data.
    assert _pin_models() == set()
    assert _usage_map() == {('work-usage', PROVIDER): 1}
    mp.apply_preferences('work-pinned', PROVIDER, True)
    assert _pin_models() == {'work-pinned'}
    assert _usage_map() == {('work-usage', PROVIDER): 1}

    monkeypatch.setattr(mp, '_request_profile', lambda: 'default')
    mp._scan_cache.clear()
    assert _pin_models() == {'root-pinned'}
    assert _usage_map() == {('root-usage', PROVIDER): 1}

    # Renaming the root must not fold the separate named profile into it.
    _seed_root_names(monkeypatch, 'kinni')
    monkeypatch.setattr(mp, '_request_profile', lambda: 'kinni')
    mp._scan_cache.clear()
    assert _pin_models() == {'root-pinned'}
    assert _usage_map() == {('root-usage', PROVIDER): 1}
    assert mp._pins_file(store) == store / 'model_preferences.json'
