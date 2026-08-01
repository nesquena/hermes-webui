from api.workspace import dir_signature, list_dir


def test_directory_signature_is_directory_metadata_only_and_changes_on_mutation(tmp_path):
    (tmp_path / "alpha.txt").write_text("one", encoding="utf-8")

    entries = list_dir(tmp_path, ".")["entries"]
    sig1 = dir_signature(tmp_path)

    assert isinstance(sig1, str)
    assert len(sig1) == 64
    assert all("mtime_ns" in entry for entry in entries)

    (tmp_path / "beta.txt").write_text("two", encoding="utf-8")
    list_dir(tmp_path, ".")
    sig2 = dir_signature(tmp_path)

    assert sig2 != sig1


def test_directory_signature_is_stable_across_pages(tmp_path):
    (tmp_path / "alpha.txt").write_text("one", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("two", encoding="utf-8")

    entries = list_dir(tmp_path, ".")["entries"]
    page_one = entries[:1]
    page_two = entries[1:]

    assert page_one and page_two
    assert dir_signature(tmp_path, ".", page_one) == dir_signature(tmp_path, ".", page_two)


def test_directory_signature_includes_followed_symlink_target_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("one", encoding="utf-8")
    link = workspace / "target-link.txt"
    link.symlink_to(target)

    before = dir_signature(workspace)
    target.write_text("changed target contents", encoding="utf-8")

    assert dir_signature(workspace) != before


def test_directory_signature_detects_same_stat_symlink_retarget(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target_a = tmp_path / "target-a.txt"
    target_b = tmp_path / "target-b.txt"
    target_a.write_text("same", encoding="utf-8")
    target_b.write_text("same", encoding="utf-8")
    link = workspace / "target-link.txt"
    link.symlink_to(target_a)

    before = dir_signature(workspace)
    link.unlink()
    link.symlink_to(target_b)

    assert dir_signature(workspace) != before
