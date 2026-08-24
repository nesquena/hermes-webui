"""End-to-end regression: the squash button's backend flow against the
isolated test server (conftest ``test_server`` / ``base_url`` fixtures).

Scenario
--------
1. Seed a 50-message sidecar directly into the test state dir.
2. POST /api/session/squash with matching confirm_session_id.
3. Poll GET /api/session/squash/status until the job finishes.
4. Verify the read path (GET /api/session?messages=1) serves exactly the
   squashed transcript — this is the regression guard for the "Loading
   conversation..." hang: before the cache-lag fix, the server kept serving
   the stale pre-squash object (thousands of messages, multi-second payload).
5. Verify the gzip archive matches the original sidecar checksum (restore
   compatibility with the squash-chat skill).

The sandboxed test server has no network (HERMES_WEBUI_TEST_NETWORK_BLOCK),
so the auxiliary-LLM summary deterministically falls back to the honest
fallback template — that path is asserted explicitly.
"""

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"])
SID = "20260801_120000_e2esq1"


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return json.loads(r.read())


def _post(base, path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"_http_error": e.code}


def _seed_session() -> Path:
    sessions_dir = STATE_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    messages = []
    for i in range(50):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"message {i}"
        if role == "assistant" and i % 10 == 1:
            content += "\n# CONCLUSION\n---\n> 🟢 étape validée"
        messages.append({"id": f"m{i}", "role": role, "content": content, "timestamp": 1000.0 + i})
    payload = {
        "session_id": SID,
        "title": "e2e squash",
        "workspace": "/tmp",
        "created_at": 1000.0,
        "updated_at": 1050.0,
        "messages": messages,
        "context_messages": list(messages),
        "tool_calls": [],
        "message_count": len(messages),
        "profile": "default",
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
    }
    path = sessions_dir / f"{SID}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_squash_end_to_end(base_url):
    sidecar = _seed_session()
    original_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()

    start = _post(base_url, "/api/session/squash", {"session_id": SID, "confirm_session_id": SID})
    assert start.get("ok"), f"squash start failed: {start}"
    job_id = start["job"]["job_id"]

    job = None
    deadline = time.time() + 120
    while time.time() < deadline:
        job = _get(base_url, f"/api/session/squash/status?job_id={job_id}")["job"]
        if job["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert job and job["status"] == "done", f"job did not complete: {job}"

    result = job["result"]
    assert result["already_squashed"] is False
    assert result["before"]["message_count"] == 50
    assert result["after"]["message_count"] == 1
    assert result["original_sha256"] == original_sha
    # No network in the sandbox → the aux-LLM summary honestly degrades to
    # the fallback template instead of failing the squash.
    assert result["summary_source"] == "fallback-template"
    assert result["summary_chars"] >= 400

    # Read path: exactly the squashed transcript, served fast (stale-cache
    # regression guard — pre-fix this returned the full pre-squash payload).
    t0 = time.monotonic()
    data = _get(base_url, f"/api/session?session_id={SID}&messages=1&resolve_model=0")
    elapsed = time.monotonic() - t0
    served = data["session"]["messages"]
    assert len(served) == 1, f"expected 1 squashed message, got {len(served)}"
    assert served[0].get("_squash_summary") is True
    assert data["session"].get("compression_anchor_mode") == "manual"
    assert elapsed < 5, f"squashed session read took {elapsed:.1f}s"

    # Archive + manifest, restore-compatible with the squash-chat skill.
    archive = Path(result["archive_path"])
    manifest = Path(result["manifest_path"])
    assert archive.is_file() and manifest.is_file()
    with gzip.open(archive, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == original_sha
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["session_id"] == SID
    assert manifest_data["source_sha256"] == original_sha

    # No stale #1558 .bak may survive (startup recovery could undo the squash).
    assert not sidecar.with_suffix(".json.bak").exists()

    # Idempotent second squash.
    start2 = _post(base_url, "/api/session/squash", {"session_id": SID, "confirm_session_id": SID})
    assert start2.get("ok")
    job2_id = start2["job"]["job_id"]
    deadline = time.time() + 30
    job2 = None
    while time.time() < deadline:
        job2 = _get(base_url, f"/api/session/squash/status?job_id={job2_id}")["job"]
        if job2["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert job2 and job2["status"] == "done"
    assert job2["result"]["already_squashed"] is True


def test_squash_requires_confirm_end_to_end(base_url):
    sidecar = _seed_session()
    sidecar = sidecar  # seeded; mismatch must be rejected before any mutation
    resp = _post(base_url, "/api/session/squash", {"session_id": SID, "confirm_session_id": "nope"})
    assert not resp.get("ok"), f"confirm mismatch was accepted: {resp}"
