"""Regression coverage for profile enumeration parity and the cron cross-profile guard.

Both bugs let a tombstoned, marker-less profile home surface in the WebUI:

* ``_build_profile_rows_fast()`` stopped at "is a dir + valid id", while upstream
  ``_iter_named_profile_dirs()`` also requires an identity marker and no tombstone.
* ``_cron_jobs_cross_profile()`` then called ``list_jobs()`` for every listed home,
  whose ``ensure_dirs()`` created ``cron/`` + ``cron/output/`` inside the ghost.

Run with ``pytest --noconftest`` when the WebUI conftest needs the full server deps.
"""

from __future__ import annotations

import json
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


# ── maintainer P2 (2026-11-07): legitimate default/custom homes beneath an
# ── unrelated ``profiles`` ancestor are skipped ──────────────────────────────

def _write_saved_default_job(home: Path, label: str) -> None:
    """A real saved cron job in the default/custom home (like #7295's fixture)."""
    cron_dir = home / "cron"
    (cron_dir / "output").mkdir(parents=True, exist_ok=True)
    job = {
        "id": "f4e3d2c1b0a9",
        "name": f"{label}-job",
        "prompt": f"{label} prompt",
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": None, "completed": 1},
        "next_run_at": "2099-01-01T00:00:00+00:00",
        "last_run_at": "2026-10-01T00:00:00+00:00",
        "last_status": "ok",
        "deliver": "local",
        "profile": None,
    }
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")


def _point_runtime_at_base(monkeypatch, base: Path, rows) -> None:
    """Point the WebUI runtime at *base* exactly as a HERMES_BASE_HOME boot does."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    monkeypatch.setenv("HERMES_BASE_HOME", str(base))
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setattr(webui_profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(webui_profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(webui_routes, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(webui_profiles, "list_profiles_api", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(webui_routes, "list_profiles_api", lambda: [dict(r) for r in rows])


@pytest.mark.parametrize(
    "rel_base",
    [
        "profiles/operator/.hermes",
        "profiles/project/custom-root",
    ],
    ids=["dot_hermes", "custom_root"],
)
def test_default_home_beneath_unrelated_profiles_ancestor_stays_readable(tmp_path, monkeypatch, rel_base):
    """Maintainer P2: a legal base home that merely lives under a coincidental
    ``profiles/`` ancestor must not be classified as a named profile.

    Both layouts are accepted by the real HERMES_BASE_HOME resolver and the
    installed Agent's ``named_profile_home(actual_home)`` answers None; the old
    path-shape classifier promoted the unrelated ancestor (``operator`` /
    ``project``) to a profile, so Tasks skipped the configured home's saved job.
    """
    from api import profiles as webui_profiles
    from api import routes as webui_routes
    import hermes_constants

    root = tmp_path / "root"
    base = root / rel_base
    base.mkdir(parents=True)
    (base / "config.yaml").write_text("model: local\n", encoding="utf-8")
    _write_saved_default_job(base, rel_base.replace("/", "_"))

    # Precondition pins: the real base-home resolver accepts the layout and the
    # installed classifier does NOT call the actual home a named profile.
    monkeypatch.setenv("HERMES_BASE_HOME", str(base))
    monkeypatch.setenv("HERMES_HOME", str(base))
    assert hermes_constants.get_default_hermes_root() == base
    assert hermes_constants.named_profile_home(base) is None

    _point_runtime_at_base(monkeypatch, base, [{"name": "default", "visible": True}])

    assert webui_profiles.is_live_profile_home_for_reads(base) is True, rel_base

    # Real reader: /api/crons' Tasks aggregator must still find the saved job.
    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("default")
    assert other_jobs == []
    assert [j["name"] for j in active_jobs] == [f"{rel_base.replace('/', '_')}-job"], (
        rel_base,
        [j["name"] for j in active_jobs],
    )


def test_real_reader_skips_tombstoned_named_home_without_creating_cron_dir(tmp_path, monkeypatch):
    """Control: the liveness guard still governs ghost named homes in the normal
    layout, and the reader never (re)creates cron dirs inside the deleted shell."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes
    import hermes_constants

    base = tmp_path / ".hermes"
    base.mkdir()
    (base / "config.yaml").write_text("model: base\n", encoding="utf-8")
    ghost = base / "profiles" / "ghostprof"
    ghost.mkdir(parents=True)
    (ghost / "config.yaml").write_text("model: ghost\n", encoding="utf-8")
    (base / "profiles" / ".deleted").mkdir()
    (base / "profiles" / ".deleted" / "ghostprof").write_text("deleted\n", encoding="utf-8")

    _point_runtime_at_base(monkeypatch, base, [{"name": "ghostprof", "visible": True}])

    assert hermes_constants.named_profile_home(ghost) == ghost
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("ghostprof")
    assert active_jobs == [] and other_jobs == []
    assert not (ghost / "cron").exists()


