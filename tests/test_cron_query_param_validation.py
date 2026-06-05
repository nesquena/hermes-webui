"""Regression: malformed numeric query params on cron endpoints return a
well-formed response instead of crashing with an uncaught ValueError (500).

``GET /api/crons/output?limit=<x>`` and ``GET /api/crons/recent?since=<x>``
parsed their numeric params with a bare ``int()`` / ``float()``. A non-numeric
value raised ValueError, which propagated to the top-level request handler and
surfaced as a generic HTTP 500 — inconsistent with sibling handlers
(``_handle_cron_run_detail``, ``_handle_insights``, ``_handle_notes_search``)
that all guard the same pattern and fall back to a default.

``_handle_cron_output`` additionally clamps ``limit`` to a positive range so a
negative value can never reach the ``files[:limit]`` slice (``files[:-1]`` would
silently drop the newest output rather than return it).
"""

from __future__ import annotations

import io
import json
import sys
import types
from types import SimpleNamespace


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


def _stub_cron_jobs(monkeypatch, *, output_dir=None, jobs=None):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    if output_dir is not None:
        cron_jobs.OUTPUT_DIR = output_dir
    if jobs is not None:
        cron_jobs.list_jobs = lambda include_disabled=True: jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    return cron_jobs


def test_cron_output_non_numeric_limit_does_not_500(monkeypatch, tmp_path):
    import api.routes as routes

    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    # Before the fix this raised ValueError -> 500.
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=abc123&limit=notanint")
    )

    assert handler.status == 200
    body = _payload(handler)
    assert body["job_id"] == "abc123"
    assert body["outputs"] == []


def test_cron_output_negative_limit_is_clamped(monkeypatch, tmp_path):
    import api.routes as routes

    out_dir = tmp_path / "cron-out" / "job42"
    out_dir.mkdir(parents=True)
    # Three outputs with distinct mtimes; newest must always be returned.
    for i in range(3):
        f = out_dir / f"run-{i}.md"
        f.write_text(f"## Response\noutput {i}\n", encoding="utf-8")
        import os
        os.utime(f, (1000 + i, 1000 + i))

    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")

    handler = _JSONHandler()
    # limit=-3 with exactly 3 files: an unclamped files[:-3] slice yields []
    # (every output silently dropped). Clamped to >= 1 it returns the newest.
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=job42&limit=-3")
    )

    assert handler.status == 200
    body = _payload(handler)
    assert len(body["outputs"]) >= 1
    assert body["outputs"][0]["filename"] == "run-2.md"


def test_cron_output_valid_limit_still_works(monkeypatch, tmp_path):
    import api.routes as routes

    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out")
    handler = _JSONHandler()
    routes._handle_cron_output(
        handler, SimpleNamespace(query="job_id=nonexistent&limit=20")
    )
    assert handler.status == 200
    assert _payload(handler)["outputs"] == []


def test_cron_recent_non_numeric_since_does_not_500(monkeypatch):
    import api.routes as routes

    _stub_cron_jobs(
        monkeypatch,
        jobs=[
            {"id": "a", "name": "Job A", "last_run_at": 50, "last_status": "success"},
        ],
    )

    handler = _JSONHandler()
    # Before the fix this raised ValueError -> 500.
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=notanum"))

    assert handler.status == 200
    body = _payload(handler)
    assert body["since"] == 0.0
    # since defaults to the epoch, so the completed job is still reported.
    assert {item["job_id"] for item in body["completions"]} == {"a"}


def test_cron_recent_valid_since_still_filters(monkeypatch):
    import api.routes as routes

    _stub_cron_jobs(
        monkeypatch,
        jobs=[
            {"id": "old", "name": "Old", "last_run_at": 5, "last_status": "success"},
            {"id": "new", "name": "New", "last_run_at": 50, "last_status": "success"},
        ],
    )

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))

    assert handler.status == 200
    body = _payload(handler)
    assert body["since"] == 10.0
    assert {item["job_id"] for item in body["completions"]} == {"new"}
