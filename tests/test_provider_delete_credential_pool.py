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
        pytest.importorskip("hermes_cli.credential_lifecycle")
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
        pytest.importorskip("hermes_cli.credential_lifecycle")
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
            import hermes_cli.credential_lifecycle as cred_lifecycle
        except ImportError:
            pytest.skip("runtime not installed")

        monkeypatch.setattr(
            cred_lifecycle,
            "purge_env_credential_references",
            lambda env_var, clear_models_cache=True: calls.append(env_var),
        )

        prov._purge_env_seeded_credential_pool("anthropic")
        assert calls == []


class TestReaddLiftsSuppression:
    """Re-adding a key through the UI must behave like `hermes auth add`."""

    def test_readd_unsuppresses_and_materializes_pool_entry(
        self, monkeypatch, tmp_path
    ):
        """Issue consequence #3: after a removal wrote suppressed_sources, a UI
        save must lift it and materialize the env-seeded pool entry (#96058)."""
        pytest.importorskip("hermes_cli.auth")
        pytest.importorskip("agent.credential_pool")
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

    def test_named_profile_delete_readd_anthropic_resolves_key_and_preserves_root_auth(
        self, monkeypatch, tmp_path
    ):
        """Regate finding 1: named-profile delete then re-add of an Anthropic key
        must resolve the named-profile key without borrowing root profile keys or
        mutating the root auth.json."""
        pytest.importorskip("hermes_cli.auth")
        pytest.importorskip("agent.credential_pool")
        pytest.importorskip("hermes_cli.runtime_provider")

        from hermes_cli import auth as h_auth
        from hermes_cli import runtime_provider
        from api.providers import remove_provider_key, set_provider_key

        root_dir = tmp_path / "root_hermes"
        named_dir = tmp_path / "named_profile"
        root_dir.mkdir(parents=True, exist_ok=True)
        named_dir.mkdir(parents=True, exist_ok=True)

        # Root auth has default Anthropic key
        root_auth = root_dir / "auth.json"
        root_auth.write_text(
            json.dumps({
                "credential_pool": {
                    "anthropic": [
                        {
                            "id": "env:ANTHROPIC_API_KEY",
                            "source": "env:ANTHROPIC_API_KEY",
                            "auth_type": "api_key",
                            "runtime_api_key": "sk-ant-ROOT-GLOBAL-KEY",
                        }
                    ]
                }
            }),
            encoding="utf-8",
        )
        root_bytes_before = root_auth.read_bytes()

        # Named profile initial setup
        profile_auth = named_dir / "auth.json"
        profile_auth.write_text(
            json.dumps({
                "credential_pool": {
                    "anthropic": [
                        {
                            "id": "env:ANTHROPIC_API_KEY",
                            "source": "env:ANTHROPIC_API_KEY",
                            "auth_type": "api_key",
                            "runtime_api_key": "sk-ant-NAMED-OLD-KEY",
                        }
                    ]
                }
            }),
            encoding="utf-8",
        )
        (named_dir / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-NAMED-OLD-KEY\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(named_dir))
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: named_dir)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "named_client")
        monkeypatch.setattr(h_auth, "_global_auth_file_path", lambda: root_auth)

        # 1. Delete Anthropic key in named profile
        del_res = remove_provider_key("anthropic")
        assert del_res["ok"] is True
        p_store_after_del = json.loads(profile_auth.read_text(encoding="utf-8"))
        assert "anthropic" not in p_store_after_del.get("credential_pool", {}) or not p_store_after_del["credential_pool"]["anthropic"]

        # 2. Re-add Anthropic key in named profile
        new_named_key = "sk-ant-NAMED-NEW-KEY-999999"
        add_res = set_provider_key("anthropic", new_named_key)
        assert add_res["ok"] is True

        # Root auth.json must be byte-unchanged
        assert root_auth.read_bytes() == root_bytes_before, "Root auth.json must be byte-unchanged"

        # Runtime provider must resolve the newly saved named-profile key
        resolved = runtime_provider.resolve_runtime_provider(requested="anthropic")
        assert resolved.get("api_key") == new_named_key

    def test_readd_google_restores_gemini_pool_and_clears_suppression(
        self, monkeypatch, tmp_path
    ):
        """Regate finding 2: deleting and re-adding GOOGLE_API_KEY must restore
        Gemini's pool row and leave no suppression marker."""
        pytest.importorskip("hermes_cli.auth")
        pytest.importorskip("agent.credential_pool")
        _pin_home(monkeypatch, tmp_path)

        from api.providers import remove_provider_key, set_provider_key

        auth_path = _write_auth_store(
            tmp_path,
            {
                "credential_pool": {
                    "google": [
                        {
                            "id": "env:GOOGLE_API_KEY",
                            "source": "env:GOOGLE_API_KEY",
                            "auth_type": "api_key",
                            "runtime_api_key": "AIzaSy-OLD-GOOGLE-111",
                        }
                    ],
                    "gemini": [
                        {
                            "id": "env:GOOGLE_API_KEY",
                            "source": "env:GOOGLE_API_KEY",
                            "auth_type": "api_key",
                            "runtime_api_key": "AIzaSy-OLD-GEMINI-111",
                        }
                    ],
                }
            },
        )
        (tmp_path / ".env").write_text("GOOGLE_API_KEY=AIzaSy-OLD-GOOGLE-111\n", encoding="utf-8")

        # 1. Delete Google provider
        del_res = remove_provider_key("google")
        assert del_res["ok"] is True

        # 2. Re-add Google provider with new key
        new_google_key = "AIzaSy-NEW-GOOGLE-222"
        add_res = set_provider_key("google", new_google_key)
        assert add_res["ok"] is True

        store = _read_auth_store(tmp_path)
        gemini_suppressed = store.get("suppressed_sources", {}).get("gemini", [])
        assert "env:GOOGLE_API_KEY" not in gemini_suppressed

        gemini_pool = store.get("credential_pool", {}).get("gemini", [])
        assert len(gemini_pool) > 0
        sources = _pool_sources(gemini_pool)
        assert "env:GOOGLE_API_KEY" in sources
