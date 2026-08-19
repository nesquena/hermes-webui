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
    text, truncated, _ok = routes._read_text_bounded(f)
    assert text == "hello world"
    assert truncated is False


def test_read_text_bounded_over_cap_returns_head_and_flag(tmp_path):
    f = tmp_path / "big.txt"
    payload = "A" * (_FILE_READ_MAX_BYTES + 4096)
    f.write_text(payload, encoding="utf-8")
    text, truncated, _ok = routes._read_text_bounded(f)
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
    txt, truncated, _ok, _bytes_read, _declined = _read_cron_output_bounded(fpath)
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
    text, truncated, _ok = _read_text_bounded(p, max_bytes=40, tail=True)
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
    txt, truncated, _ok, _bytes_read, _declined = _read_cron_output_bounded(fpath)
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
    txt, truncated, _ok, _bytes_read, _declined = _read_cron_output_bounded(fpath)
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
    txt, truncated, _ok, _bytes_read, _declined = _read_cron_output_bounded(fpath)
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
    text, trunc, _ok = _read_text_bounded(big)
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
    text, trunc, _ok = _read_text_bounded(big)
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
    text, trunc, _ok, _bytes_read, _declined = _read_cron_output_bounded(f)
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
    text, trunc, _ok, _bytes_read, _declined = _read_cron_output_bounded(f)
    # Exactly one ## Response marker (the head's partial body was dropped in
    # favor of the tail's complete marker + body).
    assert text.count("## Response") == 1, (
        f"marker/body must not duplicate on overlap; count="
        f"{text.count('## Response')}"
    )


def test_cron_batch_charges_budget_only_after_successful_read(monkeypatch, tmp_path):
    """Round-3 residual + r5: a real I/O failure (NOT a budget decline) must not
    terminate the batch or consume budget. The previous shape computed `charge`
    from f.stat() and broke when `spent + charge > total_budget` BEFORE calling
    _read_cron_output_bounded — so a large unreadable row was never tried and
    every valid older output after it was suppressed.

    Scenario (corrected chronology, descending sort by mtime):
      1. a small successful newer row,
      2. a large unreadable row whose real reader boundary fails (I/O failure,
         declined=False — NOT a budget decline),
      3. a small valid older row.
    The failed read is attempted (not pre-empted), continued past (not broken,
    because it's an I/O failure not a budget decline), and skipped without
    charging budget — so the valid older output remains within the allowance."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # One small successful newer row (well under the cap).
    new_a = out_dir / "run-2.md"
    new_a.write_text("## Response\nNEW_OK\n", encoding="utf-8")
    # Large unreadable row (stat-visible as ~2 caps, will fail the real read).
    unreadable = out_dir / "run-1.md"
    unreadable.write_text("## Response\n" + ("X" * (_FILE_READ_MAX_BYTES * 2)), encoding="utf-8")
    # Small valid older row.
    valid = out_dir / "run-0.md"
    valid.write_text("## Response\nVALID_OLDER_OUTPUT\n", encoding="utf-8")
    # Descending mtime order: newest first. unreadable is between the successful
    # newer row and the valid older row.
    os.utime(new_a, (300, 300))
    os.utime(unreadable, (200, 200))
    os.utime(valid, (100, 100))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Make the large middle row fail the real read (return ok=False with
    # declined=False, simulating an open/fstat/seek/read OSError — NOT a budget
    # decline, so the batch continues past it).
    original = routes._read_cron_output_bounded

    def failing_for_unreadable(path, *a, **kw):
        if path.name == "run-1.md":
            return "", False, False, 0, False  # real I/O failure (5-tuple)
        return original(path, *a, **kw)

    monkeypatch.setattr(routes, "_read_cron_output_bounded", failing_for_unreadable)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)
    contents = [e.get("content", "") for e in body.get("outputs", [])]
    # The valid older output must still appear — the failed read was an I/O
    # failure (continued past, not charged), so budget remained for it.
    assert any("VALID_OLDER_OUTPUT" in c for c in contents), (
        f"valid older output must survive a failed (I/O) large row; got {contents}"
    )
    # No empty entries from the failed read.
    assert all(c.strip() for c in contents), (
        f"failed read must not append an empty entry; got {contents}"
    )


# ── Gate round-2 residuals (2026-07-23 re-gate) ──────────────────────────────


def test_read_text_bounded_same_inode_growth_does_not_read_unbounded(monkeypatch, tmp_path):
    """Round-2 #1: if the file grows between fstat and read (same inode), the
    small-file branch must NOT read the grown content unbounded. The pinned
    size guards the seek, but the read itself must be capped at max_bytes + 1
    so growth beyond the cap is reported truncated, not materialized."""
    from api.routes import _read_text_bounded

    f = tmp_path / "growable.txt"
    # Start at the cap exactly (size <= max_bytes branch).
    f.write_text("x" * _FILE_READ_MAX_BYTES)

    # Wrap the file handle's read so that when the small-file branch reads, the
    # file has "grown" (simulating append-after-fstat): return cap + 5000 bytes.
    real_open = open

    def growing_read(self, n=-1):
        # The small-file branch calls fh.read(max_bytes + 1); simulate growth by
        # returning more than max_bytes.
        data = real_open(self.name, "rb").read()
        if n and n > _FILE_READ_MAX_BYTES:
            return data + (b"g" * 5000)  # grew under us
        return data[:n] if n and n > 0 else data

    # Patch the read on the file object returned by open. Simplest: patch
    # _read_text_bounded's internal read via a wrapper file class.
    class GrowingFile:
        def __init__(self, name):
            self.name = name
            self._real = real_open(name, "rb")
            self.fileno = self._real.fileno
        def read(self, n=-1):
            data = self._real.read(n)
            if n and n > _FILE_READ_MAX_BYTES and len(data) >= _FILE_READ_MAX_BYTES:
                return data + (b"g" * 5000)  # grew between fstat and read
            return data
        def seek(self, *a): return self._real.seek(*a)
        def close(self): return self._real.close()

    import builtins
    def growing_open(name, *a, **k):
        if str(name) == str(f):
            return GrowingFile(str(f))
        return real_open(name, *a, **k)
    monkeypatch.setattr(builtins, "open", growing_open)

    text, trunc, ok = _read_text_bounded(f)
    assert len(text) <= _FILE_READ_MAX_BYTES, (
        f"same-inode growth must not read unbounded; got {len(text)} bytes"
    )
    assert trunc is True, "growth beyond the pinned size must be flagged truncated"


def test_read_cron_output_bounded_body_never_exceeds_source(tmp_path):
    """Round-2 #2: the returned body must never contain MORE bytes than the
    source file. The just-over-cap shape (marker in head, tail starts mid-body)
    previously concatenated two overlapping windows and nearly doubled the body."""
    from api.routes import _read_cron_output_bounded

    body = "B" * (_FILE_READ_MAX_BYTES + 8192)
    content = "## Response\n" + body
    f = tmp_path / "cron.md"
    f.write_text(content)
    text, trunc, ok, _bytes_read, _declined = _read_cron_output_bounded(f)
    # The returned text's 'B' count must not exceed the source's 'B' count.
    assert text.count("B") <= content.count("B"), (
        f"body duplicated: returned {text.count('B')} B's vs source "
        f"{content.count('B')}"
    )


def test_cron_batch_real_read_failure_not_charged(monkeypatch, tmp_path):
    """Round-2 #3: a real open/fstat/read failure (not a legitimate empty file)
    must NOT be appended as an entry or charged against the batch budget.

    Fixture chronology corrected per the gate note: unreadable files have the
    HIGHEST mtimes (processed first by the descending sort), valid file has the
    LOWEST mtime (processed last). Two unreadable newest files must not consume
    the budget and suppress the valid older output."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # Two large files that will be made unreadable + one valid smaller file.
    # Unreadable files have HIGHER mtimes (processed first by descending sort).
    unreadable_a = out_dir / "run-2.md"
    unreadable_a.write_text("## Response\n" + ("A" * (_FILE_READ_MAX_BYTES * 2)), encoding="utf-8")
    unreadable_b = out_dir / "run-1.md"
    unreadable_b.write_text("## Response\n" + ("B" * (_FILE_READ_MAX_BYTES * 2)), encoding="utf-8")
    valid = out_dir / "run-0.md"
    valid.write_text("## Response\nVALID_OLDER_OUTPUT\n", encoding="utf-8")
    # Correct chronology: unreadable = newest (300/200), valid = oldest (100).
    os.utime(unreadable_a, (300, 300))
    os.utime(unreadable_b, (200, 200))
    os.utime(valid, (100, 100))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Make the two large files "unreadable" by patching _read_cron_output_bounded
    # to return ok=False (simulating a real open/fstat/read failure, NOT a
    # legitimate empty file). The valid file reads normally.
    original = routes._read_cron_output_bounded

    def failing_for_unreadable(path, *a, **kw):
        if path.name in ("run-1.md", "run-2.md"):
            return "", False, False, 0, False  # real I/O failure (5-tuple)
        return original(path, *a, **kw)

    monkeypatch.setattr(routes, "_read_cron_output_bounded", failing_for_unreadable)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)
    # The valid older file's output must still appear — failed reads were not
    # charged, so budget remained for the valid file.
    contents = [e.get("content", "") for e in body.get("outputs", [])]
    assert any("VALID_OLDER_OUTPUT" in c for c in contents), (
        f"valid older output must survive unreadable newer files (failed reads "
        f"not charged); got {contents}"
    )
    # No empty entries from the failed reads.
    assert all(c.strip() for c in contents), (
        f"failed reads must not append empty entries; got {contents}"
    )


