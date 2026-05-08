"""Regression tests for v0.50.257 Opus pre-release follow-ups (#1402 + #1415).

The v0.50.257 batch had four findings on PR #1402:

1. MUST-FIX (security) — `api/oauth.py::_write_auth_json` used `tmp.replace()`
   which preserves the temp file's umask-derived mode (commonly 0644 or 0664).
   `auth.json` contains OAuth access/refresh tokens; on shared systems those
   tokens landed world-readable. Fix: `tmp.chmod(0o600)` BEFORE rename.

2. SHOULD-FIX (defense-in-depth) — `_handle_cron_history` and
   `_handle_cron_run_detail` accepted `job_id` as a path component without
   validation. `Path() / "../escape"` does not normalize, mirroring the
   rollback path-traversal vector caught in v0.50.255. Fix: regex validation
   that rejects `/`, `..`, `.`.

3. SHOULD-FIX — `_handle_cron_history` parsed `offset`/`limit` via raw
   `int()`, so `?offset=foo` raised `ValueError` and surfaced as a generic
   500 instead of a clean 400. Also no upper bound on `limit` (DoS via
   `?limit=999999999`). Fix: try/except + clamp to safe ranges.

4. NIT — also propagate the cron `job_id` validation regex to make the
   pattern explicit at the parameter boundary.

PR #1415 follow-up: 8 pre-existing tests in test_issue1106 and
test_custom_provider_display_name asserted bare model IDs but #1415 changes
the named-custom-provider IDs to `@custom:NAME:model` form when active
provider differs. Tests updated to use `_strip_at_prefix` helper to keep
checking the same invariant ("does model X appear in the picker") in the
new shape.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── 1: auth.json permission fix (chmod 0600 before rename) ───────────────────


def test_oauth_write_auth_json_uses_chmod_0600_before_rename(monkeypatch, tmp_path):
    """`_write_auth_json` must chmod 0600 BEFORE renaming so tokens never land
    world-readable. The previous implementation used `tmp.replace()` which
    preserves the temp file's umask-derived mode."""
    sys.path.insert(0, str(REPO))
    import api.oauth as oauth

    # Point AUTH_JSON_PATH at a tmp dir
    fake_path = tmp_path / "auth.json"
    monkeypatch.setattr(oauth, "AUTH_JSON_PATH", fake_path)

    # Set a permissive umask so default write would create 0644
    old_umask = os.umask(0o022)
    try:
        oauth._write_auth_json({"credential_pool": {"openai-codex": []}})
    finally:
        os.umask(old_umask)

    assert fake_path.exists(), "auth.json was not written"
    mode = stat.S_IMODE(fake_path.stat().st_mode)
    # The file must be chmod 0600 — owner read/write only.
    assert mode == 0o600, (
        f"auth.json permissions are {oct(mode)}, expected 0o600. "
        f"OAuth tokens (access_token, refresh_token) live in this file. "
        f"On shared systems, world-readable tokens are a real exposure."
    )


def test_oauth_write_auth_json_source_calls_chmod():
    """Source-level pin: any future change to _write_auth_json that drops the
    chmod call must be caught even if the runtime test above is skipped on
    a filesystem that doesn't support POSIX modes."""
    src = (REPO / "api" / "oauth.py").read_text(encoding="utf-8")
    assert "tmp.chmod(0o600)" in src, (
        "_write_auth_json must call tmp.chmod(0o600) before tmp.replace() — "
        "without it, OAuth tokens land world-readable on shared systems."
    )


# ── 2: cron history job_id path-traversal validation ────────────────────────


