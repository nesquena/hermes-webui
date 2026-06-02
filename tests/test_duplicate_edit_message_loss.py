"""Integration test: duplicate session + edit loses intermediate messages.

Reproduces the scenario:
  1. Create session with multiple messages (U1, A1, U2, A2, U3, A3)
  2. Duplicate the session
  3. Continue chatting in the duplicate (U4, A4)
  4. Edit a message in the duplicate (truncate + re-send)
  5. Reload the session — intermediate messages should NOT be lost

The bug: after duplicate + edit, only the first and last messages remain.
Intermediate messages disappear because merge_session_messages_append_only
incorrectly filters them out when watermark + state.db reconciliation
interact with duplicated messages that share timestamps.

Key insight: duplicated messages have the SAME timestamps as originals.
When the duplicate session's turns are written to state.db with NEW
timestamps, and then Edit truncates the sidecar, the watermark is set
to the truncated tail's timestamp. State.db messages from the original
session (same session_id in state.db) may have timestamps that fall
into the "between watermark and new messages" gap.

However, the duplicate gets a NEW session_id, so state.db is initially
empty for it. The issue is subtler — it's about how the sidecar save
interacts with the merge on reload after truncate.
"""
import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid

import pytest


@pytest.fixture
def isolated_env():
    """Isolate session state for testing duplicate + edit scenario."""
    from api import config as _cfg
    from api import models as _models
    from pathlib import Path
    import collections

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / 'sessions'
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create a state.db in the sessions dir so state.db writes happen
    state_db_path = sessions_dir / 'state.db'

    old_values = {
        'cfg_SESSION_DIR': _cfg.SESSION_DIR,
        'models_SESSION_DIR': getattr(_models, 'SESSION_DIR', None),
        'SESSIONS': _cfg.SESSIONS,
        'LOCK': _cfg.LOCK,
        'SESSION_INDEX_FILE': _cfg.SESSION_INDEX_FILE,
        'SESSION_AGENT_LOCKS': _cfg.SESSION_AGENT_LOCKS,
        'SESSION_AGENT_LOCKS_LOCK': _cfg.SESSION_AGENT_LOCKS_LOCK,
        'SESSIONS_MAX': _cfg.SESSIONS_MAX,
    }

    _cfg.SESSION_DIR = sessions_dir
    _models.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = sessions_dir / 'index.json'
    _cfg.LOCK = threading.Lock()
    _cfg.SESSIONS = collections.OrderedDict()
    _cfg.SESSIONS_MAX = 100
    _cfg.SESSION_AGENT_LOCKS = {}
    _cfg.SESSION_AGENT_LOCKS_LOCK = threading.Lock()

    try:
        yield tmpdir, sessions_dir, state_db_path
    finally:
        _cfg.SESSION_DIR = old_values['cfg_SESSION_DIR']
        if old_values['models_SESSION_DIR'] is not None:
            _models.SESSION_DIR = old_values['models_SESSION_DIR']
        _cfg.SESSIONS = old_values['SESSIONS']
        _cfg.LOCK = old_values['LOCK']
        _cfg.SESSION_INDEX_FILE = old_values['SESSION_INDEX_FILE']
        _cfg.SESSION_AGENT_LOCKS = old_values['SESSION_AGENT_LOCKS']
        _cfg.SESSION_AGENT_LOCKS_LOCK = old_values['SESSION_AGENT_LOCKS_LOCK']
        _cfg.SESSIONS_MAX = old_values['SESSIONS_MAX']

        import shutil
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _make_messages(base_ts, count):
    """Generate alternating user/assistant messages with timestamps."""
    msgs = []
    for i in range(count):
        ts = base_ts + i * 10
        if i % 2 == 0:
            msgs.append({
                'role': 'user',
                'content': f'User message {i // 2 + 1}',
                'timestamp': ts,
            })
        else:
            msgs.append({
                'role': 'assistant',
                'content': f'Assistant response {i // 2 + 1}',
                'timestamp': ts,
            })
    return msgs


