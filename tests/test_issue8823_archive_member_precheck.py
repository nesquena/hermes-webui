"""#8823: the archive member-count cap must reject BEFORE extracting any member.

The cap (``api.upload._MAX_ARCHIVE_MEMBERS``) exists to stop a tiny archive with a
huge number of members from exhausting inodes / file descriptors. It used to be
checked only inside the extraction loop, so an over-cap archive wrote ``cap``
files (an anchored create + write each) before it tripped — which is also why
the HTTP test ``test_archive_member_count_cap_trips`` (10,001 members) could take
longer than its 10 s client timeout and flake in full-suite runs.

``zf.infolist()`` / ``tf.getmembers()`` are fully parsed before the loop, so the
count is known up front. These in-process tests spy on the member-file creator
(``open_anchored_create_fd``) and assert an over-cap archive creates ZERO member
files, while an at-cap archive (directories not counted) still extracts fully.
"""
import io
import tarfile
import zipfile

import pytest

import api.upload as upload


CAP_MESSAGE = "Archive has too many files (> {cap}). Possible archive bomb."


def _make_zip(members) -> bytes:
    """members: iterable of (name, bytes); a name ending in '/' is a directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _make_tar(members) -> bytes:
    """members: iterable of (name, bytes or None); None makes a directory member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _files(n):
    return [(f"f{i}.txt", b"x") for i in range(n)]


def _spy_creates(monkeypatch):
    """Record every member-file create; delegate to the real anchored creator."""
    created = []
    real = upload.open_anchored_create_fd

    def spy(root, path, *args, **kwargs):
        created.append(str(path))
        return real(root, path, *args, **kwargs)

    monkeypatch.setattr(upload, "open_anchored_create_fd", spy)
    return created


def _extract_expect_cap_error(data, filename, workspace):
    try:
        upload.extract_archive(data, filename, workspace)
    except ValueError as exc:
        return exc
    return None


@pytest.mark.parametrize(
    "filename, data",
    [
        ("many.zip", _make_zip(_files(6))),
        ("many.tar", _make_tar(_files(6))),
    ],
    ids=["zip", "tar"],
)
def test_over_cap_archive_rejected_before_any_member_file_is_created(
    tmp_path, monkeypatch, filename, data
):
    monkeypatch.setattr(upload, "_MAX_ARCHIVE_MEMBERS", 5)
    created = _spy_creates(monkeypatch)

    err = _extract_expect_cap_error(data, filename, tmp_path)

    assert created == [], (
        f"{len(created)} member file(s) were created before the member-count cap "
        f"rejected the archive: {created}"
    )
    assert err is not None, "over-cap archive was not rejected"
    assert str(err) == CAP_MESSAGE.format(cap=5)
    # The existing cleanup path still removes the (empty) extraction root.
    assert not (tmp_path / "many").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_real_cap_rejects_without_creating_a_single_member(tmp_path, monkeypatch, kind):
    """Unpatched production cap: 10,001 files must be refused with zero creates.

    The spy refuses to create anything, so a regression to an in-loop-only check
    fails fast on the first create instead of writing 10,000 files.
    """
    cap = upload._MAX_ARCHIVE_MEMBERS
    assert cap == 10000
    attempted = []

    class _CreatedBeforeCap(Exception):
        pass

    def refuse(root, path, *args, **kwargs):
        attempted.append(str(path))
        raise _CreatedBeforeCap(str(path))

    monkeypatch.setattr(upload, "open_anchored_create_fd", refuse)
    members = _files(cap + 1)
    data = _make_zip(members) if kind == "zip" else _make_tar(members)

    try:
        upload.extract_archive(data, f"many.{kind}", tmp_path)
    except _CreatedBeforeCap:
        err = None
    except ValueError as exc:
        err = exc
    else:
        err = None

    assert attempted == [], (
        f"a member file create was attempted before the {cap}-member cap "
        f"rejected the archive: {attempted[:3]}"
    )
    assert err is not None and str(err) == CAP_MESSAGE.format(cap=cap)
    assert not (tmp_path / "many").exists()


def test_zip_at_cap_with_directories_extracts_every_file(tmp_path, monkeypatch):
    """Directory entries are not counted: exactly-cap files + dirs still extract."""
    monkeypatch.setattr(upload, "_MAX_ARCHIVE_MEMBERS", 5)
    created = _spy_creates(monkeypatch)
    members = [
        ("d1/", b""),
        ("d1/sub/", b""),
        ("d2/", b""),
        ("d1/a.txt", b"a"),
        ("d1/sub/b.txt", b"b"),
        ("d2/c.txt", b"c"),
        ("d.txt", b"d"),
        ("e.txt", b"e"),
    ]

    result = upload.extract_archive(_make_zip(members), "five.zip", tmp_path)

    assert result["extracted"] == 5
    assert len(created) == 5
    dest = tmp_path / "five"
    assert (dest / "d1" / "a.txt").read_bytes() == b"a"
    assert (dest / "d1" / "sub" / "b.txt").read_bytes() == b"b"
    assert (dest / "d2" / "c.txt").read_bytes() == b"c"
    assert (dest / "d.txt").read_bytes() == b"d"
    assert (dest / "e.txt").read_bytes() == b"e"


def test_tar_at_cap_with_directories_extracts_every_file(tmp_path, monkeypatch):
    """Tar counts regular files only (``isfile()``), matching the loop."""
    monkeypatch.setattr(upload, "_MAX_ARCHIVE_MEMBERS", 5)
    created = _spy_creates(monkeypatch)
    members = [("d1", None), ("d1/sub", None), ("d2", None)] + _files(5)

    result = upload.extract_archive(_make_tar(members), "five.tar", tmp_path)

    assert result["extracted"] == 5
    assert len(created) == 5
    for i in range(5):
        assert (tmp_path / "five" / f"f{i}.txt").read_bytes() == b"x"
