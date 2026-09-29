"""Tests for #6205 artifact reference derivation from tool results."""

import tempfile
from pathlib import Path
import pytest
from api.streaming import _derive_artifact_references_from_tool_result


def test_write_file_valid_int_bytes_written():
    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "out.txt"
        target.write_text("hello")
        res = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "out.txt"},
            function_result={"bytes_written": 5, "files_modified": ["out.txt"]},
            workspace=ws,
        )
        assert res == ["out.txt"]


def test_write_file_rejects_float_and_bool_bytes_written():
    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "out.txt"
        target.write_text("hello")

        # float bytes_written rejected
        res_float = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "out.txt"},
            function_result={"bytes_written": 5.5, "files_modified": ["out.txt"]},
            workspace=ws,
        )
        assert res_float == []

        # bool bytes_written rejected
        res_bool = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "out.txt"},
            function_result={"bytes_written": True, "files_modified": ["out.txt"]},
            workspace=ws,
        )
        assert res_bool == []


def test_patch_success_and_failure():
    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "patched.py"
        target.write_text("print(1)")

        res_ok = _derive_artifact_references_from_tool_result(
            name="patch",
            args={"path": "patched.py"},
            function_result={"success": True, "files_modified": ["patched.py"]},
            workspace=ws,
        )
        assert res_ok == ["patched.py"]

        res_fail = _derive_artifact_references_from_tool_result(
            name="patch",
            args={"path": "patched.py"},
            function_result={"success": False, "files_modified": ["patched.py"]},
            workspace=ws,
        )
        assert res_fail == []


def test_cross_call_deduplication_via_seen():
    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "repeated.txt"
        target.write_text("content")

        seen = set()
        res1 = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "repeated.txt"},
            function_result={"bytes_written": 7, "files_modified": ["repeated.txt"]},
            workspace=ws,
            seen=seen,
        )
        assert res1 == ["repeated.txt"]
        assert "repeated.txt" in seen

        # Second call in same turn with same path
        res2 = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "repeated.txt"},
            function_result={"bytes_written": 7, "files_modified": ["repeated.txt"]},
            workspace=ws,
            seen=seen,
        )
        assert res2 == []


def test_rejects_traversal_and_urls():
    with tempfile.TemporaryDirectory() as ws:
        res_traversal = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "../outside.txt"},
            function_result={"bytes_written": 5, "files_modified": ["../outside.txt"]},
            workspace=ws,
        )
        assert res_traversal == []

        res_url = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "https://example.com/file.txt"},
            function_result={"bytes_written": 5, "files_modified": ["https://example.com/file.txt"]},
            workspace=ws,
        )
        assert res_url == []


def test_rejects_symlinks():
    with tempfile.TemporaryDirectory() as ws:
        target_file = Path(ws) / "real.txt"
        target_file.write_text("real")
        symlink_file = Path(ws) / "link.txt"
        try:
            symlink_file.symlink_to(target_file)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not permitted in this environment")

        res = _derive_artifact_references_from_tool_result(
            name="write_file",
            args={"path": "link.txt"},
            function_result={"bytes_written": 4, "files_modified": ["link.txt"]},
            workspace=ws,
        )
        assert res == []
