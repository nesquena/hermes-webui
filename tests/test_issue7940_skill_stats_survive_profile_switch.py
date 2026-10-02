"""Regression tests for issue #7940: a profile switch must not throw away the
skill stats of every profile.

``switch_profile()`` cleared the process-global ``_SKILLS_STATS_CACHE`` on
every switch, per-client ones included. A profile's counts are read from that
profile's own ``config.yaml`` and ``SKILL.md`` frontmatter, so which profile
is active changes none of them, and the stat-only mtime probe in
``_get_profile_skills_stats()`` already catches a real change. The clear only
forced the next ``list_profiles_api()`` to re-read and parse every skill file
of every profile.
"""

import os

import pytest


@pytest.fixture
def profile_tree(tmp_path, monkeypatch):
    """Two profiles with one skill each, an empty skill-stats cache, and a
    compute function that records which profile it was asked to walk."""
    import api.profiles as profiles

    default_home = tmp_path / ".hermes"
    writer_home = default_home / "profiles" / "writer"
    for home, skill in ((default_home, "notes"), (writer_home, "drafts")):
        skill_dir = home / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
        (home / "config.yaml").write_text("model:\n  default: gpt-5.5\n", encoding="utf-8")

    computed: list = []

    def recording_compute(profile_dir):
        computed.append(profile_dir)
        return (1, 1)

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_SKILLS_STATS_CACHE", {})
    monkeypatch.setattr(profiles, "_compute_profile_skills_stats", recording_compute)
    # switch_profile() returns the profile list; these tests are about the
    # skill-stats cache, read through _get_profile_skills_stats() below.
    monkeypatch.setattr(
        profiles, "list_profiles_api", lambda: [{"name": "default"}, {"name": "writer"}]
    )
    # A process-wide switch repoints HERMES_HOME, the .env keys and the config
    # cache. None of that is under test, and none of it may leak into the suite.
    monkeypatch.setattr(profiles, "_set_hermes_home", lambda home: None)
    monkeypatch.setattr(profiles, "_reload_dotenv", lambda home: None)
    monkeypatch.setattr("api.config.reload_config", lambda: None)
    profiles._tls.profile = None
    yield profiles, default_home.resolve(), writer_home.resolve(), computed
    profiles._tls.profile = None


def _warm(profiles, *homes):
    for home in homes:
        assert profiles._get_profile_skills_stats(home) == (1, 1)


@pytest.mark.parametrize("process_wide", [False, True])
def test_a_switch_keeps_every_profiles_skill_stats(profile_tree, process_wide):
    profiles, default_home, writer_home, computed = profile_tree
    _warm(profiles, default_home, writer_home)
    assert computed == [default_home, writer_home]

    result = profiles.switch_profile("writer", process_wide=process_wide)

    assert result["active"] == "writer"
    assert set(profiles._SKILLS_STATS_CACHE) == {default_home, writer_home}
    # Reading the stats again walks no skill tree: both come from the cache.
    _warm(profiles, default_home, writer_home)
    assert computed == [default_home, writer_home]


def test_a_real_change_after_a_switch_recomputes_only_that_profile(profile_tree):
    """The mtime probe, not the switch, is what notices a change."""
    profiles, default_home, writer_home, computed = profile_tree
    _warm(profiles, default_home, writer_home)
    profiles.switch_profile("writer", process_wide=False)

    new_skill = writer_home / "skills" / "outline"
    new_skill.mkdir()
    (new_skill / "SKILL.md").write_text("---\nname: outline\n---\n", encoding="utf-8")
    later = os.stat(new_skill).st_mtime_ns + 5_000_000_000
    os.utime(new_skill / "SKILL.md", ns=(later, later))

    _warm(profiles, default_home, writer_home)

    assert computed == [default_home, writer_home, writer_home]
