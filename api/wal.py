"""
Streaming WAL (Write-Ahead Log) for hermes-webui chat history safety.

Append-only JSONL log that records every token, reasoning, and tool event
during an agent streaming run. On process crash or unclean shutdown, the WAL
is replayed on session load to reconstruct in-flight assistant output that
hasn't yet been committed to the session JSON.

File layout: {SESSION_DIR}/{session_id}_wal.jsonl
Format: JSONL, one event dict per line.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from api.config import SESSION_DIR

# WAL-flush策略: 每N个token或每MAX_FLUSH_INTERVAL秒刷一次盘, 取两者先到者.
_WAL_FLUSH_TOKENS = 1            # Flush immediately after each event for crash safety
_WAL_FLUSH_INTERVAL = 3.0        # 秒

# WAL文件最大尺寸 (bytes). 超过此大小停止写入, 防止磁盘耗尽.
_WAL_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# 进程内token计数器 {session_id: count}
_token_counts: dict[str, int] = {}
_token_counts_lock = threading.Lock()

# 上次刷盘时间 {session_id: timestamp}
_last_flush_time: dict[str, float] = {}
_flush_lock = threading.Lock()

# 缓冲区: {session_id: [line1, line2, ...]} — 每批次写入前累积在此
_write_buffer: dict[str, list[str]] = {}
_buffer_lock = threading.RLock()


# ─── 路径 ────────────────────────────────────────────────────────────────────

def wal_path(session_id: str) -> Path:
    """Return Path to the WAL file for a session."""
    return SESSION_DIR / f"{session_id}_wal.jsonl"


# ─── 写入 ────────────────────────────────────────────────────────────────────

def _validate_sid(session_id: str) -> bool:
    return bool(session_id and all(c in '0123456789abcdefghijklmnopqrstuvwxyz_' for c in session_id))


def _should_flush(session_id: str) -> bool:
    """Return True if the WAL periodic checkpoint thread should flush now.

    Flush triggers:
      1. Token count >= _WAL_FLUSH_TOKENS   (must be >= not > so threshold fires
         on the Nth token, not after the buffer has already been flushed by
         _append_event and the count is still >= threshold)
      2. Time since last flush > _WAL_FLUSH_INTERVAL (only after timer initialized)

    The timer is initialized on first call (last=0 -> set to now, return False).
    This prevents the 1970-epoch bug where uninitialized timers always fire.

    NOTE: Do NOT reset _last_flush_time here when the token threshold fires.
    Resetting it here causes the periodic checkpoint thread to always see
    "just flushed" and skip its time-based flush, breaking periodic checkpoints.
    """
    # 1. Token threshold
    with _token_counts_lock:
        count = _token_counts.get(session_id, 0)
    if count >= _WAL_FLUSH_TOKENS:
        return True
    # 2. Time threshold (only after initialization)
    # Note: we don't reset the timer here since that causes deadlock when
    # _should_flush is called by the checkpoint thread while a streaming
    # thread is in _append_event.  Time-based flush is best-effort and may
    # occasionally fire slightly late — the buffer-based flush in
    # _append_event handles the common case synchronously.
    with _flush_lock:
        last = _last_flush_time.get(session_id, 0)
    if last == 0:
        _last_flush_time[session_id] = time.time()
        return False
    if time.time() - last >= _WAL_FLUSH_INTERVAL:
        return True
    return False


def _write_lines(session_id: str, lines: list[str]) -> None:
    """Append lines to the WAL file and sync.

    Uses append ('a') mode so that each call appends to the file rather than
    overwriting it.  This is critical: when threshold=1, each token event calls
    _flush_buffer which calls _write_lines; using write mode would lose prior
    events.  With 'a' mode, multiple calls accumulate correctly.

    Note: concurrent writes from multiple threads for the same session are
    serialized by the caller's _buffer_lock, so this is safe.
    """
    if not lines:
        return
    path = wal_path(session_id)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size >= _WAL_MAX_BYTES:
        return  # 安全 guard: 超过最大尺寸停止写入
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines))
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def _flush_buffer(session_id: str) -> None:
    """Flush the write buffer for a session to disk."""
    with _buffer_lock:
        lines = _write_buffer.get(session_id)
        if not lines:
            return
        del _write_buffer[session_id]
    _write_lines(session_id, lines)
    with _flush_lock:
        _last_flush_time[session_id] = time.time()


def _append_event(session_id: str, event: dict) -> None:
    """Append a single event to the session's WAL buffer, flush if needed."""
    if not _validate_sid(session_id):
        return
    line = json.dumps(event, ensure_ascii=False)
    with _buffer_lock:
        _write_buffer.setdefault(session_id, []).append(line)
        do_flush = len(_write_buffer[session_id]) >= _WAL_FLUSH_TOKENS
    if do_flush:
        _flush_buffer(session_id)


