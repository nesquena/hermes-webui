"""Agent-free coverage for the cron cross-profile liveness guard.

CI installs no Hermes Agent, so every test that needs the real
``hermes_constants`` / ``hermes_cli`` skips there — and with them this guard's
live, tombstoned, helper-missing and older-Agent branches. This module keeps
those branches covered without an Agent:

* a meta-path finder refuses every Agent module name, so a locally installed
  checkout can never silently satisfy an import (that is the CI shape, and a
  test that only passes because an Agent happens to be on disk is worthless);
* a stub ``hermes_constants`` mirroring the Agent's documented rules (identity
  markers, the tombstone beside the profile dir, the ``.hermes`` stop boundary,
  ``get_default_hermes_root``) plays the *installed* Agent where a test needs
  one;
* a stub ``cron.jobs`` reader lets the Tasks aggregator run, so the route-level
  "the saved job survives" assertions hold without the Agent's cron store.

``api.profiles`` resolves ``hermes_constants`` from ``sys.modules`` at call
time, so the stub — never a real checkout — is what the guard consults, and the
same assertions hold on a machine that has an Agent installed.

Run with the repo's standard pytest invocation; no Agent discovery is required.
"""

from __future__ import annotations

import importlib.abc
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

WEBUI_ROOT = Path(__file__).resolve().parents[1]
# Why the repo root goes on sys.path at import time, and why importorskip is
# not an option here (the repo's usual precedent):
#
# * the tests below do ``from api import profiles``, and ``api.profiles``
#   itself imports sibling ``api`` modules (``api.yaml_compat``, ``api.paths``),
#   so only the repo root on sys.path satisfies them. Collection already
#   imports this module, so the entry has to exist before any fixture runs —
#   including under ``--noconftest`` and from any cwd, where no conftest has
#   put the root there yet;
# * this module exists to run *exactly where the Agent is absent* (CI), so a
#   ``pytest.importorskip`` would skip it in the one place its coverage must
#   execute — the opposite of the goal. The Agent-present file
#   (``test_profile_enumeration_live_only.py``) keeps the importorskip gate.
# Only the WebUI repo root is inserted — never an Agent path — and the
# meta-path blocker below refuses every Agent module name, so this insertion
# cannot pull Agent code into the process.
if str(WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(WEBUI_ROOT))

# ── The Agent-free environment ───────────────────────────────────────────────

# Top-level packages that only exist inside a hermes-agent checkout. Refusing
# them turns "no Agent installed" into a hard error instead of a silent pass.
_AGENT_MODULE_ROOTS = frozenset({
    "agent", "cron", "gateway", "hermes", "hermes_agent", "hermes_cli",
    "hermes_constants", "pm", "plugins", "run_agent", "tools",
})


class _AgentFreeBlocker(importlib.abc.MetaPathFinder):
    """Refuse every Agent module name so no local checkout can satisfy an import."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _AGENT_MODULE_ROOTS:
            raise ImportError(f"agent-free test blocked Agent module {fullname!r}")
        return None


def _detach_agent_modules(monkeypatch) -> None:
    """Drop already-imported Agent modules so they cannot be resolved either.

    The shared conftest imports the Agent in-process (it boots a test server and
    reads ``api.config``), so a locally installed checkout is otherwise already
    sitting in ``sys.modules``. Removing those entries for the duration of a
    test reproduces the state CI actually runs in; they are restored afterwards.
    """
    for name in [n for n in sys.modules if n.split(".")[0] in _AGENT_MODULE_ROOTS]:
        monkeypatch.delitem(sys.modules, name)


@pytest.fixture
def agent_free(monkeypatch):
    """Simulate a machine with no Hermes Agent installed at all (the CI shape)."""
    _detach_agent_modules(monkeypatch)
    blocker = _AgentFreeBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield blocker
    finally:
        sys.meta_path.remove(blocker)


# ── A faithful hermes_constants stub ─────────────────────────────────────────

# Mirrors hermes_constants._PROFILE_IDENTITY_MARKERS / _DELETED_PROFILES_DIR /
# _HERMES_HOME_MARKERS, plus the documented rules of named_profile_home,
# named_profile_is_live and get_default_hermes_root.
_STUB_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
_STUB_HOME_MARKERS = ("config.yaml", ".env", "state.db")
_STUB_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _stub_default_hermes_root() -> Path:
    """``get_default_hermes_root()``: native home, or HERMES_HOME's own root."""
    native = Path.home() / ".hermes"
    env = os.environ.get("HERMES_HOME", "").strip()
    env_path = Path(env).expanduser() if env else None
    if env_path is None:
        return native
    try:
        env_path.resolve().relative_to(native.resolve())
    except ValueError:
        return env_path.parent.parent if env_path.parent.name == "profiles" else env_path
    return native


