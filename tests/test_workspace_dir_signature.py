from api.workspace import dir_signature, list_dir


def test_directory_signature_is_metadata_only_and_changes_with_entries(tmp_path):
    (tmp_path / "alpha.txt").write_text("one", encoding="utf-8")

    entries = list_dir(tmp_path, ".")
    sig1 = dir_signature(tmp_path, ".", entries)

    assert isinstance(sig1, str)
    assert len(sig1) == 64
    assert all("mtime_ns" in entry for entry in entries)

    (tmp_path / "beta.txt").write_text("two", encoding="utf-8")
    entries2 = list_dir(tmp_path, ".")
    sig2 = dir_signature(tmp_path, ".", entries2)

    assert sig2 != sig1


def test_directory_signature_can_be_computed_from_supplied_entries(tmp_path):
    (tmp_path / "alpha.txt").write_text("one", encoding="utf-8")

    entries = list_dir(tmp_path, ".")

    assert dir_signature(tmp_path, ".", entries) == dir_signature(tmp_path, ".", entries)
