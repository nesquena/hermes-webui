"""Regression tests for #9249 — the unresolved-``${VAR}`` pool guard must not
regress profile isolation or credential-pool failover.

#8976 stopped WebUI from forwarding a pool credential that is still an
unresolved ``${VAR}`` reference. That guard had two side effects the
maintainer called out:

1. **Named-profile credential leak.** Once the pool runtime was dropped, the
   static fallback re-resolved the record's ``api_key: ${VAR}`` against the
   server process environment, so a named profile whose own ``.env`` does not
   define ``VAR`` authenticated with the process-wide profile's value.
2. **Lost pool failover.** A pool ``[${VAR}, <valid key>]`` used to send the
   first entry, 401, and rotate to the second. The guard dropped the whole
   runtime, so the turn was refused even though a usable entry existed.

The fix pushes both decisions onto the right containers: the unresolved
credential is treated as ABSENT (never re-resolved against a process env the
profile does not own), while the pool is FILTERED to its usable entries and
the pool's own selection strategy picks one — refusing only when no usable
entry remains.
"""

from __future__ import annotations

import sys
import types

import pytest

import api.config as config

_URL = "https://pooled.example.test/v1"


class _Entry:
    """Minimal stand-in for ``agent.credential_pool.PooledCredential``."""

    def __init__(self, entry_id, key, *, priority=0):
        self.id = entry_id
        self.label = entry_id
        self.source = "manual"
        self.auth_type = "api_key"
        self.priority = priority
        self.runtime_api_key = key
        self.access_token = key
        self.request_count = 0
        self.last_status = None
        self.last_status_at = None
        self.model_cooldowns = None

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "auth_type": self.auth_type,
            "priority": self.priority,
            "access_token": self.access_token,
            "request_count": self.request_count,
        }


class _Pool:
    """Minimal stand-in for ``agent.credential_pool.CredentialPool``."""

    def __init__(self, entries, *, provider="custom:<slug>", pick=None):
        self.provider = provider
        self._entries = list(entries)
        self._pick = pick

    def entries(self):
        return list(self._entries)

    def choose(self):
        if self._pick is not None:
            return self._pick(self._entries)
        return self._entries[0] if self._entries else None


def _pool_stub(monkeypatch, runtime):
    """Point ``hermes_cli.runtime_provider``'s pool lookup at a test double.

    ``_custom_record_pool_runtime`` is documented as best-effort against a
    stubbed runtime module, so the lookup is installed directly in
    ``sys.modules`` — the same agent-free shape the repo's other
    credential-pool tests use.
    """
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module._try_resolve_from_custom_pool = lambda *a, **k: runtime
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)


def _record(**fields):
    """A minimal ``custom_providers[]`` row that owns ``_URL``."""
    record = {"name": "pooled", "base_url": _URL}
    record.update(fields)
    return record


def _patch_cfg(monkeypatch, record):
    cfg = {"custom_providers": [record]}
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    monkeypatch.setattr(config, "cfg", cfg)
    return cfg


def _merge():
    return config.merge_custom_provider_runtime_bundle("custom:pooled", None, None, None)


def _command_token_source_stub(monkeypatch):
    """Install a stand-in ``agent.command_token_source``.

    CI runs without the Agent importable, so ``key_cmd`` builds no provider
    there and the merge ladder correctly reports "declared a credential source
    that produced no key" — the test would pass only on a host that happens to
    have the Agent installed. The provider build is not the unit under test;
    the ladder is.
    """
    module = types.ModuleType("agent.command_token_source")
    module.build_command_token_provider = lambda command, label: (
        lambda: "test-token"
    )
    package = sys.modules.get("agent")
    if package is None:
        package = types.ModuleType("agent")
        package.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.command_token_source", module)
    return module


# ── regression 2: one unresolved entry must not discard the whole pool ───────


def test_mixed_pool_flips_past_an_unresolved_entry_to_the_usable_one(monkeypatch):
    """``[${VAR}, <valid>]`` must still complete a turn on the valid key."""
    pool = _Pool([_Entry("dead", "${WEBUI_9249_UNSET}"), _Entry("live", "valid-pool-key")])
    _pool_stub(monkeypatch, {"api_key": "${WEBUI_9249_UNSET}", "credential_pool": pool})
    _patch_cfg(monkeypatch, _record())

    runtime = config._custom_record_pool_runtime(_URL, _record())
    assert runtime is not None, "a pool with a usable entry must not be dropped"
    assert runtime["api_key"] == "valid-pool-key"

    bundle = _merge()
    assert bundle["api_key"] == "valid-pool-key", bundle
    assert config.custom_provider_route_error(bundle) is None
    assert bundle["base_url"] == _URL


