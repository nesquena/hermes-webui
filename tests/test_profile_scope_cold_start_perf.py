"""Cold-start performance contract for sidebar profile scoping."""

from pathlib import Path


def test_known_default_root_profile_cache_starts_warm():
    source = (Path(__file__).parent.parent / "api" / "profiles.py").read_text(encoding="utf-8")

    assert "_root_profile_name_cache: set[str] = {'default'}" in source
    assert "_root_profile_name_cache_loaded = True" in source