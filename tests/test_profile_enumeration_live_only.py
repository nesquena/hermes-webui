"""Regression coverage for profile enumeration parity and the cron cross-profile guard.

Both bugs let a tombstoned, marker-less profile home surface in the WebUI:

* ``_build_profile_rows_fast()`` stopped at "is a dir + valid id", while upstream
  ``_iter_named_profile_dirs()`` also requires an identity marker and no tombstone.
* ``_cron_jobs_cross_profile()`` then called ``list_jobs()`` for every listed home,
  whose ``ensure_dirs()`` created ``cron/`` + ``cron/output/`` inside the ghost.

Run with ``pytest --noconftest`` when the WebUI conftest needs the full server deps.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

WEBUI_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT", Path.home() / ".hermes" / "hermes-agent"))
if AGENT_ROOT.is_dir() and str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
if str(WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(WEBUI_ROOT))

pytest.importorskip("hermes_cli.profiles", reason="hermes-agent not installed")


def _make_homes(tmp_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
    profiles = home / "profiles"
    profiles.mkdir()

    live = profiles / "liveprof"
    live.mkdir()
    (live / "config.yaml").write_text("model: y\n", encoding="utf-8")

    ghost = profiles / "ghostprof"
    ghost.mkdir()
    # Give the tombstoned home the SAME identity marker as `live` so the tombstone
    # check is exercised on its own. Without a marker the identity check already
    # rejects it, and both tests would stay green even if tombstone rejection were
    # removed entirely (a deleted home whose identity files survived).
    (ghost / "config.yaml").write_text("model: y\n", encoding="utf-8")

    shell = profiles / "shellprof"
    shell.mkdir()

    (profiles / ".deleted").mkdir()
    (profiles / ".deleted" / "ghostprof").write_text("deleted\n", encoding="utf-8")
    return home, profiles, live, ghost, shell


def test_fast_path_skips_tombstoned_and_markerless_dirs(tmp_path, monkeypatch):
    from api import profiles as webui_profiles
    import hermes_cli.profiles as upstream

    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    monkeypatch.setattr(upstream, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(upstream, "_get_profiles_root", lambda: profiles)
    webui_profiles._invalidate_list_profiles_cache()

    rows = webui_profiles._build_profile_rows_fast()

    assert rows is not None
    names = [row["name"] for row in rows]
    assert names == ["default", "liveprof"], names
    assert "ghostprof" not in names
    assert "shellprof" not in names


def test_cron_read_predicate_skips_non_live_named_homes(tmp_path):
    """`/api/crons` gates every home with this predicate before calling list_jobs()."""
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    assert webui_profiles.is_live_profile_home_for_reads(home) is True  # default home
    assert webui_profiles.is_live_profile_home_for_reads(live) is True
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False  # tombstoned
    assert webui_profiles.is_live_profile_home_for_reads(shell) is False  # marker-less


def test_tombstone_alone_rejects_an_otherwise_live_home(tmp_path):
    """Control: the tombstoned home is identical to `live` except for the tombstone.

    Both halves are pinned here — with the tombstone cleared the same home must be
    readable, so this test cannot pass by the identity check doing the rejecting.
    """
    from api import profiles as webui_profiles
    import hermes_constants

    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    # Precondition: ghost carries the same identity marker as the live profile.
    assert (ghost / "config.yaml").is_file() == (live / "config.yaml").is_file()

    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False

    hermes_constants.clear_named_profile_deleted(ghost)
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is True


def test_read_predicate_fails_closed_when_liveness_cannot_be_confirmed(tmp_path, monkeypatch):
    """An unconfirmable liveness check on a NAMED home must DENY (AGENTS.md fail-closed)."""
    from api import profiles as webui_profiles
    import hermes_constants

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    def _boom(_home):
        raise OSError("identity unreadable")

    monkeypatch.setattr(hermes_constants, "named_profile_is_live", _boom)
    # A named home that cannot be proven live is not readable...
    assert webui_profiles.is_live_profile_home_for_reads(live) is False
    # ...but the default home is not under profiles/, so it never needed the
    # helper and stays readable (a shape we can prove, not an unknown).
    assert webui_profiles.is_live_profile_home_for_reads(home) is True


def test_read_predicate_denies_named_homes_when_helpers_are_missing(tmp_path, monkeypatch):
    """Import failure denies NAMED homes; the default home keeps working.

    This is the CI shape: hermes-agent is not installed there, so
    ``hermes_constants`` is simply absent. Denying every home would blank the
    Tasks panel, so the pre-check is path shape (which needs no upstream
    import) and only the named-home verdict falls back to deny.
    """
    from api import profiles as webui_profiles
    import sys

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert webui_profiles.is_live_profile_home_for_reads(live) is False
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False
    assert webui_profiles.is_live_profile_home_for_reads(home) is True