def test_the_pool_strategy_is_the_one_that_picks_the_entry(monkeypatch):
    """Selection uses the pool's own ``choose()`` rather than first-entry order."""
    live = _Entry("live", "valid-pool-key")
    pool = _Pool(
        [_Entry("dead", "${WEBUI_9249_UNSET}"), live],
        pick=lambda entries: next(e for e in entries if e.id == "live"),
    )
    _pool_stub(monkeypatch, {"api_key": "${WEBUI_9249_UNSET}", "credential_pool": pool})
    _patch_cfg(monkeypatch, _record())

    runtime = config._custom_record_pool_runtime(_URL, _record())
    assert runtime is not None
    assert runtime["api_key"] == "valid-pool-key"


def test_embedded_unresolved_entry_is_skipped_too(monkeypatch):
    """Unresolved means ``${...}`` ANYWHERE in the entry's credential."""
    pool = _Pool([_Entry("dead", "prefix-${WEBUI_9249_UNSET}"), _Entry("live", "valid-pool-key")])
    _pool_stub(monkeypatch, {"api_key": "prefix-${WEBUI_9249_UNSET}", "credential_pool": pool})
    _patch_cfg(monkeypatch, _record())

    runtime = config._custom_record_pool_runtime(_URL, _record())
    assert runtime is not None
    assert runtime["api_key"] == "valid-pool-key"


def test_a_pool_with_no_usable_entry_is_refused(monkeypatch):
    """Refusal stays terminal only when NOTHING usable remains."""
    pool = _Pool([_Entry("d1", "${A}"), _Entry("d2", "pre-${B}")])
    _pool_stub(monkeypatch, {"api_key": "${A}", "credential_pool": pool})
    _patch_cfg(monkeypatch, _record())

    bundle = _merge()
    assert bundle["api_key"] is None
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, bundle
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL
    assert "produced no API key" in verdict["message"], verdict


def test_a_stubbed_pool_without_entries_keeps_the_conservative_drop(monkeypatch):
    """An uninspectable pool cannot prove a usable sibling exists — keep dropping."""
    _pool_stub(monkeypatch, {"api_key": "${WEBUI_9249_UNSET}", "credential_pool": object()})

    assert config._custom_record_pool_runtime(_URL, _record()) is None


# ── smaller point 1: a pool-only record must get the refusal, not a placeholder ─


def test_record_without_its_own_key_refuses_rather_than_going_keyless(monkeypatch):
    """A record whose only credential source is an unresolved pool is NOT keyless."""
    pool = _Pool([_Entry("dead", "${WEBUI_9249_UNSET}")])
    _pool_stub(monkeypatch, {"api_key": "${WEBUI_9249_UNSET}", "credential_pool": pool})
    _patch_cfg(monkeypatch, _record())  # no api_key of its own

    bundle = _merge()
    assert bundle["api_key"] is None, bundle
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, bundle
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL


def test_a_record_with_no_credential_source_at_all_stays_keyless(monkeypatch):
    """Control: a genuinely keyless endpoint still receives the placeholder."""
    _patch_cfg(monkeypatch, _record())  # no api_key, no pool, no host key

    bundle = _merge()
    assert bundle["api_key"] == config.KEYLESS_CUSTOM_API_KEY
    assert config.custom_provider_route_error(bundle) is None


# ── regression 1: an unresolved reference is never re-resolved ───────────────


@pytest.mark.parametrize(
    "reference",
    ["${WEBUI_9249_PRESENT}", "prefix-${WEBUI_9249_PRESENT}"],
)
def test_unresolved_reference_is_absent_under_a_profile_scope(monkeypatch, reference):
    """Under a profile scope the ladder must NOT borrow the process env."""
    monkeypatch.setenv("WEBUI_9249_PRESENT", "process-secret-value")

    from api.config import _thread_ctx

    previous_block = getattr(_thread_ctx, "block_process_env_fallback", False)
    previous_env = getattr(_thread_ctx, "env", None)
    _thread_ctx.block_process_env_fallback = True
    _thread_ctx.env = {}
    try:
        assert config._resolve_custom_record_key(reference, None, None) is None
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del _thread_ctx.env
            except AttributeError:
                pass
        else:
            _thread_ctx.env = previous_env