def _stub_is_hermes_profiles_root(profiles_dir: Path) -> bool:
    root = profiles_dir.parent
    if root.name == ".hermes":
        return True
    try:
        if (profiles_dir / ".deleted").is_dir() or any(
            (root / marker).exists() for marker in _STUB_HOME_MARKERS
        ):
            return True
    except OSError:
        pass
    try:
        return root.resolve(strict=False) == _stub_default_hermes_root().resolve(strict=False)
    except OSError:
        return False


def _stub_named_profile_home(path):
    """``named_profile_home()``: the ``<root>/profiles/<id>`` owning *path*."""
    current = Path(path)
    for candidate in (current, *current.parents):
        if (candidate.parent.name == "profiles" and not candidate.name.startswith(".")
                and _stub_is_hermes_profiles_root(candidate.parent)):
            return candidate
        if candidate.name == ".hermes":
            return None
    return None


def _stub_named_profile_has_identity(home) -> bool:
    home = Path(home)
    return any((home / marker).is_file() or (home / marker).is_symlink()
               for marker in _STUB_IDENTITY_MARKERS)


def _stub_named_profile_is_deleted(home) -> bool:
    home = Path(home)
    return (home.parent / ".deleted" / home.name).exists()


def _build_agent_stub() -> types.ModuleType:
    module = types.ModuleType("hermes_constants")
    module.calls = []  # records every named_profile_is_live() the guard makes
    module.named_profile_home = _stub_named_profile_home
    module.named_profile_has_identity = _stub_named_profile_has_identity
    module.named_profile_is_deleted = _stub_named_profile_is_deleted
    module.get_default_hermes_root = _stub_default_hermes_root
    module.clear_named_profile_deleted = (
        lambda home: (Path(home).parent / ".deleted" / Path(home).name).unlink(missing_ok=True)
    )

    def _is_live(home):
        module.calls.append(Path(home))
        home = Path(home)
        return home.is_dir() and _stub_named_profile_has_identity(home) and not _stub_named_profile_is_deleted(home)

    module.named_profile_is_live = _is_live
    return module


def _install_module(monkeypatch, name: str, module: types.ModuleType) -> None:
    """Install *module* as *name*, restoring whatever the process had before."""
    monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def stub_agent(agent_free, monkeypatch):
    """No Agent on disk, but a faithful ``hermes_constants`` stub in sys.modules."""
    module = _build_agent_stub()
    _install_module(monkeypatch, "hermes_constants", module)
    return module


