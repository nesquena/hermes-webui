"""Tests: cron/skill file reads are size-guarded (bounded).

Regression: three handlers read whole operator-authored files into memory via
``read_text()`` with no ``st_size`` guard — inconsistent with the git-diff
``DIFF_SIZE_LIMIT`` pattern and the bounded log tail reader. A cron job (or
skill linked file) that produced a very large output file was loaded whole (and,
for the run-detail endpoint, fully serialized into the JSON response) before any
truncation. The cron-recent batch reader could read up to 500 such files fully.

Each site now checks ``st_size`` before reading: files at or under
``_FILE_READ_MAX_BYTES`` (512 KiB, mirroring DIFF_SIZE_LIMIT) are returned
verbatim; larger files are read only up to the cap and flagged ``truncated``.
The cron-recent batch also has a cumulative byte budget across its file list.
"""
from __future__ import annotations

import io
import json
import os
import sys
import types
from types import SimpleNamespace

import api.routes as routes
from api.routes import _FILE_READ_MAX_BYTES


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _stub_cron_jobs(monkeypatch, *, output_dir):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.OUTPUT_DIR = output_dir
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    return cron_jobs


# ── _read_text_bounded unit ─────────────────────────────────────────────────


def test_read_text_bounded_under_cap_returns_full(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("hello world", encoding="utf-8")
    text, truncated = routes._read_text_bounded(f)
    assert text == "hello world"
    assert truncated is False


def test_read_text_bounded_over_cap_returns_head_and_flag(tmp_path):
    f = tmp_path / "big.txt"
    payload = "A" * (_FILE_READ_MAX_BYTES + 4096)
    f.write_text(payload, encoding="utf-8")
    text, truncated = routes._read_text_bounded(f)
    assert truncated is True
    # Only up to the cap was read (never the full oversized payload).
    assert len(text.encode("utf-8")) <= _FILE_READ_MAX_BYTES
    assert len(text) < len(payload)


# ── cron run detail (single file) ────────────────────────────────────────────


def test_cron_run_detail_normal_file_returns_full_content(monkeypatch, tmp_path):
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    (out_dir / "run.md").write_text("## Response\nthe answer is 42\n", encoding="utf-8")
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_run_detail(
        handler, SimpleNamespace(query="job_id=job1&filename=run.md")
    )
    assert handler.status == 200
    body = _payload(handler)
    assert body["truncated"] is False
    assert "the answer is 42" in body["content"]


def test_cron_run_detail_oversized_file_is_truncated(monkeypatch, tmp_path):
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # Oversized cron output (well past the cap).
    (out_dir / "run.md").write_text(
        "## Response\n" + ("B" * (_FILE_READ_MAX_BYTES + 8192)), encoding="utf-8"
    )
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_run_detail(
        handler, SimpleNamespace(query="job_id=job1&filename=run.md")
    )
    assert handler.status == 200
    body = _payload(handler)
    assert body["truncated"] is True
    # The returned content was bounded, not the full oversized payload.
    # _read_cron_output_bounded composites a bounded head (frontmatter) + a
    # bounded tail (marker + body), de-duplicating the overlap so the body is
    # not doubled. Result is bounded to roughly 2 * _FILE_READ_MAX_BYTES.
    assert len(body["content"].encode("utf-8")) <= _FILE_READ_MAX_BYTES * 2 + 200, (
        f"content must be bounded; got {len(body['content'].encode('utf-8'))}"
    )
    assert "## Response" in body["content"], "the response marker must survive"


# ── cron recent (batch) ──────────────────────────────────────────────────────


def test_cron_output_normal_files_returned(monkeypatch, tmp_path):
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    for i in range(3):
        f = out_dir / f"run-{i}.md"
        f.write_text(f"## Response\noutput {i}\n", encoding="utf-8")
        os.utime(f, (1000 + i, 1000 + i))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    assert handler.status == 200
    body = _payload(handler)
    assert len(body["outputs"]) == 3
    assert body.get("truncated", False) is False


def test_cron_output_batch_byte_budget_truncates(monkeypatch, tmp_path):
    """Many large files exceed the cumulative batch budget → batch flagged
    truncated and reading stops before loading all of them."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # Create many files that together exceed _FILE_READ_MAX_BYTES * 4. Each file
    # is individually under the per-file cap so the per-file bound doesn't fire,
    # forcing the cumulative budget to be what truncates the batch.
    per_file = _FILE_READ_MAX_BYTES // 2
    n_files = 12  # total ~6x the batch budget
    for i in range(n_files):
        f = out_dir / f"run-{i}.md"
        f.write_text("## Response\n" + ("C" * per_file), encoding="utf-8")
        os.utime(f, (i, i))  # newest last
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=500")
    )
    assert handler.status == 200
    body = _payload(handler)
    # The budget stopped reading before all n_files were loaded.
    assert body.get("truncated") is True
    assert len(body["outputs"]) < n_files
    assert len(body["outputs"]) >= 1


# ── skill linked file ────────────────────────────────────────────────────────


def test_skill_linked_file_oversized_is_truncated(monkeypatch, tmp_path):
    """The skill linked-file read is bounded: an oversized file returns the head
    + a truncated flag instead of loading the whole file. The skill branch lives
    inside handle_get, so stub the skill-dir resolver and drive the
    /api/skills/content path directly (auth is enforced at the server level, not
    inside handle_get)."""
    skill_dir = tmp_path / "skills" / "myskill"
    skill_dir.mkdir(parents=True)
    big = skill_dir / "big.md"
    big.write_text("D" * (_FILE_READ_MAX_BYTES + 8192), encoding="utf-8")

    # Resolve the skill to our temp dir regardless of real settings.
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(
        routes,
        "_find_skill_in_dirs",
        lambda name, dirs: (skill_dir, skill_dir / "SKILL.md"),
    )

    handler = _JSONHandler()
    handler.headers = {}
    routes.handle_get(
        handler,
        SimpleNamespace(path="/api/skills/content", query="name=myskill&file=big.md"),
    )
    body = _payload(handler)
    assert "content" in body, f"unexpected payload: {body}"
    assert body["truncated"] is True
    # Only the bounded head was loaded, not the full oversized file.
    assert len(body["content"].encode("utf-8")) <= _FILE_READ_MAX_BYTES


def test_skill_linked_file_normal_returns_full(monkeypatch, tmp_path):
    """A normal-size skill linked file returns full content, not truncated."""
    skill_dir = tmp_path / "skills" / "myskill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "notes.md").write_text("# Notes\nsmall skill file body\n", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(
        routes,
        "_find_skill_in_dirs",
        lambda name, dirs: (skill_dir, skill_dir / "SKILL.md"),
    )

    handler = _JSONHandler()
    handler.headers = {}
    routes.handle_get(
        handler,
        SimpleNamespace(path="/api/skills/content", query="name=myskill&file=notes.md"),
    )
    body = _payload(handler)
    assert body["truncated"] is False
    assert "small skill file body" in body["content"]


# ── cron ## Response head-vs-tail policy ──────────────────────────────────────


def _write_cron_output_with_huge_prompt(path):
    """Write a cron output whose ## Prompt section alone exceeds the cap, with
    ## Response (the section the UI wants) at the END of the file."""
    prompt_blob = "P" * (_FILE_READ_MAX_BYTES + 50000)
    path.write_text(
        f"# Cron Run\nmodel: x\n\n## Prompt\n{prompt_blob}\n\n"
        "## Response\nThis is the actual reply the UI wants to show.\n",
        encoding="utf-8",
    )
    return path


def test_cron_run_detail_oversized_preserves_response_section(monkeypatch, tmp_path):
    """Regression: cron output puts ## Response at the END. A pure head cap drops
    it when the prompt section is large, leaving the snippet to serve prompt
    bytes. _read_cron_output_bounded must fall back to a tail read so the
    response survives."""
    from api.routes import _read_cron_output_bounded, _cron_output_snippet

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    fpath = _write_cron_output_with_huge_prompt(out_dir / "run.md")
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Direct helper check: the bounded read preserves ## Response.
    txt, truncated = _read_cron_output_bounded(fpath)
    assert truncated is True
    assert "## Response" in txt
    snippet = _cron_output_snippet(txt)
    assert "actual reply the UI wants to show" in snippet, (
        "snippet must show the response, not prompt bytes"
    )


def test_cron_run_detail_oversized_response_survives_in_handler(monkeypatch, tmp_path):
    """End-to-end: /api/crons/run-detail on a file whose prompt exceeds the cap
    returns a snippet containing the response, not prompt bytes."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    _write_cron_output_with_huge_prompt(out_dir / "run.md")
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_run_detail(
        handler, SimpleNamespace(query="job_id=job1&filename=run.md")
    )
    assert handler.status == 200
    body = _payload(handler)
    assert body["truncated"] is True
    assert "actual reply the UI wants to show" in body["snippet"]


def test_cron_output_batch_surfaces_per_file_truncation(monkeypatch, tmp_path):
    """Regression: a single file > 512 KiB that still fits under the batch budget
    used to have its per-file truncation flag silently discarded. The batch
    output entry must now carry `truncated: true` so a client can tell that
    file's content was clipped."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # One file over the per-file cap (within the batch budget), with ## Response
    # at the end so it takes the head-then-tail path.
    _write_cron_output_with_huge_prompt(out_dir / "run-big.md")
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    assert handler.status == 200
    body = _payload(handler)
    assert len(body["outputs"]) == 1
    entry = body["outputs"][0]
    assert entry["filename"] == "run-big.md"
    # Per-file truncation is surfaced on the entry (not just the batch flag).
    assert entry.get("truncated") is True
    # And the response content survived (head-then-tail bias).
    assert "actual reply the UI wants to show" in entry["content"]


def test_read_text_bounded_tail_mode_reads_trailing_bytes(tmp_path):
    """_read_text_bounded(tail=True) reads the trailing max_bytes and drops the
    partial line at the seek boundary — the primitive the cron head-then-tail
    policy relies on."""
    from api.routes import _read_text_bounded

    p = tmp_path / "t.txt"
    # 10 lines, each a complete marker. Cap at a small tail window.
    p.write_text("".join(f"line{i:02d}marker\n" for i in range(10)), encoding="utf-8")
    text, truncated = _read_text_bounded(p, max_bytes=40, tail=True)
    assert truncated is True
    # The last line must be present (it's within the tail window).
    assert "line09marker" in text
    # No partial leading fragment (the seek-boundary line was dropped): the
    # first retained line is a complete marker (strip CR for CRLF portability).
    first_line = text.split("\n", 1)[0].rstrip("\r")
    assert first_line.startswith("line") and first_line.endswith("marker"), (
        f"first line looks like a partial fragment: {first_line!r}"
    )


# ── head+tail preserves frontmatter/usage (regression) ────────────────────────


def test_cron_run_detail_oversized_preserves_usage_and_response(monkeypatch, tmp_path):
    """Regression (reviewer): when the prompt exceeds the cap and ## Response is
    at the end, a tail-ONLY read dropped the frontmatter/usage block, so the
    detail endpoint returned an empty usage object. The head+tail composite
    preserves BOTH: usage parses from the head frontmatter, and the response
    parses from the tail."""
    from api.routes import _read_cron_output_bounded, _cron_output_usage_metadata

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    prompt_blob = "P" * (_FILE_READ_MAX_BYTES + 50000)
    # Real cron output format the usage parser expects (bolded **Model:** etc.).
    (out_dir / "run.md").write_text(
        "# Cron Run\n\n**Model:** gpt-5\n**Tokens:** 1000 input, 500 output\n\n"
        f"## Prompt\n{prompt_blob}\n\n## Response\nThe actual reply.\n",
        encoding="utf-8",
    )
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    fpath = out_dir / "run.md"
    txt, truncated = _read_cron_output_bounded(fpath)
    assert truncated is True
    # The response survives (tail).
    assert "## Response" in txt
    assert "actual reply" in txt
    # The usage block survives (head frontmatter) — this was the regression.
    usage = _cron_output_usage_metadata(txt)
    assert usage.get("model") == "gpt-5", f"usage model lost: {usage}"
    assert usage.get("input_tokens") == 1000, f"usage tokens lost: {usage}"

    # End-to-end through the detail handler.
    handler = _JSONHandler()
    routes._handle_cron_run_detail(
        handler, SimpleNamespace(query="job_id=job1&filename=run.md")
    )
    body = _payload(handler)
    assert body["truncated"] is True
    assert body["usage"].get("model") == "gpt-5"
    assert "actual reply" in body["snippet"]


def test_cron_output_batch_newest_oversized_file_still_returned(monkeypatch, tmp_path):
    """Regression (reviewer): the batch endpoint charged the full on-disk
    st_size against the budget before the bounded read, so a single oversized
    NEWEST file exceeded the remaining budget and was skipped entirely (blank
    output). The fix charges the BOUNDED read size and always admits at least
    the newest file, so the newest run is never blank."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # One oversized file (well over the per-file cap) with a real response marker.
    prompt_blob = "P" * (_FILE_READ_MAX_BYTES * 3)
    (out_dir / "run-newest.md").write_text(
        f"## Prompt\n{prompt_blob}\n\n## Response\nNewest run reply.\n",
        encoding="utf-8",
    )
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    assert handler.status == 200
    body = _payload(handler)
    # The newest file is returned (not skipped) with a bounded preview.
    assert len(body["outputs"]) >= 1, f"newest file was skipped: {body}"
    entry = body["outputs"][0]
    assert entry["filename"] == "run-newest.md"
    assert "Newest run reply." in entry["content"], (
        f"newest file's response missing from batch output: {entry}"
    )
    assert entry.get("truncated") is True


def test_cron_run_detail_response_marker_split_at_head_boundary(monkeypatch, tmp_path):
    """Regression (reviewer round 2): when the seek splits the ## Response marker
    line so neither head nor tail has it intact, the composite must re-inject the
    marker so the snippet shows the reply body. Byte-exact padding so the marker
    straddles the cap (the round-1 fixture was off by 7 — it included '# Cron\\n'
    in the content but not in the filler-length math, so the marker landed past
    the cap rather than straddling it)."""
    from api.routes import _read_cron_output_bounded, _cron_output_snippet

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    marker = "## Response\n"
    # Byte-exact: frontmatter + filler + marker where filler fills exactly to
    # the cap, so the marker starts AT the cap and straddles it (split marker).
    frontmatter = "# Cron\n"
    filler_len = _FILE_READ_MAX_BYTES - len(frontmatter.encode("utf-8"))
    content = frontmatter + ("F" * filler_len) + marker + "Split-marker reply body.\n"
    (out_dir / "run.md").write_text(content, encoding="utf-8")
    fpath = out_dir / "run.md"
    txt, truncated = _read_cron_output_bounded(fpath)
    assert truncated is True
    snippet = _cron_output_snippet(txt)
    assert "Split-marker reply body." in snippet, (
        f"response body lost at split-marker boundary: snippet={snippet!r}"
    )


def test_cron_run_detail_marker_ends_exactly_at_boundary(monkeypatch, tmp_path):
    """Regression (reviewer round 3): when the COMPLETE ## Response heading ends
    EXACTLY at the head cap (preceded by a newline so it's recognized, but zero
    body bytes follow it in the head), marker presence used to trigger an early
    head-only return → snippet: "(empty)". The fix tail-splices the body when the
    head has the marker but no body, de-duplicating the marker so it appears once."""
    from api.routes import _read_cron_output_bounded, _cron_output_snippet

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    marker_b = b"## Response\n"
    body = "Reply body entirely past the boundary.".encode("utf-8")
    frontmatter = b"# Cron\n"
    pre_marker_newline = b"\n"  # so _cron_response_marker_index recognizes it
    # frontmatter + filler + newline + marker == exactly cap bytes (marker ends at cap)
    filler = b"F" * (_FILE_READ_MAX_BYTES - len(frontmatter) - len(pre_marker_newline) - len(marker_b))
    head_portion = frontmatter + filler + pre_marker_newline + marker_b
    assert len(head_portion) == _FILE_READ_MAX_BYTES, len(head_portion)
    (out_dir / "run.md").write_bytes(head_portion + body + b"\n")
    fpath = out_dir / "run.md"
    txt, truncated = _read_cron_output_bounded(fpath)
    assert truncated is True
    snippet = _cron_output_snippet(txt)
    # Body survives (was "(empty)" before the round-3 fix).
    assert "Reply body entirely past the boundary." in snippet, (
        f"response body lost when marker ends exactly at boundary: snippet={snippet!r}"
    )
    # Marker appears exactly once (de-duplicated, not duplicated by the splice).
    assert txt.count("## Response") == 1, (
        f"marker duplicated in splice: count={txt.count('## Response')}"
    )





# ── Gate blocker regressions (2026-07-23 RED gate) ────────────────────────────


def test_read_text_bounded_stat_failure_never_returns_unbounded(monkeypatch, tmp_path):
    """Blocker 1: if stat() raises, _read_text_bounded must NOT fall back to an
    unbounded read_text(). The previous shape returned the whole >512 KiB file
    with truncated=False on a stat failure — the exact OOM path the PR exists to
    close. Now it opens once + fstat on the descriptor + always capped read."""
    from pathlib import Path
    from api.routes import _read_text_bounded

    big = tmp_path / "big.txt"
    big.write_text("x" * (_FILE_READ_MAX_BYTES + 5000))
    real_stat = Path.stat

    def failing_stat(self, *a, **k):
        if self == big:
            raise OSError("simulated stat failure")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", failing_stat)
    text, trunc = _read_text_bounded(big)
    # Must NOT return the whole file. Either empty (open also failed) or bounded.
    assert len(text) <= _FILE_READ_MAX_BYTES, (
        f"stat failure must not trigger unbounded read; got {len(text)} bytes"
    )


def test_read_text_bounded_always_caps_oversized_file(tmp_path):
    """Blocker 1: an oversized file is always read at most max_bytes, never
    whole (no matter the stat/open race outcome)."""
    from api.routes import _read_text_bounded

    big = tmp_path / "big.txt"
    big.write_text("y" * (_FILE_READ_MAX_BYTES + 10000))
    text, trunc = _read_text_bounded(big)
    assert trunc is True
    assert len(text) <= _FILE_READ_MAX_BYTES


def test_read_cron_output_bounded_body_survives_marker_at_boundary(tmp_path):
    """Blocker 2: when ## Response sits near the head cap with minimal body in
    the head, the response body at EOF must survive (not be reduced to a prefix).

    Constructs a file where the prompt section pushes ## Response + a byte of
    body into the head, with the real body continuing at EOF. The previous
    head-only early return yielded snippet: 'R'; now the tail-splice preserves
    the full body."""
    from api.routes import _read_cron_output_bounded, _cron_output_snippet

    marker = "## Response\n\n"
    # Push the marker near the cap so only ~1 body byte lands in the head.
    prompt_len = _FILE_READ_MAX_BYTES - len(marker) - 1
    content = (
        "## Prompt\n" + ("P" * (prompt_len - 20)) + "\n" + marker
        + "R" + ("EST_OF_BODY_LINE\n" * 50)
    )
    f = tmp_path / "cron.md"
    f.write_text(content)
    text, trunc = _read_cron_output_bounded(f)
    snippet = _cron_output_snippet(text)
    # The snippet must contain body content well beyond the single 'R' prefix.
    assert "BODY" in snippet or "EST_OF_BODY" in snippet, (
        f"response body must survive the cap splice; snippet={snippet!r}"
    )


def test_read_cron_output_bounded_no_body_duplication(tmp_path):
    """Blocker 2 de-dup: when head and tail both contain ## Response (overlap),
    the body must not be duplicated in the spliced result."""
    from api.routes import _read_cron_output_bounded

    # File just over 1 cap so head and tail overlap heavily.
    content = "## Response\n" + ("B" * (_FILE_READ_MAX_BYTES + 8192))
    f = tmp_path / "cron.md"
    f.write_text(content)
    text, trunc = _read_cron_output_bounded(f)
    # Exactly one ## Response marker (the head's partial body was dropped in
    # favor of the tail's complete marker + body).
    assert text.count("## Response") == 1, (
        f"marker/body must not duplicate on overlap; count="
        f"{text.count('## Response')}"
    )


def test_cron_batch_charges_budget_only_after_successful_read(monkeypatch, tmp_path):
    """Blocker 3: the batch budget must be charged AFTER a successful read
    appends an entry, not before. Two unreadable large files previously
    consumed the budget via `spent += charge` in their except branches, so a
    later valid older file hit the cap and returned outputs: []."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # Two "unreadable" large files (we'll make _read_cron_output_bounded raise
    # on them) + one valid smaller file that should still appear.
    big_a = out_dir / "run-2.md"
    big_a.write_text("## Response\n" + ("A" * (_FILE_READ_MAX_BYTES * 2)), encoding="utf-8")
    big_b = out_dir / "run-1.md"
    big_b.write_text("## Response\n" + ("B" * (_FILE_READ_MAX_BYTES * 2)), encoding="utf-8")
    valid = out_dir / "run-0.md"
    valid.write_text("## Response\nVALID_OLDER_OUTPUT\n", encoding="utf-8")
    # run-1 is newest (mtime highest), run-2 middle, run-0 oldest.
    os.utime(big_a, (100, 100))
    os.utime(big_b, (200, 200))
    os.utime(valid, (300, 300))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Make the two big files "unreadable" by patching _read_cron_output_bounded
    # to raise on them, simulating the OSError path that previously consumed
    # budget without producing an entry.
    original = routes._read_cron_output_bounded

    def failing_for_big(path, *a, **kw):
        if path.name in ("run-1.md", "run-2.md"):
            raise OSError("simulated unreadable")
        return original(path, *a, **kw)

    monkeypatch.setattr(routes, "_read_cron_output_bounded", failing_for_big)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)
    # The valid older file's output must still appear despite the two unreadable
    # newer files consuming budget-attempted reads.
    contents = [e.get("content", "") for e in body.get("outputs", [])]
    assert any("VALID_OLDER_OUTPUT" in c for c in contents), (
        f"valid older output must survive unreadable newer files; got {contents}"
    )
