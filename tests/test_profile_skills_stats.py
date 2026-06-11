from pathlib import Path
import yaml
from api import profiles
from tests.conftest import requires_agent_modules


def _write_skill(root: Path, name: str, platforms=None):
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    platforms_line = f"platforms: {platforms}\n" if platforms else ""
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n{platforms_line}---\n\n# {name}\n",
        encoding="utf-8",
    )

def _write_config(home: Path, disabled):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"disabled": list(disabled)}}, sort_keys=False),
        encoding="utf-8",
    )

@requires_agent_modules
def test_get_profile_skills_stats(tmp_path):
    # Setup skills directory with:
    # 1 compatible & enabled skill ("alpha")
    # 1 compatible & disabled skill ("beta")
    # 1 incompatible skill ("gamma" - macos only on linux test run)
    profile_home = tmp_path / "auditor"
    _write_skill(profile_home, "alpha")
    _write_skill(profile_home, "beta")
    _write_skill(profile_home, "gamma", platforms=["macos"])
    _write_config(profile_home, ["beta"])

    # Explicitly clear the stats cache to ensure we compute fresh
    profiles._SKILLS_STATS_CACHE.clear()

    enabled, compatible = profiles._get_profile_skills_stats(profile_home)
    assert enabled == 1
    assert compatible == 2

