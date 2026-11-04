"""Real installed-resolver regression for #8664 review P2-1 (2026-11-03).

Round 3 of the whole-record ownership finding: when a canonical+legacy
"providers:" collision is split, the FINAL connection bundle a caller hands
the client must be ONE record's COMPLETE runtime authority — endpoint,
credential, wire protocol, credential pool and ACP transport — and a field
the owning record does not declare must take the OWNING record's default,
never the sibling's value.

These tests drive the merge through the REAL installed resolver
(hermes_cli.runtime_provider from the installed Hermes agent), exactly as
the maintainer's replay harness did: the native resolver computes the input
_rt a handoff sees (it picks its credential/protocol/pool by EXACT
spelling — the SIBLING when the requested spelling is not the owner), the
WebUI merge is fed that input, and the resulting bundle is asserted against
the REAL resolver's authority for the OWNING record (resolve_runtime_provider
(requested=<owner>)). No hand-written approximation of the resolver is
involved anywhere.

The suite SKIPS when the installed Hermes agent is not importable: a
hand-rolled stand-in would be exactly the approximation this finding
rejects, so there is no fake fallback.

Matrix: four split shapes x both config orders x both requested spellings.
  (a) key/URL identical, declared wire protocols differ (protocol-only split)
  (b) owner declares no api_mode (must get its OWN default, not the sibling's)
  (c) owner is truly keyless (must not inherit the sibling's inline key)
  (d) owner declares no credential pool while the sibling's pool resolves
Plus the non-colliding precedence controls (ambient key and pool key survive)
and the different-spelling single-record repair, all through the real resolver.
"""

from __future__ import annotations

import sys

import pytest

import api.config as config


# The installed Hermes agent that ships the real native resolver. The webui
# normally runs inside this venv, so this is also what production sees.
_REAL_AGENT_ROOT = "/Users/xiaobao/.hermes/hermes-agent"
if _REAL_AGENT_ROOT not in sys.path:
    sys.path.insert(0, _REAL_AGENT_ROOT)


def _real_hermes_available() -> bool:
    try:
        import hermes_cli.runtime_provider  # noqa: F401
        import agent.credential_pool  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _real_hermes_available(),
    reason="installed Hermes agent (real native resolver) not importable",
)


# ---------------------------------------------------------------------
# hermetic agent-side state
# ---------------------------------------------------------------------


