from pathlib import Path
import hashlib
import json
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


def test_cocoindex_health_detects_missing_stale_and_orphan(tmp_path, monkeypatch):
    knowledge = tmp_path / "knowledge"
    index = tmp_path / "index"
    knowledge.mkdir()
    index.mkdir()
    fresh = knowledge / "fresh.md"
    stale = knowledge / "stale.md"
    missing = knowledge / "missing.md"
    fresh.write_text("# Fresh\n", encoding="utf-8")
    stale.write_text("# Stale\n", encoding="utf-8")
    missing.write_text("# Missing\n", encoding="utf-8")
    monkeypatch.setattr(second_brain, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(second_brain, "COCOINDEX_OUT", index)

    fresh_digest = hashlib.sha256(fresh.read_bytes()).hexdigest()
    (index / "fresh.md.json").write_text(
        json.dumps({"path": "fresh.md", "sha256": fresh_digest}), encoding="utf-8"
    )
    (index / "stale.md.json").write_text(
        json.dumps({"path": "stale.md", "sha256": "old"}), encoding="utf-8"
    )
    (index / "deleted.md.json").write_text(
        json.dumps({"path": "deleted.md", "sha256": "old"}), encoding="utf-8"
    )

    health = second_brain.cocoindex_health()

    assert health["ok"] is False
    assert health["source_notes"] == 3
    assert health["derived_json_files"] == 3
    assert health["missing"] == ["missing.md"]
    assert health["stale"] == ["stale.md"]
    assert health["orphan_derived"] == ["deleted.md.json"]


def test_cocoindex_search_finds_body_text(tmp_path, monkeypatch):
    index = tmp_path / "index"
    index.mkdir()
    monkeypatch.setattr(second_brain, "COCOINDEX_OUT", index)
    (index / "japan.md.json").write_text(
        json.dumps(
            {
                "path": "japan.md",
                "title": "Japan Target",
                "headings": [],
                "wikilinks": [],
                "body": "# Japan Target\n\nJapan's AI harm evidence layer is the strategic product goal.",
            }
        ),
        encoding="utf-8",
    )

    hits = second_brain.search_cocoindex_metadata("AI harm evidence", limit=5)

    assert len(hits) == 1
    assert hits[0].path == "japan.md"
    assert hits[0].field == "body"
    assert hits[0].line == 3
    assert "evidence layer" in hits[0].preview


def test_recent_sessions_uses_raw_session_mtime_and_query(tmp_path):
    older = tmp_path / "session_older.json"
    newer = tmp_path / "session_newer.json"
    older.write_text(
        json.dumps(
            {
                "session_id": "older",
                "last_updated": "2026-05-12T03:00:00",
                "message_count": 1,
                "messages": [{"role": "user", "content": "kernel_task"}],
            }
        ),
        encoding="utf-8",
    )
    newer.write_text(
        json.dumps(
            {
                "session_id": "newer",
                "last_updated": "2026-05-12T03:45:00",
                "message_count": 2,
                "messages": [{"role": "user", "content": "AI-Books Book Expert Factory ล่าสุด"}],
            }
        ),
        encoding="utf-8",
    )
    older.touch()
    newer.touch()

    hits = second_brain.recent_sessions(limit=3, query="AI-Books Book", sessions_dir=tmp_path)

    assert [hit.session_id for hit in hits] == ["newer"]
    assert "AI-Books" in hits[0].preview
    assert hits[0].matched_terms == ["ai-books", "book"]


def test_memory_demote_candidates_finds_long_entries(tmp_path):
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "Short pointer.\n§\n"
        "This is a very long active-memory entry that should be demoted because it contains too much detail "
        "instead of staying as a compact pointer for future recall and retrieval.\n",
        encoding="utf-8",
    )

    candidates = second_brain.memory_demote_candidates(memory_file, min_chars=120)

    assert len(candidates) == 1
    assert candidates[0].index == 2
    assert candidates[0].recommendation == "demote detail to knowledge note; replace with short pointer"


def test_memory_palace_search_and_doctor(tmp_path):
    existing = tmp_path / "knowledge" / "rules.md"
    existing.parent.mkdir()
    existing.write_text("# Rules", encoding="utf-8")
    palace = tmp_path / "memory-palace.json"
    palace.write_text(
        json.dumps(
            [
                {
                    "palace_id": "ke-iprefs-core",
                    "wing": "user",
                    "room": "preferences",
                    "title": "Kei preferences",
                    "summary": "Thai and evidence-first preferences",
                    "paths": [str(existing)],
                    "commands": ["python tools/second_brain.py palace search preferences"],
                    "tags": ["preferences", "thai"],
                }
            ]
        ),
        encoding="utf-8",
    )

    hits = second_brain.search_memory_palace("evidence preferences", path=palace)
    health = second_brain.memory_palace_doctor(palace)

    assert hits[0].palace_id == "ke-iprefs-core"
    assert health["ok"] is True
    assert health["entries"] == 1


def test_capture_status_uses_quarantine_doctor(tmp_path, monkeypatch):
    quarantine = tmp_path / ".memory-quarantine"
    monkeypatch.setattr(second_brain, "MEMORY_QUARANTINE", quarantine)

    health = second_brain.capture_status()

    assert health["ok"] is True
    assert health["root"] == str(quarantine)
    assert health["counts"]["tool_error"] == 0


def test_capture_items_lists_quarantine_records(tmp_path, monkeypatch):
    quarantine = tmp_path / ".memory-quarantine"
    monkeypatch.setattr(second_brain, "MEMORY_QUARANTINE", quarantine)
    from tools.memory_capture.capture import capture_worker_receipt

    captured = capture_worker_receipt(
        root=quarantine,
        session_id="s1",
        project="demo",
        agent="qa",
        lane="qa-critic",
        task_id="t1",
        summary="OK",
        verification_status="pass",
    )

    items = second_brain.capture_items(kind="worker_receipt")

    assert [item["item_id"] for item in items] == [captured["item_id"]]


def test_capture_promote_routes_to_memory_capture(tmp_path, monkeypatch):
    quarantine = tmp_path / ".memory-quarantine"
    knowledge = tmp_path / "knowledge"
    monkeypatch.setattr(second_brain, "MEMORY_QUARANTINE", quarantine)
    monkeypatch.setattr(second_brain, "KNOWLEDGE", knowledge)
    from tools.memory_capture.capture import capture_worker_receipt

    captured = capture_worker_receipt(
        root=quarantine,
        session_id="s2",
        project="demo",
        agent="qa",
        lane="qa-critic",
        task_id="t2",
        summary="Second brain promote works",
        verification_status="pass",
    )

    promoted = second_brain.capture_promote(captured["item_id"], reviewer="yuto", rationale="tested")

    assert promoted["promotion_status"] == "promoted_to_kg_draft"
    assert Path(promoted["path"]).exists()
