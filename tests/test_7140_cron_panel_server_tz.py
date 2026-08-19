"""Regression tests for issue #7140 - cron panel Last run / Next run timezone.

Root cause: the cron detail panel formatted ``next_run_at`` / ``last_run_at``
with bare ``new Date(...).toLocaleString()`` (browser timezone), and the
``server_tz`` sent by /api/sessions came from ``time.strftime("%z")`` (the
process/container zone, typically UTC) instead of the Hermes-configured
timezone (``HERMES_TIMEZONE`` env var / ``timezone`` key in config.yaml).

Fix: static/panels.js formats the cron timestamps via ``_formatInServerTz``
(the server-tz helper from static/sessions.js), and api/routes.py derives
``server_tz`` from ``hermes_time.now()`` with a process-zone fallback.
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _render_cron_detail_body() -> str:
    """Return the body of the cron detail panel renderer from panels.js."""
    start = PANELS_JS.find("function _renderCronDetail")
    assert start != -1, "_renderCronDetail must exist in static/panels.js"
    end = PANELS_JS.find("\nfunction ", start + 1)
    return PANELS_JS[start:end if end != -1 else len(PANELS_JS)]


# ---------------------------------------------------------------------------
# Frontend: cron detail panel timestamps must use the server timezone
# ---------------------------------------------------------------------------

def test_cron_panel_last_next_run_use_server_tz():
    """Cron Last run / Next run must render in the server's configured
    timezone via _formatInServerTz, not bare browser-timezone toLocaleString()."""
    body = _render_cron_detail_body()
    # The server-tz formatter (defined in sessions.js, loaded before panels.js)
    # must be wired into the cron detail renderer.
    assert "_formatInServerTz" in body, (
        "cron detail panel must reference _formatInServerTz (server timezone)"
    )
    for var, field in (("nextRun", "next_run_at"), ("lastRun", "last_run_at")):
        line = next((l for l in body.splitlines() if f"const {var} =" in l), None)
        assert line is not None, f"{var} must be computed in _renderCronDetail"
        assert f"job.{field}" in line, f"{var} must format job.{field}"
        # Must go through the server-tz formatter, not a bare browser call.
        assert "toLocaleString()" not in line, (
            f"{var} ({field}) must not render via bare browser-timezone "
            "toLocaleString()"
        )


# ---------------------------------------------------------------------------
# Backend: server_tz must come from the Hermes timezone, not the container zone
# ---------------------------------------------------------------------------

def test_routes_server_tz_prefers_hermes_timezone():
    """server_tz must be derived from the Hermes-configured timezone
    (hermes_time), not the raw process/container zone."""
    assert "def _server_tz_offset" in ROUTES_PY, (
        "api/routes.py must define a _server_tz_offset() helper"
    )
    assert '"server_tz": _server_tz_offset()' in ROUTES_PY, (
        "/api/sessions server_tz must come from _server_tz_offset()"
    )
    start = ROUTES_PY.find("def _server_tz_offset")
    end = ROUTES_PY.find("\ndef ", start + 1)
    helper = ROUTES_PY[start:end if end != -1 else len(ROUTES_PY)]
    assert "hermes_time" in helper, (
        "server_tz must prefer the Hermes-configured timezone (hermes_time)"
    )
    assert 'time.strftime("%z")' in helper, (
        "helper must keep a process-zone fallback"
    )
