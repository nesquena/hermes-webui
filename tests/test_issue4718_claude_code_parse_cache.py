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


def test_epoch_zero_timestamps_fall_back_to_mtime(tmp_path):
    """A transcript whose timestamps parse to 0.0 still gets a real mtime fallback.

    The cached row-builder guards the mtime fallback with ``not first_ts and not
    last_ts`` so a falsy-but-not-None ``0.0`` timestamp (epoch-0 / 1970
    transcript) falls back to the file mtime, matching the pre-cache inline
    ``first_ts or last_ts or path.stat().st_mtime``. An identity (``is None``)
    guard would have left these rows with ``None``.
    """
    import api.models as models

    models.clear_claude_code_parse_cache()
    projects_dir = tmp_path / "claude" / "projects"
    fixture = projects_dir / "p" / "s.jsonl"
    # All message timestamps are epoch 0 -> _parse_claude_code_timestamp -> 0.0.
    rows = [
        {"summary": "Epoch QA"},
        {"timestamp": "1970-01-01T00:00:00Z", "message": {"role": "user", "content": "hi"}},
        {"timestamp": "1970-01-01T00:00:00Z", "message": {"role": "assistant", "content": "ok"}},
    ]
    _write_jsonl(fixture, rows)

    sessions = models.get_claude_code_sessions(projects_dir=projects_dir)
    assert len(sessions) == 1
    s = sessions[0]
    # Must NOT be None — falls back to the file mtime (a real positive float).
    assert s["created_at"] is not None
    assert s["updated_at"] is not None
    assert s["last_message_at"] is not None
    assert s["created_at"] > 0


def test_parse_cache_dicts_are_read_only_contract(tmp_path):
    """Pin the load-bearing invariant: per-message dicts are SHARED across hits.

    The cache returns a shallow ``list(messages)`` copy, so the per-message dicts
    are shared between calls. Every production caller treats them as read-only;
    this test documents that contract by proving the sharing exists — a future
    caller that mutates a returned dict in place would corrupt the cache, and
    this test makes that sharing explicit so such a change is a conscious one.
    """
    import api.models as models

    models.clear_claude_code_parse_cache()
    fixture = tmp_path / "claude" / "projects" / "p" / "s.jsonl"
    _write_jsonl(fixture, _rows())

    first_msgs, *_ = models._parse_claude_code_jsonl_cached(fixture)
    second_msgs, *_ = models._parse_claude_code_jsonl_cached(fixture)
    # List wrappers are distinct copies (append isolation, covered above)...
    assert first_msgs is not second_msgs
    # ...but the dict objects are shared (the read-only contract). If a future
    # change deep-copies on read, update this assertion deliberately.
    assert first_msgs[0] is second_msgs[0]


def test_parse_cache_invalidates_on_same_size_mtime_ctime_edit(tmp_path, monkeypatch):
    """A same-size, same-mtime in-place edit still misses the cache via ctime_ns."""
    import os
    import api.models as models

    models.clear_claude_code_parse_cache()
    fixture = tmp_path / "claude" / "projects" / "p" / "s.jsonl"
    _write_jsonl(fixture, _rows("aaaaa"))

    calls = {"n": 0}
    real = models._parse_claude_code_jsonl

    def _counting(path, **kw):
        calls["n"] += 1
        return real(path, **kw)

    monkeypatch.setattr(models, "_parse_claude_code_jsonl", _counting)

    first = models._parse_claude_code_jsonl_cached(fixture)
    assert first[0][0]["content"] == "aaaaa"
    st = fixture.stat()

    # Rewrite with same byte length and force the SAME mtime back; the os.utime
    # call still stamps ctime with the current wall clock, so a real in-place
    # edit moves ctime even when size+mtime are unchanged. Sleep so that ctime is
    # measurably distinct from the original mtime_ns we restore.
    time.sleep(0.02)
    _write_jsonl(fixture, _rows("bbbbb"))
    os.utime(fixture, ns=(st.st_atime_ns, st.st_mtime_ns))
    st2 = fixture.stat()
    assert st2.st_size == st.st_size
    assert st2.st_mtime_ns == st.st_mtime_ns
    assert st2.st_ctime_ns != st.st_ctime_ns  # only ctime moved (the edit signal)

    second = models._parse_claude_code_jsonl_cached(fixture)
    assert calls["n"] == 2  # re-parsed despite identical size+mtime (ctime caught it)
    assert second[0][0]["content"] == "bbbbb"