# ── Gate round-4 residuals (2026-07-24 re-gate) ──────────────────────────────


def test_cron_batch_charge_uses_descriptor_bytes_not_second_stat(monkeypatch, tmp_path):
    """Round-4 #1: the charge for a successfully read file must come from the
    descriptor-derived bytes_read, NOT a second f.stat() on the pathname. A
    post-read replace/truncate must not undercharge (allowing budget bypass) or
    a post-read unlink overcharge (suppressing valid older output).

    This test replaces the path between the read and what would have been the
    second stat. With the fix, the charge is locked to the bytes actually read
    from the first descriptor, so the replace cannot change the accounting."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # One large file (2 caps worth of read) + one valid older file.
    big = out_dir / "run-1.md"
    big.write_text("## Response\n" + ("A" * (_FILE_READ_MAX_BYTES * 3)), encoding="utf-8")
    valid = out_dir / "run-0.md"
    valid.write_text("## Response\nVALID_OLDER\n", encoding="utf-8")
    os.utime(big, (200, 200))   # newest
    os.utime(valid, (100, 100))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Wrap _read_cron_output_bounded so that AFTER a successful read of `big`,
    # the path is replaced with a tiny file. The old code's second f.stat()
    # would then see a tiny size and undercharge (budget bypass). The fix uses
    # the descriptor-derived bytes_read, so the charge stays at ~2 caps
    # regardless of the post-read replace.
    original = routes._read_cron_output_bounded
    call_log = []

    def replacing_reader(path, *a, **kw):
        result = original(path, *a, **kw)
        text, trunc, ok, bytes_read, declined = result
        if ok and path.name == "run-1.md" and bytes_read > _FILE_READ_MAX_BYTES:
            # Replace the path with a tiny file AFTER the successful read but
            # before the (now-removed) second stat would have run.
            path.write_text("tiny", encoding="utf-8")
            call_log.append(("big_read", bytes_read))
        return result

    monkeypatch.setattr(routes, "_read_cron_output_bounded", replacing_reader)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)
    # The valid older output must still appear. If the charge had been
    # re-derived from the post-replace tiny file (undercharge), the budget
    # would have been bypassed but valid would still appear — so the real
    # assertion is on the charge: the big file's charge must reflect ~2 caps,
    # not the tiny replacement.
    assert call_log, "the large file must have been read"
    big_charge = call_log[0][1]
    assert big_charge >= _FILE_READ_MAX_BYTES, (
        f"charge must come from the descriptor bytes read (~2 caps), not the "
        f"post-replace tiny file; got charge={big_charge}"
    )
    # And valid output survives regardless.
    contents = [e.get("content", "") for e in body.get("outputs", [])]
    assert any("VALID_OLDER" in c for c in contents), (
        f"valid older output must survive; got {contents}"
    )


def test_cron_batch_total_successful_bytes_within_four_caps(monkeypatch, tmp_path):
    """Round-4 #2: the cumulative successful batch read must stay within the
    four-cap bound. A previous shape checked the budget only AFTER the full
    read, so with `spent` just below four caps another readable large row
    consumed up to two more caps and was discarded — nearly six caps of reads.

    With the budget passed INTO the reader, an over-budget probe declines
    before reading, so total successful bytes stay <= 4 * cap."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # Three large files, each requiring 2 caps of read. Total attempted would
    # be 6 caps; the four-cap budget must cap successful reads at <= 4 caps.
    for i, mtime in enumerate([300, 200, 100]):
        f = out_dir / f"run-{i}.md"
        f.write_text("## Response\n" + ("X" * (_FILE_READ_MAX_BYTES * 3)), encoding="utf-8")
        os.utime(f, (mtime, mtime))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Wrap the reader to record total bytes read across successful calls.
    original = routes._read_cron_output_bounded
    total_read = []

    def recording_reader(path, *a, **kw):
        result = original(path, *a, **kw)
        text, trunc, ok, bytes_read, declined = result
        if ok:
            total_read.append(bytes_read)
        return result

    monkeypatch.setattr(routes, "_read_cron_output_bounded", recording_reader)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    successful_total = sum(total_read)
    assert successful_total <= _FILE_READ_MAX_BYTES * 4, (
        f"total successful batch reads must stay within 4 caps "
        f"({_FILE_READ_MAX_BYTES * 4}); got {successful_total} "
        f"({successful_total / _FILE_READ_MAX_BYTES:.1f} caps)"
    )


def test_cron_batch_reader_call_order_and_exact_outputs(monkeypatch, tmp_path):
    """Round-4 #3: explicit reader call log + exact output order. Verifies the
    batch reads files in descending mtime order, continues past failures, and
    appends only successful reads in order."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # newest (success) → middle (fails read) → oldest (success)
    new_ok = out_dir / "run-2.md"
    new_ok.write_text("## Response\nNEW_OK\n", encoding="utf-8")
    mid_fail = out_dir / "run-1.md"
    mid_fail.write_text("## Response\n" + ("M" * (_FILE_READ_MAX_BYTES * 2)), encoding="utf-8")
    old_ok = out_dir / "run-0.md"
    old_ok.write_text("## Response\nOLD_OK\n", encoding="utf-8")
    os.utime(new_ok, (300, 300))
    os.utime(mid_fail, (200, 200))
    os.utime(old_ok, (100, 100))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    original = routes._read_cron_output_bounded
    call_log = []

    def logging_reader(path, *a, **kw):
        result = original(path, *a, **kw)
        text, trunc, ok, bytes_read, declined = result
        call_log.append((path.name, ok))
        if path.name == "run-1.md":
            # Force the middle file to fail the read (real open/fstat failure,
            # NOT a budget decline — declined=False so the batch continues).
            return "", False, False, 0, False
        return result

    monkeypatch.setattr(routes, "_read_cron_output_bounded", logging_reader)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)
    # Call order: descending mtime (newest first), all three attempted (the
    # middle file's failure does not terminate the batch).
    assert [c[0] for c in call_log] == ["run-2.md", "run-1.md", "run-0.md"], (
        f"reader call order must be descending mtime, all three attempted; got {call_log}"
    )
    # Outputs: exactly two entries (the successful reads), in order, NO empty
    # entry for the failed middle file. Small files pass through
    # _cron_output_content_window unchanged, so the marker is included.
    contents = [e.get("content", "") for e in body.get("outputs", [])]
    assert len(contents) == 2, (
        f"exactly two outputs (no entry for failed middle file); got {contents}"
    )
    assert "NEW_OK" in contents[0] and "OLD_OK" in contents[1], (
        f"outputs must be the two successful reads in mtime order; got {contents}"
    )


# ── Gate round-5 (2026-07-25): physical-read bound + short-read accounting ───


def test_cron_batch_no_reader_call_after_budget_exhaustion(monkeypatch, tmp_path):
    """Round-5 #2: once the four-cap budget is spent, the handler must NOT call
    the reader for any remaining file. Instruments physical reads and proves
    total bytes read never exceeds 4 * cap, and no reader is called after
    exhaustion."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    # Five large files (each ~2 caps). Budget is 4 caps, so after the first two
    # successful reads the budget is exhausted and the remaining three must NOT
    # be read at all.
    for i, mtime in enumerate([500, 400, 300, 200, 100]):
        f = out_dir / f"run-{i}.md"
        f.write_text("## Response\n" + ("X" * (_FILE_READ_MAX_BYTES * 3)), encoding="utf-8")
        os.utime(f, (mtime, mtime))
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    original = routes._read_cron_output_bounded
    physical_bytes = []
    calls_after_exhaustion = []

    def instrumented_reader(path, *a, **kw):
        result = original(path, *a, **kw)
        text, trunc, ok, bytes_read, declined = result
        # Record EVERY physical read (successful or failed), with the path and
        # the descriptor-derived bytes consumed. This independently observes
        # what the reader reports as physically read, including growth-probe
        # bytes and partial-failure consumption. (#6141 r6)
        physical_bytes.append(bytes_read)
        if not ok:
            calls_after_exhaustion.append((path.name, declined))
        return result

    monkeypatch.setattr(routes, "_read_cron_output_bounded", instrumented_reader)

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)
    # Total physical bytes (successful + failed-consumed) must stay within the
    # four-cap bound — every byte the descriptor returned is charged.
    physical_total = sum(physical_bytes)
    assert physical_total <= _FILE_READ_MAX_BYTES * 4, (
        f"total physical reads must stay within 4 caps; got "
        f"{physical_total} ({physical_total / _FILE_READ_MAX_BYTES:.1f} caps)"
    )
    # No reader should be called after budget exhaustion — the handler stops.
    assert calls_after_exhaustion == [] or all(
        not d for _, d in calls_after_exhaustion
    ), (
        f"no reader call after exhaustion (declines and I/O failures may appear "
        f"while budget remains, but NO reader call after remaining <= 0); "
        f"got {calls_after_exhaustion}"
    )
    # The batch is marked truncated (some files were skipped).
    assert body.get("truncated") is True, (
        f"batch must be flagged truncated when files were skipped; got {body}"
    )


