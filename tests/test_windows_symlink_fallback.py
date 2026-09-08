import pathlib

import tests.conftest as conftest


def test_link_or_copy_falls_back_to_copy(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "skill.md").write_text("hello", encoding="utf-8")
    dest = tmp_path / "dest"

    def fail_symlink(self, target, target_is_directory=False):
        raise OSError("required privilege is not held")

    monkeypatch.setattr(pathlib.Path, "symlink_to", fail_symlink)

    conftest._link_path_or_copy(src, dest)

    assert dest.exists()
    assert (dest / "skill.md").read_text(encoding="utf-8") == "hello"
