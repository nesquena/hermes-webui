import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

_ROUTES_MOD = None


def _routes():
    global _ROUTES_MOD
    if _ROUTES_MOD is None:
        import api.routes as mod

        _ROUTES_MOD = mod
    return _ROUTES_MOD


def _call_get(handler, path):
    routes = _routes()
    return routes.handle_get(handler, urlparse(path))


def _capture_j():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None, pretty=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    return captured, fake_j


def _patch_j(fake_j):
    return patch("api.helpers.j", side_effect=fake_j)


def _default_handler():
    import http.client

    headers = http.client.HTTPMessage()
    client_addr = ("127.0.0.1", 54321)
    handler = MagicMock()
    handler.headers = headers
    handler.client_address = client_addr
    return handler


def _workspace_search_root():
    import api.workspace_search as ws_mod
    return ws_mod._resolve_search_root()


class TestWorkspaceSearchBasic:
    """Basic endpoint behavior."""

    def test_returns_200_for_valid_query(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "test_file.py").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=test")
        assert captured["status"] == 200

    def test_empty_q_returns_validation_error(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            result = _call_get(handler, "http://localhost/api/workspace/search?q=")
        assert captured["status"] == 400

    def test_missing_q_returns_validation_error(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            result = _call_get(handler, "http://localhost/api/workspace/search")
        assert captured["status"] == 400

    def test_invalid_type_returns_validation_error(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            result = _call_get(handler, "http://localhost/api/workspace/search?q=test&type=invalid")
        assert captured["status"] == 400

    def test_limit_default_is_applied(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=test")
        assert captured["status"] == 200
        assert captured["payload"]["limit"] == 50

    def test_limit_max_is_enforced(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=test&limit=500")
        assert captured["status"] == 200
        assert captured["payload"]["limit"] == 100

    def test_response_includes_query_type_limit_results(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=name&limit=10")
        assert captured["status"] == 200
        assert captured["payload"]["query"] == "hello"
        assert captured["payload"]["type"] == "name"
        assert captured["payload"]["limit"] == 10
        assert "results" in captured["payload"]


class TestWorkspaceSearchName:
    """Name search behavior."""

    def test_finds_file_by_basename(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "runtime_contract.py").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=runtime_contract&type=name")
        assert captured["status"] == 200
        assert len(captured["payload"]["results"]) == 1
        assert captured["payload"]["results"][0]["path"] == "runtime_contract.py"

    def test_finds_file_by_relative_path_segment(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        subdir = ws_root / "api"
        subdir.mkdir()
        (subdir / "runtime_contract.py").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=runtime&type=name")
        assert captured["status"] == 200
        paths = [r["path"] for r in captured["payload"]["results"]]
        assert any("runtime" in p for p in paths)

    def test_name_search_is_case_insensitive(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "RuntimeContract.py").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=runtimecontract&type=name")
        assert captured["status"] == 200
        assert len(captured["payload"]["results"]) == 1

    def test_name_search_returns_relative_paths_only(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "test_file.py").write_text("hello")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=test_file&type=name")
        assert captured["status"] == 200
        for r in captured["payload"]["results"]:
            assert not r["path"].startswith("/")
            assert str(ws_root) not in r["path"]

    def test_name_search_does_not_leak_absolute_workspace_root(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "test_file.py").write_text("hello")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=test_file&type=name")
        assert captured["status"] == 200
        payload_str = json.dumps(captured["payload"])
        assert str(ws_root) not in payload_str


class TestWorkspaceSearchContent:
    """Content search behavior."""

    def test_finds_file_by_content(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "hello.txt").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200
        assert len(captured["payload"]["results"]) == 1
        assert captured["payload"]["results"][0]["match_type"] == "content"

    def test_content_result_includes_line_number(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "hello.txt").write_text("line one\nline two\nline three\n")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=line%20two&type=content")
        assert captured["status"] == 200
        assert len(captured["payload"]["results"]) == 1
        assert captured["payload"]["results"][0]["line"] == 2

    def test_content_result_includes_preview(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "hello.txt").write_text("hello world foo")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200
        assert len(captured["payload"]["results"]) == 1
        assert "preview" in captured["payload"]["results"][0]

    def test_content_preview_is_trimmed(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        long_line = "x" * 500
        (ws_root / "hello.txt").write_text(long_line)
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=xxxx&type=content")
        assert captured["status"] == 200
        if captured["payload"]["results"]:
            preview = captured["payload"]["results"][0]["preview"]
            assert len(preview) <= 203

    def test_binary_files_are_ignored(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        binary_file = ws_root / "hello.bin"
        binary_file.write_bytes(b"hello\x00world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200
        results = captured["payload"]["results"]
        binary_results = [r for r in results if r["path"].endswith(".bin")]
        assert len(binary_results) == 0

    def test_large_files_are_ignored_for_content_search(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        large_file = ws_root / "large.txt"
        large_file.write_bytes(b"hello world\n" * 100000)
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200
        results = captured["payload"]["results"]
        large_results = [r for r in results if r["path"] == "large.txt"]
        assert len(large_results) == 0

    def test_unreadable_files_are_skipped_safely(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        hello_file = ws_root / "hello.txt"
        hello_file.write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200


class TestWorkspaceSearchBoth:
    """Both mode behavior."""

    def test_both_mode_returns_name_and_content_matches(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "runtime.py").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=runtime&type=both")
        assert captured["status"] == 200
        match_types = {r["match_type"] for r in captured["payload"]["results"]}
        assert "name" in match_types

    def test_match_type_is_name_or_content(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "hello.py").write_text("world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=both")
        assert captured["status"] == 200
        for r in captured["payload"]["results"]:
            assert r["match_type"] in ("name", "content")


class TestWorkspaceSearchSafety:
    """Safety checks."""

    def test_traversal_query_cannot_escape_workspace(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "test.txt").write_text("hello")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret data")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=../outside&type=name")
        assert captured["status"] == 200
        paths = [r["path"] for r in captured["payload"]["results"]]
        assert not any("outside" in p for p in paths)

    def test_ignored_dirs_are_skipped(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        git_dir = ws_root / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200
        results = captured["payload"]["results"]
        git_results = [r for r in results if ".git" in r["path"]]
        assert len(git_results) == 0

    def test_node_modules_contents_are_not_searched(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        nm_dir = ws_root / "node_modules"
        nm_dir.mkdir()
        (nm_dir / "package.json").write_text("hello world")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=hello&type=content")
        assert captured["status"] == 200
        results = captured["payload"]["results"]
        nm_results = [r for r in results if "node_modules" in r["path"]]
        assert len(nm_results) == 0

    def test_secret_like_preview_values_are_redacted(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "config.txt").write_text("api_key=sk-1234567890abcdef\npassword=secret123")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=api_key&type=content")
        assert captured["status"] == 200
        payload_str = json.dumps(captured["payload"])
        assert "sk-1234567890abcdef" not in payload_str
        assert "REDACTED" in payload_str

    def test_no_api_keys_in_response(self, monkeypatch, tmp_path):
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        (ws_root / "env.txt").write_text("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\nAuthorization: Bearer token123\npassword=secret123")
        monkeypatch.setattr("api.config.DEFAULT_WORKSPACE", ws_root)
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/workspace/search?q=Bearer&type=content")
        assert captured["status"] == 200
        payload_str = json.dumps(captured["payload"])
        assert "token123" not in payload_str
        assert "REDACTED" in payload_str


class TestWorkspaceSearchMobileIntegration:
    """Mobile capabilities integration."""

    def test_mobile_capabilities_reports_workspace_search_true(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert captured["status"] == 200
        assert captured["payload"]["features"]["workspace_search"] is True