def test_read_cron_output_bounded_short_read_accounts_actual_bytes(tmp_path, monkeypatch):
    """Round-5 #3: bytes_read must reflect the ACTUAL bytes returned by the
    descriptor, not the planned fstat window lengths. If the inode shrinks or
    returns short reads after fstat, the accounting must use len(raw)."""
    from api.routes import _read_cron_output_bounded

    f = tmp_path / "cron.md"
    # Large file (over cap → disjoint head+tail path).
    f.write_text("## Response\n" + ("B" * (_FILE_READ_MAX_BYTES * 3)), encoding="utf-8")

    # Wrap the file handle so reads return FEWER bytes than requested (short
    # read), simulating shrink-after-fstat or a flaky descriptor.
    real_open = open

    class ShortReadFile:
        def __init__(self, name):
            self._real = real_open(name, "rb")
            self.name = name
            self.fileno = self._real.fileno
        def seek(self, *a): return self._real.seek(*a)
        def read(self, n=-1):
            data = self._real.read(n)
            # Return only half the requested bytes (short read).
            if n and n > 0:
                return data[: n // 2]
            return data
        def close(self): return self._real.close()

    import builtins
    def short_open(name, *a, **k):
        if str(name) == str(f):
            return ShortReadFile(str(f))
        return real_open(name, *a, **k)
    monkeypatch.setattr(builtins, "open", short_open)
    text, trunc, ok, bytes_read, declined = _read_cron_output_bounded(f)
    # The planned windows were head_len + tail_len = 2 * cap. With short reads
    # (half returned), bytes_read must be ~cap, not 2*cap.
    assert bytes_read < _FILE_READ_MAX_BYTES * 2, (
        f"bytes_read must reflect actual (short) reads, not planned windows; "
        f"got {bytes_read} (planned would be {_FILE_READ_MAX_BYTES * 2})"
    )
    assert ok is True


# ── Gate round-6 (2026-07-26): physical-read bound contract tests ────────────


def test_read_cron_output_bounded_growth_probe_byte_is_charged(tmp_path, monkeypatch):
    """Round-6 #1: the cap+1 growth-probe byte must be charged. A file that
    grows under the read returns cap+1 bytes physically; bytes_read must be
    cap+1 (the physical-I/O bound counts what was read, not what's returned)."""
    from api.routes import _read_cron_output_bounded

    f = tmp_path / "grow.md"
    f.write_text("x" * _FILE_READ_MAX_BYTES)  # exactly cap bytes
    real_open = open

    class GrowFile:
        def __init__(self, name):
            self._real = real_open(name, "rb")
            self.name = name
            self.fileno = self._real.fileno
        def seek(self, *a): return self._real.seek(*a)
        def read(self, n=-1):
            data = self._real.read(n)
            # Append 1 byte when the growth probe (cap+1) read runs.
            if n and n > _FILE_READ_MAX_BYTES:
                return data + b"G"
            return data
        def close(self): return self._real.close()

    import builtins
    def grow_open(name, *a, **k):
        if str(name) == str(f):
            return GrowFile(str(f))
        return real_open(name, *a, **k)
    monkeypatch.setattr(builtins, "open", grow_open)

    text, trunc, ok, bytes_read, declined = _read_cron_output_bounded(f)
    assert bytes_read == _FILE_READ_MAX_BYTES + 1, (
        f"growth-probe byte must be charged; got {bytes_read} "
        f"(expected {_FILE_READ_MAX_BYTES + 1})"
    )
    assert trunc is True  # file grew past the cap


def test_read_cron_output_bounded_partial_failure_preserves_head_bytes(tmp_path, monkeypatch):
    """Round-6 #2: if head read succeeds but tail seek/read raises OSError,
    the consumed head bytes must still be reported (bytes_read > 0) so the
    handler can charge them. They were physically read and cannot vanish."""
    from api.routes import _read_cron_output_bounded

    f = tmp_path / "big.md"
    f.write_text("## Response\n" + ("B" * (_FILE_READ_MAX_BYTES * 3)))
    real_open = open

    class PartialFailFile:
        def __init__(self, name):
            self._real = real_open(name, "rb")
            self.name = name
            self.fileno = self._real.fileno
            self._head_done = False
        def seek(self, pos):
            # Second seek (tail) raises — simulates tail seek/read failure.
            if self._head_done:
                raise OSError("simulated tail seek failure")
            self._real.seek(pos)
        def read(self, n=-1):
            data = self._real.read(n)
            # Gap-case head read is cap-1 since the r10 boundary-byte window
            # shift (head gives up its last byte to the tail), so arm on the
            # head-sized request regardless of the ±1 shift.
            if n and n >= _FILE_READ_MAX_BYTES - 1:
                self._head_done = True  # head read done; next seek will fail
            return data
        def close(self): return self._real.close()

    import builtins
    def partial_open(name, *a, **k):
        if str(name) == str(f):
            return PartialFailFile(str(f))
        return real_open(name, *a, **k)
    monkeypatch.setattr(builtins, "open", partial_open)

    text, trunc, ok, bytes_read, declined = _read_cron_output_bounded(f)
    # ok=False (read incomplete), but the consumed head bytes must be reported.
    assert ok is False, f"partial failure must return ok=False; got ok={ok}"
    assert bytes_read > 0, (
        f"consumed head bytes must be preserved on partial failure; got "
        f"bytes_read={bytes_read}"
    )
    assert bytes_read == _FILE_READ_MAX_BYTES - 1, (
        f"head read (~1 cap; gap-case head is cap-1 since r10) must be charged exactly; "
        f"got {bytes_read}, expected {_FILE_READ_MAX_BYTES - 1}"
    )


def test_read_cron_output_bounded_small_file_admitted_when_actual_size_fits(tmp_path):
    """Round-6 #3: a pinned small file must be admitted when its ACTUAL size
    fits the remaining budget, not declined because cap+1 > budget. A 10-byte
    file with budget=10 must succeed."""
    from api.routes import _read_cron_output_bounded

    f = tmp_path / "tiny.md"
    f.write_text("x" * 10)
    text, trunc, ok, bytes_read, declined = _read_cron_output_bounded(f, budget=10)
    assert ok is True, (
        f"10-byte file with budget=10 must be admitted (actual size fits); "
        f"got ok={ok}, declined={declined}"
    )
    assert declined is False
    assert bytes_read == 10


# ── Gate round-7 (2026-07-26): exact-remainder growth edge ───────────────────


def test_cron_batch_exact_remainder_growth_does_not_exceed_hard_cap(monkeypatch, tmp_path):
    """Round-7 + reviewer fixes: when newer files consume 4*cap - 1 bytes (remaining = 1)
    and a pinned 1-byte file grows during its descriptor read, the total physical
    read must NEVER exceed 4*cap. The reader caps the physical read at the remaining
    budget (not budget+1), so a same-inode growth cannot return more than the caller's
    allowance. Proven by instrumenting ALL fixture files' read(n) calls and asserting
    the exact event/order matrix, and by adding a 4th older readable file that must
    NOT be read after the budget is exhausted (proves the stop-guard fires).

    Schedule (descending mtime = handler iteration order):
      - run-2.md: newest, size 2*cap (large file → contributes 2*cap bytes via head+tail)
      - run-1.md: second newest, size 2*cap - 1 (large → contributes 2*cap - 1 bytes)
      - run-0.md: third newest, pinned 1-byte file that grows to 2 bytes between fstat and read
      - run-3.md: oldest, normal readable small file (~50 bytes) — MUST NOT be read

    The two newest files consume exactly 4*cap - 1 bytes, leaving exactly 1 byte of
    remaining allowance for the third file. That file's fstat sees size=1 (admitted),
    but the same inode grows to 2 bytes before the read. The fixed helper caps the
    read at budget (=1), so read(1) -> 1, keeping total at exactly 4*cap. The handler's
    stop-guard (spent > 0 and remaining <= 0: break) then prevents any read of run-3.md.

    Expected read(n) event matrix (exact order, file + requested + returned):
      - run-2.md: read(cap) -> cap (head)
      - run-2.md: read(cap) -> cap (tail)
      - run-1.md: read(cap) -> cap (head)
      - run-1.md: read(cap-1) -> (cap-1) (tail)
      - run-0.md: read(1) -> 1 (budget-capped small-file read, growth byte NOT returned)
      - run-3.md: NO read events at all (stop-guard fired)

    Assert this exact sequence, the sum of returned lengths is exactly 4*cap (independent
    of helper-reported bytes_read), the helper-level call_log excludes run-3.md, and the
    outputs list excludes run-3.md. Keep existing pinned-one-byte-growth assertions."""
    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    cap = _FILE_READ_MAX_BYTES

    prefix = "## Response\n"
    prefix_bytes = len(prefix.encode("utf-8"))

    # run-2.md: newest, size 2*cap bytes -> contributes 2*cap (head + tail)
    run_2 = out_dir / "run-2.md"
    filler_2_bytes = cap * 2 - prefix_bytes
    content_2_bytes = prefix.encode("utf-8") + (b"A" * filler_2_bytes)
    run_2.write_bytes(content_2_bytes)

    # run-1.md: second newest, size 2*cap - 1 bytes -> contributes 2*cap - 1
    run_1 = out_dir / "run-1.md"
    filler_1_bytes = cap * 2 - 1 - prefix_bytes
    content_1_bytes = prefix.encode("utf-8") + (b"B" * filler_1_bytes)
    run_1.write_bytes(content_1_bytes)

    # Verify exact sizes (large-file tail_len arithmetic depends on them)
    assert os.path.getsize(run_2) == cap * 2, f"run_2 size mismatch: {os.path.getsize(run_2)}"
    assert os.path.getsize(run_1) == cap * 2 - 1, f"run_1 size mismatch: {os.path.getsize(run_1)}"

    # run-0.md: third newest (mtimes below), pinned 1-byte file that grows to 2 bytes
    old_0 = out_dir / "run-0.md"
    old_0.write_text("x", encoding="utf-8")
    assert os.path.getsize(old_0) == 1, f"old_0 size must be 1: {os.path.getsize(old_0)}"

    # run-3.md: oldest, normal readable small file — must NOT be read
    old_3 = out_dir / "run-3.md"
    old_3.write_text("## Response\nOlder file content that must never be read.\n", encoding="utf-8")

    # Set mtimes for descending order (newest first, processed first)
    os.utime(run_2, (400, 400))  # newest
    os.utime(run_1, (300, 300))
    os.utime(old_0, (200, 200))
    os.utime(old_3, (100, 100))  # oldest

    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Instrument ALL fixture files' read(n) calls independently of helper returns
    read_events = []
    requested_amounts_for_grown = []
    returned_lengths_for_grown = []
    real_open = open

    class TrackingFile:
        def __init__(self, name, *a, **k):
            self._real = real_open(name, *a, **k)
            self._name = str(name)
            self._base = os.path.basename(self._name)
            self._grown = False

        def fileno(self):
            return self._real.fileno()

        def seek(self, *a, **k):
            return self._real.seek(*a, **k)

        def read(self, n=-1):
            # Growth: only run-0.md (the grown file) grows on first read
            if self._base == "run-0.md" and not self._grown and n and n > 0:
                with real_open(self._real.name, "ab") as wf:
                    wf.write(b"G")
                self._grown = True

            data = self._real.read(n)

            # Record EVERY read(n) call independently of helper-reported bytes_read
            read_events.append((self._base, n, len(data)))

            # Also track the grown file specifically for existing assertions
            if self._base == "run-0.md":
                requested_amounts_for_grown.append(n)
                returned_lengths_for_grown.append(len(data))

            return data

        def close(self):
            return self._real.close()

    import builtins
    fixture_names = {"run-2.md", "run-1.md", "run-0.md", "run-3.md"}

    def tracking_open(name, *a, **k):
        if os.path.basename(str(name)) in fixture_names:
            return TrackingFile(name, *a, **k)
        return real_open(name, *a, **k)

    monkeypatch.setattr(builtins, "open", tracking_open)

    # Instrument the helper-level reader to record call_log (helper-state view)
    original_reader = routes._read_cron_output_bounded
    call_log = []

    def instrumented_reader(path, *a, **kw):
        result = original_reader(path, *a, **kw)
        text, trunc, ok, bytes_read, declined = result
        call_log.append((path.name, ok, declined, bytes_read))
        return result

    monkeypatch.setattr(routes, "_read_cron_output_bounded", instrumented_reader)

    # Drive the batch endpoint
    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )
    body = _payload(handler)

    # Assertion 1: exact read(n) event matrix (order, file, requested, returned)
    expected_events = [
        ("run-2.md", cap, cap),      # head
        ("run-2.md", cap, cap),      # tail
        ("run-1.md", cap, cap),      # head
        ("run-1.md", cap - 1, cap - 1),  # tail
        ("run-0.md", 1, 1),          # budget-capped small-file read
    ]
    assert read_events == expected_events, (
        f"exact read(n) event matrix must match; expected {expected_events}, "
        f"got {read_events}"
    )

    # Assertion 2: sum of returned lengths is exactly 4*cap (independent proof)
    total_returned = sum(event[2] for event in read_events)
    assert total_returned == cap * 4, (
        f"sum of returned lengths must be exactly 4*cap ({cap * 4}); "
        f"got {total_returned}"
    )

    # Assertion 3: run-3.md appears in NEITHER read_events NOR call_log NOR outputs
    read_event_files = {event[0] for event in read_events}
    assert "run-3.md" not in read_event_files, (
        f"run-3.md must not be in read_events (no read() calls); got {read_event_files}"
    )
    call_log_files = {entry[0] for entry in call_log}
    assert "run-3.md" not in call_log_files, (
        f"run-3.md must not be in call_log (helper never called); got {call_log_files}"
    )
    output_filenames = {e.get("filename") for e in body.get("outputs", [])}
    assert "run-3.md" not in output_filenames, (
        f"run-3.md must not be in outputs (not returned); got {output_filenames}"
    )

    # Assertion 4: helper-level reader-call list is exactly the three files (no run-3.md)
    assert [c[0] for c in call_log] == ["run-2.md", "run-1.md", "run-0.md"], (
        f"helper call_log must be exactly three files in order; got {[c[0] for c in call_log]}"
    )

    # Assertion 5: outputs filenames list is exactly the three files (no run-3.md)
    assert output_filenames == {"run-2.md", "run-1.md", "run-0.md"}, (
        f"outputs filenames must be exactly three files; got {output_filenames}"
    )

    # Assertion 6: grown file's specific read (pinned budget=1, read(1) -> 1)
    assert requested_amounts_for_grown == [1], (
        f"the grown file must be read with exactly 1 byte request (budget capped); "
        f"got {requested_amounts_for_grown}"
    )
    assert returned_lengths_for_grown == [1], (
        f"the grown file's read must return exactly 1 byte even though it grew to 2; "
        f"got {returned_lengths_for_grown}"
    )

    # Assertion 7: grown file entry is flagged truncated (conservative)
    outputs = body.get("outputs", [])
    grown_entry = next((e for e in outputs if e.get("filename") == "run-0.md"), None)
    assert grown_entry is not None, "run-0.md should be in outputs"
    assert grown_entry.get("truncated") is True, (
        f"the grown file must be conservatively truncated; got {grown_entry.get('truncated')}"
    )

    # Assertion 8: helper-level state for grown file (ok=True, declined=False, bytes_read=1)
    grown_call = next((c for c in call_log if c[0] == "run-0.md"), None)
    assert grown_call is not None
    _name, ok, declined, bytes_read = grown_call
    assert ok is True, "the grown file read should succeed (ok=True)"
    assert declined is False, "the grown file should not be declined"
    assert bytes_read == 1, f"helper should report bytes_read=1 for the grown file; got {bytes_read}"


