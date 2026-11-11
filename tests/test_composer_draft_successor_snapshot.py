"""A multi-owner authorization must have a single consistent publication view."""
from tests.test_composer_draft_profile_races import env as env
import json
import pytest


def test_successor_edge_cannot_authorize_disjoint_owner_snapshots(env, monkeypatch):
    e = env
    parent = e.owner('probe-parent', pre_compression_snapshot=True,
                     pre_compression_continuation_session_id='probe-child')
    child = e.owner('probe-child', profile='bravo', parent_session_id='probe-parent')
    # No instant in the request has both endpoints owned by default.
    e.m.SESSIONS[parent.session_id] = parent
    real_meta = e.r._persisted_session_meta_prefix
    states = [('default', 'bravo')]

    def record():
        pair = tuple(json.loads(s.path.read_text())['profile'] for s in (parent, child))
        states.append(pair)
        assert pair != ('default', 'default')

    def torn_read(sid, *args, **kw):
        if sid == parent.session_id:
            # Move child out first, then parent in: preserve no-valid-edge invariant.
            child.profile = 'bravo'
            child.save(touch_updated_at=False, skip_index=True)
            record()
            parent.profile = 'default'
            parent.save(touch_updated_at=False, skip_index=True)
            record()
            result = real_meta(sid, *args, **kw)
            # Move parent out first, then child in, before the child metadata read.
            parent.profile = 'bravo'
            parent.save(touch_updated_at=False, skip_index=True)
            record()
            child.profile = 'default'
            child.save(touch_updated_at=False, skip_index=True)
            record()
            return result
        return real_meta(sid, *args, **kw)

    monkeypatch.setattr(e.r, '_persisted_session_meta_prefix', torn_read)
    status, payload = e.call(parent.session_id, text='must not redirect')
    actual = {'status': status, 'payload': payload, 'owner_states': states}
    print('TORN_SUCCESSOR_RESULT', json.dumps(actual))
    assert status == 409 and payload.get('code') == 'draft_authority_unconfirmed', actual
    assert 'session_id' not in payload


@pytest.mark.parametrize('change', ['replace', 'delete'])
def test_whole_lineage_requires_one_publication_snapshot(env, monkeypatch, change):
    e = env
    root = e.owner('root', pre_compression_snapshot=True,
                   pre_compression_continuation_session_id='middle')
    middle = e.owner('middle', parent_session_id='root', pre_compression_snapshot=True,
                     pre_compression_continuation_session_id='leaf')
    leaf = e.owner('leaf', profile='bravo', parent_session_id='middle')
    real_meta = e.r._persisted_session_meta_prefix
    middle_reads = []
    states = []

    def record():
        profiles = tuple(json.loads(s.path.read_text())['profile'] if s.path.exists() else None
                         for s in (root, middle, leaf))
        states.append(profiles)
        assert profiles != ('default', 'default', 'default')

    def scan(sid):
        if sid == middle.session_id:
            middle_reads.append(True)
            if len(middle_reads) == 2:
                # First edge was valid, but invalidate it before the second
                # becomes valid. Per-edge validation alone accepts this chain.
                if change == 'delete':
                    e.m._remove_session_with_draft(root.path)
                else:
                    root.profile = 'bravo'
                    root.save(touch_updated_at=False, skip_index=True)
                record()
                leaf.profile = 'default'
                leaf.save(touch_updated_at=False, skip_index=True)
                record()
        return real_meta(sid)

    record()
    monkeypatch.setattr(e.r, '_persisted_session_meta_prefix', scan)
    assert not e.r._draft_compression_edges_still_authorized(
        [('root', 'middle'), ('middle', 'leaf')], 'default',
    ), states
    assert len(middle_reads) == 2
