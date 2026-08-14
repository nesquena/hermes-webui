"""Regression coverage for #6939: chat-composer attachment paths must be
visible inside remote terminal sandboxes (terminal.backend: docker, ssh, ...).

hermes-agent auto-mounts its cache directories (cache/documents, ...) into
remote sandboxes. The WebUI stages chat uploads under
``cache/documents/webui-attachments`` when a remote backend is active and
translates the host path to its sandbox-visible form (agent_path) so the
``[Attached files: ...]`` marker handed to the model contains a path tool
calls (read_file) can actually open inside the container.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
UI_JS = ROOT / "static" / "ui.js"
UPLOAD_PY = ROOT / "api" / "upload.py"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin the active profile's Hermes home to a temp dir."""
    home = tmp_path / "hermes-home"
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    return home


def _set_terminal_backend(monkeypatch, backend):
    """Rebind the in-memory config so get_config() reports *backend*."""
    import api.config as config

    monkeypatch.setattr(config, "cfg", {"terminal": {"backend": backend}})


# ── _attachment_root(): sandbox-aware default inbox ─────────────────────────


def test_attachment_root_local_backend_keeps_state_dir(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "local")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    from api.config import STATE_DIR
    from api.upload import _attachment_root

    root = _attachment_root()
    assert root == (STATE_DIR / "attachments").resolve()


def test_attachment_root_docker_backend_stages_under_cache_subtree(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "docker")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    from api.upload import _attachment_root

    root = _attachment_root()
    assert root == (fake_home / "cache" / "documents" / "webui-attachments").resolve()
    # The staged root must stay under the auto-mounted cache/documents dir.
    assert root.is_relative_to(fake_home / "cache" / "documents")


def test_attachment_root_ssh_backend_stages_under_cache_subtree(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "ssh")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    from api.upload import _attachment_root

    assert _attachment_root() == (fake_home / "cache" / "documents" / "webui-attachments").resolve()


def test_attachment_root_env_override_wins_over_sandbox_staging(monkeypatch, fake_home, tmp_path):
    _set_terminal_backend(monkeypatch, "docker")
    inbox = tmp_path / "operator-inbox"
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))

    from api.upload import _attachment_root

    assert _attachment_root() == inbox.resolve()


# ── _agent_visible_attachment_path(): host → sandbox translation ────────────


def test_translate_docker_staged_path_to_container(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "docker")
    staged = fake_home / "cache" / "documents" / "webui-attachments" / "sess-1" / "photo.jpg"

    from api.upload import _agent_visible_attachment_path

    assert _agent_visible_attachment_path(staged) == (
        "/root/.hermes/cache/documents/webui-attachments/sess-1/photo.jpg"
    )


def test_translate_modal_staged_path_to_container(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "modal")

    from api.upload import _agent_visible_attachment_path

    staged = fake_home / "cache" / "documents" / "webui-attachments" / "s" / "doc.pdf"
    assert _agent_visible_attachment_path(staged) == (
        "/root/.hermes/cache/documents/webui-attachments/s/doc.pdf"
    )


def test_translate_ssh_staged_path_uses_remote_home(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "ssh")

    from api.upload import _agent_visible_attachment_path

    staged = fake_home / "cache" / "documents" / "webui-attachments" / "s" / "doc.pdf"
    assert _agent_visible_attachment_path(staged) == (
        "~/.hermes/cache/documents/webui-attachments/s/doc.pdf"
    )


def test_translate_local_backend_passthrough(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "local")

    from api.upload import _agent_visible_attachment_path

    staged = fake_home / "cache" / "documents" / "webui-attachments" / "s" / "doc.pdf"
    assert _agent_visible_attachment_path(staged) == str(staged)


def test_translate_path_outside_cache_subtree_unchanged(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "docker")

    from api.upload import _agent_visible_attachment_path

    elsewhere = fake_home / "webui" / "attachments" / "s" / "photo.jpg"
    assert _agent_visible_attachment_path(elsewhere) == str(elsewhere)


def test_translate_survives_missing_agent_translation_api(monkeypatch, fake_home):
    """Older agent builds without the translation helpers degrade to host path."""
    _set_terminal_backend(monkeypatch, "docker")

    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "get_hermes_dir", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("old agent"))
    )

    from api.upload import _agent_visible_attachment_path

    staged = fake_home / "cache" / "documents" / "webui-attachments" / "s" / "photo.jpg"
    assert _agent_visible_attachment_path(staged) == str(staged)


# ── Upload/extract responses carry agent_path ───────────────────────────────


def test_upload_response_includes_agent_path():
    src = UPLOAD_PY.read_text(encoding="utf-8")
    handle_body = src[src.index("def handle_upload"): src.index("def extract_archive", src.index("def handle_upload"))]
    assert "'path': str(dest)" in handle_body
    assert "'agent_path': _agent_visible_attachment_path(dest)" in handle_body


def test_extract_response_includes_agent_path():
    src = UPLOAD_PY.read_text(encoding="utf-8")
    assert "'agent_path': _agent_visible_attachment_path(result.get('dest') or session_dir)" in src


# ── Frontend marker composition prefers the sandbox-visible path ─────────────


def test_messages_js_prefers_agent_path_in_marker():
    src = MESSAGES_JS.read_text(encoding="utf-8")
    assert "uploadedPaths=uploaded.map(u=>u&&(u.agent_path||u.path)" in src
    # Host path stays as the fallback so local backends behave exactly as before.
    assert "u.agent_path||u.path):(u&&u.name?u.name:(u&&u.filename?u.filename:u))" in src


def test_ui_js_forwards_agent_path_from_upload_responses():
    src = UI_JS.read_text(encoding="utf-8")
    assert "agent_path: data.agent_path||data.path" in src
    assert "agent_path: data.agent_path||data.dest" in src
