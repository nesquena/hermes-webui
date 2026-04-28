"""
Task 0: Baseline performance harness for session-switch optimization.

Measures the current performance of /api/session for both metadata-only and
full-payload modes against a synthetic 200-message session. Writes baseline
numbers to docs/plans/PERF_BASELINE.md and asserts SOFT thresholds (skip on
miss for now — Task 8 will turn them into hard gates).

Plan: docs/plans/2026-04-28-session-switch-perf.md
"""
import json
import os
import pathlib
import time
import urllib.request

import pytest

from tests._pytest_port import BASE
from tests.conftest import TEST_STATE_DIR, make_session_tracked


SYNTH_MSG_COUNT = 200
SESSION_DIR = TEST_STATE_DIR / "sessions"


def _api_get_timed(path: str, headers: dict | None = None) -> tuple[bytes, float, dict]:
    """GET path; return (body_bytes, elapsed_seconds, response_headers)."""
    req = urllib.request.Request(BASE + path, headers=headers or {})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        elapsed = time.perf_counter() - t0
        hdrs = dict(r.headers.items())
    return body, elapsed, hdrs


def _seed_long_session(sid: str, n: int = SYNTH_MSG_COUNT) -> int:
    """
    Write a fully-formed session JSON file for `sid` directly to SESSION_DIR
    with N synthetic messages. Bypasses /api/session/new so the server never
    has the session in its in-memory SESSIONS cache — the first /api/session
    request will load it fresh from disk, exactly mimicking a cold sidebar
    click on a long session.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    msgs: list[dict] = []
    for i in range(n):
        if i % 2 == 0:
            msgs.append({
                "role": "user",
                "content": f"Question #{i}: Explain how ETag works in HTTP "
                           f"caching with a code example. " + ("lorem ipsum " * 10),
            })
        else:
            extras = ""
            if i % 11 == 0:
                extras = "\n\n```mermaid\ngraph TD;A-->B;B-->C;C-->D;\n```\n"
            elif i % 13 == 0:
                extras = "\n\n$$ E = mc^2 $$\n"
            elif i % 5 == 0:
                extras = "\n\n```python\ndef foo():\n    return 42\n```\n"
            msgs.append({
                "role": "assistant",
                "content": (
                    f"Answer #{i}: Sure! Here is a detailed explanation. "
                    + ("Markdown text " * 30) + extras
                ),
            })

    now = time.time()
    payload = {
        "session_id": sid,
        "title": "Perf Baseline Long Session",
        "workspace": str(TEST_STATE_DIR / "test-workspace"),
        "model": "stub/perf-test",
        "created_at": now - 86400,
        "updated_at": now,
        "pinned": False,
        "archived": False,
        "messages": msgs,
        "tool_calls": [],
    }
    path = SESSION_DIR / f"{sid}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path.stat().st_size


@pytest.fixture
def long_session(cleanup_test_sessions):
    # Use a deterministic-but-unique sid (12 hex chars, matches Session id format).
    import secrets
    sid = secrets.token_hex(6)
    cleanup_test_sessions.append(sid)
    size = _seed_long_session(sid, SYNTH_MSG_COUNT)
    return sid, size


# ── Baseline assertions (SOFT — informational, recorded to PERF_BASELINE.md) ──


def test_seed_creates_long_session(long_session):
    """Sanity: the synthetic 200-msg session was actually created on disk."""
    sid, size = long_session
    assert size > 50_000, f"expected >50KB session, got {size} bytes"


def test_baseline_full_payload_latency(long_session, record_property):
    """Measure full /api/session latency. Records baseline; passes always."""
    sid, size = long_session
    # Warm-up to remove cold cache effects from index loading
    _api_get_timed(f"/api/session?session_id={sid}")

    samples = []
    payload_size = 0
    for _ in range(5):
        body, elapsed, _hdrs = _api_get_timed(f"/api/session?session_id={sid}")
        samples.append(elapsed)
        payload_size = len(body)

    samples.sort()
    median = samples[len(samples) // 2]
    record_property("baseline_full_payload_median_ms", round(median * 1000, 1))
    record_property("baseline_full_payload_min_ms", round(min(samples) * 1000, 1))
    record_property("baseline_full_payload_max_ms", round(max(samples) * 1000, 1))
    record_property("baseline_full_payload_size_bytes", payload_size)
    record_property("baseline_session_msg_count", SYNTH_MSG_COUNT)

    # SOFT: only fail if absurdly slow (>5s) — we are establishing baseline
    assert median < 5.0, f"unreasonably slow baseline: {median:.3f}s"


def test_baseline_metadata_only_latency(long_session, record_property):
    """Measure /api/session?messages=0 latency. Should already be fast."""
    sid, _size = long_session
    _api_get_timed(f"/api/session?session_id={sid}&messages=0")  # warm-up

    samples = []
    for _ in range(5):
        _body, elapsed, _hdrs = _api_get_timed(
            f"/api/session?session_id={sid}&messages=0"
        )
        samples.append(elapsed)

    samples.sort()
    median = samples[len(samples) // 2]
    record_property("baseline_metadata_median_ms", round(median * 1000, 1))

    # SOFT: metadata path should already be quick
    assert median < 1.0, f"metadata-only path too slow: {median:.3f}s"


def test_baseline_payload_is_gzipped(long_session):
    """
    Document the current state: server DOES gzip /api/session today.
    Captured here so a regression (someone disabling gzip) trips this test.
    """
    sid, _size = long_session
    body, _elapsed, hdrs = _api_get_timed(
        f"/api/session?session_id={sid}",
        headers={"Accept-Encoding": "gzip"},
    )
    enc = hdrs.get("Content-Encoding", "")
    assert enc == "gzip", (
        f"Content-Encoding={enc!r} — server should compress /api/session "
        f"(82KB+ payload benefits significantly from gzip)"
    )


def test_etag_present_after_task6(long_session):
    """
    After Task 6 (commit perf(api): gzip + ETag for /api/session) the
    server must emit a weak ETag on /api/session so clients can use the
    304 fast-path on session re-switch.  This test was originally a
    baseline check ("no ETag today") and was flipped when Task 6 landed.
    """
    sid, _size = long_session
    _body, _elapsed, hdrs = _api_get_timed(f"/api/session?session_id={sid}")
    etag = hdrs.get("ETag") or hdrs.get("Etag") or hdrs.get("etag")
    assert etag, "Task 6 should have added an ETag header to /api/session"
    assert etag.startswith('W/"'), f"ETag must be weak; got {etag!r}"