def test_read_predicate_fails_closed_when_the_classifier_raises(tmp_path, monkeypatch):
    """Control: a classifier failure falls back to the shape grammar, which
    still denies a path that LOOKS named when liveness cannot be confirmed."""
    from api import profiles as webui_profiles
    import hermes_constants

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    def _boom(_home):
        raise OSError("classifier unreadable")

    monkeypatch.setattr(hermes_constants, "named_profile_home", _boom)
    monkeypatch.setattr(hermes_constants, "named_profile_is_live", _boom)
    assert webui_profiles.is_live_profile_home_for_reads(live) is False
    assert webui_profiles.is_live_profile_home_for_reads(home) is True


# ── Greptile P2 (2026-11-08): symlinked homes bypass the check ─────────────

def test_symlinked_profiles_root_does_not_bypass_named_home_liveness(tmp_path, monkeypatch):
    """Greptile P2: when ``profiles`` is a symlink the resolver returns the
    RESOLVED path, which no longer carries the ``profiles`` component. The old
    shape-only pre-check called that path non-named, so a stale cookie could
    read (and recreate cron dirs inside) a tombstoned home. Named homes must be
    identified against the resolved profiles root instead."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes
    import hermes_constants

    base = tmp_path / ".hermes"
    base.mkdir()
    (base / "config.yaml").write_text("model: base\n", encoding="utf-8")
    data = tmp_path / "mnt" / "profile-data"
    (data / "ghostprof").mkdir(parents=True)
    (data / "ghostprof" / "config.yaml").write_text("model: ghost\n", encoding="utf-8")
    (data / "liveprof").mkdir()
    (data / "liveprof" / "config.yaml").write_text("model: live\n", encoding="utf-8")
    (data / ".deleted").mkdir()
    (data / ".deleted" / "ghostprof").write_text("deleted\n", encoding="utf-8")
    (base / "profiles").symlink_to(data, target_is_directory=True)

    _point_runtime_at_base(monkeypatch, base, [{"name": "ghostprof", "visible": True}])

    # The path the reader actually receives: `profiles` is resolved away.
    resolved = webui_profiles._resolve_named_profile_home("ghostprof")
    assert resolved == data / "ghostprof"
    assert resolved.parent.name != "profiles"

    # The classifier sees through the logical path (with the symlink intact)...
    assert hermes_constants.named_profile_home(base / "profiles" / "ghostprof") == base / "profiles" / "ghostprof"
    assert webui_profiles.is_live_profile_home_for_reads(base / "profiles" / "liveprof") is True
    assert webui_profiles.is_live_profile_home_for_reads(base / "profiles" / "ghostprof") is False
    # ...and the resolved path must NOT bypass the same checks.
    assert webui_profiles.is_live_profile_home_for_reads(resolved) is False

    # Real reader: a stale cookie for the deleted home yields no jobs and must
    # not create cron/ + cron/output/ inside the ghost shell.
    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("ghostprof")
    assert active_jobs == [] and other_jobs == []
    assert not (data / "ghostprof" / "cron").exists()
    assert not (base / "profiles" / "ghostprof" / "cron").exists()


def test_symlinked_profiles_root_reads_live_named_home(tmp_path, monkeypatch):
    """Control for the symlink fix: a LIVE named home reached through the
    symlinked root stays readable (the fix must not over-deny)."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    base = tmp_path / ".hermes"
    base.mkdir()
    (base / "config.yaml").write_text("model: base\n", encoding="utf-8")
    data = tmp_path / "mnt" / "profile-data"
    live = data / "liveprof"
    live.mkdir(parents=True)
    (live / "config.yaml").write_text("model: live\n", encoding="utf-8")
    _write_saved_default_job(live, "live")
    (base / "profiles").symlink_to(data, target_is_directory=True)

    _point_runtime_at_base(monkeypatch, base, [{"name": "liveprof", "visible": True}])

    resolved = webui_profiles._resolve_named_profile_home("liveprof")
    assert webui_profiles.is_live_profile_home_for_reads(resolved) is True

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("liveprof")
    assert other_jobs == []
    assert [j["name"] for j in active_jobs] == ["live-job"], [j["name"] for j in active_jobs]


