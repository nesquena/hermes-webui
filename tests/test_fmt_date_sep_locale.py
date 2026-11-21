"""Transcript date separators follow the active locale.

The sidebar already labels today and yesterday through
session_time_bucket_today and session_time_bucket_yesterday. Those keys
exist in every locale. _fmtDateSep must use the same keys instead of the
English words Today and Yesterday.
"""

import json
import os
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


# ── #9204: weekday / short-date separators follow the interface language ────
#
# Beyond today/yesterday, _fmtDateSep renders two more bands:
#   * days 2-6 back  -> weekday name   (weekday:'long')
#   * older / future -> short date      (month:'short', day:'numeric'[, year])
# Both used to pass a bare ``[]`` (the browser default locale) to
# toLocaleDateString, so a German UI inside an English browser still showed
# "Monday" / "Nov 12" (and the reverse for an English UI in a German browser).
# The fix routes the active interface locale -- ``_locale._speech``, the same
# BCP 47 tag setLocale() applies and boot.js already feeds to
# SpeechRecognition.lang -- into both calls, falling back to ``[]`` only when
# that tag is unset or not a valid BCP 47 tag (an invalid tag would otherwise
# throw a RangeError out of the render path).

GERMAN_WEEKDAYS = {
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
}
ENGLISH_WEEKDAYS = {
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
}


def _fmt_date_sep_source() -> str:
    return _extract_function(UI_JS.read_text(encoding="utf-8"), "_fmtDateSep")


def _run_sep(fn_source: str, *, speech_tag, browser_locale: str, offsets) -> dict:
    """Drive the real _fmtDateSep under a controlled interface + browser locale.

    ``speech_tag`` is injected as the module-global ``_locale._speech`` -- the
    tag ``setLocale()`` applied (``None`` leaves ``_locale`` undefined, the
    state before i18n.js boots). ``browser_locale`` becomes the node process's
    ``LC_ALL``/``LANG``, i.e. what a bare ``[]`` locale resolves to.

    Every returned row carries the separator *and* reference renderings pinned
    to the literal ``de-DE`` / ``en-US`` tags plus the browser default. Those
    references are computed independently of the implementation's own locale
    source, so the assertions cannot pass tautologically -- if _fmtDateSep
    ignored the interface locale, ``sep`` would equal the browser reference and
    fail the "not browser" / "not the other language" checks below.
    """
    if speech_tag is None:
        locale_prelude = "var _locale = undefined;"
    else:
        locale_prelude = "var _locale = { _speech: %s };" % json.dumps(speech_tag)
    script = textwrap.dedent(
        f"""
        function t(key) {{
          if (key === 'session_time_bucket_today') return 'LOCALE_TODAY';
          if (key === 'session_time_bucket_yesterday') return 'LOCALE_YESTERDAY';
          return 'MISSING:' + key;
        }}
        {locale_prelude}
        {fn_source}
        const base = new Date(); base.setHours(12, 0, 0, 0);
        // Mirror _fmtDateSep's own year rule so a reference short-date matches
        // even when the offset crosses a year boundary (e.g. early January).
        function shortStr(dd, tag) {{
          const o = {{month:'short', day:'numeric'}};
          if (base.getFullYear() !== dd.getFullYear()) o.year = 'numeric';
          return dd.toLocaleDateString(tag, o);
        }}
        const offsets = {json.dumps(offsets)};
        const rows = {{}};
        for (const off of offsets) {{
          const d = new Date(base.getTime());
          d.setDate(d.getDate() - off);
          const dd = new Date(d); dd.setHours(0, 0, 0, 0);
          rows[String(off)] = {{
            sep: _fmtDateSep(d),
            de_weekday: dd.toLocaleDateString(['de-DE'], {{weekday:'long'}}),
            en_weekday: dd.toLocaleDateString(['en-US'], {{weekday:'long'}}),
            de_short: shortStr(dd, ['de-DE']),
            en_short: shortStr(dd, ['en-US']),
            browser_weekday: dd.toLocaleDateString([], {{weekday:'long'}}),
            browser_short: shortStr(dd, []),
          }};
        }}
        process.stdout.write(JSON.stringify(rows));
        """
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("NODE_CHANNEL_FD", "HERMES_WEBUI_TEST_STATE_DIR")
    }
    env["LC_ALL"] = browser_locale
    env["LANG"] = browser_locale
    env["TMPDIR"] = "/tmp"
    proc = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, env=env
    )
    return json.loads(proc.stdout)


