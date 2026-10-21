"""Regression tests for issue #7940: the all-profiles session scan must not
build the profile picker's rows to learn the profile names.

``_all_profiles_cli_contexts()`` called ``list_profiles_api()`` only to
enumerate names, which made ``GET /api/sessions?all_profiles=1`` pay for every
profile's skill counts (the dominant stage in the issue's measurements). The
homes it needs are the root profile's, the active profile's, and one per
directory under the named-profile root, which it already scanned.
"""

import pytest


@pytest.fixture
def profile_homes(tmp_path, monkeypatch):
    """A root home and three named profiles, with the profile list replaced by
    a recorder: the function under test swallows enumeration errors, so a call
    is counted, not raised on."""
    from api import profiles

    default_home = tmp_path / ".hermes"
    profiles_root = default_home / "profiles"
    for name in ("writer", "research", "ops"):
        (profiles_root / name).mkdir(parents=True)
    (profiles_root / "notes.txt").write_text("not a profile", encoding="utf-8")

    def resolve(profile_name):
        if not profile_name or profile_name == "default":
            return default_home
        return profiles_root / profile_name

    asked: list = []

    def recorded_profile_list():
        asked.append("list_profiles_api")
        return [{"name": "default"}, {"name": "writer"}]

    monkeypatch.setattr(profiles, "_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", resolve)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "list_profiles_api", recorded_profile_list)
    return profiles, default_home.resolve(), profiles_root.resolve(), asked


def _labels(contexts):
    return [profile for _home, _db_path, profile in contexts]


def test_every_profile_is_enumerated_once_without_the_profile_list(profile_homes):
    from api import models

    _profiles, default_home, profiles_root, asked = profile_homes

    contexts, cache_key = models._all_profiles_cli_contexts()

    assert asked == []
    assert _labels(contexts) == ["default", "ops", "research", "writer"]
    homes = [home for home, _db_path, _profile in contexts]
    assert homes == [
        default_home,
        profiles_root / "ops",
        profiles_root / "research",
        profiles_root / "writer",
    ]
    assert [db_path for _home, db_path, _profile in contexts] == [
        home / "state.db" for home in homes
    ]
    assert len(cache_key) == 4


def test_the_active_named_profile_leads_and_is_not_repeated(profile_homes, monkeypatch):
    from api import models

    profiles, _default_home, _profiles_root, asked = profile_homes
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "writer")

    contexts, _cache_key = models._all_profiles_cli_contexts()

    assert asked == []
    assert _labels(contexts) == ["writer", "default", "ops", "research"]


def test_the_root_profile_is_included_with_no_named_profiles(tmp_path, monkeypatch):
    """A fresh install has no profiles directory at all."""
    from api import models, profiles

    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setattr(profiles, "_profiles_root", lambda: default_home / "profiles")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: default_home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)
    asked: list = []
    monkeypatch.setattr(
        profiles, "list_profiles_api", lambda: asked.append("list_profiles_api") or []
    )

    contexts, _cache_key = models._all_profiles_cli_contexts()

    assert asked == []
    assert _labels(contexts) == ["default"]


def test_names_that_resolve_to_one_home_give_one_context(profile_homes, monkeypatch):
    """Isolated profile mode clamps every lookup to the pinned home."""
    from api import models

    profiles, _default_home, profiles_root, _asked = profile_homes
    pinned = profiles_root / "writer"
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: pinned)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "writer")

    contexts, _cache_key = models._all_profiles_cli_contexts()

    assert [(home, profile) for home, _db, profile in contexts] == [(pinned, "writer")]
