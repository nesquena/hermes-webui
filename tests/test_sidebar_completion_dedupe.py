import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _extract_js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"could not extract {name}")


def test_mark_session_completed_in_list_dedupes_old_and_new_sid_rows():
    sessions_src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    fn_src = _extract_js_function(sessions_src, "_markSessionCompletedInList")
    script = f"""
let _allSessions = [
  {{ session_id: 'old', title: 'Before', message_count: 10, pre_compression_snapshot: true }},
  {{ session_id: 'new', title: 'After', message_count: 2, parent_session_id: 'old' }},
];
let _sessionStreamingById = new Map([['old', true], ['new', true]]);
let _sessionListSnapshotById = new Map([['old', {{message_count: 10}}]]);
let _sessionListSourceById = new Map([['old', 'webui'], ['new', 'webui']]);
function _forgetObservedStreamingSession(_sid) {{}}
function renderSessionListFromCache() {{}}
{fn_src}
_markSessionCompletedInList({{
  session_id: 'new',
  title: 'After done',
  message_count: 3,
  parent_session_id: 'old',
}}, 'old');
console.log(JSON.stringify({{
  rows: _allSessions,
  oldStreaming: _sessionStreamingById.has('old'),
  oldSourceTracked: _sessionListSourceById.has('old'),
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    payload = json.loads(result.stdout)

    assert [row["session_id"] for row in payload["rows"]] == ["new"]
    assert payload["rows"][0]["title"] == "After done"
    assert payload["rows"][0]["message_count"] == 3
    assert payload["oldStreaming"] is False
    assert payload["oldSourceTracked"] is False
