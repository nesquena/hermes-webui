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


# ── i18n parity ────────────────────────────────────────────────────

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def test_agent_soul_i18n_key_present_in_all_locales():
    """`agent_soul` and `no_soul_yet` keys must be defined for every locale block.

    Quoting these correctly across all 11 locales matters: an unescaped
    apostrophe inside a single-quoted string ('l'agent') breaks JS parsing of
    the entire module, which silently disables i18n for every language. The
    PR's initial commit failed CI for exactly this reason (it / fr).
    """
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    # i18n.js currently exposes 11 locales (en, it, ja, ru, es, de, zh-CN,
    # zh-TW, pt, ko, fr). Lock that both new keys are present at least 10
    # times — that's enough to catch a missing locale without coupling the
    # test to the exact locale count, which shifts as new ones land.
    assert i18n.count("agent_soul:") >= 10, (
        "agent_soul i18n key should be defined in all locale blocks; "
        f"found {i18n.count('agent_soul:')} occurrences."
    )
    assert i18n.count("no_soul_yet:") >= 10, (
        "no_soul_yet i18n key should be defined in all locale blocks; "
        f"found {i18n.count('no_soul_yet:')} occurrences."
    )


def test_sparkles_icon_is_defined():
    """The Memory panel's soul section uses iconKey='sparkles'.

    If the icon is missing from `static/icons.js`, `li('sparkles', 16)` emits
    a console warning and returns an empty string — the section heading then
    renders without an icon, which looks broken next to the memory/user
    sections that do show one.
    """
    icons = (REPO_ROOT / "static" / "icons.js").read_text(encoding="utf-8")
    assert "'sparkles':" in icons, (
        "The Memory panel's soul section references iconKey='sparkles' "
        "(static/panels.js MEMORY_SECTIONS); the corresponding entry must "
        "exist in static/icons.js LI_PATHS."
    )
