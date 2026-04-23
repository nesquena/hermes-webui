"""Tests for #835 — refresh button in Tasks / Scheduled Jobs panel."""
import os
import re


_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


class TestCronRefreshButtonHtml:
    """index.html must expose a refresh button in the Tasks panel header."""

    def test_refresh_button_present(self):
        html = _read("static/index.html")
        assert 'id="cronRefreshBtn"' in html, (
            "Tasks panel must have a #cronRefreshBtn element"
        )

    def test_refresh_button_has_accessibility_labels(self):
        """Icon-only buttons need aria-label + title so screen readers and
        hover tooltips work."""
        html = _read("static/index.html")
        m = re.search(r'<button[^>]*id="cronRefreshBtn"[^>]*>', html)
        assert m, "cronRefreshBtn tag not found"
        tag = m.group(0)
        assert 'aria-label=' in tag, (
            "#cronRefreshBtn is icon-only and must have aria-label"
        )
        assert 'title=' in tag, (
            "#cronRefreshBtn should have a title tooltip"
        )

    def test_refresh_button_calls_load_crons_with_animate(self):
        html = _read("static/index.html")
        m = re.search(r'<button[^>]*id="cronRefreshBtn"[^>]*>', html)
        assert m
        tag = m.group(0)
        assert 'loadCrons(true)' in tag, (
            "#cronRefreshBtn must call loadCrons(true) to enable the dim-while-fetching animation"
        )

    def test_refresh_button_sits_next_to_new_job_button(self):
        """Refresh button should appear in the same header row as the New Job
        button so the header layout stays tight."""
        html = _read("static/index.html")
        ref_pos = html.find('id="cronRefreshBtn"')
        newjob_pos = html.find('openCronCreate()')
        assert ref_pos != -1 and newjob_pos != -1
        # Must be close enough to be in the same header row (single SVG-inline
        # button can be around 500 chars by itself due to inline styles/attrs).
        assert abs(ref_pos - newjob_pos) < 1000, (
            "Refresh button and New Job button should be in the same header row"
        )


class TestLoadCronsAnimateFlag:
    """panels.js loadCrons() must accept an optional animate flag that dims
    the refresh button while fetching."""

    def test_load_crons_accepts_animate_param(self):
        js = _read("static/panels.js")
        assert re.search(r'async function loadCrons\s*\(\s*animate\s*\)', js), (
            "loadCrons must accept an `animate` parameter"
        )

    def test_load_crons_restores_button_in_finally(self):
        """The opacity/disabled restore MUST be in a finally block so a
        throwing fetch doesn't leave the button stuck at 0.5 / disabled."""
        js = _read("static/panels.js")
        m = re.search(r'async function loadCrons\(.*?\n\}', js, re.DOTALL)
        assert m, "loadCrons body not found"
        fn = m.group(0)
        assert 'finally' in fn, (
            "loadCrons must restore the refresh button's opacity/disabled state "
            "in a finally block so errors during fetch don't leave the button stuck"
        )
        # The restore block sets opacity='' (not '1') so CSS cascade wins
        assert "opacity = ''" in fn or "opacity=''" in fn, (
            "restore must use opacity='' to clear the inline override"
        )


class TestCronCreatedEventListener:
    """A global `hermes:cron_created` listener must be registered so
    future chat paths can trigger the cron list refresh."""

    def test_listener_registered_at_module_scope(self):
        js = _read("static/panels.js")
        assert re.search(
            r"addEventListener\(\s*['\"]hermes:cron_created['\"]",
            js,
        ), (
            "panels.js must register a window-level 'hermes:cron_created' event listener"
        )

    def test_listener_triggers_load_crons(self):
        js = _read("static/panels.js")
        m = re.search(
            r"addEventListener\(\s*['\"]hermes:cron_created['\"].*?\}\s*\)",
            js,
            re.DOTALL,
        )
        assert m, "hermes:cron_created listener body not found"
        body = m.group(0)
        assert 'loadCrons' in body, (
            "hermes:cron_created listener must call loadCrons() to refresh the list"
        )