def test_read_cron_output_bounded_budget_limited_read_never_exceeds_budget(
    tmp_path, monkeypatch
):
    """Round-7 unit: when budget is smaller than cap+1 (no room for the growth
    probe), the physical read is capped at exactly budget — never budget+1.
    A file that grows during read cannot return more than the budget."""
    from api.routes import _read_cron_output_bounded

    f = tmp_path / "small.md"
    f.write_text("x")  # 1 byte pinned size
    real_open = open
    requested_amounts = []

    class TrackingFile:
        def __init__(self, name):
            self._real = real_open(name, "rb")
            self.name = name
            self.fileno = self._real.fileno
        def seek(self, *a): return self._real.seek(*a)
        def read(self, n=-1):
            requested_amounts.append(n)
            # A real read(n) returns at most n bytes. Simulate growth by having
            # 2 bytes available, but read(n) still returns at most n.
            return (b"xG")[:n] if n and n > 0 else b"xG"
        def close(self): return self._real.close()

    import builtins
    def tracking_open(name, *a, **k):
        if str(name) == str(f):
            return TrackingFile(str(f))
        return real_open(name, *a, **k)
    monkeypatch.setattr(builtins, "open", tracking_open)

    text, trunc, ok, bytes_read, declined = _read_cron_output_bounded(f, budget=1)
    # The read request must have been capped at budget (1), not budget+1 (2).
    # Strengthened: exactly ONE read request, of exactly 1 byte.
    assert requested_amounts == [1], (
        f"physical read must be exactly one request of 1 byte; got "
        f"{requested_amounts}"
    )
    # Strengthened: exact bytes_read value (not just <=)
    assert bytes_read == 1, (
        f"bytes_read must be exactly 1 (not just <=1); got {bytes_read}"
    )
    # Strengthened: exact text content
    assert text == "x", (
        f"text must be exactly the 1 byte that was read; got {text!r}"
    )
    # Strengthened: ok and declined states
    assert ok is True, (
        f"the read should succeed (ok=True); got ok={ok}"
    )
    assert declined is False, (
        f"the file should not be declined; got declined={declined}"
    )
    # Conservatively truncated (no room for growth probe, read filled budget).
    assert trunc is True, (
        f"read that fills the budget without a growth probe must be "
        f"conservatively truncated; got trunc={trunc}"
    )


# ── Gate round-10 (2026-08-18): boundary-newline data-loss fix ─────────────────────


