"""Behavioral regression tests for the shared cron operation dispatcher."""

import sys
import types


def test_cron_operation_dispatches_requested_operation(monkeypatch):
    from api import cron_runtime

    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")

    def run_one_job(job, *args, **kwargs):
        calls.append((job, args, kwargs))
        return "handled"

    scheduler.run_one_job = run_one_job
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    result = cron_runtime._invoke_cron_operation(
        {"id": "dispatcher"},
        "run_one_job",
        ("positional",),
        {"flag": True},
    )

    assert result == "handled"
    assert calls == [
        ({"id": "dispatcher"}, ("positional",), {"flag": True})
    ]
