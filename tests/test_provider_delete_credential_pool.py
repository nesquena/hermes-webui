"""Regression tests for #7412: provider delete must purge the env-seeded
``credential_pool`` entry from ``auth.json``.

Root cause: ``remove_provider_key()`` only removed the key from ``.env`` and
``config.yaml``. The ``auth.json`` credential_pool row (source
``env:<VAR>``) survived because ``load_pool()`` is deliberately additive-only
for ``env:*`` sources (upstream #9331) — so the provider card kept appearing
after a restart, the live runtime couldn't authenticate, and re-adding the
key through the UI never lifted ``suppressed_sources``.

Fix: on delete, prune the env-seeded pool entry AND record
``suppressed_sources`` (same mechanism as the runtime's
``hermes_cli.credential_lifecycle``); on save, lift the suppression and force
``load_pool()`` so the entry is materialized immediately (upstream #96058).

These tests exercise the REAL runtime helpers (no fake ``hermes_cli``), so
``HERMES_HOME`` is pinned to an isolated tmp dir on every test.
"""

import json

import pytest

from api import profiles


def _pin_home(monkeypatch, tmp_path):
    """Point every home resolution (WebUI profile + runtime env) at tmp_path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)


def _write_auth_store(tmp_path, payload: dict):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(payload), encoding="utf-8")
    return auth_path


def _read_auth_store(tmp_path) -> dict:
    auth_path = tmp_path / "auth.json"
    if not auth_path.exists():
        return {}
    return json.loads(auth_path.read_text(encoding="utf-8"))


def _pool_sources(pool_entries) -> list:
    if not isinstance(pool_entries, list):
        return []
    return [str(e.get("source") or "") for e in pool_entries]


class TestRemoveProviderKeyPurging:
    """Deleting a provider key must purge the env-seeded pool entry."""

    def test_remove_prunes_env_seeded_entry_and_suppresses_source(
        self, monkeypatch, tmp_path
    ):
        """The exact #7412 repro: env-seeded pool row must not survive delete."""
        _pin_home(monkeypatch, tmp_path)
        auth_path = _write_auth_store(
            tmp_path,
            {
                "credential_pool": {
                    "anthropic": [
                        {
                            "source": "env:ANTHROPIC_API_KEY",
                            "id": "env:ANTHROPIC_API_KEY",
                            "auth_type": "api_key",
                            "runtime_api_key": "sk-stale-secret-123456",
                        }
                    ]
                }
            },
        )

        from api.providers import remove_provider_key

        result = remove_provider_key("anthropic")
        assert result["ok"] is True
        assert result["action"] == "removed"

        store = _read_auth_store(tmp_path)
        # Env-seeded entry gone: provider key removed entirely (no other lane).
        pool = store.get("credential_pool", {})
        assert "anthropic" not in pool or not pool.get("anthropic")
        # Suppression record written like `hermes auth remove` / the runtime
        # lifecycle (CLI parity) so a lingering shell export can't re-seed it.
        suppressed = store.get("suppressed_sources", {})
        assert "env:ANTHROPIC_API_KEY" in (suppressed.get("anthropic") or [])
        # auth.json exists and the env var is gone from .env too.
        assert auth_path.exists()
        env_path = tmp_path / ".env"
        content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        assert "ANTHROPIC_API_KEY" not in content

    def test_remove_preserves_non_env_pool_entries(self, monkeypatch, tmp_path):
        """OAuth/manual/borrowed rows must survive — the purge only targets
        ``env:<VAR>`` sources (OAuth preservation contract)."""
        _pin_home(monkeypatch, tmp_path)
        _write_auth_store(
            tmp_path,
            {
                "credential_pool": {
                    "anthropic": [
                        {
                            "source": "env:ANTHROPIC_API_KEY",
                            "id": "env:ANTHROPIC_API_KEY",
                            "auth_type": "api_key",
                            "runtime_api_key": "sk-stale-secret-123456",
                        },
                        {
                            "source": "manual",
                            "id": "manual-1",
                            "auth_type": "api_key",
                            "runtime_api_key": "sk-manual-secret-123456",
                        },
                    ],
                    "openai": [
                        {
                            "source": "oauth",
                            "id": "oauth-grant-1",
                            "auth_type": "oauth",
                            "access_token": "tok-123456",
                        }
                    ],
                }
            },
        )

        from api.providers import remove_provider_key

        result = remove_provider_key("anthropic")
        assert result["ok"] is True

        pool = _read_auth_store(tmp_path).get("credential_pool", {})
        # Same-provider manual row kept; the env row is gone.
        anthropic_sources = _pool_sources(pool.get("anthropic"))
        assert "manual" in anthropic_sources
        assert "env:ANTHROPIC_API_KEY" not in anthropic_sources
        # Unrelated provider's OAuth grant untouched.
        assert _pool_sources(pool.get("openai")) == ["oauth"]

    def test_remove_graceful_without_runtime(self, monkeypatch, tmp_path):
        """When hermes_cli is stubbed/absent (ImportError) the removal must
        still succeed — the pool purge is strictly best-effort."""
        import types
        import sys

        _pin_home(monkeypatch, tmp_path)
        fake_pkg = types.ModuleType("hermes_cli")
        fake_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
        monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
        monkeypatch.delitem(sys.modules, "agent", raising=False)

        from api.providers import remove_provider_key

        result = remove_provider_key("anthropic")
        assert result["ok"] is True
        assert result["action"] == "removed"

    def test_purge_helper_noop_without_env_var(self, monkeypatch, tmp_path):
        """Providers without an env-var mapping must not invoke the runtime
        purge (there is nothing env-seeded to prune)."""
        _pin_home(monkeypatch, tmp_path)

        import api.providers as prov

        calls = []
        monkeypatch.setattr(prov, "_provider_env_var_for", lambda _pid: None)

        try:
            from hermes_cli.credential_lifecycle import purge_env_credential_references

            monkeypatch.setattr(
                "hermes_cli.credential_lifecycle.purge_env_credential_references",
                lambda env_var, clear_models_cache=True: calls.append(env_var),
            )
        except ImportError:
            pytest.skip("runtime not installed")

        prov._purge_env_seeded_credential_pool("anthropic")
        assert calls == []


class TestReaddLiftsSuppression:
    """Re-adding a key through the UI must behave like `hermes auth add`."""

    def test_readd_unsuppresses_and_materializes_pool_entry(
        self, monkeypatch, tmp_path
    ):
        """Issue consequence #3: after a removal wrote suppressed_sources, a UI
        save must lift it and materialize the env-seeded pool entry (#96058)."""
        _pin_home(monkeypatch, tmp_path)
        _write_auth_store(
            tmp_path,
            {
                "credential_pool": {},
                "suppressed_sources": {
                    "anthropic": ["env:ANTHROPIC_API_KEY"]
                },
            },
        )

        from api.providers import set_provider_key

        result = set_provider_key("anthropic", "sk-test-readd-abcdefghij123456")
        assert result["ok"] is True
        assert result["action"] == "updated"

        store = _read_auth_store(tmp_path)
        # Suppression lifted.
        suppressed = store.get("suppressed_sources", {})
        assert "env:ANTHROPIC_API_KEY" not in (suppressed.get("anthropic") or [])
        # Pool entry materialized right now with the env source.
        sources = _pool_sources(store.get("credential_pool", {}).get("anthropic"))
        assert "env:ANTHROPIC_API_KEY" in sources