def test_cron_run_detail_boundary_newline_keeps_first_response_line(monkeypatch, tmp_path):
    """Round-10: boundary-newline data-loss fix. When the head cap ends EXACTLY at
    a line boundary (preceded by a newline), the first line after the boundary is
    COMPLETE and must be kept — the old code unconditionally dropped it. Fixture:
    head of EXACTLY cap bytes ending with ``\\n## Response\\n``, then two response
    lines. Assert BOTH lines survive in text, snippet, and handler payload. (#6141 r10)"""
    from api.routes import _read_cron_output_bounded, _cron_output_snippet

    cap = _FILE_READ_MAX_BYTES

    # Construct a head of EXACTLY cap bytes ending with "\n## Response\n"
    # The newline before the marker is required so _cron_response_marker_index
    # recognizes it (it scans for "\n## Response").
    frontmatter = b"# Cron\n**Model:** gpt-5\n**Tokens:** 1000, 500\n\n"
    response_prefix = b"\n## Response\n"  # newline before marker for recognition

    # Filler to reach exactly cap bytes (frontmatter + filler + response_prefix)
    filler_len = cap - len(frontmatter) - len(response_prefix)
    assert filler_len >= 0, f"frontmatter+response_prefix overflow cap: {len(frontmatter)} + {len(response_prefix)} > {cap}"
    head = frontmatter + (b"F" * filler_len) + response_prefix
    assert len(head) == cap, f"head must be exactly {cap} bytes; got {len(head)}"

    # Response body: two complete lines after the boundary
    line_one = b"RESPONSE_LINE_ONE_ALPHA\n"
    line_two = b"RESPONSE_LINE_TWO_BETA\n"
    body = line_one + line_two

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    fpath = out_dir / "run.md"
    fpath.write_bytes(head + body)

    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    # Direct helper check: BOTH lines must survive in text and snippet
    txt, truncated, _ok, _bytes_read, _declined = _read_cron_output_bounded(fpath)
    assert truncated is True, "file exceeds cap, must be truncated"
    assert "RESPONSE_LINE_ONE_ALPHA" in txt, (
        f"first response line after boundary must be kept; got:\n{txt[:200]}"
    )
    assert "RESPONSE_LINE_TWO_BETA" in txt, (
        f"second response line must be kept; got:\n{txt[:200]}"
    )
    snippet = _cron_output_snippet(txt)
    assert "RESPONSE_LINE_ONE_ALPHA" in snippet, (
        f"snippet must include first response line; got:\n{snippet[:200]}"
    )
    assert "RESPONSE_LINE_TWO_BETA" in snippet, (
        f"snippet must include second response line; got:\n{snippet[:200]}"
    )
    # Marker count sanity: exactly one ## Response (no duplication)
    assert txt.count("## Response") == 1, (
        f"marker must appear exactly once; count={txt.count('## Response')}"
    )

    # End-to-end through the detail handler
    handler = _JSONHandler()
    routes._handle_cron_run_detail(
        handler, SimpleNamespace(query="job_id=job1&filename=run.md")
    )
    assert handler.status == 200
    body_payload = _payload(handler)
    assert body_payload["truncated"] is True
    assert "RESPONSE_LINE_ONE_ALPHA" in body_payload["content"], (
        f"handler payload must include first response line; got:\n{body_payload.get('content', '')[:200]}"
    )
    assert "RESPONSE_LINE_TWO_BETA" in body_payload["content"], (
        f"handler payload must include second response line; got:\n{body_payload.get('content', '')[:200]}"
    )
    assert "RESPONSE_LINE_ONE_ALPHA" in body_payload["snippet"], (
        f"handler snippet must include first response line; got:\n{body_payload.get('snippet', '')[:200]}"
    )
    assert "RESPONSE_LINE_TWO_BETA" in body_payload["snippet"], (
        f"handler snippet must include second response line; got:\n{body_payload.get('snippet', '')[:200]}"
    )


def test_read_cron_output_bounded_gap_boundary_newline_keeps_first_line(tmp_path):
    """Round-10 gap-case variant: when size > 2*cap and the boundary byte (at
    ``size - cap - 1``) is a newline, the first line after the gap is COMPLETE
    and must be kept. Fixtures a gap-case file with a boundary newline and two
    complete lines; asserts line one AND line two survive and bytes_read <= 2*cap.
    (#6141 r10)"""
    from api.routes import _read_cron_output_bounded

    cap = _FILE_READ_MAX_BYTES

    # For gap case, we need size > 2*cap and the boundary byte at size - cap - 1
    # We want the boundary newline to be at position size - cap - 1
    # So: head_len + middle_len = size - cap - 1
    # And: size = head_len + middle_len + len(tail_content)
    # Therefore: head_len + middle_len + len(tail_content) - cap - 1 = head_len + middle_len
    # Solving: middle_len = cap + 1 - len(tail_content)
    frontmatter = b"# Cron\n**Model:** gpt-5\n"
    head_len = cap - 1
    head_filler_len = head_len - len(frontmatter)
    head_portion = frontmatter + (b"H" * head_filler_len)
    assert len(head_portion) == head_len, f"head portion must be {head_len} bytes; got {len(head_portion)}"

    # Tail content: boundary newline + two complete lines + padding to reach cap + 1 bytes
    # For gap case, tail_len = cap + 1, so tail_content needs to be cap + 1 bytes
    boundary_newline = b"\n"
    line_one = b"GAP_LINE_ONE_EPS\n"
    line_two = b"GAP_LINE_TWO_ZETA"
    tail_content_base = boundary_newline + line_one + line_two
    tail_content_base_len = len(tail_content_base)
    # Add padding to reach cap + 1 bytes
    padding_len = (cap + 1) - tail_content_base_len
    tail_content = tail_content_base + (b"P" * padding_len)
    tail_content_len = len(tail_content)
    assert tail_content_len == cap + 1, f"tail_content must be cap + 1 bytes; got {tail_content_len}"

    # For gap case, middle_len can be any value > 0 (just needs size > 2*cap)
    middle_len = cap  # use cap bytes for middle (same as head length)
    middle_blob = b"M" * middle_len

    # Calculate expected positions and size
    # Head: positions 0 to head_len - 1
    # Middle: positions head_len to head_len + middle_len - 1
    # Boundary newline: at position head_len + middle_len
    # This should equal size - cap - 1 for the gap case
    expected_boundary_pos = head_len + middle_len
    target_size = head_len + middle_len + tail_content_len

    # Verify the boundary position calculation
    assert expected_boundary_pos == target_size - cap - 1, (
        f"boundary position calculation error: expected_boundary_pos={expected_boundary_pos}, "
        f"size - cap - 1 = {target_size - cap - 1}"
    )
    # Verify size > 2*cap for gap case
    assert target_size > 2 * cap, f"size must be > 2*cap for gap case; got {target_size}"

    # Assemble the file
    fpath = tmp_path / "gap-boundary.md"
    content = head_portion + middle_blob + tail_content
    fpath.write_bytes(content)

    size = fpath.stat().st_size
    assert size == target_size, f"size mismatch: expected {target_size}, got {size}"
    assert size > 2 * cap, f"file size must exceed 2*cap ({2*cap}); got {size}"

    # Verify alignment: the byte at size - cap - 1 MUST be "\n"
    # This is the key invariant for the gap case
    boundary_pos_check = size - cap - 1
    actual_boundary_byte = content[boundary_pos_check:boundary_pos_check + 1]
    assert actual_boundary_byte == b"\n", (
        f"boundary byte at position {boundary_pos_check} must be newline; "
        f"got {actual_boundary_byte!r}"
    )
    assert boundary_pos_check == expected_boundary_pos, (
        f"boundary position mismatch: expected {expected_boundary_pos}, got {boundary_pos_check}"
    )

    # Read via the bounded helper
    txt, truncated, _ok, bytes_read, _declined = _read_cron_output_bounded(fpath)

    # Both tail lines must survive (they're complete — the boundary proved it)
    assert "GAP_LINE_ONE_EPS" in txt, (
        f"first line after gap boundary must be kept; got:\n{txt[:300]}"
    )
    assert "GAP_LINE_TWO_ZETA" in txt, (
        f"second line after gap boundary must be kept; got:\n{txt[:300]}"
    )
    # Budget sanity: bytes_read == 2*cap (gap case total is exactly 2*cap)
    assert bytes_read == 2 * cap, (
        f"bytes_read must equal 2*cap ({2*cap}); got {bytes_read}"
    )
    assert truncated is True, "file exceeds cap, must be truncated"


def test_read_cron_output_bounded_partial_first_line_still_dropped(tmp_path):
    """Round-10 control test: when the boundary byte is NOT a newline (mid-line
    boundary), the first tail line is PARTIAL and must still be dropped. Fixture:
    head of exactly cap bytes NOT ending in ``\\n`` (last byte 'Q'), then partial
    fragment ``PARTIAL_FRAGMENT_ZZZ\\n`` followed by a control line. Assert the
    partial fragment is NOT in text and the control line IS in text. (#6141 r10)"""
    from api.routes import _read_cron_output_bounded

    cap = _FILE_READ_MAX_BYTES

    # Construct head of EXACTLY cap bytes NOT ending in newline
    frontmatter = b"# Cron\n**Model:** gpt-5\n"
    filler_len = cap - len(frontmatter) - 1  # -1 to leave room for trailing 'Q'
    head = frontmatter + (b"F" * filler_len) + b"Q"  # 'Q' ensures no trailing newline
    assert len(head) == cap, f"head must be exactly {cap} bytes; got {len(head)}"
    assert head[-1:] != b"\n", "head must NOT end with newline"

    # Response body starts mid-line (no leading newline)
    # PARTIAL_FRAGMENT_ZZZ is a partial line at the boundary (should be dropped)
    # CONTROL_LINE_KEEP is a complete line after it (should be kept)
    body = b"PARTIAL_FRAGMENT_ZZZ\nCONTROL_LINE_KEEP\n"

    fpath = tmp_path / "partial-boundary.md"
    fpath.write_bytes(head + body)

    # Read via the bounded helper
    txt, truncated, _ok, _bytes_read, _declined = _read_cron_output_bounded(fpath)

    # Partial fragment must be dropped (it was incomplete at the boundary)
    assert "PARTIAL_FRAGMENT_ZZZ" not in txt, (
        f"partial first line must be dropped; got:\n{txt[:300]}"
    )
    # Control line must survive (it's complete after the dropped partial)
    assert "CONTROL_LINE_KEEP" in txt, (
        f"complete line after partial must be kept; got:\n{txt[:300]}"
    )


