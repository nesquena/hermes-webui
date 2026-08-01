from api.workspace import dir_signature, list_dir


def test_directory_signature_is_directory_metadata_only_and_changes_on_mutation(tmp_path):
    (tmp_path / "alpha.txt").write_text("one", encoding="utf-8")

    entries = list_dir(tmp_path, ".")["entries"]
    sig1 = dir_signature(tmp_path)

    assert isinstance(sig1, str)
    assert len(sig1) == 64
    assert all("mtime_ns" in entry for entry in entries)

    (tmp_path / "beta.txt").write_text("two", encoding="utf-8")
    entries2 = list_dir(tmp_path, ".")["entries"]
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
    target = tmp_path / "target.txt"
    target.write_text("one", encoding="utf-8")
    link = tmp_path / "target-link.txt"
    link.symlink_to(target)

    before = dir_signature(tmp_path)
    target.write_text("changed target contents", encoding="utf-8")

    assert dir_signature(tmp_path) != before
