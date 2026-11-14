"""Transcript date separators follow the active locale.

The sidebar already labels today and yesterday through
session_time_bucket_today and session_time_bucket_yesterday. Those keys
exist in every locale. _fmtDateSep must use the same keys instead of the
English words Today and Yesterday.
"""

import json
import pathlib
import subprocess
import textwrap

REPO = pathlib.Path(__file__).parent.parent.resolve()
UI_JS = REPO / "static" / "ui.js"


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Could not extract {name}")


def _run(fn_source: str) -> dict:
    script = textwrap.dedent(
        f"""
        function t(key) {{
          if (key === 'session_time_bucket_today') return 'LOCALE_TODAY';
          if (key === 'session_time_bucket_yesterday') return 'LOCALE_YESTERDAY';
          return 'MISSING:' + key;
        }}
        {fn_source}
        const today = new Date();
        today.setHours(12, 0, 0, 0);
        const yesterday = new Date(today.getTime());
        yesterday.setDate(yesterday.getDate() - 1);
        const older = new Date(today.getTime());
        older.setDate(older.getDate() - 3);
        process.stdout.write(JSON.stringify({{
          today: _fmtDateSep(today),
          yesterday: _fmtDateSep(yesterday),
          older: _fmtDateSep(older),
        }}));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(proc.stdout)


def test_fmt_date_sep_today_yesterday_follow_locale():
    fn = _extract_function(UI_JS.read_text(encoding="utf-8"), "_fmtDateSep")
    result = _run(fn)
    assert result["today"] == "LOCALE_TODAY"
    assert result["yesterday"] == "LOCALE_YESTERDAY"
    assert result["older"] not in (
        "LOCALE_TODAY",
        "LOCALE_YESTERDAY",
        "Today",
        "Yesterday",
    )
