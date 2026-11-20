"""GET /api/session copies only the requested window on a display-merge cache hit.

The display-merge cache used to shallow-copy every cached row on each hit and
the handler then rescanned the whole transcript for todo_state, the visible
count, the last timestamp and the imported active-turn token. A hit now hands
back the cached list read-only plus a summary memoized at merge time; only the
returned window rows are copied. The JSON response must not change at all.
All data here is synthetic.
"""

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

import api.routes as routes

SID = "20260301_090000_w10copy"


class _FakeSession:
    def __init__(self, messages):
        self.session_id = SID
        self.profile = None
        self.title = "W10"
        self.workspace = "/tmp"
        self.model = "gpt-test"
        self.model_provider = None
        self.messages = messages
        self.tool_calls = []
        self.context_length = 1
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.truncation_watermark = None
        self.truncation_boundary = None

    def compact(self):
        return {"session_id": self.session_id, "title": self.title, "message_count": len(self.messages)}


def _synthetic(n):
    rows = []
    for i in range(n):
        ts = 1_700_000_000.0 + i
        kind = i % 5
        if kind == 0:
            rows.append({"role": "user", "content": f"q{i}", "timestamp": ts})
        elif kind == 1:
            rows.append({"role": "assistant", "content": "", "timestamp": ts,
                         "tool_calls": [{"id": f"c{i}", "function": {"name": "todo", "arguments": "{}"}}]})
        elif kind == 2:
            todos = [{"id": "t1", "content": f"step {i}", "status": "in_progress"}]
            content = json.dumps({"todos": todos}) if i % 15 == 2 else "x" * 300
            rows.append({"role": "tool", "tool_call_id": f"c{i - 1}", "content": content, "timestamp": ts})
        elif kind == 3:
            rows.append({"role": "assistant", "content": f"a{i}", "timestamp": ts})
        else:
            rows.append({"role": "user", "content": f"hidden{i}", "timestamp": ts, "_source": "background_process"})
    rows[n // 3]["_active_turn_token"] = "turn-tok"
    return rows


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    monkeypatch.setattr("api.models._sidecar_stat_signature", lambda p: ("sig", 1, 2, 3), raising=False)
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: "SIG-A")
    yield
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()


def _invoke(session, state_rows, query):
    captured = {}

    def fake_j(_h, data, status=200, extra_headers=None):
        captured["data"] = data
        return data

    with patch("api.routes.get_session", return_value=session), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.get_state_db_session_messages", side_effect=lambda *a, **k: [dict(r) for r in state_rows]), \
         patch("api.routes._load_state_db_messages_with_stable_signature",
               side_effect=lambda *a, **k: ([dict(r) for r in state_rows], "SIG-A")), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), urlparse(f"/api/session?session_id={SID}&{query}"))
    return captured["data"]


def _dump(data):
    return json.dumps(data, sort_keys=True, default=str)


def _session(n):
    rows = _synthetic(n)
    split = max(1, n - 7)  # state.db carries a few rows newer than the sidecar
    return _FakeSession([dict(r) for r in rows[:split]]), rows


QUERIES = ["messages=1&resolve_model=0&msg_limit=30",
           "messages=1&resolve_model=0&msg_limit=30&msg_before=20",
           "messages=1&resolve_model=0&msg_limit=all"]


@pytest.mark.parametrize("n", [50, 1000, 5000])
@pytest.mark.parametrize("query", QUERIES)
def test_response_byte_identical_miss_hit_and_disabled(monkeypatch, n, query):
    session, rows = _session(n)
    miss = _dump(_invoke(session, rows, query))
    hit = _dump(_invoke(session, rows, query))
    if "msg_limit=30" in query and "msg_before" not in query:
        assert "summary" in routes._display_merge_cache[SID]
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    monkeypatch.setattr(routes, "_DISPLAY_MERGE_CACHE_MAX", 0)
    disabled = _dump(_invoke(session, rows, query))
    assert miss == hit == disabled
    payload = json.loads(hit)["session"]
    if "msg_limit=30" in query and "msg_before" not in query:
        assert payload["todo_state"] and payload["visible_message_count"] > 0


def test_cache_hit_copies_only_the_window(monkeypatch):
    session, rows = _session(5000)
    q = "messages=1&resolve_model=0&msg_limit=30"
    _invoke(session, rows, q)  # populate
    counts = {"todo": 0, "hidden": 0}
    real_derive, real_hidden = routes.derive_todo_state, routes.is_hidden_transcript_row

    def derive(m):
        counts["todo"] += 1
        return real_derive(m)

    def hidden(m):
        counts["hidden"] += 1
        return real_hidden(m)

    monkeypatch.setattr(routes, "derive_todo_state", derive)
    monkeypatch.setattr(routes, "is_hidden_transcript_row", hidden)
    copied = []
    real_cached = routes._display_merge_cached_messages

    def spy(*a, **k):
        out = real_cached(*a, **k)
        if out is not None:
            entry = routes._display_merge_cache[SID]
            copied.append(sum(1 for x, y in zip(out, entry["messages"], strict=True) if x is not y))
        return out

    monkeypatch.setattr(routes, "_display_merge_cached_messages", spy)
    data = _invoke(session, rows, q)["session"]
    assert copied == [0], "cache hit must not copy every cached row"
    assert counts["todo"] == 0, "cache hit must reuse the memoized todo_state"
    assert counts["hidden"] < 500, f"cache hit rescanned the transcript ({counts['hidden']} row checks)"
    assert len(data["messages"]) <= 60