# ── Maintainer residual (2026-11-08): resolved-root ownership must precede an
# ── unrelated live named ancestor ───────────────────────────────────────────

def _make_symlinked_root_beneath_live_ancestor(tmp_path: Path):
    """The maintainer's layout: the configured ``profiles`` root is a symlink
    whose target itself lives under an unrelated real ``profiles`` root.

        local/.hermes/profiles -> foreign/.hermes/profiles/storage
        foreign/.hermes/profiles/storage/config.yaml   # live named ancestor
        foreign/.hermes/profiles/storage/actualprof/   # requested child

    The resolved child path therefore carries a second ``profiles`` ancestor
    (``storage``) that the native classifier reports as a live named home.
    """
    base = tmp_path / "local" / ".hermes"
    base.mkdir(parents=True)
    (base / "config.yaml").write_text("model: base\n", encoding="utf-8")
    storage = tmp_path / "foreign" / ".hermes" / "profiles" / "storage"
    storage.mkdir(parents=True)
    (storage / "config.yaml").write_text("model: storage\n", encoding="utf-8")
    (base / "profiles").symlink_to(storage, target_is_directory=True)
    child = storage / "actualprof"
    child.mkdir()
    return base, storage, child


def _assert_reader_receives_resolved_child(monkeypatch, base, storage, child):
    """Pin the trap: the reader resolves the child (``profiles`` gone from the
    path) while the native ancestor classifier names the live ``storage`` home.
    """
    from api import profiles as webui_profiles
    import hermes_constants

    _point_runtime_at_base(monkeypatch, base, [{"name": "actualprof", "visible": True}])
    resolved = webui_profiles.get_hermes_home_for_profile("actualprof")
    assert resolved == child
    assert resolved.parent.name != "profiles"  # the `profiles` component was resolved away
    # The trap is real: the native ancestor classifier reports the live
    # `storage` profile home for the resolved child path.
    assert hermes_constants.named_profile_home(resolved) == storage
    return resolved


def test_resolved_root_child_markerless_denied_despite_live_ancestor(tmp_path, monkeypatch):
    """Residual: the guard must validate the logical CHILD's identity, not the
    live ``storage`` ancestor the native classifier reports. A markerless
    child is a ghost shell — reading it would recreate cron dirs inside it."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    base, storage, child = _make_symlinked_root_beneath_live_ancestor(tmp_path)
    resolved = _assert_reader_receives_resolved_child(monkeypatch, base, storage, child)

    assert webui_profiles.is_live_profile_home_for_reads(resolved) is False

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("actualprof")
    assert active_jobs == [] and other_jobs == []
    assert not (child / "cron").exists()


def test_resolved_root_child_tombstoned_denied_despite_live_ancestor(tmp_path, monkeypatch):
    """Residual: the tombstone beside the RESOLVED root's ``<id>`` (reachable
    through the symlink) must deny the child even though the ancestor is live."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    base, storage, child = _make_symlinked_root_beneath_live_ancestor(tmp_path)
    # Same identity marker as a live profile, so only the tombstone decides.
    (child / "config.yaml").write_text("model: child\n", encoding="utf-8")
    (storage / ".deleted").mkdir()
    (storage / ".deleted" / "actualprof").write_text("deleted\n", encoding="utf-8")

    resolved = _assert_reader_receives_resolved_child(monkeypatch, base, storage, child)

    assert webui_profiles.is_live_profile_home_for_reads(resolved) is False

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("actualprof")
    assert active_jobs == [] and other_jobs == []
    assert not (child / "cron").exists()


def test_resolved_root_live_child_reads_through_live_named_ancestor(tmp_path, monkeypatch):
    """Control: the fix must not over-deny — a LIVE child under the same
    symlinked root keeps its saved job through the real reader."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    base, storage, child = _make_symlinked_root_beneath_live_ancestor(tmp_path)
    (child / "config.yaml").write_text("model: child\n", encoding="utf-8")
    _write_saved_default_job(child, "actualprof")

    resolved = _assert_reader_receives_resolved_child(monkeypatch, base, storage, child)

    assert webui_profiles.is_live_profile_home_for_reads(resolved) is True

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("actualprof")
    assert other_jobs == []
    assert [j["name"] for j in active_jobs] == ["actualprof-job"], [j["name"] for j in active_jobs]
