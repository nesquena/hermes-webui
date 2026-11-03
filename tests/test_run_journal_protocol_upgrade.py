"""Old valid increasing gaps survive upgrade; version2 remains gapless."""
import json
from pathlib import Path

import pytest

from api import models, run_journal
from api.run_journal import RunJournalWriter
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 -- disposable state
    _persist_recovery_boundary_turn,
    _simulate_restart,
    _stream_output,
)


def _row(sid, stream, seq, text, version=1):
    row = {'version': version, 'session_id': sid, 'run_id': stream, 'event_id': f'{stream}:{seq}',
           'seq': seq, 'event': 'token', 'type': 'token', 'terminal': False,
           'terminal_state': None, 'created_at': float(seq), 'payload': {'text': text}}
    if version is None:
        row.pop('version')
    return row


@pytest.mark.parametrize('version', [None, 1])
@pytest.mark.parametrize('seqs', [[1, 3], [1, 3, 4], [4, 9]])
def test_master_written_gaps_recover_with_original_event_ids(version, seqs):
    sid = f'legacy-gaps-{version}-{seqs[-1]}'
    stream = sid + '-run'
    _persist_recovery_boundary_turn(sid, stream, 'crash')
    path = run_journal._run_path(sid, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_row(sid, stream, seq, f'answer{seq}', version) for seq in seqs]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = run_journal.read_run_events(sid, stream, validated_recovery=True)
    assert not result['malformed']
    assert [row['event_id'] for row in result['events']] == [row['event_id'] for row in rows]
    _simulate_restart()
    for _ in range(2):
        recovered = models.get_session(sid)
        assert [row['content'] for row in _stream_output(recovered, stream)] == [''.join(f'answer{seq}' for seq in seqs)]
        models.SESSIONS.clear()
    # A new writer may extend legacy rows without changing their cursor IDs.
    event = RunJournalWriter(sid, stream).append_sse_event('token', {'text': 'after upgrade'})
    assert event['seq'] == seqs[-1] + 1 and event['version'] == 2
    result = run_journal.read_run_events(sid, stream, validated_recovery=True)
    assert not result['malformed']
    assert [row['seq'] for row in result['events']] == seqs + [seqs[-1] + 1]


@pytest.mark.parametrize('shape', ['new-gap', 'new-start-gap', 'downgrade', 'duplicate', 'decrease',
                                   'bool-version', 'unknown-version', 'foreign-id', 'foreign-session', 'forged-terminal'])
def test_versioned_recovery_rejects_unproven_protocol_or_identity(shape):
    sid, stream = 'protocol-reject-' + shape, 'protocol-run-' + shape
    rows = [_row(sid, stream, 1, 'prefix', 2), _row(sid, stream, 2, 'tail', 2)]
    if shape == 'new-gap':
        rows[1].update(seq=3, event_id=f'{stream}:3')
    elif shape == 'new-start-gap':
        rows = [_row(sid, stream, 3, 'gap', 2)]
    elif shape == 'downgrade':
        rows[1]['version'] = 1
    elif shape in ('duplicate', 'decrease'):
        rows[0]['version'] = rows[1]['version'] = 1
        rows[1].update(seq=1 if shape == 'duplicate' else 0, event_id=f'{stream}:{1 if shape == "duplicate" else 0}')
    elif shape == 'bool-version':
        rows[1]['version'] = True
    elif shape == 'unknown-version':
        rows[1]['version'] = 99
    elif shape == 'foreign-id':
        rows[1]['event_id'] = 'foreign:2'
    elif shape == 'foreign-session':
        rows[1]['session_id'] = 'foreign'
    else:
        rows[1]['terminal'] = True
    path = run_journal._run_path(sid, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = run_journal.read_run_events(sid, stream, validated_recovery=True)
    assert result['events'] == [] and result['malformed']


def test_actual_frozen_master_failed_append_fixture_recovers_after_upgrade():
    # Raw bytes from master3ffbf0b6 RunJournalWriter: token1, failed open burns2,
    # token3, done4. No rows/IDs were renumbered when producing this fixture.
    sid, stream = 'legacy-master-session', 'legacy-master-run'
    _persist_recovery_boundary_turn(sid, stream, 'crash')
    path = run_journal._run_path(sid, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / 'fixtures' / 'legacy_gapped_run_journal_v1.jsonl'
    original = fixture.read_bytes()
    path.write_bytes(original)
    rows = run_journal.read_run_events(sid, stream, validated_recovery=True)
    assert not rows['malformed']
    assert [row['event_id'] for row in rows['events']] == [stream + ':1', stream + ':3', stream + ':4']
    _simulate_restart()
    for _ in range(2):
        session = models.get_session(sid)
        assert [row['content'] for row in _stream_output(session, stream)] == ['Legacy durable prefix and continuation']
        assert models._run_journal_terminal_state(session, stream) == 'completed'
        assert path.read_bytes() == original
        models.SESSIONS.clear()
