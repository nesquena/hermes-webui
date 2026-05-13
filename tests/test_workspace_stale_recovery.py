from pathlib import Path
from types import SimpleNamespace

import pytest

from api import config as api_config
from api import routes, workspace


def test_profile_default_workspace_uses_live_config_default(monkeypatch, tmp_path):
    live_default = tmp_path / "live-default"
    live_default.mkdir()

    monkeypatch.setattr(api_config, "DEFAULT_WORKSPACE", live_default)
    monkeypatch.setattr(api_config, "get_config", lambda: {})

    assert workspace._profile_default_workspace() == str(live_default.resolve())


def test_resolve_chat_workspace_with_recovery_repairs_missing_implicit_workspace(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"

    def fake_resolve(value):
        if value == str(stale):
            raise ValueError(f"Path does not exist: {stale}")
        return Path(value).resolve()

    saved = {"count": 0}

    def fake_save():
        saved["count"] += 1

    session = SimpleNamespace(session_id="sess-1", workspace=str(stale), save=fake_save)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    resolved = routes._resolve_chat_workspace_with_recovery(session, None)

    assert resolved == str(fallback.resolve())
    assert session.workspace == str(fallback.resolve())
    assert saved["count"] == 1


def test_resolve_chat_workspace_with_recovery_preserves_explicit_errors(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"

    def fake_resolve(value):
        if value == str(stale):
            raise ValueError(f"Path does not exist: {stale}")
        return Path(value).resolve()

    saved = {"count": 0}

    def fake_save():
        saved["count"] += 1

    session = SimpleNamespace(session_id="sess-2", workspace=str(fallback), save=fake_save)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    with pytest.raises(ValueError, match="Path does not exist"):
        routes._resolve_chat_workspace_with_recovery(session, str(stale))

    assert session.workspace == str(fallback)
    assert saved["count"] == 0
