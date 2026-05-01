from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import second_brain  # noqa: E402


def test_slugify_keeps_thai_and_ascii():
    assert second_brain.slugify("Yuto Second Brain ใช้งาน") == "yuto-second-brain-ใช้งาน"


def test_search_notes_finds_content(tmp_path):
    (tmp_path / "a.md").write_text("# Alpha\n\nEvidence first retrieval", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Beta\n\nOther", encoding="utf-8")

    hits = second_brain.search_notes("evidence retrieval", root=tmp_path)

    assert len(hits) == 1
    assert hits[0].path.name == "a.md"
    assert hits[0].line == 3


def test_create_note_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(second_brain, "KNOWLEDGE", tmp_path)

    path = second_brain.create_note("My Note", why="Useful", evidence="User request")

    assert path == tmp_path / "my-note.md"
    assert "Useful" in path.read_text(encoding="utf-8")
    try:
        second_brain.create_note("My Note")
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")
