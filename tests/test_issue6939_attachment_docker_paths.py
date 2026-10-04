"""Regression coverage for #6939: chat-composer attachment paths must be
visible inside remote terminal sandboxes (terminal.backend: docker, ...).

hermes-agent auto-mounts its cache directories (cache/documents, ...) into
container sandboxes (docker/modal). The WebUI stages chat uploads under
``cache/documents/webui-attachments`` when such a backend is active and
translates the host path to its sandbox-visible form (agent_path) so the
``[Attached files: ...]`` marker handed to the model contains a path tool
calls (read_file) can actually open inside the container.

Re-gate coverage (#7022): staging/translation is scoped to docker/modal only
(ssh-style backends keep the legacy root and the host path — a translated
``~/.hermes`` would be host-expanded on the WebUI side), reads/deletion
consider BOTH the legacy STATE_DIR/attachments/<sid> root and the sandbox
root so backend switches don't orphan files, duplicate filenames across roots
are disambiguated via persisted metadata, and active-profile resolution
failures fail CLOSED instead of falling back to the process-global home.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
UI_JS = ROOT / "static" / "ui.js"
UPLOAD_PY = ROOT / "api" / "upload.py"
ROUTES_PY = ROOT / "api" / "routes.py"


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


def test_attachment_root_modal_backend_stages_under_cache_subtree(monkeypatch, fake_home):
    _set_terminal_backend(monkeypatch, "modal")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    from api.upload import _attachment_root

    assert _attachment_root() == (fake_home / "cache" / "documents" / "webui-attachments").resolve()


def test_attachment_root_ssh_backend_keeps_state_dir(monkeypatch, fake_home):
    """ssh-style backends keep the legacy root: a ``~/.hermes``-based staged
    path would be host-expanded by the agent's read_file resolver on the WebUI
    host, not the remote user's home (#7022 re-gate gap 2)."""
    _set_terminal_backend(monkeypatch, "ssh")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    from api.config import STATE_DIR
    from api.upload import _attachment_root

    assert _attachment_root() == (STATE_DIR / "attachments").resolve()


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


@pytest.mark.parametrize("backend", ["ssh", "daytona", "vercel_sandbox"])
def test_translate_ssh_style_backends_return_host_path(monkeypatch, fake_home, backend):
    """ssh/daytona/vercel keep the host path unchanged: a translated
    ``~/.hermes/...`` would be host-expanded to the WebUI host's home before
    remote dispatch and point the agent at a nonexistent path
    (#7022 re-gate gap 2)."""
    _set_terminal_backend(monkeypatch, backend)

    from api.upload import _agent_visible_attachment_path

    staged = fake_home / "cache" / "documents" / "webui-attachments" / "s" / "doc.pdf"
    assert _agent_visible_attachment_path(staged) == str(staged)


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

    # Simulate an older agent build: the canonical resolver raises, so the
    # function must fall back to the WebUI-local home-derived root and still
    # translate (not crash). hermes_constants lives in hermes-agent, not in
    # the WebUI tree, so this test must not import it directly.
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "hermes_constants":
            raise ImportError("No module named 'hermes_constants'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    from api.upload import _agent_visible_attachment_path

    staged = fake_home / "cache" / "documents" / "webui-attachments" / "s" / "photo.jpg"
    assert _agent_visible_attachment_path(staged) == (
        "/root/.hermes/cache/documents/webui-attachments/s/photo.jpg"
    )


# ── Dual read/cleanup roots: backend switches must not orphan files ─────────
# (#7022 re-gate gap 1)


def test_legacy_attachment_readable_after_switch_to_docker(monkeypatch, fake_home, tmp_path):
    """Files written under the legacy STATE_DIR root stay readable after the
    backend switches to docker (write root moves to the sandbox subtree)."""
    from api.upload import _session_attachment_dir, resolve_session_attachment

    _set_terminal_backend(monkeypatch, "local")
    legacy = _session_attachment_dir("sess-1")
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "old.txt").write_text("legacy bytes")

    _set_terminal_backend(monkeypatch, "docker")
    resolved = resolve_session_attachment("sess-1", "old.txt")
    assert resolved is not None
    root, target = resolved
    assert target == (legacy / "old.txt").resolve()
    assert target.read_text() == "legacy bytes"


def test_sandbox_attachment_readable_after_switch_back_to_local(monkeypatch, fake_home, tmp_path):
    """Files staged under the sandbox root stay readable after switching back
    to a local backend."""
    from api.upload import _session_attachment_dir, resolve_session_attachment

    _set_terminal_backend(monkeypatch, "docker")
    sandbox = _session_attachment_dir("sess-1")
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "new.txt").write_text("sandbox bytes")

    _set_terminal_backend(monkeypatch, "local")
    resolved = resolve_session_attachment("sess-1", "new.txt")
    assert resolved is not None
    root, target = resolved
    assert target == (sandbox / "new.txt").resolve()
    assert target.read_text() == "sandbox bytes"