def test_mutating_returned_rows_does_not_touch_cache():
    session, rows = _session(1000)
    q = "messages=1&resolve_model=0&msg_limit=30"
    _invoke(session, rows, q)
    first = _dump(_invoke(session, rows, q))
    data = _invoke(session, rows, q)["session"]
    for m in data["messages"]:
        m["content"] = "MUTATED"
        m["injected"] = True
    if data.get("todo_state"):
        data["todo_state"]["todos"].clear()
    assert all("injected" not in m for m in routes._display_merge_cache[SID]["messages"])
    assert _dump(_invoke(session, rows, q)) == first


def test_summary_is_derived_outside_the_shared_cache_lock(monkeypatch):
    """A cache miss must not hold the process-wide lock while it walks every row.

    The stub parks the miss inside derive_todo_state (the full-transcript
    summary walk). While it is parked, no thread holds the lock, so a
    non-blocking acquire from here must succeed. Event-driven, no sleeps.
    """
    session, rows = _session(50)
    q = "messages=1&resolve_model=0&msg_limit=30"
    real_derive = routes.derive_todo_state
    entered, release = threading.Event(), threading.Event()
    calls = []

    def parked_derive(messages):
        calls.append(len(messages))
        if len(calls) == 1:
            entered.set()
            assert release.wait(10), "summary stub was never released"
        return real_derive(messages)

    monkeypatch.setattr(routes, "derive_todo_state", parked_derive)
    errors = []

    def cold_miss():
        try:
            _invoke(session, rows, q)
        except BaseException as exc:  # surfaced via `errors` below
            errors.append(repr(exc))

    worker = threading.Thread(target=cold_miss, name="display-merge-cold-miss")
    worker.start()
    acquired = False
    try:
        assert entered.wait(10), "the cache miss never reached the summary walk"
        acquired = routes._display_merge_cache_lock.acquire(blocking=False)
        if acquired:
            routes._display_merge_cache_lock.release()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive(), "the cache miss did not finish"
    assert not errors, errors
    assert acquired, "the display-merge cache lock was held while the summary was derived"
    entry = routes._display_merge_cache[SID]
    assert entry["summary"] == routes._display_merge_summary(entry["messages"])


def _with_hidden_wakeup_rows(n):
    """Like _session(n), but every fifth row is a truly hidden wakeup row."""
    session, rows = _session(n)
    for row in [*rows, *session.messages]:
        if row.get("_source") == "background_process":
            row["_source"] = "delegation_wakeup"
    return session, rows


@pytest.mark.parametrize("n", [50, 1000])
def test_visible_count_excludes_truly_hidden_rows_on_miss_and_hit(monkeypatch, n):
    session, rows = _with_hidden_wakeup_rows(n)
    hidden = [r for r in rows if r.get("_source") == "delegation_wakeup"]
    assert hidden and all(routes.is_hidden_transcript_row(r) for r in hidden)
    # The PR fixture's background_process rows are visible rows, not hidden ones.
    assert not routes.is_hidden_transcript_row({"role": "user", "_source": "background_process"})
    expected_visible = n - len(hidden)
    q = "messages=1&resolve_model=0&msg_limit=30"

    miss = _invoke(session, rows, q)["session"]
    entry = routes._display_merge_cache[SID]
    assert len(entry["messages"]) == n
    assert entry["summary"]["visible_count"] == expected_visible
    hit = _invoke(session, rows, q)["session"]

    assert miss["visible_message_count"] == expected_visible
    assert hit["visible_message_count"] == expected_visible
    assert _dump(miss) == _dump(hit)


@pytest.mark.skipif(not os.environ.get("W10_BENCH"), reason="set W10_BENCH=1 to run the microbenchmark")
@pytest.mark.parametrize("n", [5000, 50000])
def test_bench_hit_path(n):
    session, rows = _session(n)
    q = "messages=1&resolve_model=0&msg_limit=30"
    _invoke(session, rows, q)
    samples = []
    for _ in range(15):
        t = time.perf_counter()
        _invoke(session, rows, q)
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    print(f"\nW10 hit-path n={n}: p50={samples[len(samples) // 2]:.1f}ms min={samples[0]:.1f}ms")
