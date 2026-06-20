"""Regression coverage for #3294 — profile tool configuration not respected in WebUI.

When a non-default profile leaves the per-session "Tool Restrictions"
(`enabled_toolsets`) blank, the streaming worker must resolve toolsets (and the
rest of the per-profile config: prefill context, fallback chains) from the
*session's own profile* config.yaml — not from the process-global ``default``
profile.

Root cause: the streaming agent runs on a detached ``threading.Thread`` that
does NOT inherit the per-request thread-local profile context (set from the
``hermes_profile`` cookie on the HTTP handler thread). On that worker the
ambient ``get_config()`` resolves through ``get_active_profile_name()`` which
falls back to the process-global ``_active_profile`` (usually ``default``). A
profile B with an empty ``platform_toolsets.cli`` would therefore load the
default profile's full toolset list, inflating the prompt from ~400 to ~15K
tokens (the reporter's symptom).

Fix: ``api.config.get_config_for_profile_home(profile_home)`` reads the config
for an explicit profile home directly off disk, bypassing the thread-local
resolver. The streaming worker passes the session's own resolved profile home.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _write_cfg(home: Path, cli_toolsets) -> None:
    home.mkdir(parents=True, exist_ok=True)
    import yaml

    home.joinpath("config.yaml").write_text(
        yaml.safe_dump({"platform_toolsets": {"cli": cli_toolsets}}, sort_keys=False),
        encoding="utf-8",
    )


@pytest.fixture
def _two_profiles(tmp_path, monkeypatch):
    """default profile = all tools; profile B = empty toolset list."""
    from api import config as cfg

    default_home = tmp_path / "default"
    profile_b_home = tmp_path / "profiles" / "b"
    _write_cfg(default_home, ["terminal", "file", "web", "search", "browser"])
    _write_cfg(profile_b_home, [])

    # Point the ambient resolver at the DEFAULT profile, simulating a worker
    # thread that fell back to the process-global default profile.
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(default_home / "config.yaml"))
    cfg.reload_config()
    yield cfg, default_home, profile_b_home


def test_ambient_get_config_reads_default_profile(_two_profiles):
    """Baseline: the ambient resolver sees the DEFAULT profile (the bug source)."""
    cfg, default_home, profile_b_home = _two_profiles
    ambient = cfg.get_config()
    assert ambient.get("platform_toolsets", {}).get("cli") == [
        "terminal",
        "file",
        "web",
        "search",
        "browser",
    ]


def test_get_config_for_profile_home_reads_session_profile(_two_profiles):
    """The fix: explicitly resolving profile B's home reads B's empty toolsets,
    even though the ambient resolver still points at the default profile."""
    cfg, default_home, profile_b_home = _two_profiles

    profile_b_cfg = cfg.get_config_for_profile_home(profile_b_home)
    assert profile_b_cfg.get("platform_toolsets", {}).get("cli") == []

    # And the toolset resolver run against profile B's config must yield a
    # strictly smaller set than the default profile's — the token-inflation
    # the reporter observed comes from resolving against the wrong (default)
    # profile's full list.
    default_toolsets = set(cfg._resolve_cli_toolsets(cfg.get_config()))
    profile_b_toolsets = set(cfg._resolve_cli_toolsets(profile_b_cfg))
    assert profile_b_toolsets < default_toolsets
    assert "terminal" in default_toolsets
    assert "terminal" not in profile_b_toolsets
    assert "browser" not in profile_b_toolsets


def test_matching_home_defers_to_get_config(_two_profiles):
    """When the requested home matches the ambient path, defer to get_config()
    so in-memory overrides (monkeypatched cfg / runtime overrides) are honored."""
    cfg, default_home, profile_b_home = _two_profiles
    same = cfg.get_config_for_profile_home(default_home)
    # Same dict identity path as get_config() (cached) — verify it returns the
    # default profile's config, not a fresh disk read that could miss overrides.
    assert same.get("platform_toolsets", {}).get("cli") == [
        "terminal",
        "file",
        "web",
        "search",
        "browser",
    ]


def test_matching_ambient_home_that_does_not_exist_still_defers_to_get_config(_two_profiles, monkeypatch, tmp_path):
    """Regression (#4516 gate): the nonexistent-home guard must run AFTER the
    ambient-resolver short-circuit. A home that matches the ambient config path
    but whose directory doesn't physically exist (fresh install / monkeypatched
    cfg with no dir on disk) must still defer to get_config() — NOT return {}."""
    cfg, default_home, profile_b_home = _two_profiles
    ghost_ambient = tmp_path / "ghost-ambient"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(ghost_ambient / "config.yaml"))
    cfg.reload_config()
    assert not ghost_ambient.exists()
    # The requested home matches the (nonexistent) ambient home → must defer to
    # get_config(), honoring the in-memory cfg, not short-circuit to {}.
    result = cfg.get_config_for_profile_home(ghost_ambient)
    assert result == cfg.get_config()


def test_empty_or_none_home_falls_back_to_get_config(_two_profiles):
    """A missing/empty profile home (ImportError fallback path in the worker)
    must not crash — it falls back to the ambient get_config()."""
    cfg, default_home, profile_b_home = _two_profiles
    assert cfg.get_config_for_profile_home(None) == cfg.get_config()
    assert cfg.get_config_for_profile_home("") == cfg.get_config()


def test_nonexistent_profile_home_returns_empty_not_default(_two_profiles):
    """A profile home that diverges from ambient but has no config.yaml yet
    returns an empty dict (the profile's own absent config) rather than silently
    inheriting the default profile's tools."""
    cfg, default_home, profile_b_home = _two_profiles
    ghost = profile_b_home.parent / "ghost"
    result = cfg.get_config_for_profile_home(ghost)
    assert result == {}
    # Critically: it does NOT return the default profile's full toolset list.
    assert result.get("platform_toolsets", {}).get("cli") != [
        "terminal",
        "file",
        "web",
        "search",
        "browser",
    ]