def _build_cron_jobs_stub() -> types.ModuleType:
    """A ``cron.jobs`` reader that serves the home the cron scope pinned."""
    module = types.ModuleType("cron.jobs")

    def list_jobs(include_disabled=False):
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        jobs_file = home / "cron" / "jobs.json"
        if not jobs_file.is_file():
            return []
        try:
            payload = json.loads(jobs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        return [job for job in jobs if include_disabled or job.get("enabled", True)]

    module.list_jobs = list_jobs
    return module


@pytest.fixture
def stub_cron_reader(stub_agent, monkeypatch):
    """Adds the ``cron.jobs`` reader the Tasks aggregator imports."""
    module = _build_cron_jobs_stub()
    _install_module(monkeypatch, "cron.jobs", module)
    return module


# ── Fixtures shared with the Agent-present file ──────────────────────────────

def _make_homes(tmp_path: Path):
    """A default home plus live / tombstoned / marker-less named profiles."""
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
    # Same identity marker as `live`, so only the tombstone decides.
    (ghost / "config.yaml").write_text("model: y\n", encoding="utf-8")

    shell = profiles / "shellprof"
    shell.mkdir()

    (profiles / ".deleted").mkdir()
    (profiles / ".deleted" / "ghostprof").write_text("deleted\n", encoding="utf-8")
    return home, profiles, live, ghost, shell


def _write_saved_job(home: Path, label: str) -> None:
    """A real saved cron job in *home* (the shape #7295's fixture uses)."""
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
    webui_profiles._invalidate_root_profile_cache()


# ── Agent-absent deployment: no Agent module is importable at all ────────────

def test_agent_absent_live_and_dead_named_homes(agent_free, tmp_path):
    """The CI shape: no Agent, so the shape grammar plus the older-Agent rule decide.

    A live named home stays readable (its jobs must not vanish just because the
    deployment has no Agent), tombstoned and marker-less shells are still
    skipped, and the default home never needed an import to be classified.
    """
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    # Precondition: the Agent really is unreachable in this environment.
    with pytest.raises(ImportError):
        from hermes_constants import named_profile_is_live  # noqa: F401

    assert webui_profiles.is_live_profile_home_for_reads(live) is True
    assert webui_profiles.is_live_profile_home_for_reads(live, owner_profile="liveprof") is True
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False  # tombstoned
    assert webui_profiles.is_live_profile_home_for_reads(shell) is False  # marker-less
    assert webui_profiles.is_live_profile_home_for_reads(home) is True


# ── Stubbed Agent: live / tombstoned / helper-missing / raising ──────────────

def test_stub_agent_live_tombstoned_and_default_homes(stub_agent, tmp_path):
    """With an Agent present, its own classifier and liveness helper decide."""
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    assert webui_profiles._named_profile_home_for_reads(live) == live
    assert webui_profiles.is_live_profile_home_for_reads(live) is True
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False
    assert webui_profiles.is_live_profile_home_for_reads(shell) is False
    assert webui_profiles.is_live_profile_home_for_reads(home) is True

    # The stub — not some real checkout — produced those verdicts, and the
    # default home was admitted without consulting the helper at all.
    assert any(call == live for call in stub_agent.calls)
    assert not any(call == home for call in stub_agent.calls)


def test_stub_agent_missing_liveness_helper_uses_the_older_agent_rule(stub_agent, tmp_path, monkeypatch):
    """Older Agent cores load and serve cron jobs but never shipped the helper.

    The missing helper must not read as "not live" — that blanked Tasks for every
    named profile on those cores. The Agent's own identity/tombstone rule decides
    instead, so the live home stays readable while ghosts are still skipped.
    """
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    monkeypatch.delattr(stub_agent, "named_profile_is_live", raising=False)

    assert webui_profiles._named_profile_home_for_reads(live) == live
    assert webui_profiles.is_live_profile_home_for_reads(live) is True
    assert webui_profiles.is_live_profile_home_for_reads(live, owner_profile="liveprof") is True
    assert webui_profiles.is_live_profile_home_for_reads(ghost) is False  # tombstoned
    assert webui_profiles.is_live_profile_home_for_reads(shell) is False  # marker-less
    assert webui_profiles.is_live_profile_home_for_reads(home) is True


def test_stub_agent_raising_liveness_check_still_denies(stub_agent, tmp_path, monkeypatch):
    """A liveness check that RAISES is a failure, not a missing helper: deny."""
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    def _boom(_home):
        raise OSError("identity unreadable")

    monkeypatch.setattr(stub_agent, "named_profile_is_live", _boom)
    assert webui_profiles.is_live_profile_home_for_reads(live) is False
    # The default home is a shape we can prove, so it never needed the helper.
    assert webui_profiles.is_live_profile_home_for_reads(home) is True


def test_stub_agent_raising_classifier_falls_back_to_the_shape_grammar(stub_agent, tmp_path, monkeypatch):
    """A classifier that raises must not fail open: the shape grammar decides.

    With liveness still confirmable the shape grammar's named verdict is enough
    to keep reading; with BOTH checks raising, a path that merely LOOKS named is
    denied rather than admitted.
    """
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)

    def _boom(_path):
        raise OSError("classifier unreadable")

    monkeypatch.setattr(stub_agent, "named_profile_home", _boom)
    # Shape grammar still classifies the named home, and the working liveness
    # helper confirms it...
    assert webui_profiles._named_profile_home_for_reads(live) == live
    assert webui_profiles.is_live_profile_home_for_reads(live) is True
    assert webui_profiles.is_live_profile_home_for_reads(home) is True

    # ...but when liveness cannot be confirmed either, the same home is denied.
    monkeypatch.setattr(stub_agent, "named_profile_is_live", _boom)
    assert webui_profiles.is_live_profile_home_for_reads(live) is False
    assert webui_profiles.is_live_profile_home_for_reads(home) is True


