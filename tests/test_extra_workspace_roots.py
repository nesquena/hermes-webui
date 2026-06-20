"""HERMES_WEBUI_EXTRA_WORKSPACE_ROOTS carve-out (api/workspace.py).

A deployment may mount legitimate user workspaces under a normally-blocked
system prefix (e.g. REANA mounts them under /var/reana/...). The env var opts
specific roots back in. This must hold across all three gating functions the
add/validate/resolve paths use — in particular the POSIX probe that
_is_blocked_workspace_path consults first — be default-off, and never over-reach
to sibling or parent paths.
"""

import os
from pathlib import Path

from api.workspace import (
    _USER_TMP_PREFIXES,
    _extra_workspace_prefixes,
    _is_blocked_posix_workspace_path,
    _is_blocked_system_path,
    _is_blocked_workspace_path,
    _parse_extra_workspace_roots,
    _workspace_carveout_prefixes,
)

ENV = "HERMES_WEBUI_EXTRA_WORKSPACE_ROOTS"
WS = "/var/reana/users/u1/workflows/w1"


def test_default_off_blocks_var_reana_on_every_gate(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    # No extra carve-outs → identical to the historical behavior.
    assert _workspace_carveout_prefixes() == _USER_TMP_PREFIXES
    assert _is_blocked_posix_workspace_path(WS) is True
    assert _is_blocked_workspace_path(Path(WS), WS) is True
    assert _is_blocked_system_path(Path(WS)) is True


def test_carveout_allows_listed_root_on_every_gate(monkeypatch):
    monkeypatch.setenv(ENV, "/var/reana")
    # The POSIX probe runs first in _is_blocked_workspace_path; before the fix it
    # returned True here (dead carve-out), so /api/session/new still 400'd.
    assert _is_blocked_posix_workspace_path(WS) is False
    assert _is_blocked_workspace_path(Path(WS), WS) is False
    assert _is_blocked_system_path(Path(WS)) is False


def test_carveout_does_not_over_reach(monkeypatch):
    monkeypatch.setenv(ENV, "/var/reana")
    # Parent, siblings, an unrelated system dir, and a traversal that escapes the
    # carve-out (normalizes to /etc) all stay blocked.
    for p in ("/var", "/var/reana-evil", "/var/other", "/etc", "/var/reana/../etc"):
        assert _is_blocked_posix_workspace_path(p) is True, p
        assert _is_blocked_workspace_path(Path(p), p) is True, p


def test_relative_entries_are_skipped_with_warning(monkeypatch, caplog):
    import logging

    _parse_extra_workspace_roots.cache_clear()  # warn-once cache; make this call fresh
    monkeypatch.setenv(ENV, os.pathsep.join(["relative/dir", "/var/reana"]))
    with caplog.at_level(logging.WARNING):
        prefixes = _extra_workspace_prefixes()

    # the absolute root is kept and still carves /var/reana out; the relative one
    # is dropped (not resolved against CWD) and surfaced as a warning.
    assert any(p.as_posix() == "/var/reana" for p in prefixes)
    assert all("relative" not in p.as_posix() for p in prefixes)
    assert _is_blocked_workspace_path(Path(WS), WS) is False
    assert any("non-absolute" in r.getMessage() for r in caplog.records)
