"""Tests for the server-side Markdown export endpoint.

Replaces the client-side Blob-URL-based transcript download which suffered from
a Chromium quirk where the download UI never reached "complete" state even
though the file was fully written. The server-side path uses standard HTTP
Content-Disposition + Content-Length, exactly like the JSON export.
"""
import json
import secrets
import urllib.error
import urllib.request

from tests._pytest_port import BASE, TEST_STATE_DIR

# The server is launched with HERMES_WEBUI_STATE_DIR=TEST_STATE_DIR by conftest.
# api/models.SESSION_DIR is computed from that env var inside the server process,
# resolving to <state_dir>/sessions. Mirror that here so tests target the right dir
# rather than the production sessions dir which the test process would otherwise pick up.
TEST_SESSION_DIR = TEST_STATE_DIR / "sessions"


def _get_raw(path):
    """GET that returns body+headers+status for both 2xx and 4xx."""
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.read(), r.headers, r.status
    except urllib.error.HTTPError as e:
        return e.read(), e.headers, e.code


def _make_session(messages):
    """Seed a session JSON directly into the SERVER's state dir.

    Bypasses /api/session/new so we can inject arbitrary message shapes
    (e.g. tool messages, array content, attachments) without going through
    the more permissive client-facing API surface.
    """
    sid = "tx" + secrets.token_hex(6)
    payload = {
        "session_id": sid,
        "title": "MD export test",
        "workspace": "/tmp/test-ws",
        "model": "test/model-v1",
        "messages": messages,
    }
    TEST_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (TEST_SESSION_DIR / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")
    return sid


def test_md_export_requires_session_id():
    raw, _, status = _get_raw("/api/session/export?format=md")
    assert status == 400, (status, raw[:200])


def test_md_export_unknown_session_returns_404():
    raw, _, status = _get_raw("/api/session/export?format=md&session_id=nosuch_xyz")
    assert status == 404, (status, raw[:200])


def test_md_export_serves_with_correct_headers():
    sid = _make_session([
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ])
    raw, headers, status = _get_raw(f"/api/session/export?format=md&session_id={sid}")
    assert status == 200, (status, raw[:200])
    ct = headers.get("Content-Type", "")
    assert "text/markdown" in ct, ct
    cd = headers.get("Content-Disposition", "")
    assert "attachment" in cd and f"hermes-{sid}.md" in cd, cd
    assert headers.get("Content-Length") is not None, "Content-Length must be set"
    assert int(headers["Content-Length"]) == len(raw), "Content-Length must match body"


def test_md_export_renders_messages():
    sid = _make_session([
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "It is 4."},
    ])
    raw, _, status = _get_raw(f"/api/session/export?format=md&session_id={sid}")
    assert status == 200
    md = raw.decode("utf-8")
    assert md.startswith(f"# Hermes session {sid}"), md[:120]
    assert "Workspace: /tmp/test-ws" in md
    assert "Model: test/model-v1" in md
    assert "## user" in md and "What is 2+2?" in md
    assert "## assistant" in md and "It is 4." in md


def test_md_export_skips_tool_messages():
    sid = _make_session([
        {"role": "user", "content": "run a tool"},
        {"role": "tool", "content": "tool output should not appear"},
        {"role": "assistant", "content": "done"},
    ])
    raw, _, _ = _get_raw(f"/api/session/export?format=md&session_id={sid}")
    md = raw.decode("utf-8")
    assert "tool output should not appear" not in md
    assert "## tool" not in md
    assert "## user" in md and "## assistant" in md


def test_md_export_handles_array_content_text_blocks():
    sid = _make_session([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first part"},
                {"type": "image", "url": "ignored"},
                {"type": "text", "text": "second part"},
            ],
        },
    ])
    raw, _, _ = _get_raw(f"/api/session/export?format=md&session_id={sid}")
    md = raw.decode("utf-8")
    assert "first part" in md and "second part" in md


def test_md_export_includes_attachments_marker():
    sid = _make_session([
        {"role": "user", "content": "see file", "attachments": ["foo.png", "bar.txt"]},
    ])
    raw, _, _ = _get_raw(f"/api/session/export?format=md&session_id={sid}")
    md = raw.decode("utf-8")
    assert "_Files: foo.png, bar.txt_" in md


def test_md_export_default_format_is_still_json():
    """No regression: omitting ?format=md must still serve JSON."""
    sid = _make_session([{"role": "user", "content": "hi"}])
    raw, headers, status = _get_raw(f"/api/session/export?session_id={sid}")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    json.loads(raw)


def test_btn_download_uses_server_endpoint():
    """Pin the boot.js click handler to use the server-side endpoint, not blob:."""
    import pathlib
    import re
    boot = (pathlib.Path(__file__).resolve().parents[1] / "static" / "boot.js").read_text()
    m = re.search(
        r"\$\(['\"]btnDownload['\"]\)\.onclick\s*=\s*\(\)\s*=>\s*\{(.*?)\};",
        boot,
        flags=re.S,
    )
    assert m, "could not locate #btnDownload handler"
    body = m.group(1)
    assert "/api/session/export" in body and "format=md" in body, (
        "Markdown download must hit the server endpoint, not a client-side blob URL "
        "(blob: URLs hang the Chromium download UI even when the file is complete)"
    )
    assert "createObjectURL" not in body, (
        "transcript download must not create blob URLs — use the server endpoint"
    )
