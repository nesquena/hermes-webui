import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.reflection_pipeline.run_event_checkpoint import run_checkpoint  # noqa: E402


def make_session(path: Path, session_id: str = "session_test") -> Path:
    data = {
        "session_id": session_id,
        "session_start": "2026-04-26T00:00:00",
        "last_updated": "2026-04-26T00:30:00",
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "please remember safe autonomy"},
            {"role": "assistant", "content": "ack"},
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_run_checkpoint_creates_candidate_archive_report_and_no_promotion(tmp_path):
    sessions = tmp_path / "sessions"
    archive = tmp_path / "archive"
    reflections = tmp_path / "reflections"
    report = tmp_path / "status" / "reflection_checkpoint_latest.json"
    sessions.mkdir()
    session = make_session(sessions / "session_test.json")

    result = run_checkpoint(
        session=session,
        archive_dir=archive,
        reflections_root=reflections,
        report_path=report,
        trigger="after-complex-task",
    )

    assert result["created_candidate"] is True
    assert Path(result["candidate"]).exists()
    assert Path(result["archive"]).exists()
    assert report.exists()
    report_data = json.loads(report.read_text())
    assert report_data["trigger"] == "after-complex-task"
    assert report_data["promotion_status"] == "candidate"
    assert report_data["auto_promoted"] is False
    assert not list((reflections / "promoted").glob("*.md"))


def test_run_checkpoint_is_idempotent_for_same_session(tmp_path):
    sessions = tmp_path / "sessions"
    archive = tmp_path / "archive"
    reflections = tmp_path / "reflections"
    report = tmp_path / "status" / "reflection_checkpoint_latest.json"
    sessions.mkdir()
    session = make_session(sessions / "session_test.json")

    first = run_checkpoint(session, archive, reflections, report, trigger="context-compression")
    second = run_checkpoint(session, archive, reflections, report, trigger="context-compression")

    assert first["candidate"] == second["candidate"]
    assert second["created_candidate"] is False
    assert len(list((reflections / "candidate").glob("*.candidate.md"))) == 1
    assert json.loads(report.read_text())["created_candidate"] is False