def test_unresolved_reference_keeps_todays_resolution_outside_a_profile_scope(
    monkeypatch,
):
    """Control: the root/ambient read path keeps resolving a whole-string ref."""
    monkeypatch.setenv("WEBUI_9249_PRESENT", "process-secret-value")

    assert (
        config._resolve_custom_record_key("${WEBUI_9249_PRESENT}", None, None)
        == "process-secret-value"
    )
    # An embedded reference never had a resolution path; it stays absent.
    assert config._resolve_custom_record_key("prefix-${WEBUI_9249_PRESENT}", None, None) is None


def test_a_literal_key_is_still_forwarded_byte_for_byte(monkeypatch):
    """Control: ordinary keys keep today's behaviour."""
    monkeypatch.setenv("WEBUI_9249_PRESENT", "process-secret-value")

    assert config._resolve_custom_record_key("literal-key", None, None) == "literal-key"
    assert config._resolve_custom_record_key("", None, None) is None


def test_named_profile_scope_refuses_a_foreign_process_env_reference(monkeypatch):
    """The chat bundle must refuse, never authenticate with the other profile's value."""
    monkeypatch.setenv("WEBUI_9249_PRESENT", "process-secret-value")
    _patch_cfg(monkeypatch, _record(api_key="${WEBUI_9249_PRESENT}"))

    from api.config import _thread_ctx

    previous_env = getattr(_thread_ctx, "env", None)
    previous_block = getattr(_thread_ctx, "block_process_env_fallback", False)
    _thread_ctx.env = {"PROFILE_ONLY_VAR": "profile-value"}
    _thread_ctx.block_process_env_fallback = True
    try:
        bundle = _merge()
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del _thread_ctx.env
            except AttributeError:
                pass
        else:
            _thread_ctx.env = previous_env

    assert bundle["api_key"] is None, bundle
    assert "process-secret-value" not in str(bundle)
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, bundle
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL
    # No usable key in the bundle means the chat request cannot carry an
    # Authorization header for this route.


def test_a_reference_the_profile_defines_expands_upstream_and_flows(monkeypatch):
    """Control: the profile's OWN value still resolves, via upstream expansion."""
    monkeypatch.setenv("WEBUI_9249_PRESENT", "process-secret-value")

    from api.config import _expand_env_vars, _thread_ctx

    previous_env = getattr(_thread_ctx, "env", None)
    previous_block = getattr(_thread_ctx, "block_process_env_fallback", False)
    _thread_ctx.env = {"WEBUI_9249_OWN": "profile-own-secret"}
    _thread_ctx.block_process_env_fallback = True
    try:
        expanded = _expand_env_vars({"api_key": "${WEBUI_9249_OWN}"})
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del _thread_ctx.env
            except AttributeError:
                pass
        else:
            _thread_ctx.env = previous_env

    assert expanded["api_key"] == "profile-own-secret"
    assert config._resolve_custom_record_key(expanded["api_key"], None, None) == (
        "profile-own-secret"
    )


