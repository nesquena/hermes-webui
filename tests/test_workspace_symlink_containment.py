import pytest

from api.workspace import list_dir, read_file_content, safe_resolve_ws


def test_safe_resolve_blocks_external_symlink_directory(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    # The read/list gate still blocks navigation through the escape symlink.
    with pytest.raises(ValueError, match="Path traversal blocked"):
        safe_resolve_ws(workspace, "escape")

    with pytest.raises(ValueError, match="Path traversal blocked"):
        list_dir(workspace, "escape")

    # The escape symlink is now emitted (display-only) with target_outside_workspace=True.
    entries = {e["name"]: e for e in list_dir(workspace, ".")}
    assert "escape" in entries
    assert entries["escape"]["type"] == "symlink"
    assert entries["escape"]["target_outside_workspace"] is True
    # #4581 hardening: display-only escape rows are uniformly is_dir=False — the
    # target's real dir/file nature is target-derived metadata we don't disclose
    # (the row is non-navigable regardless).
    assert entries["escape"]["is_dir"] is False
    assert "target" not in entries["escape"]


def test_read_file_blocks_external_symlink_file(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "secret-link.txt").symlink_to(outside / "secret.txt")

    # The read gate still blocks reading through the escape symlink.
    with pytest.raises(ValueError, match="Path traversal blocked"):
        read_file_content(workspace, "secret-link.txt")

    # The escape symlink is now emitted (display-only) with target_outside_workspace=True.
    entries = {e["name"]: e for e in list_dir(workspace, ".")}
    assert "secret-link.txt" in entries
    assert entries["secret-link.txt"]["type"] == "symlink"
    assert entries["secret-link.txt"]["target_outside_workspace"] is True
    assert entries["secret-link.txt"]["is_dir"] is False


def test_internal_symlink_still_resolves_within_workspace(tmp_path):
    import api.workspace as w

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "inside-link.txt").symlink_to(nested / "inside.txt")

    resolved = safe_resolve_ws(workspace, "inside-link.txt")

    assert resolved == (nested / "inside.txt").resolve()
    assert read_file_content(workspace, "inside-link.txt")["content"] == "inside"
    if not w._DIR_FD_OK:
        pytest.skip("internal symlink listing is platform-dependent without dir_fd")
    assert "inside-link.txt" in {entry["name"] for entry in list_dir(workspace, ".")}


# ── TOCTOU hardening (#3398): a path that passes safe_resolve_ws() but is then
#    swapped to an external symlink before the open must not read/list/write
#    outside the workspace. The read/list/write paths use a portable anchored
#    openat-walk (openat + O_NOFOLLOW per component, dir_fd where supported). ──


def test_read_file_toctou_swap_to_external_symlink_blocked(tmp_path, monkeypatch):
    """If the resolved path is swapped to an external symlink AFTER the
    safe_resolve_ws() check, read_file_content must refuse, not follow the
    symlink and leak external content."""
    import api.workspace as w
    if not w._DIR_FD_OK:
        pytest.skip("TOCTOU symlink-swap hardening requires dir_fd support")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.txt").write_text("LEGIT", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET-LEAK", encoding="utf-8")

    real_resolve = w.safe_resolve_ws

    def racing_resolve(root, rel):
        p = real_resolve(root, rel)
        if rel == "data.txt":
            try:
                p.unlink()
            except OSError:
                pass
            p.symlink_to(outside / "secret.txt")
        return p

    monkeypatch.setattr(w, "safe_resolve_ws", racing_resolve)
    try:
        result = w.read_file_content(workspace, "data.txt")
        assert "SECRET" not in result["content"], "TOCTOU symlink swap leaked external content"
    except (FileNotFoundError, ValueError):
        pass  # refused — the correct outcome


