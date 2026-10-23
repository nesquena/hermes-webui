"""Owner concurrency and exclusive-publication admission ordering."""
from contextlib import contextmanager
import threading

import pytest

from tests.test_composer_draft_profile_races import env as env


def test_waiting_exclusive_publication_precedes_new_owner(env, monkeypatch):
    gate = env.m._DraftOwnerPublicationGate()
    waiting, late_waiting = threading.Event(), threading.Event()
    committed, release, late_done = threading.Event(), threading.Event(), threading.Event()
    errors, order = [], []
    real_wait = gate._condition.wait

    def wait(timeout=None):
        if threading.current_thread().name == 'exclusive':
            waiting.set()
        elif threading.current_thread().name == 'late-owner':
            late_waiting.set()
        return real_wait(timeout)

    monkeypatch.setattr(gate._condition, 'wait', wait)

    def commit():
        try:
            with gate.exclusive():
                order.append('exclusive')
                committed.set()
                assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)

    def owner():
        try:
            with gate.shared():
                order.append('owner')
                late_done.set()
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=commit, name='exclusive')
    reader = threading.Thread(target=owner, name='late-owner')
    try:
        with gate.shared():
            writer.start()
            assert waiting.wait(5)
            reader.start()
            assert late_waiting.wait(5)
            assert not committed.is_set() and not late_done.is_set()
        assert committed.wait(5)
        assert not late_done.is_set()
    finally:
        release.set()
        writer.join(6)
        if reader.ident is not None:
            reader.join(6)
    assert not writer.is_alive() and not reader.is_alive() and not errors
    assert order == ['exclusive', 'owner']


@pytest.mark.parametrize('mode', ['shared', 'exclusive'])
def test_publication_gate_releases_after_failed_attempt(env, mode):
    gate = env.m._DraftOwnerPublicationGate()
    with pytest.raises(PermissionError):
        with getattr(gate, mode)():
            raise PermissionError('synthetic sharing violation')
    completed = threading.Event()

    def publish():
        with gate.exclusive():
            completed.set()
        with gate.shared():
            completed.set()

    thread = threading.Thread(target=publish, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and completed.is_set()


def test_continually_arriving_validations_cannot_starve_waiting_checkpoint(env, monkeypatch):
    """Every validator queues its successor before releasing exclusive mode."""
    original = env.owner(composer_draft={'text': 'durable'})
    captured = env.m.get_session_for_draft(original.session_id)
    gate = env.m._DRAFT_OWNER_PUBLICATION_GATE
    real_wait = gate._condition.wait
    real_revision = env.m._draft_owner_revision
    real_shared = gate.shared
    owner_waiting, owner_admitted = threading.Event(), threading.Event()
    waiting = [threading.Event() for _ in range(32)]
    threads, errors, trace = [], [], []

    @contextmanager
    def shared():
        with real_shared():
            if threading.current_thread().name == 'waiting-checkpoint':
                owner_admitted.set()
            yield

    def wait(timeout=None):
        name = threading.current_thread().name
        if name == 'waiting-checkpoint':
            owner_waiting.set()
        elif name.startswith('validation-'):
            waiting[int(name.split('-')[1])].set()
        return real_wait(timeout)

    def validate(i):
        try:
            assert isinstance(env.m._draft_owner_is_current(captured), bool)
        except BaseException as exc:
            errors.append(exc)

    def revision(sid):
        name = threading.current_thread().name
        if name.startswith('validation-'):
            i = int(name.split('-')[1])
            trace.append(owner_admitted.is_set())
            if i + 1 < len(waiting):
                thread = threading.Thread(target=validate, args=(i + 1,), name=f'validation-{i + 1}')
                threads.append(thread)
                thread.start()
                assert waiting[i + 1].wait(5)
        return real_revision(sid)

    def checkpoint():
        try:
            original.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(gate, 'shared', shared)
    monkeypatch.setattr(gate._condition, 'wait', wait)
    monkeypatch.setattr(env.m, '_draft_owner_revision', revision)
    owner_thread = threading.Thread(target=checkpoint, name='waiting-checkpoint')
    with gate.exclusive():
        owner_thread.start()
        assert owner_waiting.wait(5)
        first = threading.Thread(target=validate, args=(0,), name='validation-0')
        threads.append(first)
        first.start()
        assert waiting[0].wait(5)
    owner_thread.join(10)
    for thread in threads:
        thread.join(5)
    assert not owner_thread.is_alive() and all(not thread.is_alive() for thread in threads)
    assert not errors, errors
    assert any(trace), 'the extendable validation backlog overtook the waiting checkpoint'


def test_fifo_admission_batches_shared_owners_without_exclusive_overtaking(env, monkeypatch):
    gate = env.m._DraftOwnerPublicationGate()
    modes = ['shared', 'shared', 'exclusive', 'shared', 'exclusive']
    waiting = [threading.Event() for _ in modes]
    admitted = [threading.Event() for _ in modes]
    release = [threading.Event() for _ in modes]
    threads, errors, order = [], [], []
    real_wait = gate._condition.wait

    def wait(timeout=None):
        waiting[int(threading.current_thread().name.split('-')[1])].set()
        return real_wait(timeout)

    def publish(i):
        try:
            with getattr(gate, modes[i])():
                order.append(i)
                admitted[i].set()
                assert release[i].wait(5)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(gate._condition, 'wait', wait)
    try:
        with gate.exclusive():
            for i in range(len(modes)):
                thread = threading.Thread(target=publish, args=(i,), name=f'publisher-{i}')
                threads.append(thread)
                thread.start()
                assert waiting[i].wait(5)
        # Both earlier owners must overlap, even with an exclusive waiter.
        assert admitted[0].wait(5) and admitted[1].wait(5)
        assert not any(event.is_set() for event in admitted[2:])
        release[0].set()
        release[1].set()
        for i in range(2, len(modes)):
            assert admitted[i].wait(5)
            assert not any(event.is_set() for event in admitted[i + 1:])
            release[i].set()
    finally:
        for event in release:
            event.set()
        for thread in threads:
            thread.join(6)
    assert all(not thread.is_alive() for thread in threads) and not errors, errors
    assert sorted(order[:2]) == [0, 1] and order[2:] == [2, 3, 4]


@pytest.mark.parametrize('mode', ['shared', 'exclusive'])
def test_interrupted_publication_waiter_removes_its_ticket(env, monkeypatch, mode):
    gate = env.m._DraftOwnerPublicationGate()
    original_wait = gate._condition.wait
    errors = []

    class Interrupted(BaseException):
        pass

    def wait(timeout=None):
        raise Interrupted()

    def contender():
        try:
            with getattr(gate, mode)():
                errors.append('unexpected admission')
        except Interrupted:
            pass

    with gate.exclusive():
        monkeypatch.setattr(gate._condition, 'wait', wait)
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and not errors
        assert not gate._queue and gate._waiting_exclusive == 0
        monkeypatch.setattr(gate._condition, 'wait', original_wait)
    with gate.shared():
        assert gate._owners == 1
    with gate.exclusive():
        assert gate._exclusive
    assert not gate._queue and gate._owners == 0 and not gate._exclusive