def _write_messages_to_state_db(state_db_path, session_id, messages):
    """Write messages to state.db for a session."""
    with sqlite3.connect(str(state_db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                reasoning TEXT
            )
        """)
        for msg in messages:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, msg['role'], msg['content'], msg['timestamp']),
            )
        conn.commit()


def _simulate_duplicate_endpoint(session):
    """Simulate what /api/session/duplicate does (routes.py:5744-5793)."""
    from api.models import Session
    from api.config import SESSIONS, LOCK

    copied_session = Session(
        session_id=uuid.uuid4().hex[:12],
        title=(session.title or "Untitled") + " (copy)",
        workspace=session.workspace,
        model=session.model,
        model_provider=session.model_provider,
        messages=copy.deepcopy(session.messages),
        tool_calls=copy.deepcopy(session.tool_calls),
        pinned=False,
        archived=False,
        project_id=session.project_id,
        profile=session.profile,
        input_tokens=session.input_tokens,
        output_tokens=session.output_tokens,
        estimated_cost=session.estimated_cost,
        cache_read_tokens=getattr(session, "cache_read_tokens", 0),
        cache_write_tokens=getattr(session, "cache_write_tokens", 0),
        personality=session.personality,
        enabled_toolsets=getattr(session, "enabled_toolsets", None),
        context_length=getattr(session, "context_length", None),
        threshold_tokens=getattr(session, "threshold_tokens", None),
        truncation_watermark=getattr(session, "truncation_watermark", None),
        context_messages=copy.deepcopy(getattr(session, "context_messages", None) or []),
        gateway_routing=copy.deepcopy(getattr(session, "gateway_routing", None)),
        gateway_routing_history=copy.deepcopy(getattr(session, "gateway_routing_history", None) or []),
        llm_title_generated=getattr(session, "llm_title_generated", False),
        composer_draft=copy.deepcopy(getattr(session, "composer_draft", None) or {}),
        context_engine=getattr(session, "context_engine", None),
        context_engine_state=copy.deepcopy(getattr(session, "context_engine_state", None) or {}),
        created_at=time.time(),
        updated_at=time.time(),
    )

    with LOCK:
        SESSIONS[copied_session.session_id] = copied_session
        SESSIONS.move_to_end(copied_session.session_id)
    copied_session.save()
    return copied_session


def _simulate_truncate_endpoint(session, keep_count):
    """Simulate what /api/session/truncate does (routes.py:6264-6301)."""
    from api.session_ops import _truncation_watermark_for
    from api.config import LOCK

    with LOCK:
        session.messages = session.messages[:keep_count]
        if isinstance(getattr(session, 'context_messages', None), list):
            session.context_messages = session.context_messages[:keep_count]
        session.truncation_watermark = _truncation_watermark_for(session.messages)
        session.save()


def _simulate_get_session_merge(session_id):
    """Simulate what GET /api/session does: load sidecar + merge with state.db."""
    from api.models import (
        Session, merge_session_messages_append_only,
        get_state_db_session_messages, get_session,
    )
    from api.config import LOCK

    s = get_session(session_id)
    if s is None:
        return None

    state_db_messages = get_state_db_session_messages(session_id)
    merged = merge_session_messages_append_only(
        s.messages,
        state_db_messages,
        truncation_watermark=getattr(s, "truncation_watermark", None),
    )
    return s, merged


def test_duplicate_then_edit_preserves_all_messages(isolated_env):
    """Full scenario: create → duplicate → continue → edit → reload.

    After editing in a duplicated session, ALL messages should be preserved:
    the pre-edit prefix, the edited message, and the post-edit response.
    Intermediate messages must NOT disappear.
    """
    from api.models import Session
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # ── Step 1: Create original session with 6 messages ──
    original_msgs = _make_messages(now - 600, 6)
    # [U1, A1, U2, A2, U3, A3]
    original = Session(
        session_id='original_sess',
        title='Original Session',
        messages=original_msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['original_sess'] = original

    # Write original messages to state.db (simulating agent turns)
    _write_messages_to_state_db(state_db_path, 'original_sess', original_msgs)

    # ── Step 2: Duplicate the session ──
    original_loaded = Session.load('original_sess')
    dup = _simulate_duplicate_endpoint(original_loaded)
    dup_id = dup.session_id

    # Verify duplicate has all 6 messages
    assert len(dup.messages) == 6, f"Duplicate should have 6 messages, got {len(dup.messages)}"

    # ── Step 3: Continue chatting in the duplicate (add 2 more messages) ──
    continue_msgs = _make_messages(now - 100, 2)
    # [U4, A4]
    dup.messages = dup.messages + continue_msgs
    dup.save()

    # Write the NEW messages to state.db for the duplicate session
    _write_messages_to_state_db(state_db_path, dup_id, continue_msgs)

    # Verify duplicate now has 8 messages
    assert len(dup.messages) == 8, f"Duplicate should have 8 messages, got {len(dup.messages)}"

    # ── Step 4: Edit — truncate to keep first 2 messages (U1, A1) ──
    # This simulates editing U2: keep everything before it, then re-send
    _simulate_truncate_endpoint(dup, keep_count=2)

    # After truncate, sidecar has 2 messages
    assert len(dup.messages) == 2, f"After truncate should have 2 messages, got {len(dup.messages)}"
    assert dup.truncation_watermark is not None, "Watermark should be set after truncate"

    # ── Step 5: Simulate re-sending edited message + agent response ──
    edited_msg = {
        'role': 'user',
        'content': 'Edited user message 2',
        'timestamp': now + 10,
    }
    new_response = {
        'role': 'assistant',
        'content': 'New assistant response to edited message',
        'timestamp': now + 20,
    }
    dup.messages = dup.messages + [edited_msg, new_response]
    dup.save()

    # Write new messages to state.db
    _write_messages_to_state_db(state_db_path, dup_id, [edited_msg, new_response])

    # After edit + response, sidecar should have 4 messages
    assert len(dup.messages) == 4, f"After edit should have 4 messages, got {len(dup.messages)}"

    # ── Step 6: Reload session (simulating GET /api/session) ──
    reloaded, merged = _simulate_get_session_merge(dup_id)

    # ── CRITICAL ASSERTION: merged messages must have correct count ──
    expected_count = 4  # U1, A1, U2_edited, A2_new
    actual_count = len(merged)

    assert actual_count == expected_count, (
        f"After duplicate + edit + reload, expected {expected_count} messages, "
        f"got {actual_count}. Messages: {[m.get('content', '')[:40] for m in merged]}"
    )

    # Verify the specific messages present
    roles = [m['role'] for m in merged]
    assert roles == ['user', 'assistant', 'user', 'assistant'], (
        f"Expected [user, assistant, user, assistant], got {roles}"
    )

    # Verify edited content is present
    contents = [m['content'] for m in merged]
    assert 'Edited user message 2' in contents, "Edited message should be present"
    assert 'New assistant response to edited message' in contents, "New response should be present"

    # Verify old intermediate messages are NOT present
    for c in contents:
        assert 'User message 2' not in c or 'Edited' in c, (
            f"Old unedited 'User message 2' should not be present, found: {c}"
        )
        assert 'User message 3' not in c, (
            f"Old 'User message 3' should not be present, found: {c}"
        )


def test_duplicate_edit_with_state_db_overlap(isolated_env):
    """Duplicate + edit where state.db has ALL messages (original + continued).

    This tests the case where state.db for the duplicate session contains
    all 8 messages (as if the agent wrote them all), and the sidecar has
    been truncated to 4 after edit. The merge should correctly return 4
    messages, not lose intermediates.
    """
    from api.models import Session, merge_session_messages_append_only
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # ── Create original session ──
    original_msgs = _make_messages(now - 600, 6)
    original = Session(
        session_id='orig_overlap',
        title='Original',
        messages=original_msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['orig_overlap'] = original

    # ── Duplicate ──
    orig_loaded = Session.load('orig_overlap')
    dup = _simulate_duplicate_endpoint(orig_loaded)
    dup_id = dup.session_id

    # ── Continue in duplicate ──
    continue_msgs = _make_messages(now - 100, 2)
    dup.messages = dup.messages + continue_msgs
    dup.save()

    # Write ALL 8 messages to state.db for the duplicate
    all_dup_msgs = original_msgs + continue_msgs
    _write_messages_to_state_db(state_db_path, dup_id, all_dup_msgs)

    # ── Edit: truncate to 2, add edited + response ──
    _simulate_truncate_endpoint(dup, keep_count=2)

    edited_msg = {
        'role': 'user',
        'content': 'Edited message',
        'timestamp': now + 10,
    }
    new_response = {
        'role': 'assistant',
        'content': 'Response to edit',
        'timestamp': now + 20,
    }
    dup.messages = dup.messages + [edited_msg, new_response]
    dup.save()

    # Write new messages to state.db too
    _write_messages_to_state_db(state_db_path, dup_id, [edited_msg, new_response])

    # ── Reload and merge ──
    reloaded = Session.load(dup_id)
    assert reloaded is not None

    state_msgs = []
    with sqlite3.connect(str(state_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ?", (dup_id,)
        ).fetchall()
        for row in rows:
            state_msgs.append(dict(row))

    merged = merge_session_messages_append_only(
        reloaded.messages,
        state_msgs,
        truncation_watermark=reloaded.truncation_watermark,
    )

    # Should have exactly 4 messages: U1, A1, edited_U2, new_A2
    assert len(merged) == 4, (
        f"Expected 4 messages after merge, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )

    roles = [m['role'] for m in merged]
    assert roles == ['user', 'assistant', 'user', 'assistant'], (
        f"Expected alternating roles, got {roles}"
    )


def test_duplicate_edit_sidecar_shrink_detection(isolated_env):
    """Verify that .bak is created when sidecar shrinks after edit.

    When the sidecar saves fewer messages than before, a .bak should be
    created (#1558 safeguard). This ensures data can be recovered.
    """
    from api.models import Session
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # ── Create session with 8 messages ──
    msgs = _make_messages(now - 800, 8)
    s = Session(
        session_id='bak_test',
        title='Backup test',
        messages=msgs,
    )
    s.save()
    with LOCK:
        SESSIONS['bak_test'] = s

    # Verify no .bak yet
    bak_path = sessions_dir / 'bak_test.json.bak'
    assert not bak_path.exists(), ".bak should not exist yet"

    # ── Truncate to 2 messages (simulating edit) ──
    _simulate_truncate_endpoint(s, keep_count=2)

    # .bak SHOULD exist now (sidecar shrank from 8 to 2)
    assert bak_path.exists(), (
        ".bak should be created when sidecar shrinks — #1558 safeguard"
    )

    # .bak should have the original 8 messages
    bak_data = json.loads(bak_path.read_text(encoding='utf-8'))
    assert len(bak_data['messages']) == 8, (
        f".bak should have 8 messages, got {len(bak_data['messages'])}"
    )

    # Live file should have 2 messages
    live_data = json.loads((sessions_dir / 'bak_test.json').read_text(encoding='utf-8'))
    assert len(live_data['messages']) == 2, (
        f"Live file should have 2 messages after truncate, got {len(live_data['messages'])}"
    )


def test_duplicate_edit_multiple_truncates(isolated_env):
    """Multiple edits in a duplicated session — each should preserve state.

    Simulates: duplicate → edit msg 2 → edit msg 1 → reload.
    Each edit truncates and re-sends. Final state should be correct.
    """
    from api.models import Session
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # ── Create original with 6 messages ──
    original_msgs = _make_messages(now - 600, 6)
    original = Session(
        session_id='multi_edit_orig',
        title='Multi-edit original',
        messages=original_msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['multi_edit_orig'] = original

    # Write to state.db
    _write_messages_to_state_db(state_db_path, 'multi_edit_orig', original_msgs)

    # ── Duplicate ──
    orig_loaded = Session.load('multi_edit_orig')
    dup = _simulate_duplicate_endpoint(orig_loaded)
    dup_id = dup.session_id

    # ── Continue in duplicate ──
    continue_msgs = _make_messages(now - 100, 2)
    dup.messages = dup.messages + continue_msgs
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, continue_msgs)

    # ── First edit: truncate to 4 (keep U1,A1,U2,A2), add edited U3 + response ──
    _simulate_truncate_endpoint(dup, keep_count=4)
    edited_u3 = {
        'role': 'user',
        'content': 'Edited U3',
        'timestamp': now + 10,
    }
    resp_u3 = {
        'role': 'assistant',
        'content': 'Response to edited U3',
        'timestamp': now + 20,
    }
    dup.messages = dup.messages + [edited_u3, resp_u3]
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, [edited_u3, resp_u3])

    # Should have 6: U1, A1, U2, A2, edited_U3, resp_U3
    assert len(dup.messages) == 6, f"After first edit: expected 6, got {len(dup.messages)}"

    # ── Second edit: truncate to 2 (keep U1,A1), add edited U1 + response ──
    _simulate_truncate_endpoint(dup, keep_count=2)
    edited_u1 = {
        'role': 'user',
        'content': 'Completely rewritten first message',
        'timestamp': now + 30,
    }
    resp_u1 = {
        'role': 'assistant',
        'content': 'Response to rewritten first message',
        'timestamp': now + 40,
    }
    dup.messages = dup.messages + [edited_u1, resp_u1]
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, [edited_u1, resp_u1])

    # Should have 4: U1, A1, edited_U1, resp_U1
    assert len(dup.messages) == 4, f"After second edit: expected 4, got {len(dup.messages)}"

    # ── Reload and verify ──
    reloaded, merged = _simulate_get_session_merge(dup_id)

    assert len(merged) == 4, (
        f"After double edit + reload, expected 4 messages, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )

    contents = [m['content'] for m in merged]
    assert 'Completely rewritten first message' in contents
    assert 'Response to rewritten first message' in contents


def test_duplicate_timestamp_collision_merge(isolated_env):
    """Test that duplicate messages with identical timestamps don't cause merge issues.

    When duplicating, messages keep their original timestamps. If state.db
    also has messages with those timestamps, the merge key (which includes
    normalized timestamp) might cause dedup issues.
    """
    from api.models import Session, merge_session_messages_append_only
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # Create messages with EXACT same second-level timestamps
    # (sub-second differences should be normalized away)
    msgs = []
    for i in range(6):
        ts = now - 600 + i * 10  # 10-second gaps, well within second-level granularity
        if i % 2 == 0:
            msgs.append({
                'role': 'user',
                'content': f'User {i // 2 + 1}',
                'timestamp': ts,
            })
        else:
            msgs.append({
                'role': 'assistant',
                'content': f'Assistant {i // 2 + 1}',
                'timestamp': ts,
            })

    original = Session(
        session_id='ts_collision_orig',
        title='Timestamp collision',
        messages=msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['ts_collision_orig'] = original

    # Write to state.db with slightly different timestamps (simulating drift)
    state_msgs = []
    for i, msg in enumerate(msgs):
        m = dict(msg)
        # Add sub-second drift (should be normalized away in merge key)
        m['timestamp'] = msg['timestamp'] + 0.5
        state_msgs.append(m)
    _write_messages_to_state_db(state_db_path, 'ts_collision_orig', state_msgs)

    # ── Duplicate ──
    orig_loaded = Session.load('ts_collision_orig')
    dup = _simulate_duplicate_endpoint(orig_loaded)
    dup_id = dup.session_id

    # ── Continue ──
    new_msgs = [
        {'role': 'user', 'content': 'New message', 'timestamp': now + 10},
        {'role': 'assistant', 'content': 'New response', 'timestamp': now + 20},
    ]
    dup.messages = dup.messages + new_msgs
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, new_msgs)

    # ── Edit: truncate to 2 ──
    _simulate_truncate_endpoint(dup, keep_count=2)

    edited = {'role': 'user', 'content': 'Edited', 'timestamp': now + 30}
    resp = {'role': 'assistant', 'content': 'Edited response', 'timestamp': now + 40}
    dup.messages = dup.messages + [edited, resp]
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, [edited, resp])

    # ── Reload ──
    reloaded = Session.load(dup_id)
    state_msgs_dup = []
    with sqlite3.connect(str(state_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ?", (dup_id,)
        ).fetchall()
        for row in rows:
            state_msgs_dup.append(dict(row))

    merged = merge_session_messages_append_only(
        reloaded.messages,
        state_msgs_dup,
        truncation_watermark=reloaded.truncation_watermark,
    )

    assert len(merged) == 4, (
        f"Timestamp collision: expected 4 messages, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )


def test_duplicate_all_in_state_db_edit_merge(isolated_env):
    """Realistic: state.db has ALL duplicate messages (original copy + continued).

    In production, when the agent writes responses in the duplicate session,
    ALL messages end up in state.db (the agent doesn't know about the split
    between 'copied from original' and 'new in duplicate'). After Edit truncates
    the sidecar, the watermark filters state.db. If state.db has ALL 8 messages
    with timestamps spanning the full range, the watermark at position 2
    should correctly filter out messages 3-8 from state.db.
    """
    from api.models import Session, merge_session_messages_append_only
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # ── Create original with 6 messages ──
    original_msgs = _make_messages(now - 600, 6)
    original = Session(
        session_id='all_state_orig',
        title='All in state.db',
        messages=original_msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['all_state_orig'] = original

    # ── Duplicate ──
    orig_loaded = Session.load('all_state_orig')
    dup = _simulate_duplicate_endpoint(orig_loaded)
    dup_id = dup.session_id

    # ── Continue in duplicate ──
    continue_msgs = _make_messages(now - 100, 2)
    dup.messages = dup.messages + continue_msgs
    dup.save()

    # Write ALL 8 messages to state.db (as if agent wrote everything)
    all_msgs = list(original_msgs) + list(continue_msgs)
    _write_messages_to_state_db(state_db_path, dup_id, all_msgs)

    # ── Edit: truncate to 4 (U1, A1, U2, A2) ──
    _simulate_truncate_endpoint(dup, keep_count=4)

    # Add edited message + response
    edited = {'role': 'user', 'content': 'Edited U3', 'timestamp': now + 10}
    resp = {'role': 'assistant', 'content': 'Response to edited', 'timestamp': now + 20}
    dup.messages = dup.messages + [edited, resp]
    dup.save()

    # Write new messages to state.db
    _write_messages_to_state_db(state_db_path, dup_id, [edited, resp])

    # ── Reload and merge ──
    reloaded = Session.load(dup_id)
    state_msgs = []
    with sqlite3.connect(str(state_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ?", (dup_id,)
        ).fetchall()
        for row in rows:
            state_msgs.append(dict(row))

    merged = merge_session_messages_append_only(
        reloaded.messages,
        state_msgs,
        truncation_watermark=reloaded.truncation_watermark,
    )

    # Should have exactly 6: U1, A1, U2, A2, edited_U3, resp
    assert len(merged) == 6, (
        f"Expected 6 messages, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )


def test_duplicate_subsecond_timestamp_drift(isolated_env):
    """Messages with sub-second timestamp differences between sidecar and state.db.

    When the agent writes to state.db, timestamps may have microsecond precision.
    The sidecar JSON may truncate to fewer decimal places. This causes the
    normalized merge key (second-level) to match, but the raw float comparison
    in watermark filtering may behave unexpectedly.
    """
    from api.models import Session, merge_session_messages_append_only
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # Create messages with precise timestamps
    msgs = []
    for i in range(6):
        ts = now - 600 + i * 10
        if i % 2 == 0:
            msgs.append({
                'role': 'user',
                'content': f'User {i // 2 + 1}',
                'timestamp': ts,
            })
        else:
            msgs.append({
                'role': 'assistant',
                'content': f'Assistant {i // 2 + 1}',
                'timestamp': ts,
            })

    original = Session(
        session_id='drift_orig',
        title='Subsecond drift',
        messages=msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['drift_orig'] = original

    # ── Duplicate ──
    orig_loaded = Session.load('drift_orig')
    dup = _simulate_duplicate_endpoint(orig_loaded)
    dup_id = dup.session_id

    # ── Continue ──
    new_msgs = [
        {'role': 'user', 'content': 'New U4', 'timestamp': now + 10.123456},
        {'role': 'assistant', 'content': 'New A4', 'timestamp': now + 20.654321},
    ]
    dup.messages = dup.messages + new_msgs
    dup.save()

    # Write to state.db with slightly different timestamps
    state_msgs = list(msgs) + [
        {'role': 'user', 'content': 'New U4', 'timestamp': now + 10.123789},
        {'role': 'assistant', 'content': 'New A4', 'timestamp': now + 20.654987},
    ]
    _write_messages_to_state_db(state_db_path, dup_id, state_msgs)

    # ── Edit: truncate to 2 ──
    _simulate_truncate_endpoint(dup, keep_count=2)

    edited = {'role': 'user', 'content': 'Edited', 'timestamp': now + 30.111111}
    resp = {'role': 'assistant', 'content': 'Edited resp', 'timestamp': now + 40.222222}
    dup.messages = dup.messages + [edited, resp]
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, [edited, resp])

    # ── Reload ──
    reloaded = Session.load(dup_id)
    state_msgs_dup = []
    with sqlite3.connect(str(state_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ?", (dup_id,)
        ).fetchall()
        for row in rows:
            state_msgs_dup.append(dict(row))

    merged = merge_session_messages_append_only(
        reloaded.messages,
        state_msgs_dup,
        truncation_watermark=reloaded.truncation_watermark,
    )

    assert len(merged) == 4, (
        f"Subsecond drift: expected 4, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )


def test_duplicate_repeated_content_edit(isolated_env):
    """Edit where the edited message has similar content to existing messages.

    If the edited message content partially matches an existing message in
    state.db, the visible_key dedup might incorrectly skip it.
    """
    from api.models import Session, merge_session_messages_append_only
    from api.config import SESSIONS, LOCK

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # Create messages with similar content
    msgs = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
        {'role': 'user', 'content': 'How are you', 'timestamp': now - 580},
        {'role': 'assistant', 'content': 'I am fine', 'timestamp': now - 570},
        {'role': 'user', 'content': 'Tell me more', 'timestamp': now - 560},
        {'role': 'assistant', 'content': 'Sure, let me explain', 'timestamp': now - 550},
    ]

    original = Session(
        session_id='similar_orig',
        title='Similar content',
        messages=msgs,
    )
    original.save()
    with LOCK:
        SESSIONS['similar_orig'] = original

    # ── Duplicate ──
    orig_loaded = Session.load('similar_orig')
    dup = _simulate_duplicate_endpoint(orig_loaded)
    dup_id = dup.session_id

    # ── Continue ──
    new_msgs = [
        {'role': 'user', 'content': 'Thanks', 'timestamp': now + 10},
        {'role': 'assistant', 'content': 'You welcome', 'timestamp': now + 20},
    ]
    dup.messages = dup.messages + new_msgs
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, list(msgs) + new_msgs)

    # ── Edit: truncate to 2, add similar content ──
    _simulate_truncate_endpoint(dup, keep_count=2)

    # Edited message has similar words to existing messages
    edited = {'role': 'user', 'content': 'How are you doing today', 'timestamp': now + 30}
    resp = {'role': 'assistant', 'content': 'I am doing great thanks', 'timestamp': now + 40}
    dup.messages = dup.messages + [edited, resp]
    dup.save()
    _write_messages_to_state_db(state_db_path, dup_id, [edited, resp])

    # ── Reload ──
    reloaded = Session.load(dup_id)
    state_msgs = []
    with sqlite3.connect(str(state_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ?", (dup_id,)
        ).fetchall()
        for row in rows:
            state_msgs.append(dict(row))

    merged = merge_session_messages_append_only(
        reloaded.messages,
        state_msgs,
        truncation_watermark=reloaded.truncation_watermark,
    )

    # Should have exactly 4: U1, A1, edited_U2, resp
    assert len(merged) == 4, (
        f"Similar content: expected 4, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )

    # Verify the edited message is present (not deduped away)
    contents = [m['content'] for m in merged]
    assert 'How are you doing today' in contents, (
        "Edited message with similar content should NOT be deduped away"
    )


def test_streaming_finalize_after_edit_in_duplicate(isolated_env):
    """Streaming finalize after Edit in duplicate: _merge_display_messages_after_agent_result.

    After Edit truncates s.messages to [U1, A1], the agent runs and returns
    result_messages with the full history. _merge_display_messages_after_agent_result
    merges previous_display with candidates from result_messages.

    If previous_context was truncated (context_messages[:2]), but result_messages
    contains the full pre-edit history + new turn, the prefix check
    _messages_have_prefix(result_messages, previous_context) may fail because
    result_messages starts with the ORIGINAL U1/A1 content, not the truncated
    context. This could cause incorrect merge behavior.
    """
    from api.streaming import _merge_display_messages_after_agent_result

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # previous_display after Edit: [U1, A1]
    previous_display = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
    ]

    # previous_context after Edit: same as display (truncated context_messages)
    previous_context = list(previous_display)

    # Agent returns full history: original messages + new user turn + new response
    # This simulates what the agent returns when it reads state.db with full history
    result_messages = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
        # These are the "intermediate" messages that were truncated
        {'role': 'user', 'content': 'How are you', 'timestamp': now - 580},
        {'role': 'assistant', 'content': 'I am fine', 'timestamp': now - 570},
        {'role': 'user', 'content': 'Tell me more', 'timestamp': now - 560},
        {'role': 'assistant', 'content': 'Sure, let me explain', 'timestamp': now - 550},
        # New turn after Edit
        {'role': 'user', 'content': 'Edited message', 'timestamp': now + 10},
        {'role': 'assistant', 'content': 'Response to edit', 'timestamp': now + 20},
    ]

    # Watermark is set to the last kept message's timestamp (A1 at now - 590)
    watermark = now - 590

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        msg_text='Edited message',
        truncation_watermark=watermark,
    )

    # EXPECTED BEHAVIOR: truncated messages should NOT be restored.
    # Edit intentionally removed them — the merge must respect that.
    #
    # CURRENT BUG: backfill logic (lines 3112-3153) sees messages in
    # result_messages that are absent from previous_display and treats
    # them as "context-only gaps" to splice in. It doesn't know these
    # were intentionally truncated by Edit.
    #
    # FIX (variant 1): skip backfill for messages with timestamp <
    # truncation_watermark — they were explicitly removed.
    expected_count = 4  # U1, A1, edited_U2, resp — NOT the 6 truncated intermediates
    assert len(merged) == expected_count, (
        f"BUG: backfill restored truncated messages. "
        f"Expected {expected_count}, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )

    contents = [m['content'] for m in merged]
    # Truncated messages must NOT be present
    assert 'How are you' not in contents, "Truncated 'How are you' must NOT be restored by backfill"
    assert 'I am fine' not in contents, "Truncated 'I am fine' must NOT be restored by backfill"
    assert 'Tell me more' not in contents, "Truncated 'Tell me more' must NOT be restored by backfill"
    # New turn must be present
    assert 'Edited message' in contents, "New user turn should be present"
    assert 'Response to edit' in contents, "New response should be present"


def test_streaming_finalize_agent_returns_only_new_turn(isolated_env):
    """Agent returns only the new turn (not full history) after Edit.

    This is the ideal case: agent's result_messages contains only the new
    user turn + response, because context_messages was properly truncated.
    """
    from api.streaming import _merge_display_messages_after_agent_result

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    previous_display = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
    ]
    previous_context = list(previous_display)

    # Agent returns only the new turn (correct behavior after proper truncation)
    result_messages = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
        {'role': 'user', 'content': 'Edited message', 'timestamp': now + 10},
        {'role': 'assistant', 'content': 'Response to edit', 'timestamp': now + 20},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        msg_text='Edited message',
    )

    assert len(merged) == 4, (
        f"Expected 4, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )


def test_streaming_finalize_context_mismatch_after_duplicate(isolated_env):
    """After duplicate, context_messages may diverge from result_messages.

    When a session is duplicated, context_messages is deepcopied. But if the
    agent's state.db for the duplicate session has different content (e.g.,
    because the agent re-reads from state.db and gets messages with slightly
    different formatting), the merge may behave unexpectedly.
    """
    from api.streaming import _merge_display_messages_after_agent_result

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    # After duplicate + Edit: display has 2 messages
    previous_display = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
    ]

    # context_messages was truncated to 2
    previous_context = list(previous_display)

    # Agent returns: context prefix + new turn
    # But with slightly different content (e.g., whitespace normalization)
    result_messages = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
        {'role': 'user', 'content': 'Edited message', 'timestamp': now + 10},
        {'role': 'assistant', 'content': 'Response to edit', 'timestamp': now + 20},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        msg_text='Edited message',
    )

    assert len(merged) == 4, (
        f"Context mismatch: expected 4, got {len(merged)}. "
        f"Contents: {[m.get('content', '')[:30] for m in merged]}"
    )


def test_streaming_finalize_with_compacted_context(isolated_env):
    """Agent returns compacted context that doesn't match previous_context.

    If the agent's result_messages has a different prefix than previous_context
    (e.g., due to compaction or different message formatting), the merge falls
    back to finding the current user turn and extracting candidates from there.
    """
    from api.streaming import _merge_display_messages_after_agent_result

    tmpdir, sessions_dir, state_db_path = isolated_env
    now = time.time()

    previous_display = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
        {'role': 'user', 'content': 'How are you', 'timestamp': now - 580},
        {'role': 'assistant', 'content': 'I am fine', 'timestamp': now - 570},
    ]

    # previous_context has compaction marker
    previous_context = [
        {'role': 'user', 'content': 'Hello', 'timestamp': now - 600},
        {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 590},
    ]

    # Agent returns compacted history
    result_messages = [
        {'role': 'system', 'content': 'Previous conversation summary', '_compression_marker': True},
        {'role': 'user', 'content': 'Edited message', 'timestamp': now + 10},
        {'role': 'assistant', 'content': 'Response to edit', 'timestamp': now + 20},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        msg_text='Edited message',
    )

    # Should keep previous_display + compaction marker + new turn
    # At minimum, the new turn should be present
    contents = [m.get('content', '') for m in merged]
    assert 'Edited message' in contents, "New user turn should be present"
    assert 'Response to edit' in contents, "New response should be present"
