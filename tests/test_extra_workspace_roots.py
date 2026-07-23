"""Extra-trusted-workspace-roots carve-out (api/workspace.py).

A deployment may mount legitimate user workspaces under a normally-blocked
system prefix (e.g. REANA mounts them under /var/reana/...). The user-facing
surface is the ``config.yaml`` key ``workspace.extra_trusted_roots`` (a list of
absolute paths); the ``HERMES_WEBUI_EXTRA_WORKSPACE_ROOTS`` env var is retained
as an internal deployment bridge. Either opts specific roots back in. This must
hold across all three gating functions the add/validate/resolve paths use — in
particular the POSIX probe that _is_blocked_workspace_path consults first — be
default-off, and never over-reach to sibling or parent paths.
"""

import os
from pathlib import Path

from api.workspace import (
    _USER_TMP_PREFIXES,
    _extra_workspace_posix_carveouts,
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


def test_posix_carveouts_keep_both_symlink_forms(monkeypatch, tmp_path):
    """The POSIX probe matches raw (unresolved) strings, so a symlinked root must
    contribute both its literal and resolved forms — the macOS /var -> /private/var
    case greptile flagged. Simulated with a real symlink so it runs on any OS.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    # A path *under the symlink* resolves to a different (real) prefix.
    assert Path(link).resolve() == real.resolve()
    assert link.resolve() != link

    _parse_extra_workspace_roots.cache_clear()
    monkeypatch.setenv(ENV, str(link))
    forms = {p.as_posix() for p in _extra_workspace_posix_carveouts()}

    # Both the literal (unresolved) and the resolved form are present, mirroring
    # the static /var/folders + /private/var/folders pattern.
    assert link.as_posix() in forms
    assert real.resolve().as_posix() in forms
    # And the resolved-only view (_extra_workspace_prefixes) still excludes the link.
    resolved_only = {p.as_posix() for p in _extra_workspace_prefixes()}
    assert real.resolve().as_posix() in resolved_only


def test_over_broad_roots_are_rejected(monkeypatch, caplog):
    """An over-broad or '..'-laden entry must not widen the boundary to a system
    root. Path.resolve() collapses '..', so '/var/reana/..' -> /var and
    '/var/reana/../..' -> / would otherwise silently turn a whole system tree
    (up to '/') into a trusted workspace root.
    """
    import logging

    # (env value, a victim path that must STAY blocked if the entry is rejected)
    cases = (
        ("/var/reana/..", "/var/secret"),      # '..' -> resolves to /var
        ("/var/reana/../..", "/etc/passwd"),   # '..' -> resolves to /
        ("/", "/etc/passwd"),                  # filesystem root
        ("/var", "/var/secret"),               # a bare blocked system root
    )
    for value, victim in cases:
        _parse_extra_workspace_roots.cache_clear()
        monkeypatch.setenv(ENV, value)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            prefixes = _extra_workspace_prefixes()
        # The entry is dropped entirely — no carve-out is registered …
        assert prefixes == (), f"{value!r} should be rejected, got {prefixes}"
        # … so the victim path stays blocked on the gates the carve-out feeds.
        assert _is_blocked_workspace_path(Path(victim), victim) is True, f"{value!r} -> {victim}"
        assert _is_blocked_system_path(Path(victim)) is True, f"{value!r} -> {victim}"
        assert _is_blocked_posix_workspace_path(victim) is True, f"{value!r} -> {victim}"
        # … and the operator gets a warning rather than a silent over-reach.
        assert caplog.records, f"{value!r} should log a warning"


def test_strict_subdir_of_blocked_root_still_allowed(monkeypatch):
    """The bounding check must not regress the legitimate case: a root that sits
    strictly below a blocked system root (the whole point of the feature)."""
    _parse_extra_workspace_roots.cache_clear()
    monkeypatch.setenv(ENV, "/var/reana")
    assert any(p.as_posix() == "/var/reana" for p in _extra_workspace_prefixes())
    assert _is_blocked_workspace_path(Path(WS), WS) is False


# --- config.yaml surface (workspace.extra_trusted_roots) -----------------------
#
# The user-facing surface is config.yaml, not the env var. _config_extra_workspace
# _root_entries() reads workspace.extra_trusted_roots via api.config.get_config(),
# resolved lazily, so these tests stub that call. The env var stays valid as an
# internal bridge; the two sources merge (config first) and dedup.


def _set_config(monkeypatch, cfg):
    """Stub api.config.get_config() (resolved lazily inside workspace.py)."""
    _parse_extra_workspace_roots.cache_clear()
    monkeypatch.setattr("api.config.get_config", lambda: cfg)


def test_config_yaml_list_carves_out_on_every_gate(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    _set_config(monkeypatch, {"workspace": {"extra_trusted_roots": ["/var/reana"]}})
    # Same carve-out as the env var, sourced purely from config.yaml.
    assert any(p.as_posix() == "/var/reana" for p in _extra_workspace_prefixes())
    assert _is_blocked_posix_workspace_path(WS) is False
    assert _is_blocked_workspace_path(Path(WS), WS) is False
    assert _is_blocked_system_path(Path(WS)) is False


def test_config_yaml_accepts_scalar_pathsep_string(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    _set_config(
        monkeypatch,
        {"workspace": {"extra_trusted_roots": os.pathsep.join(["/var/reana", "/srv/data"])}},
    )
    prefixes = {p.as_posix() for p in _extra_workspace_prefixes()}
    assert "/var/reana" in prefixes
    assert "/srv/data" in prefixes


def test_config_default_off_when_key_absent(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    _set_config(monkeypatch, {})  # no workspace key at all
    assert _extra_workspace_prefixes() == ()
    assert _is_blocked_posix_workspace_path(WS) is True


def test_config_and_env_merge_and_dedup(monkeypatch):
    # config supplies /var/reana; env adds the same root plus another. The shared
    # entry must not be duplicated, and both distinct roots must carve out.
    _set_config(monkeypatch, {"workspace": {"extra_trusted_roots": ["/var/reana"]}})
    monkeypatch.setenv(ENV, os.pathsep.join(["/var/reana", "/srv/extra"]))
    resolved = [p.as_posix() for p in _extra_workspace_prefixes()]
    assert resolved.count("/var/reana") == 1, resolved
    assert "/srv/extra" in resolved
    assert _is_blocked_workspace_path(Path(WS), WS) is False


def test_config_invalid_type_is_ignored_with_warning(monkeypatch, caplog):
    import logging

    monkeypatch.delenv(ENV, raising=False)
    _set_config(monkeypatch, {"workspace": {"extra_trusted_roots": 1234}})
    with caplog.at_level(logging.WARNING):
        prefixes = _extra_workspace_prefixes()
    assert prefixes == ()
    assert _is_blocked_posix_workspace_path(WS) is True
    assert any("extra_trusted_roots" in r.getMessage() for r in caplog.records)


def test_config_unloadable_falls_back_to_env(monkeypatch):
    # get_config() raising must not break the env-var bridge.
    _parse_extra_workspace_roots.cache_clear()

    def _boom():
        raise RuntimeError("config not ready")

    monkeypatch.setattr("api.config.get_config", _boom)
    monkeypatch.setenv(ENV, "/var/reana")
    assert any(p.as_posix() == "/var/reana" for p in _extra_workspace_prefixes())
    assert _is_blocked_workspace_path(Path(WS), WS) is False
