"""Archive extraction must never materialize symlinks or archive permission bits.

extract_archive copies member bytes manually (open/write) instead of using
extractall(), so archive-declared modes are never applied and tar symlinks are
skipped via member.isfile(). Zip symlink members (Unix S_IFLNK in
external_attr) are skipped explicitly. These tests lock that contract in.
"""

import io
import os
import stat
import sys
import tarfile
import zipfile

import pytest

from api.upload import extract_archive


def _zip_with_symlink_and_exec() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("normal.txt", "hello")
        link = zipfile.ZipInfo("evil-link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "../../outside-target")
        script = zipfile.ZipInfo("script.sh")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(script, "#!/bin/sh\necho pwned\n")
    return buf.getvalue()


def _tar_with_symlink() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"hello"
        info = tarfile.TarInfo("normal.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside-target"
        tf.addfile(link)
    return buf.getvalue()


def _assert_no_symlinks(root):
    offenders = [p for p in root.rglob("*") if p.is_symlink()]
    assert not offenders, f"symlinks materialized from archive: {offenders}"


class TestZipExtraction:
    def test_symlink_member_skipped(self, tmp_path):
        result = extract_archive(_zip_with_symlink_and_exec(), "bundle.zip", tmp_path)
        _assert_no_symlinks(tmp_path)
        names = [os.path.basename(f) for f in result["files"]]
        assert "normal.txt" in names
        assert "evil-link" not in names
        # The link-target text must not have been written as a plain file either.
        assert not list(tmp_path.rglob("evil-link"))

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
    def test_exec_bit_not_applied(self, tmp_path):
        extract_archive(_zip_with_symlink_and_exec(), "bundle.zip", tmp_path)
        script = next(tmp_path.rglob("script.sh"))
        assert not (script.stat().st_mode & 0o111), "archive exec bit leaked through"

    def test_nothing_written_outside_workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        extract_archive(_zip_with_symlink_and_exec(), "bundle.zip", ws)
        assert not (tmp_path / "outside-target").exists()


class TestTarExtraction:
    def test_symlink_member_skipped(self, tmp_path):
        result = extract_archive(_tar_with_symlink(), "bundle.tar", tmp_path)
        _assert_no_symlinks(tmp_path)
        names = [os.path.basename(f) for f in result["files"]]
        assert names == ["normal.txt"]
        assert not list(tmp_path.rglob("evil-link"))