def _scrub_provider_env(monkeypatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
        "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY",
        "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_CLOUD_API_KEY", "OLLAMA_API_KEY", "GLM_API_KEY",
        "KIMI_API_KEY", "MOONSHOT_API_KEY", "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY", "NOUS_API_KEY", "NVIDIA_API_KEY",
        "LM_API_KEY", "LMSTUDIO_API_KEY", "XIAOMI_API_KEY",
        "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _swap_in_test_config(extra_cfg: dict):
    """Swap api.config.cfg for the test, returning a restore callback."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg.update(extra_cfg)
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    def _restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    return _restore


def _synthetic_pool(provider: str, key: str):
    """Real CredentialPool with one synthetic api_key entry."""
    from agent.credential_pool import CredentialPool, PooledCredential

    return CredentialPool(provider, [
        PooledCredential(
            provider=provider,
            id="synthetic-1",
            label="synthetic",
            auth_type="api_key",
            priority=1,
            source="manual",
            access_token=key,
        ),
    ])


@pytest.fixture
def native(monkeypatch):
    """Point the REAL installed resolver at the swapped WebUI config and a
    hermetic (synthetic-pool-only) credential state. Returns a callable that
    resolves a provider id through the REAL resolver with that state."""
    import hermes_cli.runtime_provider as rp
    import hermes_cli.config as cfg_mod
    import agent.credential_pool as acp

    _scrub_provider_env(monkeypatch)
    state = {"pools": {}}

    def current_cfg() -> dict:
        providers = config.cfg.get("providers") or {}
        return {
            "model": dict(config.cfg.get("model") or {}),
            "providers": dict(providers),
        }

    monkeypatch.setattr(cfg_mod, "load_config", current_cfg)
    monkeypatch.setattr(cfg_mod, "load_config_readonly", current_cfg)
    monkeypatch.setattr(rp, "load_config", current_cfg)
    monkeypatch.setattr(acp, "_load_config_safe", current_cfg)

    def fake_load_pool(pool_key):
        return state["pools"].get(str(pool_key or "").strip().lower())

    monkeypatch.setattr(rp, "load_pool", fake_load_pool)

    def set_pool(pool_key: str, pool):
        state["pools"][str(pool_key or "").strip().lower()] = pool

    def resolve(provider_id: str) -> dict:
        return rp.resolve_runtime_provider(requested=provider_id)

    resolve.set_pool = set_pool
    return resolve


_OWNER_SIDE_FIELDS = ("base_url", "api_key", "api_mode", "credential_pool")


# ---------------------------------------------------------------------
# the four split shapes x two config orders x two spellings
# ---------------------------------------------------------------------


class TestCompleteAuthorityMatrix:
    """Each requested spelling, in each config order, must hand the client a
    bundle equal to the owning record's COMPLETE native authority."""

    # (a) protocol-only split: key and URL identical, protocols differ.
    _PROTOCOL_ONLY = {
        "legacy-first": {
            "mistralai": {"api_key": "shared-key-canary", "base_url": "https://relay.example/v1", "api_mode": "chat_completions"},
            "mistral": {"api_key": "shared-key-canary", "base_url": "https://relay.example/v1", "api_mode": "anthropic_messages"},
        },
        "canonical-first": {
            "mistral": {"api_key": "shared-key-canary", "base_url": "https://relay.example/v1", "api_mode": "anthropic_messages"},
            "mistralai": {"api_key": "shared-key-canary", "base_url": "https://relay.example/v1", "api_mode": "chat_completions"},
        },
    }

    # (b) owner declares no api_mode; sibling declares anthropic_messages.
    _OWNER_NO_MODE = {
        "legacy-first": {
            "mistralai": {"api_key": "owner-inline-canary", "base_url": "https://owner-relay.example/v1"},
            "mistral": {"api_key": "sibling-key-canary", "base_url": "https://sibling-relay.example/v1", "api_mode": "anthropic_messages"},
        },
        "canonical-first": {
            "mistral": {"api_key": "owner-inline-canary", "base_url": "https://owner-relay.example/v1"},
            "mistralai": {"api_key": "sibling-key-canary", "base_url": "https://sibling-relay.example/v1", "api_mode": "anthropic_messages"},
        },
    }

    # (c) owner is truly keyless; sibling carries an inline key.
    _KEYLESS_OWNER = {
        "legacy-first": {
            "mistralai": {"base_url": "https://owner-relay.example/v1"},
            "mistral": {"api_key": "sibling-inline-canary", "base_url": "https://sibling-relay.example/v1", "api_mode": "anthropic_messages"},
        },
        "canonical-first": {
            "mistral": {"base_url": "https://owner-relay.example/v1"},
            "mistralai": {"api_key": "sibling-inline-canary", "base_url": "https://sibling-relay.example/v1", "api_mode": "anthropic_messages"},
        },
    }

    # (d) owner declares no credential pool; the sibling's pool resolves.
    _SIBLING_ONLY_POOL = {
        "legacy-first": {
            "mistralai": {"api_key": "owner-inline-canary", "base_url": "https://owner-relay.example/v1"},
            "mistral": {"api_key": "sibling-inline-canary", "base_url": "https://sibling-relay.example/v1", "credential_pool": "mistral"},
        },
        "canonical-first": {
            "mistral": {"api_key": "owner-inline-canary", "base_url": "https://owner-relay.example/v1"},
            "mistralai": {"api_key": "sibling-inline-canary", "base_url": "https://sibling-relay.example/v1", "credential_pool": "mistralai"},
        },
    }

    @pytest.mark.parametrize(
        "scenario_name, scenario, sibling_pool, expect_pool_in_native",
        [
            ("protocol-only", _PROTOCOL_ONLY, None, False),
            ("owner-no-mode", _OWNER_NO_MODE, None, False),
            ("keyless-owner", _KEYLESS_OWNER, None, False),
            ("sibling-only-pool", _SIBLING_ONLY_POOL, "synthetic-pool-canary", True),
        ],
    )
    @pytest.mark.parametrize("order", ["legacy-first", "canonical-first"])
    @pytest.mark.parametrize("spelling", ["mistral", "mistralai"])
    def test_final_bundle_equals_owner_complete_authority(
        self, native, scenario_name, scenario, sibling_pool, expect_pool_in_native, order, spelling,
    ):
        providers_cfg = scenario[order]
        if sibling_pool is not None:
            sibling_key = list(providers_cfg.keys())[1]
            native.set_pool(sibling_key, _synthetic_pool(sibling_key, sibling_pool))

        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            owner_key = config._raw_provider_key_for_provider_id(spelling)
            native_rt = native(spelling)
            bundle = config.merge_custom_provider_runtime_bundle(
                spelling,
                native_rt.get("api_key"),
                native_rt.get("base_url"),
                native_rt,
            )
            # The oracle: the REAL resolver's complete authority for the OWNING
            # record (the maintainer's stated expectation for the repair).
            owner_rt = native(owner_key)
        finally:
            restore()

        # The native input must actually have exercised what this scenario is
        # about: the non-owning spelling saw the SIBLING's pooled credential.
        if expect_pool_in_native and spelling != owner_key:
            assert native_rt.get("credential_pool") is not None, (
                f"{order}/{spelling}: the native input was supposed to carry the "
                "sibling's pool so the repair is actually put to the test"
            )

        label = f"{scenario_name}/{order}/{spelling} (owner={owner_key})"
        for field in _OWNER_SIDE_FIELDS:
            assert bundle[field] == owner_rt.get(field), (
                f"{label}: bundle['{field}'] = {bundle[field]!r} but the owning "
                f"record's complete native authority is {owner_rt.get(field)!r}"
            )

    def test_protocol_only_split_fires_without_key_or_url_difference(self, native):
        """The whole point of (a): key and URL are IDENTICAL, only the declared
        protocol differs, so only a protocol check can detect the split."""
        providers_cfg = self._PROTOCOL_ONLY["legacy-first"]
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": providers_cfg,
        })
        try:
            owner_key = config._raw_provider_key_for_provider_id("mistral")
            assert owner_key == "mistralai"
            native_rt = native("mistral")
            owner_rt = native(owner_key)
            # The native input resolved the SIBLING (canonical record,
            # anthropic_messages) while key and URL equal the owner's — a pure
            # protocol split. This test dies if the merge ever regresses to
            # only checking key/URL.
            assert native_rt.get("api_mode") == "anthropic_messages"
            assert owner_rt.get("api_mode") == "chat_completions"
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistral", native_rt.get("api_key"), native_rt.get("base_url"), native_rt,
            )
            assert bundle["api_mode"] == "chat_completions", (
                "a handoff must not keep the sibling's anthropic_messages when "
                "key/URL agree but the owning record says chat_completions; got "
                f"{bundle['api_mode']!r}"
            )
        finally:
            restore()