def test_cron_history_rejects_traversal_in_job_id():
    """`_handle_cron_history` and `_handle_cron_run_detail` must regex-validate
    job_id at the parameter boundary. Mirrors the rollback regex shape from
    v0.50.255."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    # Both handlers must call the validator
    history_idx = src.find("def _handle_cron_history(")
    detail_idx = src.find("def _handle_cron_run_detail(")
    assert history_idx != -1, "_handle_cron_history missing"
    assert detail_idx != -1, "_handle_cron_run_detail missing"

    history_body = src[history_idx : history_idx + 1500]
    detail_body = src[detail_idx : detail_idx + 1500]

    # Both must include the regex check
    for body, name in [(history_body, "_handle_cron_history"), (detail_body, "_handle_cron_run_detail")]:
        assert "_re.fullmatch" in body and "[A-Za-z0-9_-]" in body, (
            f"{name} must validate job_id via regex — without this, "
            f"`?job_id=../<other>` enumerates sibling directory contents."
        )
        assert 'job_id in (".", "..")' in body, (
            f"{name} must explicitly reject `.` and `..` in addition to the regex."
        )


# ── 3: int() bounds checking on offset/limit ────────────────────────────────


def test_cron_history_clamps_offset_and_limit():
    """`_handle_cron_history` must catch `ValueError` from int() and clamp
    `limit` to a sane upper bound. Without this, `?offset=foo` raises a
    ValueError that surfaces as a confusing 500 from `do_GET`'s exception
    handler, and `?limit=999999999` would slice through unbounded glob output."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    history_idx = src.find("def _handle_cron_history(")
    body = src[history_idx : history_idx + 1500]
    assert "(ValueError, TypeError)" in body, (
        "_handle_cron_history must catch ValueError from int() so malformed "
        "offset/limit return a clean 400, not a generic 500."
    )
    assert "min(500, int(qs.get" in body, (
        "_handle_cron_history must clamp `limit` to a sane upper bound (500 chosen) "
        "to prevent DoS via `?limit=999999999`."
    )



# ── Critical Opus finding: enabled_toolsets actually applies ─────────────────


def test_run_agent_streaming_uses_session_enabled_toolsets():
    """The per-session toolset override (#493) was non-functional in PR #1402:
    `Session.load_metadata_only()` returns a Session INSTANCE, but the code
    called `.get('enabled_toolsets')` on it. AttributeError was swallowed by
    the surrounding `except Exception`, so the user's toolset chip silently
    no-op'd every time. Pin the source-level invariant so this exact regression
    can't return."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # The bug shape that must NOT come back: dict-style access on the result.
    # Negative-pattern guard (prevents revert).
    bad_pattern = "_session_meta.get('enabled_toolsets')"
    assert bad_pattern not in src, (
        f"streaming.py contains {bad_pattern!r} — Session.load_metadata_only() "
        f"returns a Session INSTANCE, not a dict, so .get() raises AttributeError. "
        f"The bare `except Exception:` swallows the failure silently and the "
        f"per-session toolset override is non-functional. Use getattr() instead. "
        f"(Opus pre-release advisor caught this in v0.50.257.)"
    )

    bad_pattern2 = "_session_meta['enabled_toolsets']"
    assert bad_pattern2 not in src, (
        f"streaming.py contains {bad_pattern2!r} — same bug shape. "
        f"Session.load_metadata_only() returns an instance, not a dict."
    )

    # Positive pattern: getattr() must be used.
    assert "getattr(_session_meta, 'enabled_toolsets'" in src, (
        "streaming.py must use getattr(_session_meta, 'enabled_toolsets', None) "
        "since load_metadata_only returns a Session instance."
    )


def test_session_load_metadata_only_returns_instance_not_dict():
    """End-to-end: Session.load_metadata_only must return a Session instance,
    not a dict. This is the contract that breaks PR #1402's toolset override
    if a future change converts it to a dict."""
    sys.path.insert(0, str(REPO))
    from api.models import Session
    import tempfile

    with tempfile.TemporaryDirectory() as tmpd:
        # Create a fake session file
        import json as _json
        sid = "test1234abcd"
        from api import models
        original = models.SESSION_DIR
        models.SESSION_DIR = Path(tmpd)
        try:
            session_file = Path(tmpd) / f"{sid}.json"
            session_file.write_text(_json.dumps({
                "session_id": sid,
                "title": "Test",
                "workspace": tmpd,
                "model": "test/model",
                "enabled_toolsets": ["bash", "file"],
                "messages": [],
                "tool_calls": [],
            }))
            result = Session.load_metadata_only(sid)
        finally:
            models.SESSION_DIR = original

    # Result must be a Session instance — not None and not a dict.
    assert result is not None, "load_metadata_only returned None for valid session"
    assert isinstance(result, Session), (
        f"load_metadata_only must return Session instance, got {type(result)}"
    )
    # And the enabled_toolsets must be readable via getattr
    assert getattr(result, 'enabled_toolsets', None) == ["bash", "file"]
