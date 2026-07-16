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
    assert len(body["content"].encode("utf-8")) <= _FILE_READ_MAX_BYTES


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

