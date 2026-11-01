"""Regression coverage for #6619 / #6620 — extension archive dotfile handling.

`_is_safe_relative_path()` is shared by static serving, asset URLs and manifest
paths, and MUST stay strict (no dot-prefixed segment ever reachable) — loosening
it once let `/extensions/.env`, `/extensions/.git/config`, etc. return HTTP 200.
Archive members instead go through `_is_safe_archive_member()`, which permits a
narrow allowlist of benign hidden LEAF files (`.gitkeep`, `.env.example`, …) so
extensions can ship them, without touching the shared validator.
"""
import pytest

from api.extensions import _is_safe_relative_path, _is_safe_archive_member


class TestSharedValidatorStaysStrict:
    """Static serving / assets / manifest must never reach a hidden file."""

    @pytest.mark.parametrize("rel", [
        ".env", ".secret", ".git", ".git/config", ".git/hooks/pre-commit",
        ".gitkeep", ".gitignore", ".env.example",
        "screenshots/.gitkeep", "sub/.env.example", "a/.git/config",
    ])
    def test_rejects_every_dotfile(self, rel):
        assert _is_safe_relative_path(rel) is False

    @pytest.mark.parametrize("rel", ["index.html", "assets/style.css", "a/b/c.js"])
    def test_allows_normal_paths(self, rel):
        assert _is_safe_relative_path(rel) is True

    @pytest.mark.parametrize("rel", ["../evil", "a/../../etc", "a/./b", "..", ".", "", "a\x00b", "a\\b"])
    def test_still_blocks_traversal_and_junk(self, rel):
        assert _is_safe_relative_path(rel) is False


class TestArchiveMemberAllowlist:
    """Install/uninstall: benign hidden leaf files allowed, nothing else loosened."""

    @pytest.mark.parametrize("rel", [
        ".gitkeep", ".gitignore", ".gitattributes", ".env.example",
        "screenshots/.gitkeep", "a/b/.env.example",
        "profile-avatars/screenshots/.gitkeep",
    ])
    def test_allows_benign_leaf_dotfiles(self, rel):
        assert _is_safe_archive_member(rel) is True

    @pytest.mark.parametrize("rel", ["index.html", "assets/x.css", "a/b/c.js"])
    def test_allows_normal_paths(self, rel):
        assert _is_safe_archive_member(rel) is True

    @pytest.mark.parametrize("rel", [
        ".env", ".secret", ".htaccess", ".npmrc",            # non-allowlisted leaf dotfiles
        ".git/config", ".git/hooks/pre-commit", "a/.git/config", ".ssh/id_rsa",  # dot-directories
        ".gitignore/evil",                                    # allowlisted name used as a directory
        "../evil", "a/../../etc", "a/./b", "..", ".",         # traversal
        "", "a\x00b", "a\\b",                                 # junk / separators
    ])
    def test_rejects_everything_else(self, rel):
        assert _is_safe_archive_member(rel) is False
