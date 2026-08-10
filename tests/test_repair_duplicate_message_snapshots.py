import pytest

from scripts.repair_duplicate_message_snapshots import repair


def test_repair_rejects_same_source_and_destination_without_truncating(tmp_path):
    source = tmp_path / "session.json"
    original = '{"message_count":1,"messages":[{"id":1,"role":"user","content":"hello"}]}'
    source.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="different"):
        repair(source, source)

    assert source.read_text(encoding="utf-8") == original
