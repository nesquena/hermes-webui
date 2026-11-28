"""Spec for the #7418 re-gate: the named-profile ``env:<VAR>`` pre-seed must not
reorder the user's preferred credential and must not resurrect a dead row.

Root cause (reviewer's table, reproduced with the real ``set_provider_key()`` and
the installed Agent's ``load_pool()``):

1. ``_global_auth_file_path()`` is ``None`` for the default profile, and
   ``_same_path(local, None)`` is false, so the "named profile" fallback also
   pre-seeds in the default profile. The written row carries no ``priority``, so
   it lands at 0 next to the user's ``manual`` row and wins on store order.
2. The pre-seed writes ``id: env:<VAR>``, replacing an existing env row with a
   different id and dropping its ``last_status: dead`` — the next turn tries a
   key the pool had already ruled out.

The assertions below are behavioural (selected source + the dead row's status)
because that is what the reviewer measured.
"""

import json

import pytest

from api import profiles

ENV_SOURCE = "env:OPENROUTER_API_KEY"
PROVIDER = "openrouter"


def _pin_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)


def _write_auth(tmp_path, payload: dict):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rows(tmp_path, provider=PROVIDER) -> list:
    path = tmp_path / "auth.json"
    if not path.exists():
        return []
    store = json.loads(path.read_text(encoding="utf-8"))
    return (store.get("credential_pool") or {}).get(provider) or []


def _selected_source(provider=PROVIDER):
    from agent.credential_pool import load_pool

    chosen = load_pool(provider).select()
    return None if chosen is None else chosen.source


def _manual_row(**over):
    row = {
        "id": "manual-1",
        "source": "manual",
        "auth_type": "api_key",
        "runtime_api_key": "sk-or-manual-row-0001",
        "access_token": "sk-or-manual-row-0001",
        "priority": 0,
        "last_status": "ok",
    }
    row.update(over)
    return row


class TestPreseedDoesNotReorderPreferredCredential:
    """Saving a key must not move chat off the user's manual credential."""

    def _assert_manual_still_wins(self, tmp_path):
        rows = _rows(tmp_path)
        manual = [r for r in rows if r.get("source") == "manual"]
        assert manual, "the user's manual pool row must survive a key save"
        manual_priority = int(manual[0].get("priority") or 0)
        for row in rows:
            if row.get("source") != ENV_SOURCE:
                continue
            assert int(row.get("priority") or 0) > manual_priority, (
                "a pre-seeded env row must take the next priority after the "
                "existing rows, never 0 alongside the manual row"
            )
        assert _selected_source() == "manual", (
            "select() must still resolve the user's manual credential after a save"
        )

    def test_default_profile_save_keeps_manual_row_selected(self, monkeypatch, tmp_path):
        pytest.importorskip("hermes_cli.auth")
        pytest.importorskip("agent.credential_pool")
        from hermes_cli import auth as h_auth

        from api.providers import set_provider_key

        _pin_home(monkeypatch, tmp_path)
        # Default profile: profile home IS the global root, so the global auth
        # path helper returns None (this is exactly the fallback that misfires).
        monkeypatch.setattr(h_auth, "_global_auth_file_path", lambda: None)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
        _write_auth(tmp_path, {"credential_pool": {PROVIDER: [_manual_row()]}})

        result = set_provider_key(PROVIDER, "sk-or-env-key-1111")
        assert result["ok"] is True

        self._assert_manual_still_wins(tmp_path)

    def test_named_profile_save_keeps_manual_row_selected(self, monkeypatch, tmp_path):
        pytest.importorskip("hermes_cli.auth")
        pytest.importorskip("agent.credential_pool")
        from hermes_cli import auth as h_auth

        from api.providers import set_provider_key

        root_dir = tmp_path / "root_hermes"
        named_dir = tmp_path / "named_profile"
        root_dir.mkdir(parents=True, exist_ok=True)
        named_dir.mkdir(parents=True, exist_ok=True)
        root_auth = root_dir / "auth.json"
        root_auth.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(named_dir))
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: named_dir)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "named_client")
        monkeypatch.setattr(h_auth, "_global_auth_file_path", lambda: root_auth)

        _write_auth(named_dir, {"credential_pool": {PROVIDER: [_manual_row()]}})
        (named_dir / ".env").write_text("", encoding="utf-8")

        result = set_provider_key(PROVIDER, "sk-or-env-key-2222")
        assert result["ok"] is True

        rows = _rows(named_dir)
        manual = [r for r in rows if r.get("source") == "manual"]
        assert manual, "named-profile save must not drop the local manual row"
        manual_priority = int(manual[0].get("priority") or 0)
        for row in rows:
            if row.get("source") != ENV_SOURCE:
                continue
            assert int(row.get("priority") or 0) > manual_priority
        # The root store is read-only fallback: nothing borrowed may be written.
        assert json.loads(root_auth.read_text(encoding="utf-8")) == {"credential_pool": {}}


class TestPreseedDoesNotResurrectDeadRow:
    """Re-saving an unchanged key must keep the pool's own verdict."""

    def test_resaving_same_key_keeps_dead_env_row_id_priority_and_status(
        self, monkeypatch, tmp_path
    ):
        pytest.importorskip("hermes_cli.auth")
        pytest.importorskip("agent.credential_pool")
        from hermes_cli import auth as h_auth

        from api.providers import set_provider_key

        _pin_home(monkeypatch, tmp_path)
        monkeypatch.setattr(h_auth, "_global_auth_file_path", lambda: None)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
        _write_auth(
            tmp_path,
            {
                "credential_pool": {
                    PROVIDER: [
                        {
                            "id": "old-env-id",
                            "source": ENV_SOURCE,
                            "auth_type": "api_key",
                            "runtime_api_key": "sk-or-same-key-3333",
                            "access_token": "sk-or-same-key-3333",
                            "priority": 3,
                            "last_status": "dead",
                        }
                    ]
                }
            },
        )
        (tmp_path / ".env").write_text(
            "OPENROUTER_API_KEY=sk-or-same-key-3333\n", encoding="utf-8"
        )

        result = set_provider_key(PROVIDER, "sk-or-same-key-3333")
        assert result["ok"] is True

        rows = [r for r in _rows(tmp_path) if r.get("source") == ENV_SOURCE]
        assert len(rows) == 1, (
            "re-saving the same key must not add a second env row next to the "
            "existing one: %r" % [r.get("id") for r in rows]
        )
        row = rows[0]
        assert row.get("id") == "old-env-id", (
            "the pre-seed replaced the existing row id %r" % row.get("id")
        )
        assert int(row.get("priority") or 0) == 3
        assert row.get("last_status") == "dead", (
            "a dead credential was resurrected as selectable by the pre-seed"
        )
        assert _selected_source() is None, (
            "select() must not hand out a credential the pool marked dead"
        )