# ---------------------------------------------------------------------
# non-colliding precedence, through the real resolver (P2-2 preservation)
# ---------------------------------------------------------------------


class TestNonCollidingPrecedenceRealResolver:
    """P2-2 preservation through the real resolver: a single record, resolved
    by the exact spelling that names it, must keep the credential the caller's
    resolution carried (an ambient or pool key) — the repair must not fire and
    replace it with the record's inline key. And the different-spelling single
    record must still be repairable: no sibling exists, yet the canonical
    spelling resolves nothing, and the OWNING record's own authority (verified
    by the real resolver) is what the bundle carries."""

    def test_ambient_key_survives_on_single_record(self, native):
        """Single providers.mistralai record with an inline key; the caller
        already resolved an AMBIENT key for the exact spelling. No sibling,
        no disagreement, no repair — the ambient key is the resolver's
        authority and must survive."""
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "mistralai": {
                    "base_url": "https://legacy-relay.example/v1",
                    "api_key": "inline-record-canary",
                },
            },
        })
        try:
            # The exact spelling resolves the record through the real resolver.
            record_rt = native("mistralai")
            assert record_rt.get("api_key") == "inline-record-canary"
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistralai",
                "ambient-resolver-canary",
                "https://legacy-relay.example/v1",
                {
                    "provider": "mistralai",
                    "api_key": "ambient-resolver-canary",
                    "base_url": "https://legacy-relay.example/v1",
                },
            )
        finally:
            restore()
        assert bundle["api_key"] == "ambient-resolver-canary", (
            "a non-colliding provider must keep the credential the caller's ",
            "resolution selected, not the record's inline key; got "
            f"{bundle['api_key']!r}"
        )

    def test_pool_key_survives_on_single_record(self):
        """Single record with a pool; the caller resolved a POOL-selected key.
        No sibling, so the pool key must survive the inline declaration.
        (Same guard, pool variant.)"""
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "mistralai": {
                    "base_url": "https://legacy-relay.example/v1",
                    "api_key": "inline-record-canary",
                    "credential_pool": "some-pool",
                },
            },
        })
        try:
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistralai",
                "pool-resolver-canary",
                "https://legacy-relay.example/v1",
                {
                    "provider": "mistralai",
                    "api_key": "pool-resolver-canary",
                    "base_url": "https://legacy-relay.example/v1",
                    "credential_pool": "some-pool",
                },
            )
        finally:
            restore()
        assert bundle["api_key"] == "pool-resolver-canary", (
            "a pool-selected credential must survive on a non-colliding record; ",
            f"got {bundle['api_key']!r}"
        )

    def test_single_record_other_spelling_equals_owner_native_authority(self, native):
        """Different-spelling single record: no sibling exists, yet the real
        resolver cannot answer the canonical spelling; the bundle the merge
        builds must equal the OWNING record's complete native authority
        (resolve_runtime_provider on the owner itself)."""
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "mistralai": {
                    "base_url": "https://legacy-relay.example/v1",
                    "api_key": "legacy-record-canary",
                },
            },
        })
        try:
            # What the owner is: the real resolver's authority for the owning
            # record (the oracle the maintainer used for this exact finding).
            owner_rt = native("mistralai")
            # The WebUI-side runtime for the different spelling: it sees no
            # exact record, so the credential the resolver produced is empty.
            webui_rt = {"provider": "mistral", "base_url": "https://legacy-relay.example/v1"}
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistral", None, "https://legacy-relay.example/v1", webui_rt,
            )
        finally:
            restore()
        for field in _OWNER_SIDE_FIELDS:
            assert bundle[field] == owner_rt.get(field), (
                f"single-record different-spelling: bundle['{field}'] = "
                f"{bundle[field]!r} but the owning record's native authority is "
                f"{owner_rt.get(field)!r}"
            )
        assert owner_rt.get("api_mode") == "chat_completions"

    def test_non_colliding_owner_spelling_is_untouched(self, native):
        """The owning spelling on a single record must leave the resolver's
        input alone entirely — no split, no repair, byte-for-byte the values
        the caller resolved."""
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "mistralai": {
                    "base_url": "https://legacy-relay.example/v1",
                    "api_key": "legacy-record-canary",
                },
            },
        })
        try:
            rt = native("mistralai")
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistralai", rt.get("api_key"), rt.get("base_url"), rt,
            )
        finally:
            restore()
        assert bundle["base_url"] == rt.get("base_url")
        assert bundle["api_key"] == rt.get("api_key")
        assert bundle["api_mode"] == rt.get("api_mode")