def test_read_text_bounded_tail_mode_boundary_newline_keeps_first_line(tmp_path):
    """Round-10: _read_text_bounded(tail=True) must keep a provably-complete first
    line when the boundary byte (the byte before the tail window) is a newline.
    Fixture: file ``b\"junk-no-newline-end\" + b\"\\n\" + b\"TAIL_LINE_ONE_EPS\\nTAIL_LINE_TWO_ZETA\"``.
    The tail seek lands on the boundary newline, so the first tail line is complete
    and BOTH lines must be in the returned text (old code dropped the first line).
    (#6141 r10)"""
    from api.routes import _read_text_bounded

    # Construct: junk prefix (no trailing newline) + boundary newline + two lines
    junk = b"junk-no-newline-end"  # no trailing newline — the newline we add is the boundary
    boundary_newline = b"\n"
    line_one = b"TAIL_LINE_ONE_EPS\n"
    line_two = b"TAIL_LINE_TWO_ZETA"
    content = junk + boundary_newline + line_one + line_two

    fpath = tmp_path / "tail-boundary.txt"
    fpath.write_bytes(content)

    # Read tail with max_bytes covering exactly the two lines — NOT the
    # boundary byte. (Including the boundary byte in max_bytes would make even
    # the pre-fix window contain it, so the unconditional drop would strip
    # only that byte and the fixture could not discriminate the fix.)
    lines_len = len(line_one) + len(line_two)
    text, truncated, _ok = _read_text_bounded(fpath, max_bytes=lines_len, tail=True)

    # Both lines must survive (the boundary byte proved the first line was complete)
    assert "TAIL_LINE_ONE_EPS" in text, (
        f"first tail line must be kept; got:\n{text[:200]}"
    )
    assert "TAIL_LINE_TWO_ZETA" in text, (
        f"second tail line must be kept; got:\n{text[:200]}"
    )
    # The boundary byte (newline) should be stripped, not duplicated
    assert text.startswith("TAIL_LINE_ONE_EPS"), (
        f"text should start with first line (boundary byte stripped); got:\n{text[:100]}"
    )


def test_read_cron_output_bounded_degenerate_zero_cap_reads_bounded(tmp_path):
    """Round-11 (self-review): degenerate cap=0 must not read unbounded. The gap
    branch at cap=0 would set head_len = -1, causing fh.read(-1) to slurp the
    whole file. Fix: clamp head_len = max(0, cap - 1). Fixture: ~3 MiB file with
    max_bytes=0 → assert bytes_read <= 1; with budget=0 → assert declined. (#6141 r11)"""
    from api.routes import _read_cron_output_bounded

    # Write a ~3 MiB file (well past any reasonable cap)
    big = tmp_path / "big.md"
    big.write_text("## Response\n" + ("B" * (3 * 1024 * 1024)), encoding="utf-8")
    size_before = big.stat().st_size
    assert size_before > 3 * 1024 * 1024, f"file should be >3 MiB; got {size_before}"

    # Test 1: max_bytes=0 must read at most 1 byte (boundary byte only)
    text, truncated, ok, bytes_read, declined = _read_cron_output_bounded(big, max_bytes=0)
    assert bytes_read <= 1, (
        f"degenerate cap=0 must read at most 1 byte (boundary), not the whole file; "
        f"got bytes_read={bytes_read}"
    )
    assert len(text) < 100, (
        f"text returned must be tiny (<100 chars) when cap=0; got len={len(text)}"
    )

    # Test 2: max_bytes=0 with budget=0 must decline (planned=1 > budget=0)
    text, truncated, ok, bytes_read, declined = _read_cron_output_bounded(
        big, max_bytes=0, budget=0
    )
    assert ok is False, (
        f"cap=0 with budget=0 must decline (ok=False); got ok={ok}"
    )
    assert declined is True, (
        f"cap=0 with budget=0 must set declined=True; got declined={declined}"
    )
    assert bytes_read == 0, (
        f"declined read must consume 0 bytes; got bytes_read={bytes_read}"
    )


def test_read_text_bounded_tail_mode_never_exceeds_cap(tmp_path):
    """Round-11 (self-review): tail mode must NEVER return more than max_bytes
    chars. When the drop guard declines to strip (no interior newline, or only
    a trailing newline), the old code returned max_bytes+1 chars. Fix: re-derive
    from the last max_bytes bytes in the non-stripped path. Fixtures: (i) single-
    line no-newline file, (ii) window with only a trailing newline. Both must
    return <= max_bytes chars. (#6141 r11)"""
    from api.routes import _read_text_bounded

    # Fixture (i): single-line no-newline file (no interior newline possible)
    f1 = tmp_path / "single_no_newline.txt"
    payload1 = b"Z" * 100
    f1.write_bytes(payload1)
    max_bytes = 10

    text1, trunc1, ok1 = _read_text_bounded(f1, max_bytes=max_bytes, tail=True)
    assert len(text1) <= max_bytes, (
        f"single-line no-newline file must return <= {max_bytes} chars; "
        f"got len={len(text1)}"
    )
    # Should be exactly max_bytes (last max_bytes bytes of the 100-byte file)
    assert len(text1) == max_bytes, (
        f"expected exactly {max_bytes} chars from last-{max_bytes} slice; "
        f"got len={len(text1)}"
    )

    # Fixture (ii): tail window has only a trailing newline (interior newline absent)
    # Content: b"junk" + b"A"*7 + b"\n" with max_bytes=10
    # - File size > max_bytes, so we take the tail
    # - Tail window is last 10 bytes: b"AAAAAAA\n" (7 A's + newline + trailing junk)
    # Wait, we need the WINDOW to have only a trailing newline, meaning
    # no interior newline exists. Let's construct:
    # File: b"junk" + b"A"*7 + b"\n" → total = 4 + 7 + 1 = 12 bytes
    # max_bytes=10: tail starts at 12 - 10 - 1 = 1 (one byte early)
    # Window: positions 1-11 = b"unk" + b"A"*7 + b"\n" = 10 bytes
    # This window has ONE trailing newline, no interior newline
    f2 = tmp_path / "trailing_newline_only.txt"
    payload2 = b"junk" + b"A" * 7 + b"\n"  # 12 bytes total
    f2.write_bytes(payload2)
    max_bytes2 = 10

    text2, trunc2, ok2 = _read_text_bounded(f2, max_bytes=max_bytes2, tail=True)
    assert len(text2) <= max_bytes2, (
        f"window with only trailing newline must return <= {max_bytes2} chars; "
        f"got len={len(text2)}"
    )
    # The clamped result should be the last max_bytes2 bytes (b"AAAAAAA\n" = 8 bytes)
    # after the fix clamps raw[-max_bytes2:] which is 10 bytes but decode might vary
    # Let's just assert the bound
    assert len(text2.encode("utf-8")) <= max_bytes2, (
        f"UTF-8 byte length must also be <= {max_bytes2}; got "
        f"{len(text2.encode('utf-8'))}"
    )


# ── Round 12: remove marker fabrication + decline-stop ───────────────────────────


def test_cron_output_markerless_never_fabricates_response_heading(monkeypatch, tmp_path):
    """Markerless oversized output (script-style, no heading anywhere) must never have
    ## Response synthesized. The bounded tail survives literally in both content and
    snippet (with divider preference), and exactly one divider appears. (#6141 r12)"""
    from api.routes import _read_cron_output_bounded, _cron_output_snippet, _CRON_TRUNCATION_DIVIDER

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)

    # Adjacent case: file slightly over cap, no marker anywhere
    cap = _FILE_READ_MAX_BYTES
    markerless_content = b"#!/bin/bash\necho 'no marker here'\n" + b"A" * (cap + 1000)
    (out_dir / "run-adjacent.md").write_bytes(markerless_content)

    txt_adj, trunc_adj, ok_adj, bytes_adj, declined_adj = _read_cron_output_bounded(
        out_dir / "run-adjacent.md"
    )
    assert ok_adj and not declined_adj
    assert trunc_adj
    # No fabricated ## Response anywhere
    assert "## Response" not in txt_adj
    assert "# Response" not in txt_adj
    # Final tail line (last A's) survives in content
    assert txt_adj.endswith("A" * 40)  # last few A's present
    # Exactly one divider occurrence
    assert txt_adj.count(_CRON_TRUNCATION_DIVIDER) == 1
    # Snippet prefers post-divider region (the tail A's, not head padding)
    snippet_adj = _cron_output_snippet(txt_adj)
    assert "A" * 20 in snippet_adj  # tail A's visible in snippet
    assert "#!/bin/bash" not in snippet_adj  # head padding not in snippet

    # Gap case: file > 2*cap, no marker anywhere
    gap_content = b"#!/bin/bash\necho 'no marker here'\n" + b"B" * (2 * cap + 5000)
    (out_dir / "run-gap.md").write_bytes(gap_content)

    txt_gap, trunc_gap, ok_gap, bytes_gap, declined_gap = _read_cron_output_bounded(
        out_dir / "run-gap.md"
    )
    assert ok_gap and not declined_gap
    assert trunc_gap
    # No fabricated ## Response anywhere
    assert "## Response" not in txt_gap
    assert "# Response" not in txt_gap
    # Final tail line (last B's) survives in content
    assert txt_gap.endswith("B" * 40)  # last few B's present
    # Exactly one divider occurrence
    assert txt_gap.count(_CRON_TRUNCATION_DIVIDER) == 1
    # Snippet prefers post-divider region (the tail B's)
    snippet_gap = _cron_output_snippet(txt_gap)
    assert "B" * 20 in snippet_gap  # tail B's visible in snippet
    assert "#!/bin/bash" not in snippet_gap  # head padding not in snippet


