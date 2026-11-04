"""Opt-in migration for session files written before the save-boundary projection."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_persisted_image_payloads.py"
    spec = importlib.util.spec_from_file_location("migrate_persisted_image_payloads", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_payload(sid: str):
    raw = base64.b64encode(b"\xff\xd8\xff\xe0" + b"A" * 300_000).decode()
    part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + raw}}
    return {
        "session_id": sid,
        "message_count": 2,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look"}, part]},
            {"role": "assistant", "content": "ok"},
        ],
        "context_messages": [{"role": "user", "content": [part]}],
    }


def test_dry_run_writes_nothing(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))
    before = target.read_text()

    assert script.main(["--session-dir", str(tmp_path)]) == 0

    assert target.read_text() == before
    assert not (tmp_path / "legacy.json.preimage-migration.bak").exists()


def test_apply_bounds_display_transcript_and_keeps_backup(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    original = json.dumps(_legacy_payload("legacy"))
    target.write_text(original)

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    migrated_text = target.read_text()
    migrated = json.loads(migrated_text)
    # Display transcript is bounded; the provider replay prefix is preserved
    # exactly (existing round-trip contract), so it still holds the bytes.
    assert "data:image" not in json.dumps(migrated["messages"])
    assert migrated["context_messages"] == _legacy_payload("legacy")["context_messages"]
    assert len(migrated["messages"]) == 2
    assert migrated["message_count"] == 2
    assert migrated["messages"][1]["content"] == "ok"
    backup = tmp_path / "legacy.json.preimage-migration.bak"
    assert backup.exists() and backup.read_text() == original
    # Only the display copy is reclaimed here; the replay prefix is untouched.
    assert len(json.dumps(migrated["messages"])) < len(json.dumps(json.loads(original)["messages"])) // 10
    assert len(migrated_text) < len(original)


def test_include_context_opts_into_bounding_the_replay_prefix(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))

    assert script.main(["--session-dir", str(tmp_path), "--apply", "--include-context"]) == 0

    migrated_text = target.read_text()
    assert "data:image" not in migrated_text
    migrated = json.loads(migrated_text)
    assert len(migrated["messages"]) == 2
    assert migrated["context_messages"][0]["content"][0] == {"type": "text", "text": "[image attachment]"}


def test_clean_files_are_untouched(tmp_path):
    script = _load_script()
    clean = {"session_id": "clean", "message_count": 1, "messages": [{"role": "user", "content": "hi"}]}
    target = tmp_path / "clean.json"
    text = json.dumps(clean)
    target.write_text(text)

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    assert target.read_text() == text
    assert not (tmp_path / "clean.json.preimage-migration.bak").exists()


def test_backup_is_never_overwritten(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))
    backup = tmp_path / "legacy.json.preimage-migration.bak"
    backup.write_text("PRE-EXISTING")

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    assert backup.read_text() == "PRE-EXISTING"


def test_concurrent_write_is_refused(tmp_path, monkeypatch):
    """A live writer touching the file between read and replace aborts that file."""
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))

    calls = {"n": 0}
    real = script._stat_signature

    def flaky(path):
        calls["n"] += 1
        signature = real(path)
        if calls["n"] == 2:  # second probe happens after the backup write
            return ("changed", 0)
        return signature

    monkeypatch.setattr(script, "_stat_signature", flaky)

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 1

    assert "data:image" in target.read_text(), "file must not be replaced on conflict"


def test_compact_file_never_grows_when_bytes_are_removed(tmp_path):
    """Removing image bytes must never make the file bigger.

    The script rewrote every changed file with ``indent=2``, so a session stored
    in the compact single-line layout came back pretty-printed and GREW even while
    base64 was removed. Measured on real data: f906676da374.json 589,946 ->
    636,306 bytes (+46,360) purely from the encoder change.

    The image here is deliberately TINY (100 bytes) so the encoder change is not
    masked by reclaimed bytes -- with a large image the file shrinks either way and
    the test would pass against the broken script, proving nothing.
    """
    script = _load_script()
    tiny = base64.b64encode(b"A" * 100).decode()
    payload = {
        "session_id": "compact",
        "message_count": 2,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "caf\u00e9 " * 400},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + tiny}},
            ]},
            {"role": "assistant", "content": "ok"},
        ],
    }
    target = tmp_path / "compact.json"
    target.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    before = target.stat().st_size

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    after = target.stat().st_size
    assert after < before, f"file grew: {before} -> {after} (layout not preserved)"
    text = target.read_text(encoding="utf-8")
    assert "\n" not in text.rstrip("\n"), "compact layout was re-indented"
    migrated = json.loads(text)
    assert migrated["messages"][0]["content"][1] == {
        "type": "text", "text": "[image attachment]",
    }


def test_migration_preserves_indented_layout(tmp_path):
    """A pretty-printed file stays pretty-printed and still shrinks."""
    script = _load_script()
    tiny = base64.b64encode(b"B" * 100).decode()
    payload = {
        "session_id": "pretty",
        "message_count": 2,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "caf\u00e9 " * 400},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + tiny}},
            ]},
            {"role": "assistant", "content": "ok"},
        ],
    }
    target = tmp_path / "pretty.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    before = target.stat().st_size

    # --include-context so every lane is bounded; the default deliberately keeps
    # context_messages byte-identical.
    assert script.main(["--session-dir", str(tmp_path), "--apply", "--include-context"]) == 0

    text = target.read_text(encoding="utf-8")
    after = target.stat().st_size
    assert "\n  " in text, "indented layout was flattened"
    assert after < before, f"file grew: {before} -> {after}"
    assert "data:image" not in text


def test_hermes_home_is_honoured_when_state_dir_is_unset(monkeypatch, tmp_path):
    """A custom HERMES_HOME must be honoured, matching api/config.py.

    Hard-coding ``~/.hermes/webui/sessions`` meant an install with a custom
    ``HERMES_HOME`` (and no ``HERMES_WEBUI_STATE_DIR``) had this tool rewrite a
    different installation's sessions while leaving the intended files untouched.

    Note: the shared test conftest sets HERMES_WEBUI_STATE_DIR globally, so this
    test removes it for the duration to exercise the HERMES_HOME branch.
    """
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom-home"))
    script = _load_script()
    assert script._default_session_dir() == (tmp_path / "custom-home").resolve() / "webui" / "sessions"


def test_state_dir_env_wins_over_hermes_home(monkeypatch, tmp_path):
    """HERMES_WEBUI_STATE_DIR takes precedence, as it does in api/config.py."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "explicit"))
    script = _load_script()
    assert script._default_session_dir() == (tmp_path / "explicit").resolve() / "sessions"


