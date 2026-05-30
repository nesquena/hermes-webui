"""Test for session duplicate + edit message bug (#2914).

When a session is duplicated, truncation_watermark is not copied.
After editing a message (truncate + send) in the duplicated session,
the watermark is set based on the copied messages' timestamps.
On next load, merge with state.db may filter out messages incorrectly.
"""
import copy
import os
import tempfile
import threading
import time
import uuid

import pytest


@pytest.fixture
def isolated_session_env():
    """Isolate session state for testing duplicate + edit scenario."""
    from api import config as _cfg
    from api import models as _models
    from pathlib import Path
    import collections

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / 'sessions'
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot config BEFORE any mutation so we always restore the real state.
    # Note: api.models imports SESSION_DIR at module level, so we must update
    # BOTH api.config.SESSION_DIR and api.models.SESSION_DIR.
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
        yield tmpdir
    finally:
        # Always restore, even on exception
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


def test_duplicate_preserves_truncation_watermark(isolated_session_env):
    """When duplicating a session with truncation_watermark, the watermark should be copied."""
    from api.models import Session
    from api.config import SESSIONS, LOCK

    now = time.time()
    watermark_value = now - 50
    original = Session(
        session_id='original123',
        title='Original Session',
        messages=[
            {'role': 'user', 'content': 'Hello', 'timestamp': now - 100},
            {'role': 'assistant', 'content': 'Hi there', 'timestamp': now - 90},
        ],
        truncation_watermark=watermark_value,
    )
    original.save()
    with LOCK:
        SESSIONS['original123'] = original

    session = Session.load('original123')
    assert session is not None
    assert session.truncation_watermark is not None

    # Simulate duplicate endpoint (routes.py:5113) — must include all fields
    # that the real endpoint copies, including truncation_watermark.
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
        personality=session.personality,
        enabled_toolsets=getattr(session, "enabled_toolsets", None),
        context_length=getattr(session, "context_length", None),
        threshold_tokens=getattr(session, "threshold_tokens", None),
        truncation_watermark=getattr(session, "truncation_watermark", None),
        created_at=time.time(),
        updated_at=time.time(),
    )

    # truncation_watermark MUST be copied from original
    assert copied_session.truncation_watermark == watermark_value, \
        f"truncation_watermark should be copied (expected {watermark_value}, got {copied_session.truncation_watermark})"


def test_duplicate_edit_does_not_lose_messages(isolated_session_env):
    """When editing a message in a duplicated session, messages should not be lost after reload."""
    from api.models import Session, merge_session_messages_append_only, get_state_db_session_messages
    from api.config import SESSIONS, LOCK

    now = time.time()
    original = Session(
        session_id='original456',
        title='Original Session',
        messages=[
            {'role': 'user', 'content': 'First message', 'timestamp': now - 200},
            {'role': 'assistant', 'content': 'First response', 'timestamp': now - 190},
            {'role': 'user', 'content': 'Second message', 'timestamp': now - 100},
            {'role': 'assistant', 'content': 'Second response', 'timestamp': now - 90},
        ],
    )
    original.save()
    with LOCK:
        SESSIONS['original456'] = original

    # Duplicate the session
    session = Session.load('original456')
    assert session is not None

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
        personality=session.personality,
        enabled_toolsets=getattr(session, "enabled_toolsets", None),
        context_length=getattr(session, "context_length", None),
        threshold_tokens=getattr(session, "threshold_tokens", None),
        created_at=time.time(),
        updated_at=time.time(),
    )
    copied_session.save()
    with LOCK:
        SESSIONS[copied_session.session_id] = copied_session
        SESSIONS.move_to_end(copied_session.session_id)

    # Simulate edit: truncate to keep first 2 messages, then set watermark
    keep_count = 2
    copied_session.messages = copied_session.messages[:keep_count]
    from api.session_ops import _truncation_watermark_for
    copied_session.truncation_watermark = _truncation_watermark_for(copied_session.messages)
    copied_session.save()

    # Reload the duplicated session
    reloaded = Session.load(copied_session.session_id)
    assert reloaded is not None

    # Messages must be preserved after reload
    assert len(reloaded.messages) == keep_count, \
        f"Expected {keep_count} messages after truncate+reload, got {len(reloaded.messages)}"

    # Watermark must be preserved
    assert reloaded.truncation_watermark is not None, \
        "truncation_watermark should be preserved after save/load"

    # Merge with state.db (empty for new session) must not lose messages
    state_messages = get_state_db_session_messages(copied_session.session_id)
    merged = merge_session_messages_append_only(
        reloaded.messages,
        state_messages,
        truncation_watermark=reloaded.truncation_watermark,
    )

    assert len(merged) >= keep_count, \
        f"Expected at least {keep_count} messages after merge, got {len(merged)}"