def test_weekday_band_follows_interface_locale_not_browser():
    """German UI in an English browser -> German weekday, not "Monday"."""
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag="de-DE",
        browser_locale="en_US.UTF-8",
        offsets=[2, 3, 6],  # lower edge, interior, upper edge of the 2..6 band
    )
    for off, row in rows.items():
        assert row["sep"] in GERMAN_WEEKDAYS, (
            f"offset {off}: {row['sep']!r} is not a German weekday"
        )
        assert row["sep"] == row["de_weekday"], (
            f"offset {off}: {row['sep']!r} != German {row['de_weekday']!r}"
        )
        assert row["sep"] != row["en_weekday"]
        assert row["sep"] != row["browser_weekday"]  # no silent browser fallback


def test_short_date_band_follows_interface_locale_not_browser():
    """German UI in an English browser -> German short date, not "Nov 12"."""
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag="de-DE",
        browser_locale="en_US.UTF-8",
        offsets=[7, 30],  # 7 = first day past the weekday band
    )
    for off, row in rows.items():
        assert row["sep"] == row["de_short"], (
            f"offset {off}: {row['sep']!r} != German {row['de_short']!r}"
        )
        assert row["sep"] != row["en_short"], (
            f"offset {off}: {row['sep']!r} is the English short date"
        )
        assert row["sep"] != row["browser_short"]
        assert row["sep"] not in GERMAN_WEEKDAYS  # it is a date, not a weekday


def test_reverse_english_interface_in_german_browser():
    """English UI in a German browser -> English weekday/date, not "Dienstag"."""
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag="en-US",
        browser_locale="de_DE.UTF-8",
        offsets=[3, 30],
    )
    weekday = rows["3"]
    assert weekday["sep"] in ENGLISH_WEEKDAYS
    assert weekday["sep"] == weekday["en_weekday"]
    assert weekday["sep"] != weekday["de_weekday"]
    assert weekday["sep"] != weekday["browser_weekday"]
    short = rows["30"]
    assert short["sep"] == short["en_short"]
    assert short["sep"] != short["de_short"]
    assert short["sep"] != short["browser_short"]


def test_future_timestamp_uses_short_date_band():
    """A future timestamp (diffDays < 0) falls through to the short-date band.

    Pins the semantics the maintainer flagged as untested: a future date is not
    a weekday -- it renders through the older-date branch, in the interface
    language.
    """
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag="de-DE",
        browser_locale="en_US.UTF-8",
        offsets=[-2],
    )
    row = rows["-2"]
    assert row["sep"] == row["de_short"]
    assert row["sep"] != row["en_short"]
    assert row["sep"] not in GERMAN_WEEKDAYS
    assert row["sep"] not in ENGLISH_WEEKDAYS


def test_invalid_locale_tag_falls_back_without_range_error():
    """An invalid BCP 47 tag must not throw a RangeError out of the render.

    toLocaleDateString throws RangeError on an invalid tag; the fix validates
    the interface locale and falls back to the browser default instead of
    crashing the transcript render.
    """
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag="not a valid tag!!",
        browser_locale="en_US.UTF-8",
        offsets=[3, 30],
    )  # reaching here at all proves node did not throw
    assert rows["3"]["sep"] == rows["3"]["browser_weekday"]
    assert rows["30"]["sep"] == rows["30"]["browser_short"]


def test_missing_locale_falls_back_to_browser():
    """No active interface locale (pre-boot) -> browser default, no crash."""
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag=None,
        browser_locale="en_US.UTF-8",
        offsets=[3, 30],
    )
    assert rows["3"]["sep"] == rows["3"]["browser_weekday"]
    assert rows["30"]["sep"] == rows["30"]["browser_short"]


def test_today_yesterday_paths_unchanged():
    """The t()-backed today/yesterday separators are untouched by the fix."""
    rows = _run_sep(
        _fmt_date_sep_source(),
        speech_tag="de-DE",
        browser_locale="en_US.UTF-8",
        offsets=[0, 1],
    )
    assert rows["0"]["sep"] == "LOCALE_TODAY"
    assert rows["1"]["sep"] == "LOCALE_YESTERDAY"
