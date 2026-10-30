import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from api import model_preferences as mp


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, '_state_dir', lambda: tmp_path)
    monkeypatch.setattr(mp, '_request_profile', lambda: 'default')
    mp._scan_cache.clear()
    return tmp_path


def test_pin_input_types_capacity_and_corrupt_file(store):
    for model, provider, pinned in [(123, 'p', True), ('m', [], True), ('m', 'p', 'yes')]:
        with pytest.raises(mp.PreferencesError):
            mp.apply_preferences(model, provider, pinned)
    for i in range(6):
        mp.apply_preferences(f'm{i}', 'p', True)
    with pytest.raises(mp.PreferencesError):
        mp.apply_preferences('seventh', 'p', True)
    path = mp._pins_file(store)
    path.write_text('{broken')
    with pytest.raises(mp.PreferencesError):
        mp.apply_preferences('m', 'p', True)
    assert path.read_text() == '{broken'


def test_pins_are_profile_scoped_and_concurrent(store, monkeypatch):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: mp.apply_preferences(f'm{i}', 'p', True), range(6)))
    assert len(mp.get_preferences()['pins']) == 6
    monkeypatch.setattr(mp, '_request_profile', lambda: 'work')
    assert mp.get_preferences()['pins'] == []
    mp.apply_preferences('work-m', 'p', True)
    monkeypatch.setattr(mp, '_request_profile', lambda: 'default')
    assert len(mp.get_preferences()['pins']) == 6


def seed(store):
    sessions = store / 'sessions'
    journals = sessions / '_turn_journal'
    journals.mkdir(parents=True)
    entries = [{'session_id': 'legacy'}, {'session_id': 'work', 'profile': 'work'}]
    (sessions / '_index.json').write_text(json.dumps(entries))
    for entry in entries:
        sid = entry['session_id']
        (sessions / f'{sid}.json').write_text('{}')
        events = [dict(event='submitted', session_id=sid, stream_id=f's-{i}',
                       model=f'{sid}-m{i}', model_provider='p', created_at=9000)
                  for i in range(8)]
        events += [dict(event='submitted', model=['invalid'], model_provider='p',
                        stream_id='bad', created_at=9000)]
        (journals / f'{sid}~1.jsonl').write_text('\n'.join(map(json.dumps, events)))
    return journals


def test_full_ranking_before_available_filter_and_cache_profile(store, monkeypatch):
    monkeypatch.setattr(mp.time, 'time', lambda: 10000)
    seed(store)
    data = mp.get_preferences()
    assert len(data['usage']) == 8
    monkeypatch.setattr(mp, '_request_profile', lambda: 'work')
    data = mp.get_preferences()
    assert len(data['usage']) == 8
    assert all(u['model'].startswith('work-') for u in data['usage'])


def test_dedup_window_compensation_and_exact_identity(store, monkeypatch):
    now = 3000000
    monkeypatch.setattr(mp.time, 'time', lambda: now)
    root = seed(store)
    events = [dict(event='submitted', stream_id='ok', model='@p:Model/x-y', model_provider='p', created_at=now-1),
              dict(event='submitted', stream_id='other-p', model='Model/x-y', model_provider='P', created_at=now-1),
              dict(event='submitted', stream_id='future', model='future', model_provider='p', created_at=now+1),
              dict(event='submitted', stream_id='old', model='old', model_provider='p', created_at=now-30*86400-1),
              dict(event='submitted', stream_id='comp', model='failed', model_provider='p', created_at=now-1),
              dict(event='submitted', stream_id='background', model='bg', model_provider='p', source='process_wakeup', created_at=now-1),
              dict(event='submitted', stream_id='legacy-wake', model='bg', model_provider='p', content='[ASYNC DELEGATION BATCH COMPLETE — d]\nresults', created_at=now-1),
              dict(event='submitted', stream_id='legacy-process', model='bg', model_provider='p', content='[IMPORTANT: Background process 123 completed normally (exit code 0).\nOutput: done]', created_at=now-1)]
    for name in ['legacy~1.jsonl', 'legacy~2.jsonl']:
        (root/name).write_text('\n'.join(map(json.dumps, events)))
    (root/'legacy~3.jsonl').write_text(json.dumps(dict(event='interrupted',stream_id='comp',reason='start_compensated')))
    rows=mp.get_preferences()['usage']
    assert {(r['model'],r['provider'],r['count']) for r in rows} == {('Model/x-y','p',1),('Model/x-y','P',1)}
