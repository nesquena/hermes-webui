import json
from pathlib import Path


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "issue6611_regeneration_rows.json"
_EXPECTED_ROWS = [
    {"role": "user", "content": "same prompt"},
    {"role": "assistant", "content": "provider failed", "_error": True},
]


def load_issue6611_fixture():
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert set(fixture) == {"issue", "rows"}
    assert fixture["issue"] == 6611
    assert isinstance(fixture["rows"], list)
    assert fixture["rows"] == _EXPECTED_ROWS
    assert all(set(row) == {"role", "content"} for row in fixture["rows"][:1])
    assert set(fixture["rows"][1]) == {"role", "content", "_error"}
    assert [row["role"] for row in fixture["rows"]] == ["user", "assistant"]
    assert fixture["rows"][1]["_error"] is True
    return fixture