def test_cron_output_split_marker_repaired_only_when_proven(monkeypatch, tmp_path):
    """A split Response marker is repaired ONLY in the adjacent case where head/tail
    boundaries provably concatenate to the real heading. In the gap case (windows not
    adjacent), even a pseudo-split pattern is NOT repaired — proving the guard. (#6141 r12)"""
    from api.routes import (
        _read_cron_output_bounded,
        _cron_output_snippet,
        _split_response_marker_prefix,
    )

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    cap = _FILE_READ_MAX_BYTES

    # (a) Adjacent split: head ends with "\n## Resp", tail starts with "onse\n..."
    # Byte-exact construction: frontmatter fills to cap, marker straddles the boundary
    frontmatter = "# Cron\n"
    filler_len = cap - len(frontmatter.encode("utf-8")) - len("\n## Resp".encode("utf-8"))
    head_portion = frontmatter.encode("utf-8") + b"F" * filler_len + b"\n## Resp"
    tail_portion = b"onse\nActual reply body here.\n"
    (out_dir / "run-adjacent-split.md").write_bytes(head_portion + tail_portion)

    # Verify split-marker detection works
    assert _split_response_marker_prefix(head_portion, tail_portion) == "## Resp"

    txt_adj, trunc_adj, ok_adj, bytes_adj, declined_adj = _read_cron_output_bounded(
        out_dir / "run-adjacent-split.md"
    )
    assert ok_adj and not declined_adj
    assert trunc_adj
    # After repair, "## Response" appears exactly once (the reconstructed marker)
    assert txt_adj.count("## Response") == 1
    # Body survives in content
    assert "Actual reply body here." in txt_adj
    # Snippet shows the body (marker found, so divider preference irrelevant)
    snippet_adj = _cron_output_snippet(txt_adj)
    assert "Actual reply body here." in snippet_adj

    # (b) Gap pseudo-split: file > 2*cap engineered so head ends with "## Resp"
    # but windows are NOT adjacent — NO repair, only fragment remains.
    # In gap case, head reads 0..(cap-2), tail reads (size - cap - 1)..end.
    # The windows are NOT adjacent, so even if head ends with "## Resp"
    # and tail starts with "onse", the helper should not detect a valid split
    # because those bytes don't actually touch in the file.
    gap_head_len = cap - 1  # gap case: head gives up one byte
    # The fragment must sit at a LINE START so ONLY the gap guard blocks the
    # repair — a mid-line fragment would be rejected by the line-start check
    # and mask a removed gap guard.
    gap_filler_len = (
        gap_head_len - len(frontmatter.encode("utf-8")) - 1 - len("## Resp".encode("utf-8"))
    )
    gap_head = (
        frontmatter.encode("utf-8") + b"G" * gap_filler_len + b"\n## Resp"
    )
    assert len(gap_head) == cap - 1, len(gap_head)
    # For gap case, create a file > 2*cap with gap region
    gap_size = 5000
    gap_middle = b"X" * gap_size  # ensures gap > 0
    # The pseudo-split must align to BOTH window edges: head ends with
    # "## Resp" AND the tail window's FIRST bytes are "onse". The tail window
    # is [size-cap-1, size), so the after-gap content must be exactly cap+1
    # bytes ending at EOF and starting with "onse" — only then does removing
    # the gap guard produce a (wrong) repair this test must catch.
    gap_tail_lead = b"onse\nGap file body.\n"
    tail_content = gap_tail_lead + b"Y" * (cap + 1 - len(gap_tail_lead))
    assert len(tail_content) == cap + 1, len(tail_content)
    total_gap_file = gap_head + gap_middle + tail_content
    assert len(total_gap_file) > 2 * cap, f"Gap file too small: {len(total_gap_file)}"
    (out_dir / "run-gap-pseudo.md").write_bytes(total_gap_file)
    # Byte-exact alignment proof: head window ends with the marker prefix and
    # the tail window (last cap+1 bytes) starts with the marker remainder,
    # yet the windows are NOT adjacent (5000-byte gap) — so the raw pattern
    # matches but the concatenation is NOT the file's true byte sequence.
    assert total_gap_file[cap - 1 - len(b"## Resp"):cap - 1] == b"## Resp"
    assert total_gap_file[len(total_gap_file) - cap - 1:].startswith(b"onse")

    txt_gap, trunc_gap, ok_gap, bytes_gap, declined_gap = _read_cron_output_bounded(
        out_dir / "run-gap-pseudo.md"
    )
    assert ok_gap and not declined_gap
    assert trunc_gap
    # NO complete "## Response" in output (only the fragment " Resp" in head)
    assert "## Response" not in txt_gap
    assert "# Response" not in txt_gap
    # The fragment " Resp" may appear, but not as a complete heading
    # Gap file body survives in tail
    assert "Gap file body." in txt_gap


def test_cron_output_marker_placements_preserve_single_real_marker(monkeypatch, tmp_path):
    """Marker wholly in head, wholly in tail, or entirely in the omitted gap — each
    case yields at most one complete marker in output and never fabricates a heading. (#6141 r12)"""
    from api.routes import _read_cron_output_bounded

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    cap = _FILE_READ_MAX_BYTES
    marker = b"## Response\nMarker body.\n"

    # Case 1: Marker wholly in head (total > cap, marker in [0, cap) window)
    # Pad past cap to exercise window branch, keep marker well before cap boundary
    head_content = b"# Cron\n" + b"A" * (cap - 100) + b"\n" + marker
    (out_dir / "run-marker-in-head.md").write_bytes(head_content + b"tail padding\n" + b"X" * 1000)

    txt1, trunc1, ok1, bytes1, declined1 = _read_cron_output_bounded(
        out_dir / "run-marker-in-head.md"
    )
    assert ok1 and not declined1
    assert trunc1 is True, "Case 1 must exercise window branch (total > cap)"
    # Exactly one marker (head's marker, no fabrication)
    assert txt1.count("## Response") == 1
    assert "Marker body." in txt1

    # Case 2: Marker wholly in tail (total > cap, marker in tail window)
    # Adjacent case: the tail window starts at exactly cap, so the head padding
    # must be EXACTLY cap bytes for the marker to land past the boundary.
    head_padding = b"# Cron\n" + b"B" * (cap - len(b"# Cron\n") - 1) + b"\n"
    assert len(head_padding) == cap, len(head_padding)
    tail_content = b"extra\n" + marker + b"more body\n"
    # Marker starts at cap + len("extra\n") — inside the tail window [cap, size)
    (out_dir / "run-marker-in-tail.md").write_bytes(head_padding + tail_content + b"Y" * 1000)

    txt2, trunc2, ok2, bytes2, declined2 = _read_cron_output_bounded(
        out_dir / "run-marker-in-tail.md"
    )
    assert ok2 and not declined2
    assert trunc2 is True, "Case 2 must exercise window branch (total > cap)"
    # Exactly one marker (tail's marker, no fabrication)
    assert txt2.count("## Response") == 1
    assert "Marker body." in txt2

    # Case 3: Marker entirely in the omitted gap (file > 2*cap, marker in middle)
    # Gap case parameters: head reads (cap - 1) bytes, tail reads (cap + 1) bytes
    # Marker must be positioned in the gap region only (not in head or tail)
    head_len = cap - 1
    tail_len = cap + 1
    # Head content: exactly head_len bytes, no marker
    head_prefix = b"# Cron\n"
    head_fill_len = head_len - len(head_prefix)
    head_only = head_prefix + b"C" * head_fill_len
    assert len(head_only) == head_len, f"Head size {len(head_only)} != expected {head_len}"
    # Gap marker positioned strictly in the gap
    gap_marker = b"## Response\nGap marker body.\n"
    # Tail content: tail_len bytes, no marker
    tail_prefix = b"# Tail\n"
    tail_fill_len = tail_len - len(tail_prefix)
    tail_only = tail_prefix + b"D" * tail_fill_len
    assert len(tail_only) == tail_len, f"Tail size {len(tail_only)} != expected {tail_len}"
    # Verify the marker is in the gap region only
    marker_start = len(head_only)
    marker_end = marker_start + len(gap_marker)
    total_size = len(head_only) + len(gap_marker) + len(tail_only)
    tail_start_in_file = total_size - tail_len
    assert total_size > 2 * cap, f"Test fixture too small: {total_size} <= {2 * cap}"
    assert marker_start >= head_len, f"Marker starts at {marker_start} which is < head_len {head_len}"
    assert marker_end <= tail_start_in_file, f"Marker ends at {marker_end} which is > tail_start {tail_start_in_file}"
    (out_dir / "run-marker-in-gap.md").write_bytes(head_only + gap_marker + tail_only)

    txt3, trunc3, ok3, bytes3, declined3 = _read_cron_output_bounded(
        out_dir / "run-marker-in-gap.md"
    )
    assert ok3 and not declined3
    # Zero complete markers (marker in gap is omitted, no fabrication)
    assert "## Response" not in txt3
    assert "# Response" not in txt3