def test_profile_scope_expands_the_config_under_its_own_binding(tmp_path, monkeypatch):
    """The CONFIG read itself must not borrow the process environment (#9249).

    ``get_config()`` serves a process-env-expanded cache; inside a named
    profile that would already have turned ``${VAR}`` into another profile's
    value before any credential ladder ran. The credential selection reads the
    raw file under the profile binding instead.
    """
    import yaml as _yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _yaml.safe_dump(
            {
                "custom_providers": [
                    {"name": "pooled", "base_url": _URL, "api_key": "${WEBUI_9249_PRESENT}"}
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEBUI_9249_PRESENT", "process-secret-value")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    shared: dict = {}
    monkeypatch.setattr(config, "_cfg_cache", shared)
    monkeypatch.setattr(config, "cfg", shared)

    from api.config import _thread_ctx

    previous_env = getattr(_thread_ctx, "env", None)
    previous_block = getattr(_thread_ctx, "block_process_env_fallback", False)
    _thread_ctx.env = {"PROFILE_ONLY_VAR": "profile-value"}
    _thread_ctx.block_process_env_fallback = True
    try:
        selected = config._credential_selection_config()
        record = selected["custom_providers"][0]
        assert record["api_key"] == "${WEBUI_9249_PRESENT}", (
            "a profile-scoped config read must leave an undefined reference verbatim"
        )
        bundle = _merge()
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del _thread_ctx.env
            except AttributeError:
                pass
        else:
            _thread_ctx.env = previous_env

    assert bundle["api_key"] is None, bundle
    assert "process-secret-value" not in str(bundle)
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, bundle
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL


def test_profile_scope_expands_a_reference_the_profile_defines(tmp_path, monkeypatch):
    """Control: the profile's own binding resolves its OWN variables."""
    import yaml as _yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _yaml.safe_dump(
            {
                "custom_providers": [
                    {"name": "pooled", "base_url": _URL, "api_key": "${WEBUI_9249_OWN}"}
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEBUI_9249_OWN", "process-must-not-win")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    shared: dict = {}
    monkeypatch.setattr(config, "_cfg_cache", shared)
    monkeypatch.setattr(config, "cfg", shared)

    from api.config import _thread_ctx

    previous_env = getattr(_thread_ctx, "env", None)
    previous_block = getattr(_thread_ctx, "block_process_env_fallback", False)
    _thread_ctx.env = {"WEBUI_9249_OWN": "profile-own-secret"}
    _thread_ctx.block_process_env_fallback = True
    try:
        bundle = _merge()
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del _thread_ctx.env
            except AttributeError:
                pass
        else:
            _thread_ctx.env = previous_env

    assert bundle["api_key"] == "profile-own-secret", bundle
    assert config.custom_provider_route_error(bundle) is None


# ── smaller point 2: declared credentials block the host-gated env fallback ──


def test_a_record_that_declares_a_credential_never_takes_the_host_key(monkeypatch):
    """``api_key: ${VAR}`` (unresolved) must not silently become OPENAI_API_KEY."""
    monkeypatch.setattr(config, "_host_gated_env_key", lambda base_url: "host-env-key")
    _patch_cfg(
        monkeypatch,
        _record(base_url="https://api.openai.com/v1", api_key="${WEBUI_9249_UNSET}"),
    )

    bundle = _merge()
    assert bundle["api_key"] is None, bundle
    assert bundle["api_key"] != "host-env-key"
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, bundle
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL


@pytest.mark.parametrize("field", ["api_key", "key_env"])
def test_each_declared_env_reference_blocks_the_host_key(monkeypatch, field):
    """``api_key``/``key_env`` declarations block the ambient host key."""
    monkeypatch.setattr(config, "_host_gated_env_key", lambda base_url: "host-env-key")
    _patch_cfg(
        monkeypatch,
        _record(base_url="https://api.openai.com/v1", **{field: "${WEBUI_9249_UNSET}"}),
    )

    bundle = _merge()
    assert bundle["api_key"] is None, bundle
    assert bundle["api_key"] != "host-env-key"


def test_a_declared_key_cmd_blocks_the_host_key_too(monkeypatch):
    """``key_cmd`` mints its own bearer — the ambient host key must not replace it."""
    monkeypatch.setattr(config, "_host_gated_env_key", lambda base_url: "host-env-key")
    _command_token_source_stub(monkeypatch)
    _patch_cfg(
        monkeypatch,
        _record(base_url="https://api.openai.com/v1", key_cmd="printf test-token"),
    )

    bundle = _merge()
    assert bundle["api_key"] != "host-env-key"
    assert callable(bundle["api_key"])
    assert config.custom_provider_route_error(bundle) is None


def test_a_record_with_no_declared_credential_still_uses_the_host_key(monkeypatch):
    """Control: the host-gated key keeps serving records that declare nothing."""
    monkeypatch.setattr(config, "_host_gated_env_key", lambda base_url: "host-env-key")
    _patch_cfg(monkeypatch, _record(base_url="https://api.openai.com/v1"))

    bundle = _merge()
    assert bundle["api_key"] == "host-env-key"
    assert config.custom_provider_route_error(bundle) is None


# ── controls: real keys and non-reference dollar shapes are untouched ────────


@pytest.mark.parametrize(
    "value",
    ["$VAR", "{VAR}", "key$with$dollars", "${VAR", "sk-🔑", "  padded-key  "],
)
def test_dollar_shapes_that_are_not_env_refs_keep_flowing(monkeypatch, value):
    """Only ``${...}`` is a reference; ordinary dollar text is a real key."""
    pool = _Pool([_Entry("live", value)])
    _pool_stub(monkeypatch, {"api_key": value, "credential_pool": pool})
    _patch_cfg(monkeypatch, _record())

    runtime = config._custom_record_pool_runtime(_URL, _record())
    assert runtime is not None, value
    assert runtime["api_key"] == value