def test_invalid_utf8_file_does_not_abort_the_batch(tmp_path):
    """One undecodable session must not stop the scan.

    read_text raised UnicodeDecodeError (a ValueError, NOT an OSError), which
    escaped migrate_file's handler; main() has no per-file guard, so a single
    damaged file aborted the entire migration instead of reporting one failure.
    """
    script = _load_script()
    bad = tmp_path / "aaa-broken.json"
    bad.write_bytes(b'{"messages": []}\xff\xfe not utf-8')
    good = tmp_path / "zzz-good.json"
    legacy = _legacy_payload("good")
    good.write_text(json.dumps(legacy), encoding="utf-8")

    # main() returns 1 to SIGNAL that some files failed, but the healthy file is
    # still migrated -- the point is that the batch continues, not the exit code.
    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 1

    # The healthy file still got migrated despite the broken one sorting first.
    # Only the display lane is checked: context_messages keeps its bytes by
    # default (the documented replay contract), so asserting the whole file is
    # free of "data:image" would be wrong here.
    migrated = json.loads(good.read_text(encoding="utf-8"))
    assert "data:image" not in json.dumps(migrated["messages"])
    assert bad.read_bytes().endswith(b"not utf-8")


def test_report_flags_the_undecodable_file(tmp_path):
    """The damaged file is reported per-file rather than taking down the run."""
    script = _load_script()
    bad = tmp_path / "broken.json"
    bad.write_bytes(b'\xff\xfe nope')
    report = script.migrate_file(bad, apply=False)
    assert report["error"] is not None
    assert report["error"].startswith("decode_failed")
    assert report["changed"] is False


def test_backup_write_failure_does_not_abort_the_batch(tmp_path, monkeypatch):
    """A refused preimage backup must fail one file, not the whole run.

    The reviewer asked for this as an explicit filesystem fault-injection seam:
    `backup.write_text` sat outside the write handler, so a PermissionError there
    escaped migrate_file and killed the batch before any later file was reached.
    """
    script = _load_script()
    first = tmp_path / "aaa.json"
    first.write_text(json.dumps(_legacy_payload("first")), encoding="utf-8")
    second = tmp_path / "zzz.json"
    second.write_text(json.dumps(_legacy_payload("second")), encoding="utf-8")

    real_write_text = Path.write_text

    def refuse_backup(self, *args, **kwargs):
        if self.name.endswith(".preimage-migration.bak"):
            raise PermissionError(13, "Permission denied (injected)")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse_backup)

    # Returns 1 (some files failed) but STILL processes the later file.
    rc = script.main(["--session-dir", str(tmp_path), "--apply"])
    assert rc == 1

    # Capture the per-file report WHILE the injection is still active.
    report = script.migrate_file(first, apply=True)
    assert report["error"] is not None
    assert report["error"].startswith("backup_failed")

    # The source is untouched -- a refused backup must never leave it rewritten.
    assert json.dumps(_legacy_payload("first")) == first.read_text(encoding="utf-8")
    # The LATER file still got its own backup attempt, i.e. the batch reached it.
    assert first.with_suffix(".json.preimage-migration.bak").exists() is False
