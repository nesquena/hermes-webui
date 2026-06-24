"""Regression tests for the Claude Code transcript parse cache (#4718/#4662).

The sidebar/profile-switch cold path was dominated by re-parsing every Claude
Code JSONL transcript on each /api/sessions build. ``_parse_claude_code_jsonl``
is now memoized by the file's (path, mtime_ns, size) so a warm build re-stats
instead of re-parsing, while any genuine edit transparently invalidates just
the changed file. These tests pin that behavior.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _rows(text: str = "hello") -> list:
    return [
        {"summary": "Cache QA"},
        {"timestamp": "2026-04-18T12:00:01Z", "message": {"role": "user", "content": text}},
        {"timestamp": "2026-04-18T12:00:02Z", "message": {"role": "assistant", "content": "ok"}},
    ]


def test_parse_cache_hit_skips_reparse(tmp_path, monkeypatch):
    import api.models as models

    models.clear_claude_code_parse_cache()
    fixture = tmp_path / "claude" / "projects" / "p" / "s.jsonl"
    _write_jsonl(fixture, _rows())

    calls = {"n": 0}
    real = models._parse_claude_code_jsonl

    def _counting(path, **kw):
        calls["n"] += 1
        return real(path, **kw)

    monkeypatch.setattr(models, "_parse_claude_code_jsonl", _counting)

    first = models._parse_claude_code_jsonl_cached(fixture)
    second = models._parse_claude_code_jsonl_cached(fixture)

    # Second call is served from cache: underlying parser ran exactly once.
    assert calls["n"] == 1
    assert first == second
    assert first[0][0]["content"] == "hello"


def test_parse_cache_invalidates_on_content_change(tmp_path, monkeypatch):
    import api.models as models

    models.clear_claude_code_parse_cache()
    fixture = tmp_path / "claude" / "projects" / "p" / "s.jsonl"
    _write_jsonl(fixture, _rows("first"))

    calls = {"n": 0}
    real = models._parse_claude_code_jsonl

    def _counting(path, **kw):
        calls["n"] += 1
        return real(path, **kw)

    monkeypatch.setattr(models, "_parse_claude_code_jsonl", _counting)

    first = models._parse_claude_code_jsonl_cached(fixture)
    assert first[0][0]["content"] == "first"

    # Rewrite with different content + a guaranteed-distinct mtime/size so the
    # stat signature changes and the cache must miss.
    time.sleep(0.01)
    _write_jsonl(fixture, _rows("second-edition-longer"))
    import os
    st = fixture.stat()
    os.utime(fixture, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    second = models._parse_claude_code_jsonl_cached(fixture)

    assert calls["n"] == 2  # re-parsed after the edit
    assert second[0][0]["content"] == "second-edition-longer"


def test_parse_cache_returns_independent_message_lists(tmp_path):
    """A caller mutating the returned list must not corrupt the cached entry."""
    import api.models as models

    models.clear_claude_code_parse_cache()
    fixture = tmp_path / "claude" / "projects" / "p" / "s.jsonl"
    _write_jsonl(fixture, _rows())

    first_msgs, *_ = models._parse_claude_code_jsonl_cached(fixture)
    first_msgs.append({"role": "user", "content": "injected"})

    second_msgs, *_ = models._parse_claude_code_jsonl_cached(fixture)
    assert not any(m.get("content") == "injected" for m in second_msgs)


def test_parse_cache_is_bounded(tmp_path, monkeypatch):
    import api.models as models

    models.clear_claude_code_parse_cache()
    monkeypatch.setattr(models, "_CLAUDE_CODE_PARSE_CACHE_MAX", 5)

    for i in range(12):
        f = tmp_path / "claude" / "projects" / "p" / f"s{i}.jsonl"
        _write_jsonl(f, _rows(f"msg-{i}"))
        models._parse_claude_code_jsonl_cached(f)

    assert len(models._CLAUDE_CODE_PARSE_CACHE) <= 5


def test_parse_cache_handles_missing_file(tmp_path):
    import api.models as models

    models.clear_claude_code_parse_cache()
    missing = tmp_path / "nope.jsonl"
    # Must not raise; matches the empty-tuple contract of the uncached parser.
    assert models._parse_claude_code_jsonl_cached(missing) == ([], None, None, None)


def test_get_claude_code_sessions_warm_uses_cache(tmp_path, monkeypatch):
    """End-to-end: a 2nd get_claude_code_sessions() does not re-parse files."""
    import api.models as models

    models.clear_claude_code_parse_cache()
    projects_dir = tmp_path / "claude" / "projects"
    for i in range(3):
        _write_jsonl(projects_dir / f"proj{i}" / "s.jsonl", _rows(f"row-{i}"))

    calls = {"n": 0}
    real = models._parse_claude_code_jsonl

    def _counting(path, **kw):
        calls["n"] += 1
        return real(path, **kw)

    monkeypatch.setattr(models, "_parse_claude_code_jsonl", _counting)

    cold = models.get_claude_code_sessions(projects_dir=projects_dir)
    cold_calls = calls["n"]
    warm = models.get_claude_code_sessions(projects_dir=projects_dir)

    assert cold_calls == 3            # parsed each file once on the cold build
    assert calls["n"] == cold_calls   # warm build added zero re-parses
    assert [s["title"] for s in cold] == [s["title"] for s in warm]