def test_list_dir_toctou_swap_to_external_symlink_blocked(tmp_path, monkeypatch):
    """If a checked directory path is swapped to an external symlink after
    safe_resolve_ws(), list_dir must refuse rather than enumerate the external
    directory."""
    import api.workspace as w
    if not w._DIR_FD_OK:
        pytest.skip("TOCTOU symlink-swap hardening requires dir_fd support")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")

    real_resolve = w.safe_resolve_ws

    def racing_resolve(root, rel):
        p = real_resolve(root, rel)
        if rel == "sub":
            try:
                p.rmdir()
            except OSError:
                pass
            p.symlink_to(outside)
        return p

    monkeypatch.setattr(w, "safe_resolve_ws", racing_resolve)
    try:
        entries = w.list_dir(workspace, "sub")
        names = {e["name"] for e in entries}
        assert "secret.txt" not in names, "TOCTOU symlink swap leaked external dir listing"
    except (FileNotFoundError, ValueError):
        pass  # refused — the correct outcome


def test_anchored_create_blocks_symlinked_component(tmp_path):
    """open_anchored_create_fd must refuse to write through a symlinked path
    component (the upload / archive-extraction write race), landing nothing
    outside the workspace."""
    import api.workspace as w
    if not w._DIR_FD_OK:
        pytest.skip("anchored symlink-component rejection requires dir_fd support")
    from api.workspace import open_anchored_create_fd

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "evil").symlink_to(outside)  # symlinked intermediate dir

    with pytest.raises((FileNotFoundError, ValueError, OSError)):
        open_anchored_create_fd(workspace, (workspace / "evil" / "pwned.txt"))
    assert not (outside / "pwned.txt").exists()


def test_anchored_create_no_fd_leak_on_rejection(tmp_path):
    """Repeated rejected anchored creates must not leak file descriptors."""
    import os

    from api.workspace import open_anchored_create_fd

    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("fd-count check requires /proc/self/fd")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "evil").symlink_to(outside)

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(200):
        try:
            open_anchored_create_fd(workspace, (workspace / "evil" / "x.txt"))
        except Exception:
            pass
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before + 2, f"fd leak: before={before} after={after}"


def test_anchored_create_nested_autocreates_dirs(tmp_path):
    """A normal (non-escaping) nested create works and lands under the workspace."""
    import os

    from api.workspace import open_anchored_create_fd

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fd = open_anchored_create_fd(workspace, workspace / "a" / "b" / "file.txt")
    os.write(fd, b"hello")
    os.close(fd)
    assert (workspace / "a" / "b" / "file.txt").read_text() == "hello"


def test_rename_anchored_reports_destination_traversal(tmp_path):
    from api.workspace import rename_anchored

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    source = workspace / "inside.txt"
    source.write_text("inside", encoding="utf-8")
    dest = outside / "outside.txt"

    with pytest.raises(ValueError) as exc_info:
        rename_anchored(workspace, source, dest)
    assert str(dest) in str(exc_info.value)


