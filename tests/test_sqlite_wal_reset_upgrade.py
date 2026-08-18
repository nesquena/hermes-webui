"""Regression coverage for the SQLite WAL-reset corruption bug upgrade.

The python:3.12-slim base ships SQLite 3.46.1 (Debian Trixie), which is
vulnerable to the WAL-reset corruption bug discovered March 2026.
https://sqlite.org/wal.html#walresetbug

The Dockerfile compiles a patched SQLite from source to replace the
system library. These tests pin the structural invariants so a
Dockerfile refactor cannot silently drop the upgrade.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_compiles_sqlite_from_source():
    """The Dockerfile must compile SQLite from the amalgamation tarball."""
    assert "sqlite-autoconf" in DOCKERFILE, (
        "Dockerfile must compile SQLite from the official amalgamation tarball "
        "to replace the vulnerable system library."
    )
    assert "sqlite.org" in DOCKERFILE, (
        "Dockerfile must download SQLite from sqlite.org."
    )


def test_dockerfile_sqlite_upgrade_uses_build_args():
    """Version, year, and SHA-256 must be build args so bumps don't require
    editing the RUN block."""
    assert "ARG SQLITE_VERSION=" in DOCKERFILE
    assert "ARG SQLITE_YEAR=" in DOCKERFILE
    assert "ARG SQLITE_SHA256=" in DOCKERFILE, (
        "Dockerfile must pin the amalgamation tarball SHA-256 as a build arg."
    )


def test_dockerfile_sqlite_upgrade_verifies_checksum():
    """The tarball must be verified against the pinned SHA-256 before
    extraction to prevent artifact substitution."""
    start = DOCKERFILE.find("ARG SQLITE_VERSION=")
    assert start != -1
    end = DOCKERFILE.find("\n\n", DOCKERFILE.find("RUN", start))
    block = DOCKERFILE[start:end] if end != -1 else DOCKERFILE[start:]

    assert "sha256sum -c" in block, (
        "The SQLite compile layer must verify the downloaded tarball against "
        "the pinned SHA-256 checksum before extraction."
    )


def test_dockerfile_sqlite_upgrade_has_build_time_assertion():
    """The upgrade layer must include a Python assertion that fails the
    build if the linked SQLite is still vulnerable."""
    assert "sqlite3.sqlite_version" in DOCKERFILE, (
        "Dockerfile must verify the linked SQLite version at build time."
    )
    assert "3, 51, 3" in DOCKERFILE, (
        "Build-time assertion must check for >= 3.51.3 (the minimum fix version)."
    )


def test_dockerfile_sqlite_upgrade_enables_fts5():
    """The compile must enable FTS5 to match the distro package's feature
    set. state.db uses FTS5 for session/message full-text search."""
    start = DOCKERFILE.find("ARG SQLITE_VERSION=")
    assert start != -1
    end = DOCKERFILE.find("\n\n", DOCKERFILE.find("RUN", start))
    block = DOCKERFILE[start:end] if end != -1 else DOCKERFILE[start:]

    assert "--enable-fts5" in block, (
        "The SQLite compile must pass --enable-fts5 to ./configure. "
        "Without it, FTS5 virtual tables (used by state.db) fail silently."
    )


def test_dockerfile_sqlite_upgrade_verifies_fts5_at_build_time():
    """The build-time assertion must prove FTS5 vtable creation works,
    not just the version number."""
    start = DOCKERFILE.find("ARG SQLITE_VERSION=")
    assert start != -1
    end = DOCKERFILE.find("\n\n", DOCKERFILE.find("RUN", start))
    block = DOCKERFILE[start:end] if end != -1 else DOCKERFILE[start:]

    assert "fts5" in block.lower() and "CREATE VIRTUAL TABLE" in block, (
        "The build-time assertion must create an FTS5 virtual table to prove "
        "the module is available, not just check the version number."
    )


def test_dockerfile_sqlite_upgrade_purges_build_tools():
    """Build tools (gcc, make, libc6-dev) must be purged in the same layer
    to avoid inflating the image."""
    # Find the SQLite RUN block
    start = DOCKERFILE.find("ARG SQLITE_VERSION=")
    assert start != -1
    # Find the next RUN or ARG that isn't part of this block
    end = DOCKERFILE.find("\n\n", DOCKERFILE.find("RUN", start))
    block = DOCKERFILE[start:end] if end != -1 else DOCKERFILE[start:]

    assert "apt-get purge" in block, (
        "The SQLite compile layer must purge build tools (gcc, make, libc6-dev) "
        "to keep the image lean."
    )
    assert "autoremove" in block, (
        "The SQLite compile layer must apt-get autoremove after purging "
        "build tools."
    )


def test_dockerfile_sqlite_upgrade_before_user_creation():
    """The SQLite upgrade must run before the unprivileged user is created,
    while the build is still running as root with write access to /usr/local."""
    sqlite_pos = DOCKERFILE.find("ARG SQLITE_VERSION=")
    user_pos = DOCKERFILE.find("groupadd")
    assert sqlite_pos != -1, "SQLite upgrade block not found"
    assert user_pos != -1, "User creation block not found"
    assert sqlite_pos < user_pos, (
        "SQLite upgrade must appear before user creation in the Dockerfile "
        "so it runs as root with write access to system library paths."
    )


def test_dockerfile_sqlite_upgrade_comments_link_bug():
    """The upgrade block must link to the upstream bug documentation so
    future maintainers understand why it exists."""
    start = DOCKERFILE.find("ARG SQLITE_VERSION=")
    # Look in the 500 chars before the ARG for the comment block
    comment_block = DOCKERFILE[max(0, start - 500):start]
    assert "walresetbug" in comment_block or "wal.html" in comment_block, (
        "The SQLite upgrade comment block must link to "
        "https://sqlite.org/wal.html#walresetbug so future maintainers "
        "know why a source compile is necessary."
    )