def test_duplicate_filename_disambiguated_by_metadata(monkeypatch, fake_home, tmp_path):
    """Same filename in both roots: persisted provenance picks the recorded
    copy instead of the first root in scan order."""
    from api.upload import (
        _record_attachment_metadata,
        _session_attachment_dir,
        resolve_session_attachment,
    )

    _set_terminal_backend(monkeypatch, "local")
    legacy = _session_attachment_dir("sess-1")
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "dup.txt").write_text("legacy copy")
    _record_attachment_metadata("sess-1", "dup.txt", legacy / "dup.txt", str(legacy / "dup.txt"))

    _set_terminal_backend(monkeypatch, "docker")
    sandbox = _session_attachment_dir("sess-1")
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "dup.txt").write_text("sandbox copy")

    resolved = resolve_session_attachment("sess-1", "dup.txt")
    assert resolved is not None
    root, target = resolved
    assert target == (legacy / "dup.txt").resolve()
    assert target.read_text() == "legacy copy"


def test_upload_records_attachment_metadata():
    """handle_upload persists write-root provenance for the stored filename.

    Source-level contract (this file's established style for handler bodies):
    the recorder is invoked with the exact stored destination so reads can
    disambiguate duplicates across roots after a backend switch.
    """
    src = UPLOAD_PY.read_text(encoding="utf-8")
    handle_body = src[src.index("def handle_upload"): src.index("def extract_archive", src.index("def handle_upload"))]
    assert "_record_attachment_metadata(session_id, dest.name, dest, _agent_visible_attachment_path(dest))" in handle_body


def test_delete_removes_all_roots_and_metadata(monkeypatch, fake_home, tmp_path):
    """Session deletion must clean the legacy root, the sandbox root, and the
    metadata sidecar."""
    from api.upload import (
        _attachment_metadata_path,
        _record_attachment_metadata,
        _session_attachment_dir,
        remove_session_attachments,
    )

    _set_terminal_backend(monkeypatch, "local")
    legacy = _session_attachment_dir("sess-9")
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "a.txt").write_text("a")
    _record_attachment_metadata("sess-9", "a.txt", legacy / "a.txt", str(legacy / "a.txt"))

    _set_terminal_backend(monkeypatch, "docker")
    sandbox = _session_attachment_dir("sess-9")
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "b.txt").write_text("b")

    remove_session_attachments("sess-9")
    assert not legacy.exists()
    assert not sandbox.exists()
    assert not _attachment_metadata_path("sess-9").exists()


def test_routes_read_path_considers_all_roots():
    """/api/file/raw lookup delegates to the dual-root resolver."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "from api.upload import resolve_session_attachment" in src
    assert "resolve_session_attachment(sid, rel)" in src


def test_routes_delete_path_removes_all_roots():
    """Session deletion delegates to the all-roots cleanup helper."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "from api.upload import remove_session_attachments" in src
    assert "remove_session_attachments(sid)" in src


# ── Fail-closed profile resolution (#7022 re-gate gap 3) ────────────────────


def test_profile_resolution_failure_fails_closed(monkeypatch):
    """Resolver failure must propagate (fail-closed), never fall back to the
    process-global HERMES_HOME / STATE_DIR.parent — the fallback silently
    staged files inside the WRONG profile."""
    import api.profiles as profiles

    def _boom():
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(profiles, "get_active_hermes_home", _boom)
    monkeypatch.setenv("HERMES_HOME", str(Path("/somewhere/else/beta")))
    _set_terminal_backend(monkeypatch, "docker")

    from api.upload import _attachment_root, _sandbox_attachment_root

    with pytest.raises(RuntimeError):
        _sandbox_attachment_root()
    with pytest.raises(RuntimeError):
        _attachment_root()


# ── Real remote file-tool consumer shape (#7022 re-gate coverage) ───────────


def test_marker_path_is_readable_by_remote_file_consumer(monkeypatch, fake_home, tmp_path):
    """End-to-end shape of the real consumer: an upload staged under the
    sandbox root yields an agent_path that, joined to the container mount
    root, resolves to the exact staged bytes — the path the agent's read_file
    will open inside the container."""
    _set_terminal_backend(monkeypatch, "docker")

    from api.upload import _agent_visible_attachment_path, _session_attachment_dir

    staged = _session_attachment_dir("sess-1")
    staged.mkdir(parents=True, exist_ok=True)
    payload = b"attached payload"
    (staged / "report.txt").write_bytes(payload)

    agent_path = _agent_visible_attachment_path(staged / "report.txt")
    assert agent_path == "/root/.hermes/cache/documents/webui-attachments/sess-1/report.txt"
    # The container-visible path is the staged host file under the
    # /root/.hermes mount: resolving the suffix back under the real host root
    # must yield the exact bytes the agent will read.
    container_suffix = agent_path.removeprefix("/root/.hermes/")
    host_back = (fake_home / container_suffix).resolve()
    assert host_back == (staged / "report.txt").resolve()
    assert host_back.read_bytes() == payload


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
