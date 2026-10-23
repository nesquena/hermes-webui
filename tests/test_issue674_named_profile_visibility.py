"""Regression tests for #674: Aider rows must survive the sidebar profile filter.

``get_aider_sessions()`` stamps ``profile: None`` on every row: the transcript
lives in a git root, not inside a Hermes profile tree. The sidebar's profile
scope filter (``_profiles_match(s.get("profile"), active_profile)``) coerces a
missing profile to ``'default'``, so under a NAMED (non-root) active profile
every Aider row was removed from the payload -- "Show Aider sessions" showed
nothing at all.

Aider has no global store: the bridge scans exactly ONE bounded root, resolved
from the ACTIVE profile's own workspace (``get_last_workspace(profile=...)``).
Every Aider row in the payload therefore belongs to the active profile, and the
scope filter must not drop it.

The control row pins that ordinary profile ownership is untouched: a CLI row
owned by another profile stays hidden.
"""

from __future__ import annotations

import pytest


def _aider_row(tmp_path):
    """The real list row the Aider bridge emits (profile is None by design)."""
    import api.models as models

    scan_dir = tmp_path / "repo"
    scan_dir.mkdir(parents=True, exist_ok=True)
    (scan_dir / ".aider.chat.history.md").write_text(
        "\n#### hello from aider\n", encoding="utf-8"
    )
    rows = models.get_aider_sessions(scan_dir=scan_dir)
    assert len(rows) == 1
    return rows[0]


def _other_profile_cli_row():
    """An ordinary CLI row owned by a DIFFERENT profile."""
    return {
        "session_id": "cli_other_profile_0001",
        "title": "another profile's CLI session",
        "profile": "someone-else",
        "archived": False,
        "message_count": 2,
        "updated_at": 2000,
        "last_message_at": 2000,
        "source": "cli",
        "raw_source": "cli",
        "session_source": "cli",
        "source_tag": "cli",
        "source_label": "CLI",
        "is_cli_session": True,
    }


def _wire(monkeypatch, tmp_path, cli_rows):
    import api.routes as routes
    import api.profiles as profiles

    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_claude_code_sessions": True,
        "show_aider_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
    })
    monkeypatch.setattr(routes, "all_sessions", lambda **kw: [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **kw: list(cli_rows))
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda _s: False
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _r: None)
    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing",
        lambda ids, profile=None: set(ids),
    )
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")


@pytest.fixture(autouse=True)
def _clear_cache():
    import api.routes as routes
    import api.models as models

    routes._session_list_cache_clear()
    models.clear_cli_sessions_cache()
    yield
    routes._session_list_cache_clear()
    models.clear_cli_sessions_cache()


def test_aider_row_survives_the_profile_scope_filter_under_a_named_profile(
    monkeypatch, tmp_path
):
    """The Aider row must be listed under the profile whose workspace was scanned."""
    import api.routes as routes

    aider = _aider_row(tmp_path)
    assert aider["profile"] is None, (
        "Aider rows stay profile-less by design (the transcript is not owned "
        "by a Hermes profile); the LIST filter is what must not drop them"
    )
    _wire(monkeypatch, tmp_path, [aider])

    payload = routes._build_session_list_cache_payload(
        active_profile="work",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    listed = [r["session_id"] for r in payload["sessions"]]
    assert listed == [aider["session_id"]], (
        "the Aider row was scanned from the ACTIVE profile's workspace, so the "
        f"profile scope filter must keep it; got {listed!r}"
    )


def test_ordinary_other_profile_rows_are_still_filtered_out(monkeypatch, tmp_path):
    """Control: profile ownership for ordinary rows is unchanged."""
    import api.routes as routes

    _wire(monkeypatch, tmp_path, [_other_profile_cli_row()])

    payload = routes._build_session_list_cache_payload(
        active_profile="work",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    assert [r["session_id"] for r in payload["sessions"]] == []
    assert payload["other_profile_count"] == 1