def _increment(session_id: str) -> None:
    """Increment token count and flush if threshold reached."""
    with _token_counts_lock:
        _token_counts[session_id] = _token_counts.get(session_id, 0) + 1
        over = _token_counts[session_id] >= _WAL_FLUSH_TOKENS
    if over:
        _flush_buffer(session_id)


# ─── 公共 API ────────────────────────────────────────────────────────────────

def write_wal_start(session_id: str, stream_id: str) -> None:
    """Record stream start event."""
    _append_event(session_id, {
        'type': 'start',
        'stream_id': stream_id,
        'timestamp': int(time.time()),
    })


def write_wal_token(session_id: str, text: str, timestamp: Optional[int] = None) -> None:
    """Record a single token chunk of assistant output."""
    _append_event(session_id, {
        'type': 'token',
        'text': text,
        'timestamp': timestamp or int(time.time()),
    })
    _increment(session_id)


def write_wal_reasoning(session_id: str, text: str, timestamp: Optional[int] = None) -> None:
    """Record a single token chunk of reasoning/thinking output."""
    _append_event(session_id, {
        'type': 'reasoning',
        'text': text,
        'timestamp': timestamp or int(time.time()),
    })
    _increment(session_id)


def write_wal_tool(session_id: str, tool_id: str, name: str,
                   args: str, timestamp: Optional[int] = None) -> None:
    """Record a tool call invocation."""
    _append_event(session_id, {
        'type': 'tool',
        'id': tool_id,
        'name': name,
        'args': args,
        'timestamp': timestamp or int(time.time()),
    })


def write_wal_tool_result(session_id: str, tool_id: str, result: str,
                          timestamp: Optional[int] = None) -> None:
    """Record a tool call result."""
    _append_event(session_id, {
        'type': 'tool_result',
        'id': tool_id,
        'result': result,
        'timestamp': timestamp or int(time.time()),
    })


def write_wal_end(session_id: str, stream_id: str,
                  timestamp: Optional[int] = None) -> None:
    """Record stream end event. Triggers a final flush."""
    _append_event(session_id, {
        'type': 'end',
        'stream_id': stream_id,
        'timestamp': timestamp or int(time.time()),
    })
    _flush_buffer(session_id)
    # Clean up per-session state
    with _token_counts_lock:
        _token_counts.pop(session_id, None)
    with _flush_lock:
        _last_flush_time.pop(session_id, None)
    with _buffer_lock:
        _write_buffer.pop(session_id, None)


def write_wal_aperror(session_id: str, message: str,
                      timestamp: Optional[int] = None) -> None:
    """Record an apperror event (silent agent failure)."""
    _append_event(session_id, {
        'type': 'apperror',
        'message': message,
        'timestamp': timestamp or int(time.time()),
    })
    _flush_buffer(session_id)


# ─── 读取 / 回放 ─────────────────────────────────────────────────────────────

def read_wal(session_id: str) -> list[dict]:
    """
    Read all WAL events for a session from disk.
    Returns [] if the WAL file does not exist.
    Raises on corrupt lines (returns partial list on error).
    """
    path = wal_path(session_id)
    if not path.exists():
        return []
    events = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # 跳过损坏行, 返回已读取的部分
                    break
    except Exception:
        return []
    return events


def replay_wal(events: list[dict]) -> dict:
    """
    Reconstruct a pending assistant message dict from WAL events.

    Returns a dict with keys:
      - content (str): accumulated assistant text
      - reasoning (str): accumulated reasoning text
      - tool_calls (list[dict]): tool call events
      - tool_results (list[dict]): tool result events
      - had_error (bool): whether an apperror event was present
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    had_error = False

    for ev in events:
        ev_type = ev.get('type', '')
        if ev_type == 'token':
            content_parts.append(ev.get('text', ''))
        elif ev_type == 'reasoning':
            reasoning_parts.append(ev.get('text', ''))
        elif ev_type == 'tool':
            tool_calls.append({
                'id': ev.get('id', ''),
                'name': ev.get('name', ''),
                'args': ev.get('args', ''),
            })
        elif ev_type == 'tool_result':
            tool_results.append({
                'id': ev.get('id', ''),
                'result': ev.get('result', ''),
            })
        elif ev_type == 'apperror':
            had_error = True

    return {
        'content': ''.join(content_parts),
        'reasoning': ''.join(reasoning_parts),
        'tool_calls': tool_calls,
        'tool_results': tool_results,
        'had_error': had_error,
    }


def delete_wal(session_id: str) -> None:
    """
    Delete the WAL file for a session, if it exists.
    Idempotent — missing file is silently ignored.
    """
    path = wal_path(session_id)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    # Clean up in-memory state
    with _token_counts_lock:
        _token_counts.pop(session_id, None)
    with _flush_lock:
        _last_flush_time.pop(session_id, None)
    with _buffer_lock:
        _write_buffer.pop(session_id, None)