# ── Must-fix 2: a symlinked profile dir is checked under its logical name ────

def _make_symlinked_profile_dir(tmp_path: Path):
    """``profiles/worker`` is a symlink to ``profiles/storage/worker-home``."""
    base = tmp_path / ".hermes"
    base.mkdir()
    (base / "config.yaml").write_text("model: base\n", encoding="utf-8")
    profiles = base / "profiles"
    profiles.mkdir()
    storage = profiles / "storage"
    storage.mkdir()
    worker_home = storage / "worker-home"
    worker_home.mkdir()
    (worker_home / "config.yaml").write_text("model: worker\n", encoding="utf-8")
    (profiles / "worker").symlink_to(worker_home, target_is_directory=True)
    return base, profiles, storage, worker_home


def test_symlinked_profile_dir_is_checked_under_its_logical_name(stub_cron_reader, tmp_path, monkeypatch):
    """Must-fix 2: a live symlinked profile keeps its saved jobs in Tasks.

    The guard derived ``storage`` (marker-less) from the RESOLVED path, so the
    live ``worker`` profile's jobs disappeared. The route passes the logical
    ``owner_profile`` and the guard checks that home's identity/tombstone; the
    resolved path is used only for containment and cron I/O.
    """
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    base, profiles, storage, worker_home = _make_symlinked_profile_dir(tmp_path)
    _write_saved_job(worker_home, "worker")
    _point_runtime_at_base(monkeypatch, base, [{"name": "worker", "visible": True}])

    resolved = webui_profiles.get_hermes_home_for_profile("worker")
    assert resolved.resolve(strict=False) == worker_home.resolve(strict=False)
    assert resolved.parent.name == "storage"  # the `profiles` component is gone

    # Path-only classification answers with the marker-less ancestor...
    assert webui_profiles._named_profile_home_for_reads(resolved) == storage
    assert webui_profiles.is_live_profile_home_for_reads(resolved) is False
    # ...the logical owner_profile keeps the live home readable.
    assert webui_profiles.is_live_profile_home_for_reads(resolved, owner_profile="worker") is True

    # Real reader: the saved job survives, and nothing is created in the ancestor.
    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("worker")
    assert other_jobs == []
    assert [j["name"] for j in active_jobs] == ["worker-job"], [j["name"] for j in active_jobs]
    assert not (storage / "cron").exists()


def test_symlinked_profile_dir_tombstone_uses_the_logical_name(stub_cron_reader, tmp_path, monkeypatch):
    """Control for must-fix 2: the tombstone beside the LOGICAL home still denies."""
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    base, profiles, storage, worker_home = _make_symlinked_profile_dir(tmp_path)
    _write_saved_job(worker_home, "worker")
    (profiles / ".deleted").mkdir()
    (profiles / ".deleted" / "worker").write_text("deleted\n", encoding="utf-8")
    _point_runtime_at_base(monkeypatch, base, [{"name": "worker", "visible": True}])

    resolved = webui_profiles.get_hermes_home_for_profile("worker")
    assert webui_profiles.is_live_profile_home_for_reads(resolved, owner_profile="worker") is False

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("worker")
    assert active_jobs == [] and other_jobs == []