def test_list_read_create_work_on_no_dir_fd_fallback(tmp_path, monkeypatch):
    """The no-dir_fd portability fallback (Windows path) must still list, read,
    and create within the workspace, and still hide/block external symlinks via
    the static safe_resolve_ws guard — no fd-relative API that would brick on
    platforms without os.supports_dir_fd."""
    import os

    import api.workspace as w

    monkeypatch.setattr(w, "_DIR_FD_OK", False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hi", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "internal").symlink_to(workspace / "sub")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("x", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    names = {e["name"] for e in w.list_dir(workspace, ".")}
    assert "a.txt" in names
    if w._DIR_FD_OK:
        assert "internal" in names          # legit internal symlink listed
    assert "escape" in names            # external symlink emitted (display-only)
    escape_entry = next(e for e in w.list_dir(workspace, ".") if e["name"] == "escape")
    assert escape_entry["target_outside_workspace"] is True
    assert w.read_file_content(workspace, "a.txt")["content"] == "hi"

    fd = w.open_anchored_create_fd(workspace, workspace / "new" / "f.txt")
    os.write(fd, b"ok")
    os.close(fd)
    assert (workspace / "new" / "f.txt").read_text() == "ok"


def test_read_blocked_when_workspace_root_raced_to_symlink(tmp_path):
    """If the workspace root itself is swapped to an external symlink after
    resolve() but before the anchored open, read_file_content must refuse
    (O_NOFOLLOW on the root open), not follow it and leak external content."""
    import os
    import shutil

    import api.workspace as w

    if not w._DIR_FD_OK:
        pytest.skip("anchored root-open race only applies on dir_fd platforms")

    outside = tmp_path / "evil"
    outside.mkdir()
    (outside / "f.txt").write_text("SECRET-LEAK", encoding="utf-8")
    wsroot = tmp_path / "wsroot"
    wsroot.mkdir()
    (wsroot / "f.txt").write_text("LEGIT", encoding="utf-8")

    real_open = os.open
    state = {"swapped": False}

    def racing_open(path, *args, **kwargs):
        if (not state["swapped"]) and "dir_fd" not in kwargs and str(path) == str(wsroot.resolve()):
            state["swapped"] = True
            shutil.rmtree(str(wsroot))
            os.symlink(str(outside), str(wsroot))
        return real_open(path, *args, **kwargs)

    os.open = racing_open
    try:
        try:
            result = w.read_file_content(wsroot, "f.txt")
            assert "SECRET" not in result["content"], "root-swap race leaked external content"
        except (FileNotFoundError, ValueError, NotADirectoryError, OSError):
            pass  # refused — correct
    finally:
        os.open = real_open


# ── #4510: escape-target symlinks are now emitted as display-only rows ──────
#    The escape filter was widened (not removed): symlinks whose resolved target
#    sits outside the workspace root are now emitted with
#    target_outside_workspace=True instead of being silently dropped. The
#    read/list gate (safe_resolve_ws / open_anchored_fd) is unchanged and still
#    blocks navigation through them. ──────────────────────────────────────────


def test_list_dir_in_workspace_symlink_shape(tmp_path):
    """In-workspace symlinks emit type='symlink' with target_outside_workspace=False."""
    import api.workspace as w

    if not w._DIR_FD_OK:
        pytest.skip("symlink listing requires dir_fd support")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.txt").write_text("hello", encoding="utf-8")
    (workspace / "link.txt").symlink_to(workspace / "data.txt")

    entries = {e["name"]: e for e in w.list_dir(workspace, ".")}
    assert "link.txt" in entries
    assert entries["link.txt"]["type"] == "symlink"
    assert entries["link.txt"]["target_outside_workspace"] is False
    assert entries["link.txt"]["is_dir"] is False
    assert entries["link.txt"]["target"] == str((workspace / "data.txt").resolve())


def test_list_dir_outside_workspace_symlink_emitted_with_flag(tmp_path):
    """Escape-target symlinks are emitted with target_outside_workspace=True."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "file.txt").write_text("external", encoding="utf-8")
    (workspace / "ext-link.txt").symlink_to(outside / "file.txt")

    entries = {e["name"]: e for e in list_dir(workspace, ".")}
    assert "ext-link.txt" in entries
    assert entries["ext-link.txt"]["type"] == "symlink"
    assert entries["ext-link.txt"]["target_outside_workspace"] is True
    assert entries["ext-link.txt"]["is_dir"] is False
    # #4581 hardening: a display-only escape-target row must NOT disclose where it
    # points — no resolved outside path, no target-derived size, no target-derived
    # metadata. Only the link name/path + the display-only flag are emitted.
    assert "target" not in entries["ext-link.txt"]
    assert "size" not in entries["ext-link.txt"]


def test_list_dir_external_symlink_blocked_system_path_unchanged(tmp_path):
    """Symlinks to blocked system paths (/etc, /usr) are still filtered out."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "etc-link").symlink_to("/etc")
    (workspace / "usr-link").symlink_to("/usr")

    names = {e["name"] for e in list_dir(workspace, ".")}
    assert "etc-link" not in names
    assert "usr-link" not in names


def test_list_dir_escape_symlink_read_still_blocked(tmp_path):
    """Listing shows the escape symlink (display-only) but read_file_content
    on the same target still raises ValueError — proving the read gate is intact."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape.txt").symlink_to(outside / "secret.txt")

    # Listing emits the entry with target_outside_workspace=True
    entries = {e["name"]: e for e in list_dir(workspace, ".")}
    assert "escape.txt" in entries
    assert entries["escape.txt"]["target_outside_workspace"] is True

    # But reading through it is still blocked
    with pytest.raises(ValueError, match="Path traversal blocked"):
        read_file_content(workspace, "escape.txt")