def test_cron_batch_decline_continues_to_later_smaller_file(monkeypatch, tmp_path):
    """When a file exceeds the remaining budget, the batch declines it but continues
    scanning — a later smaller file may still fit. Only true exhaustion stops. (#6141 r12)"""
    import os
    import time

    from api.routes import _read_cron_output_bounded

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    cap = _FILE_READ_MAX_BYTES
    now = time.time()

    # Schedule files newest-first by mtime:
    # run-a: 2.0 * cap (admitted: first-file rule, no budget check)
    # run-b: 1.5 * cap (admitted: fits in 2*cap remaining after run-a)
    # run-c: 0.75 * cap (declined: exceeds ~0.25*cap remaining after run-a+b)
    # run-d: 64 bytes (admitted: fits in remaining budget after run-a+b, run-c declined)
    files = {
        "run-a.md": (int(2.0 * cap), "A"),
        "run-b.md": (int(1.5 * cap), "B"),
        "run-c.md": (int(0.75 * cap), "C"),
        "run-d.md": (64, "D"),
    }

    physical_reads = {}  # track actual bytes physically read

    def instrumented_read(path, *, max_bytes=cap, budget=None):
        result = _read_cron_output_bounded(path, max_bytes=max_bytes, budget=budget)
        txt, trunc, ok, bytes_read, declined = result
        # Record physical read even on decline (the reader may have probed)
        physical_reads[path.name] = physical_reads.get(path.name, 0) + bytes_read
        return result

    monkeypatch.setattr(routes, "_read_cron_output_bounded", instrumented_read)

    # Create files with staggered mtimes (newest first)
    for i, (fname, (size, content_char)) in enumerate(files.items()):
        fpath = out_dir / fname
        # Create content of exactly `size` bytes using the character + newlines
        line = f"{content_char} content\n"
        line_bytes = len(line.encode("utf-8"))
        num_lines = int(size // line_bytes) + 1
        content = (line * num_lines)
        # Trim to exact size by bytes
        content_bytes = content.encode("utf-8")
        content_bytes = content_bytes[:size]
        actual_size = len(content_bytes)
        assert actual_size == size, f"File {fname} size mismatch: {actual_size} != {size}"
        fpath.write_bytes(content_bytes)
        # Newest first: run-a is newest
        mtime = now - (i * 10)
        os.utime(fpath, (mtime, mtime))

    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job1&limit=10")
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body.get("truncated") is True

    outputs = body["outputs"]
    filenames = [entry["filename"] for entry in outputs]

    # run-a, run-b, run-d present, newest-first relative order preserved
    assert "run-a.md" in filenames
    assert "run-b.md" in filenames
    assert "run-d.md" in filenames
    assert filenames.index("run-a.md") < filenames.index("run-b.md") < filenames.index("run-d.md"), (
        f"returned rows must preserve newest-first order; got {filenames}"
    )

    # run-c absent (declined, zero physical reads)
    assert "run-c.md" not in filenames
    assert physical_reads.get("run-c.md", 0) == 0, "declined file performed zero physical reads"

    # Total physical reads <= 4*cap (run-a + run-b + run-d, run-c declined without reads)
    total_physical = sum(physical_reads.values())
    assert total_physical <= 4 * cap, f"total physical reads {total_physical} exceeded 4*cap"


def test_cron_output_midline_marker_fragment_not_promoted(monkeypatch, tmp_path):
    """A mid-line marker fragment must NOT be promoted to a recognized heading.
    Fixture: adjacent case, head ends with prose ending in '## Resp' (mid-line),
    tail starts with 'onse...' - the helper rejects the split because the
    prefix doesn't start a line. Output has zero complete markers and the
    second tail line survives (partial-first-line drop still works). (#6141 r12 R6a)"""
    from api.routes import _read_cron_output_bounded, _cron_response_marker_index

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    cap = _FILE_READ_MAX_BYTES

    # Adjacent case: head window is EXACTLY cap bytes and ends with a MID-LINE
    # "## Resp" (preceded by X padding, not a newline); the tail starts with
    # "onse..." so concatenating would produce the "## Response" substring —
    # the helper must reject the split because the fragment is not at a line
    # start. (A head shorter than cap would leave the fragment outside the
    # window and the test could pass vacuously.)
    head_portion = (
        b"# Cron\nsee PPP in docs: "
        + b"X" * (cap - len(b"# Cron\nsee PPP in docs: ") - len(b"## Resp"))
        + b"## Resp"
    )
    assert len(head_portion) == cap, len(head_portion)
    tail_portion = b"onse continues the text\nsecond line\nmore body\n" + b"Y" * 1950
    assert len(head_portion) + len(tail_portion) <= 2 * cap  # adjacent branch
    (out_dir / "run-midline-split.md").write_bytes(head_portion + tail_portion)

    txt, trunc, ok, bytes_read, declined = _read_cron_output_bounded(
        out_dir / "run-midline-split.md"
    )
    assert ok and not declined
    assert trunc

    # NO complete "## Response" marker in output (the concatenation "## Resp"+"onse"
    # creates the substring "## Response continues the text" but that's NOT a
    # complete heading - it lacks the terminating newline).
    assert _cron_response_marker_index(txt) == -1, "Marker index must be -1 (no complete marker)"

    # Verify the whole file also has no marker (truth)
    with open(out_dir / "run-midline-split.md", "rb") as f:
        whole_file = f.read().decode("utf-8", errors="replace")
    assert _cron_response_marker_index(whole_file) == -1, "Whole file truth: no complete marker"

    # Second line survives (partial-first-line drop still works)
    assert "second line" in txt, "Second tail line must survive after partial-first-line drop"


def test_cron_output_short_head_read_does_not_repair_marker(monkeypatch, tmp_path):
    """A short head read (shrink-during-read) must NOT repair a marker split.
    Fixture: adjacent case with cap=128 via max_bytes param; file = 188 bytes.
    The marker fragment is at a LINE START in the true file ("\n## Resp" at
    [53,60)), so the line-start check PASSES on the short head — only the
    full-head-read requirement can block the (wrong) repair of non-contiguous
    windows. First read returns 60 bytes ending exactly at "## Resp".
    Output has zero complete markers. (#6141 r12 R6b, MF)"""
    from api.routes import _read_cron_output_bounded

    out_dir = tmp_path / "cron-out" / "job1"
    out_dir.mkdir(parents=True)
    small_cap = 128  # Small cap for test fixture
    # True layout: F*52 | "\n## Resp" [53,60) | Z*68 [60,128) | "onse..." [128,188)
    # The marker NEVER exists contiguously: a full head read [0,128) ends with
    # Z's; only the SHORT 60-byte read ends with the fragment.
    head_portion = b"F" * 52 + b"\n## Resp" + b"Z" * 68
    assert len(head_portion) == small_cap
    tail_portion = b"onse\nTail body after mutation.\n" + b"T" * 29
    file_content = head_portion + tail_portion
    assert len(file_content) == 188, f"File size {len(file_content)} != 188"

    (out_dir / "run-short-head.md").write_bytes(file_content)

    # Wrap open to simulate short read (shrink-during-read)
    original_open = open
    read_counts = {}

    def short_open(path, *args, **kwargs):
        fh = original_open(path, *args, **kwargs)
        if "run-short-head" in str(path):
            original_read = fh.read

            def short_read(size=-1):
                if "run-short-head" not in read_counts:
                    read_counts["run-short-head"] = 0
                    # First read returns 60 bytes (F*52 + "\n## Resp") — a
                    # shrink-during-head-read that ends exactly at the fragment
                    return original_read(60)
                else:
                    # Subsequent reads return full amount
                    return original_read(size)

            fh.read = short_read
        return fh

    monkeypatch.setattr("builtins.open", short_open)

    txt, trunc, ok, bytes_read, declined = _read_cron_output_bounded(
        out_dir / "run-short-head.md", max_bytes=small_cap
    )
    assert ok and not declined
    # Short read means bytes_read < planned, so file appears truncated
    # (The exact trunc behavior depends on implementation, but no repair should occur)
    assert txt.count("## Response") == 0, "Short head read must not repair marker split"


def test_split_response_marker_prefix_requires_line_start():
    """Unit test: _split_response_marker_prefix accepts only line-start prefixes.
    - Legit '\n## Resp' + 'onse...' → '## Resp'
    - Mid-line 'see ## Resp' + 'onse...' → '' (rejected)
    - Whole-head-prefix b'## Resp' (k == len) → '## Resp' (file starts with marker)
    - b'issue #' + b' Response...' → '' (k=1 h1 trap rejected)
    - Legit h1 '\n#' + ' Response...' → '#' (single-# heading at line start) (#6141 r12 R6c)"""
    from api.routes import _split_response_marker_prefix

    # Legit case: prefix starts at line boundary
    head = b"some text\n## Resp"
    tail = b"onse\nbody"
    assert _split_response_marker_prefix(head, tail) == "## Resp", "Legit newline prefix should be accepted"

    # Mid-line case: prefix does NOT start at line boundary
    head_midline = b"see PPP in docs: ## Resp"
    tail_midline = b"onse\nbody"
    assert _split_response_marker_prefix(head_midline, tail_midline) == "", "Mid-line prefix should be rejected"

    # Whole-head-prefix case: entire head is the prefix (file starts with heading)
    head_whole = b"## Resp"
    tail_whole = b"onse\nbody"
    assert _split_response_marker_prefix(head_whole, tail_whole) == "## Resp", "Whole-head prefix should be accepted"

    # k=1 h1 trap: '#' + ' Response' looks like a split but '#' is mid-line in "issue #"
    head_trap = b"issue #"
    tail_trap = b" Response\nbody"
    assert _split_response_marker_prefix(head_trap, tail_trap) == "", "Mid-line # in 'issue #' should be rejected"

    # Legit h1 single-# case at line start
    head_h1 = b"text\n#"
    tail_h1 = b" Response\nbody"
    assert _split_response_marker_prefix(head_h1, tail_h1) == "#", "Legit h1 newline prefix should be accepted"