# ── Must-fix 3: a custom default home under a `profiles/` ancestor ───────────

def test_custom_default_home_under_profiles_ancestor_is_readable(stub_agent, stub_cron_reader, tmp_path, monkeypatch):
    """Must-fix 3: the configured default home is admitted before classifying.

    ``<root>/.hermes/profiles/storage/custom-root`` is a valid custom
    ``HERMES_HOME``: both the Agent's ``get_default_hermes_root()`` and WebUI's
    base-home resolver accept it as the default root. The ancestor classifier
    still mapped it to the marker-less ``storage`` profile, so the default
    profile's own saved jobs disappeared — contradicting the PR's "default and
    custom homes are unaffected" claim.
    """
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    root = tmp_path / "root"
    storage = root / ".hermes" / "profiles" / "storage"
    base = storage / "custom-root"
    base.mkdir(parents=True)
    (base / "config.yaml").write_text("model: custom\n", encoding="utf-8")
    _write_saved_job(base, "custom")
    _point_runtime_at_base(monkeypatch, base, [{"name": "default", "visible": True}])

    # Precondition pins: the Agent accepts the custom root as the default...
    assert stub_agent.get_default_hermes_root() == base
    # ...while the classifier maps it to the marker-less `storage` ancestor: the trap.
    assert stub_agent.named_profile_home(base) == storage

    assert webui_profiles.is_live_profile_home_for_reads(base) is True
    # Admission is scoped to the configured default home: the marker-less
    # ancestor it lives inside is still not a live named profile.
    assert webui_profiles.is_live_profile_home_for_reads(storage) is False

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("default")
    assert other_jobs == []
    assert [j["name"] for j in active_jobs] == ["custom-job"], [j["name"] for j in active_jobs]


# ── Per-entry isolation: an unreadable tombstone store ───────────────────────

def _install_hermes_cli_profiles_stub(monkeypatch, *, home: Path, profiles: Path, tombstone_exc=None):
    """Install a ``hermes_cli.profiles`` stand-in the fast path imports from.

    ``_build_profile_rows_fast`` imports its helpers from ``hermes_cli.profiles``
    at call time, so a stub here exercises the real code path on a machine with
    no Agent on disk — and lets one helper raise on demand.
    """
    profiles_mod = types.ModuleType("hermes_cli.profiles")
    profiles_mod._get_default_hermes_home = lambda: home
    profiles_mod._get_profiles_root = lambda: profiles
    profiles_mod._read_config_model = lambda _home: (None, None)
    profiles_mod._check_gateway_running = lambda _home: False
    profiles_mod._PROFILE_ID_RE = _STUB_PROFILE_ID_RE
    profiles_mod.named_profile_has_identity = _stub_named_profile_has_identity
    if tombstone_exc is None:
        profiles_mod.named_profile_is_deleted = _stub_named_profile_is_deleted
    else:
        def _tombstone_raises(_home):
            raise tombstone_exc
        profiles_mod.named_profile_is_deleted = _tombstone_raises

    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []  # package marker so `from hermes_cli.profiles import ...` resolves
    pkg.profiles = profiles_mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles_mod)
    return profiles_mod


def test_tombstone_stub_control_lists_live_named_home(agent_free, monkeypatch, tmp_path):
    """Control: with a readable tombstone store the same stub lists the live home.

    Without this arm, the regression below would also pass if the stub simply
    never reached the named-home branch.
    """
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    _install_hermes_cli_profiles_stub(monkeypatch, home=home, profiles=profiles)
    webui_profiles._invalidate_list_profiles_cache()

    rows = webui_profiles._build_profile_rows_fast()

    assert rows is not None
    assert [row["name"] for row in rows] == ["default", "liveprof"]


