from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"
WORKSPACE_PY = ROOT / "api" / "workspace.py"
UPLOAD_PY = ROOT / "api" / "upload.py"


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)


def _func_body(src: str, name: str) -> str:
    start = src.index(f"def {name}")
    try:
        end = src.index("\n\ndef ", start + 1)
    except ValueError:
        end = len(src)
    return src[start:end]


def test_serve_file_bytes_reads_through_anchor(monkeypatch, tmp_path):
    from api import routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("abcdef", encoding="utf-8")
    calls = []

    def fake_open(root, resolved, *, want_dir):
        calls.append((root, resolved, want_dir))
        return os.open(str(target), os.O_RDONLY)

    monkeypatch.setattr(routes, "open_anchored_fd", fake_open)

    handler = _FakeHandler()
    assert routes._serve_file_bytes(
        handler,
        target,
        "text/plain",
        "inline",
        "no-store",
        anchor_root=workspace,
    ) is True

    assert handler.status == 200
    assert handler.body == b"abcdef"
    assert calls == [(workspace, target.resolve(), False)]


def test_inline_html_preview_reads_through_anchor(monkeypatch, tmp_path):
    from api import routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "page.html"
    target.write_text("<html><head></head><body>x</body></html>", encoding="utf-8")
    calls = []

    def fake_open(root, resolved, *, want_dir):
        calls.append((root, resolved, want_dir))
        return os.open(str(target), os.O_RDONLY)

    monkeypatch.setattr(routes, "open_anchored_fd", fake_open)

    handler = _FakeHandler()
    routes._serve_inline_html_preview(handler, target, "no-store", csp="sandbox", anchor_root=workspace)

    assert handler.status == 200
    assert b'<base target="_blank">' in handler.body
    assert calls == [(workspace, target.resolve(), False)]


def test_editor_file_endpoints_use_anchored_helpers():
    src = ROUTES_PY.read_text(encoding="utf-8")
    delete_body = _func_body(src, "_handle_file_delete")
    save_body = _func_body(src, "_handle_file_save")
    create_body = _func_body(src, "_handle_file_create")
    rename_body = _func_body(src, "_handle_file_rename")
    mkdir_body = _func_body(src, "_handle_create_dir")

    assert "rmtree_anchored(ws_root, target)" in delete_body
    assert "unlink_anchored(ws_root, target)" in delete_body
    assert "target.unlink()" not in delete_body
    assert "shutil.rmtree(target)" not in delete_body

    assert "open_anchored_write_fd(ws_root, target)" in save_body
    assert "target.write_text" not in save_body

    assert "open_anchored_create_fd(ws_root, target)" in create_body
    assert "target.parent.mkdir" not in create_body
    assert "target.write_text" not in create_body

    assert "rename_anchored(ws_root, source, dest)" in rename_body
    assert "source.rename(dest)" not in rename_body

    assert "make_anchored_dir(ws_root, target)" in mkdir_body
    assert "target.mkdir(parents=True)" not in mkdir_body


def test_folder_zip_reopens_members_through_anchor():
    src = ROUTES_PY.read_text(encoding="utf-8")
    body = _func_body(src, "_handle_folder_download")

    assert "open_anchored_fd(workspace_root, fp.resolve(), want_dir=False)" in body
    assert "info.compress_type = zipfile.ZIP_DEFLATED" in body
    assert "zf.open(info, \"w\")" in body
    assert "zf.write(fp" not in body


def test_raw_and_inline_file_targets_carry_anchor_root():
    src = ROUTES_PY.read_text(encoding="utf-8")
    raw_target = _func_body(src, "_file_raw_target")
    raw_handler = _func_body(src, "_handle_file_raw")

    assert "return workspace_root, target" in raw_target
    assert "return attachment_root, attachment_target" in raw_target
    assert "anchor_root, target = resolved" in raw_handler
    assert "_serve_inline_html_preview(handler, target, \"no-store\", csp=sandbox_csp, anchor_root=anchor_root)" in raw_handler
    assert "_serve_file_bytes(handler, target, mime, disposition, \"no-store\", csp=csp, anchor_root=anchor_root)" in raw_handler


def test_escape_raw_and_read_routes_use_authorized_helpers():
    src = ROUTES_PY.read_text(encoding="utf-8")
    escape_read = _func_body(src, "_handle_escape_file_read")
    escape_raw = _func_body(src, "_handle_escape_file_raw")

    assert "read_authorized_escape_file_content" in escape_read
    assert "raw_authorized_escape_target" in escape_raw
    assert "_serve_inline_html_preview(handler, target, \"no-store\", csp=sandbox_csp, anchor_root=anchor_root)" in escape_raw
    assert "_serve_file_bytes(handler, target, mime, disposition, \"no-store\", csp=csp, anchor_root=anchor_root)" in escape_raw


def test_escape_raw_helper_reanchors_through_safe_resolve():
    src = WORKSPACE_PY.read_text(encoding="utf-8")
    helper = _func_body(src, "raw_authorized_escape_target")

    assert "safe_resolve_ws(resolved[\"external_root\"], resolved[\"external_rel\"])" in helper
    assert "return resolved[\"external_root\"], target" in helper


def test_upload_archive_cleanup_uses_anchored_helpers():
    src = UPLOAD_PY.read_text(encoding="utf-8")

    assert "rmtree_anchored(workspace, dest_dir)" in src
    assert "unlink_anchored(workspace, dest.resolve())" in src
    assert "shutil.rmtree(dest_dir" not in src
    assert "dest.unlink(" not in src
