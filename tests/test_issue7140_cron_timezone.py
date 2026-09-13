"""Regression tests for issue #7140 — cron panel timezone.

Root cause: ``api/routes.py`` sent ``"server_tz": time.strftime("%z")``, which
returns the *process* timezone (UTC inside a typical container) rather than
the configured Hermes timezone. The cron detail panel also formatted
``Last run``/``Next run`` with a raw ``toLocaleString()``, so a job firing at
08:00 server-local read as 1:35 AM UTC on the operator's screen.

Fix:
- ``_server_tz_offset()`` resolves the zone exactly like the agent:
  ``HERMES_TIMEZONE`` env var -> ``timezone`` key in the active profile's
  config.yaml -> process offset fallback.
- ``static/panels.js`` cron detail and run-history rows use
  ``_formatInServerTz()`` (guarded) instead of plain ``toLocaleString()``.
"""

import os
import pathlib
import re

import pytest

import api.routes as routes  # noqa: E402  — mirrors other WebUI test modules

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend: _server_tz_offset() resolves the configured Hermes timezone
# ---------------------------------------------------------------------------

def _server_tz_offset():
    return routes._server_tz_offset()


def test_server_tz_uses_hermes_timezone_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TIMEZONE", "America/Sao_Paulo")
    offset = _server_tz_offset()
    assert re.fullmatch(r"[+-]\d{4}", offset)
    assert offset in ("-0300", "-0200")  # São Paulo DST range


def test_server_tz_falls_back_to_config_timezone(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("timezone: Asia/Kolkata\n")
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: config)
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    assert _server_tz_offset() == "+0530"


def test_server_tz_falls_back_to_process_offset(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("timezone: ''\n")
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: config)
    assert re.fullmatch(r"[+-]\d{4}", _server_tz_offset())


def test_server_tz_invalid_zone_falls_back_safely(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TIMEZONE", "Not/AZone")
    assert re.fullmatch(r"[+-]\d{4}", _server_tz_offset())


def test_sessions_response_uses_server_tz_offset(monkeypatch):
    """The sessions response must call _server_tz_offset(), not raw strftime."""
    assert re.search(
        r'"server_tz"\s*:\s*_server_tz_offset\(\)', ROUTES_PY
    ), "routes.py must use _server_tz_offset() for server_tz"


def test_routes_has_server_tz_helper():
    """_server_tz_offset() must consult HERMES_TIMEZONE then config.yaml."""
    assert "_server_tz_offset" in ROUTES_PY
    assert 'os.getenv("HERMES_TIMEZONE"' in ROUTES_PY
    assert 'cfg.get("timezone")' in ROUTES_PY


# ---------------------------------------------------------------------------
# Frontend: cron detail + run history use _formatInServerTz
# ---------------------------------------------------------------------------

def test_cron_detail_uses_format_in_server_tz():
    """Cron detail 'Last run'/'Next run' must use _formatInServerTz (guarded)."""
    assert "_formatInServerTz" in PANELS_JS
    # The detail panel's nextRun/lastRun lines must route through _fmtTz.
    detail_block = PANELS_JS[
        PANELS_JS.index("const status = _cronStatusMeta(job);"):
        PANELS_JS.index("const schedule = job.schedule_display")
    ]
    assert "_fmtTz(new Date(job.next_run_at))" in detail_block
    assert "_fmtTz(new Date(job.last_run_at))" in detail_block


def test_cron_run_history_uses_format_in_server_tz():
    """Cron run-history timestamps must use _formatInServerTz (guarded)."""
    assert "_fmtTz(new Date(run.modified * 1000))" in PANELS_JS