def test_unreadable_tombstone_store_keeps_the_default_row(agent_free, monkeypatch, tmp_path):
    """An unreadable ``profiles/.deleted`` must not erase the whole listing.

    The native ``named_profile_is_deleted`` reads the tombstone store beside
    every named home, so one unreadable store raises for each entry. Unguarded,
    that takes the entire enumeration with it — including the default row the
    cron aggregator needs before it can return any job at all. The guard must
    isolate the failure per entry: the default home survives, and the named
    entries that cannot be classified are skipped rather than fatal.
    """
    from api import profiles as webui_profiles

    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    _install_hermes_cli_profiles_stub(
        monkeypatch, home=home, profiles=profiles,
        tombstone_exc=PermissionError(13, "Permission denied"),
    )
    webui_profiles._invalidate_list_profiles_cache()

    rows = webui_profiles._build_profile_rows_fast()

    assert rows is not None, "the fast path must stay on (the helpers imported fine)"
    names = [row["name"] for row in rows]
    assert names == ["default"], names


# ── Aggregator level: the real `list_profiles_api()` inside `/api/crons` ─────
#
# The builder/stub tests above call `_build_profile_rows_fast()` directly, so
# they cannot catch a regression in the two entry points the reviewer named:
# `list_profiles_api()` and `_cron_jobs_cross_profile("default")`. These tests
# drive both for real — no monkeypatched listing — so the default home's saved
# job (and its ownership / read-only fields) is what is actually asserted.

def _point_aggregator_at_base(monkeypatch, home: Path, profiles: Path):
    """Point the runtime at *home* WITHOUT stubbing `list_profiles_api`.

    Same boot shape as `_point_runtime_at_base`, minus the listing stub: the
    aggregator must go through the real builder/stub chain under test.
    """
    from api import profiles as webui_profiles
    from api import routes as webui_routes

    monkeypatch.setenv("HERMES_BASE_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(webui_profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(webui_profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(webui_routes, "_is_isolated_profile_mode", lambda: False)
    webui_profiles._invalidate_list_profiles_cache()
    webui_profiles._invalidate_root_profile_cache()


def test_cron_aggregator_survives_an_unreadable_tombstone_store(agent_free, stub_cron_reader, monkeypatch, tmp_path):
    """An unreadable tombstone store must not erase the default profile's jobs.

    `_cron_jobs_cross_profile("default")` enumerates profiles before it reads
    any cron store, so when that enumeration raises the aggregator loses even
    the default home's saved job — the second half of the reported blocker.
    This drives the REAL listing and the REAL aggregator.
    """
    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    _write_saved_job(home, "default")
    _install_hermes_cli_profiles_stub(
        monkeypatch, home=home, profiles=profiles,
        tombstone_exc=PermissionError(13, "Permission denied"),
    )
    _point_aggregator_at_base(monkeypatch, home, profiles)

    from api import profiles as webui_profiles
    from api import routes as webui_routes

    # Precondition: the listing itself must stay up (it is what raised before).
    assert [row["name"] for row in webui_profiles.list_profiles_api()] == ["default"]

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("default")

    assert other_jobs == [], other_jobs
    assert [job["name"] for job in active_jobs] == ["default-job"], active_jobs
    job = active_jobs[0]
    assert job["owner_profile"] == "default", job
    assert job["read_only"] is False, (
        "the default profile's own job must stay writable in the active view"
    )


def test_cron_aggregator_healthy_control_keeps_the_default_job(agent_free, stub_cron_reader, monkeypatch, tmp_path):
    """Control: with a readable tombstone store the same path still works.

    Without this arm the regression above would also pass if the harness never
    reached the aggregator at all.
    """
    home, profiles, live, ghost, shell = _make_homes(tmp_path)
    _write_saved_job(home, "default")
    _install_hermes_cli_profiles_stub(monkeypatch, home=home, profiles=profiles)
    _point_aggregator_at_base(monkeypatch, home, profiles)

    from api import profiles as webui_profiles
    from api import routes as webui_routes

    assert [row["name"] for row in webui_profiles.list_profiles_api()] == ["default", "liveprof"]

    active_jobs, other_jobs = webui_routes._cron_jobs_cross_profile("default")

    assert other_jobs == [], other_jobs
    assert [job["name"] for job in active_jobs] == ["default-job"], active_jobs
    assert active_jobs[0]["owner_profile"] == "default", active_jobs[0]
    assert active_jobs[0]["read_only"] is False, active_jobs[0]