@requires_agent_modules
def test_list_profiles_api_contains_formatted_skills(monkeypatch, tmp_path):
    """list_profiles_api() formats skill counts for each profile.

    Drives the fast path (``_build_profile_rows_fast``), which discovers
    profiles via the cheap upstream helpers and skips the alias scan, so this
    patches ``_get_default_hermes_home`` / ``_get_profiles_root`` to point at a
    tmp profile layout rather than monkeypatching the (now-bypassed)
    ``list_profiles`` aggregate.
    """
    p_default = tmp_path / "default"
    profiles_root = tmp_path / "profiles"
    p_fintech = profiles_root / "fintech"

    _write_skill(p_default, "a1")
    _write_skill(p_default, "a2")
    _write_config(p_default, ["a2"])

    _write_skill(p_fintech, "f1")
    _write_skill(p_fintech, "f2")
    _write_skill(p_fintech, "f3")
    _write_config(p_fintech, ["f2", "f3"])

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    # Point the fast-path discovery helpers at our tmp layout. The base home is
    # surfaced as "default" by _build_profile_rows_fast regardless of dir name.
    import hermes_cli.profiles as cli_p
    monkeypatch.setattr(cli_p, "_get_default_hermes_home", lambda: p_default)
    monkeypatch.setattr(cli_p, "_get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(cli_p, "_check_gateway_running", lambda home: False)

    profiles._SKILLS_STATS_CACHE.clear()
    profiles._invalidate_list_profiles_cache()

    results = profiles.list_profiles_api()
    by_name = {p["name"]: p for p in results}

    assert "default" in by_name
    assert "fintech" in by_name

    # backward-compatible skill_count as an integer:
    assert by_name["default"]["skill_count"] == 1
    assert by_name["fintech"]["skill_count"] == 1

    # new enabled_skills and total_skills integer fields:
    assert by_name["default"]["enabled_skills"] == 1
    assert by_name["default"]["total_skills"] == 2
    assert by_name["fintech"]["enabled_skills"] == 1
    assert by_name["fintech"]["total_skills"] == 3

    # the base home is surfaced as the default profile
    assert by_name["default"]["is_default"] is True
    assert by_name["fintech"]["is_default"] is False
    # exactly one active, and it's the one get_active_profile_name reports
    assert sum(1 for p in results if p["is_active"]) == 1
    assert by_name["default"]["is_active"] is True

    profiles._invalidate_list_profiles_cache()

@requires_agent_modules
def test_no_skills_dir(tmp_path):
    """Profile with no skills/ directory should return (0, 0)."""
    profiles._SKILLS_STATS_CACHE.clear()
    enabled, compat = profiles._get_profile_skills_stats(tmp_path)
    assert enabled == 0 and compat == 0

@requires_agent_modules
def test_corrupt_config(tmp_path):
    """Corrupt config.yaml should not crash — disabled set stays empty."""
    profiles._SKILLS_STATS_CACHE.clear()
    _write_skill(tmp_path, "a")
    (tmp_path / "config.yaml").write_text("not: [valid: yaml: {{", encoding="utf-8")
    enabled, compat = profiles._get_profile_skills_stats(tmp_path)
    assert compat == 1 and enabled == 1  # no disabled parsing, all enabled

@requires_agent_modules
def test_platform_disabled_webui(tmp_path):
    """platform_disabled.webui list should be used when present."""
    profiles._SKILLS_STATS_CACHE.clear()
    _write_skill(tmp_path, "web-only")
    cfg = {"skills": {"platform_disabled": {"webui": ["web-only"]}, "disabled": []}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    enabled, compat = profiles._get_profile_skills_stats(tmp_path)
    assert compat == 1 and enabled == 0

@requires_agent_modules
def test_skills_stats_cache(tmp_path):
    """Verify that caching works and has short TTL behavior."""
    profiles._SKILLS_STATS_CACHE.clear()
    
    _write_skill(tmp_path, "alpha")
    enabled, compat = profiles._get_profile_skills_stats(tmp_path)
    assert enabled == 1 and compat == 1

    # Add a skill but since cache is active (TTL 8s), we should still get old values
    _write_skill(tmp_path, "beta")
    enabled, compat = profiles._get_profile_skills_stats(tmp_path)
    assert enabled == 1 and compat == 1

    # Force clear or override the mock TTL, or clear cache manually to see changes
    profiles._SKILLS_STATS_CACHE.clear()
    enabled, compat = profiles._get_profile_skills_stats(tmp_path)
    assert enabled == 2 and compat == 2


@requires_agent_modules
def test_list_profiles_api_skips_alias_scan(monkeypatch, tmp_path):
    """The fast path must NOT call find_alias_for_profile.

    find_alias_for_profile reads every file in the wrapper dir (~/.local/bin),
    including large binaries — the multi-second profile-dropdown hang. The
    WebUI discards alias data, so the fast path must avoid that call entirely.
    """
    p_default = tmp_path / "default"
    _write_skill(p_default, "a1")
    _write_config(p_default, [])

    import hermes_cli.profiles as cli_p
    monkeypatch.setattr(cli_p, "_get_default_hermes_home", lambda: p_default)
    monkeypatch.setattr(cli_p, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(cli_p, "_check_gateway_running", lambda home: False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    alias_called = []
    if hasattr(cli_p, "find_alias_for_profile"):
        monkeypatch.setattr(
            cli_p, "find_alias_for_profile",
            lambda *a, **k: alias_called.append(a) or None,
        )

    profiles._SKILLS_STATS_CACHE.clear()
    profiles._invalidate_list_profiles_cache()

    results = profiles.list_profiles_api()
    assert any(p["name"] == "default" for p in results)
    assert alias_called == [], "fast path must not call find_alias_for_profile"

    profiles._invalidate_list_profiles_cache()


@requires_agent_modules
def test_list_profiles_api_caches_and_invalidates(monkeypatch, tmp_path):
    """Repeated calls hit the TTL cache; create/delete invalidation drops it."""
    p_default = tmp_path / "default"
    _write_skill(p_default, "a1")
    _write_config(p_default, [])

    import hermes_cli.profiles as cli_p
    monkeypatch.setattr(cli_p, "_get_default_hermes_home", lambda: p_default)
    monkeypatch.setattr(cli_p, "_get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(cli_p, "_check_gateway_running", lambda home: False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    build_calls = []
    real_build = profiles._build_profile_rows_fast

    def counting_build():
        build_calls.append(1)
        return real_build()

    monkeypatch.setattr(profiles, "_build_profile_rows_fast", counting_build)

    profiles._SKILLS_STATS_CACHE.clear()
    profiles._invalidate_list_profiles_cache()

    profiles.list_profiles_api()
    profiles.list_profiles_api()
    # Second call should be served from the TTL cache — no rebuild.
    assert len(build_calls) == 1, "second call within TTL must hit the cache"

    # Invalidation forces a rebuild on the next call.
    profiles._invalidate_list_profiles_cache()
    profiles.list_profiles_api()
    assert len(build_calls) == 2, "invalidation must force a rebuild"

    profiles._invalidate_list_profiles_cache()


@requires_agent_modules
def test_list_profiles_api_falls_back_when_fast_path_unavailable(monkeypatch, tmp_path):
    """If the cheap helpers can't build rows, fall back to upstream list_profiles."""
    class FakeProfile:
        def __init__(self, name, path):
            self.name = name
            self.path = Path(path)
            self.is_default = name == "default"
            self.gateway_running = False
            self.model = "gpt-4"
            self.provider = "openai"
            self.has_env = False
            self.skill_count = 0

    p_default = tmp_path / "default"
    _write_skill(p_default, "a1")
    _write_config(p_default, [])

    monkeypatch.setattr(profiles, "_build_profile_rows_fast", lambda: None)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    import hermes_cli.profiles as cli_p
    monkeypatch.setattr(cli_p, "list_profiles", lambda: [FakeProfile("default", p_default)])

    profiles._SKILLS_STATS_CACHE.clear()
    profiles._invalidate_list_profiles_cache()

    results = profiles.list_profiles_api()
    by_name = {p["name"]: p for p in results}
    assert "default" in by_name
    assert by_name["default"]["is_active"] is True

    profiles._invalidate_list_profiles_cache()
