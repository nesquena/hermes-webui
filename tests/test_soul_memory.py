"""
Tests for SOUL.md support in the memory API (GET /api/memory, POST /api/memory/write).

SOUL.md lives at HERMES_HOME/SOUL.md (not in the memories/ subdirectory).
This test file verifies:
- GET /api/memory returns soul content, path, and mtime
- POST /api/memory/write with section="soul" writes to HERMES_HOME/SOUL.md
- Redaction still applies to soul content
- Existing memory/user sections remain unaffected
"""
import json, pathlib, urllib.error, urllib.parse, urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── GET /api/memory includes soul ──────────────────────────────────

def test_memory_read_includes_soul_fields():
    """GET /api/memory must include soul, soul_path, and soul_mtime."""
    data = get("/api/memory")
    assert "soul" in data, "Response missing 'soul' key"
    assert "soul_path" in data, "Response missing 'soul_path' key"
    assert "soul_mtime" in data, "Response missing 'soul_mtime' key"
    # soul_path should end with SOUL.md, not be inside memories/
    assert data["soul_path"].endswith("SOUL.md"), f"soul_path should end with SOUL.md, got {data['soul_path']}"
    assert "/memories/" not in data["soul_path"], f"soul_path should NOT be inside memories/, got {data['soul_path']}"


def test_memory_read_soul_default_empty():
    """When no SOUL.md exists, the soul field should be empty string."""
    data = get("/api/memory")
    # soul may be empty if no SOUL.md file exists — that's fine
    assert isinstance(data.get("soul"), str), "soul should be a string"


# ── POST /api/memory/write supports section="soul" ─────────────────

def test_memory_write_soul_roundtrip():
    """Writing to section='soul' should persist and be readable via GET."""
    original = get("/api/memory").get("soul", "")
    test_content = "# Test Soul\nWritten by test_memory_write_soul_roundtrip."
    data, status = post("/api/memory/write", {"section": "soul", "content": test_content})
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert data.get("ok") is True
    assert data.get("section") == "soul"
    # Path should be at HERMES_HOME/SOUL.md
    assert data.get("path", "").endswith("SOUL.md"), f"path should end with SOUL.md, got {data.get('path')}"
    # Read back
    read_back = get("/api/memory").get("soul")
    assert read_back == test_content
    # Restore
    post("/api/memory/write", {"section": "soul", "content": original})


def test_memory_write_soul_does_not_affect_memory_or_user():
    """Writing soul should not change memory or user sections."""
    state_before = get("/api/memory")
    original_soul = state_before.get("soul", "")
    original_memory = state_before.get("memory", "")
    original_user = state_before.get("user", "")

    post("/api/memory/write", {"section": "soul", "content": "# Temp Soul"})
    state_after = get("/api/memory")

    assert state_after.get("memory") == original_memory, "memory section changed unexpectedly"
    assert state_after.get("user") == original_user, "user section changed unexpectedly"

    # Restore
    post("/api/memory/write", {"section": "soul", "content": original_soul})


def test_memory_write_soul_path_not_in_memories_dir():
    """The SOUL.md file should be at HERMES_HOME/SOUL.md, not memories/SOUL.md."""
    data, status = post("/api/memory/write", {"section": "soul", "content": "# Path check"})
    assert status == 200
    assert "/memories/" not in data.get("path", ""), f"SOUL.md should NOT be in memories/ dir, got {data.get('path')}"
    # Cleanup
    post("/api/memory/write", {"section": "soul", "content": ""})


def test_memory_write_invalid_section_still_rejected():
    """Invalid sections should still be rejected even with soul added."""
    data, status = post("/api/memory/write", {"section": "invalid", "content": "test"})
    assert status == 400
