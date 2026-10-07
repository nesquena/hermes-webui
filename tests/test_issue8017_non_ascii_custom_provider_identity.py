"""Regression tests for #8017 — a non-ASCII custom provider name is not dropped.

Reported symptom: a ``custom_providers[]`` entry whose ``name`` contains no ASCII
characters (a pure-CJK name such as ``晨光鑫遇专用``) slugified to the empty
string, so the model-catalog builder treated it as "no provider" and skipped the
whole entry. Its models never appeared in the WebUI picker while the CLI kept
resolving the same endpoint, with no error or diagnostic on the response.

Root cause: ``_custom_provider_slug_from_name()`` slugifies with an ASCII-only
character class and returned ``""`` when nothing survived. The entry then lost
its identity in ``get_available_models()`` (the only place the catalog decides
which named groups exist), and the group was never built.

The fix keeps the name's own characters when no ASCII identifier character
survives, so the WebUI mints the same ``custom:<name>`` the Agent resolves it to
(``_agent_custom_provider_slug`` already folds spaces and keeps such characters).
A name with ANY ASCII identifier character is unchanged, so existing ASCII
identities are preserved.
"""

from __future__ import annotations

import sys
import types

import pytest

import api.config as config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_models_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Keep the credential path off the real ~/.hermes (#8017 config uses key_env).

    The reported config resolves the key through ``key_env``, so the catalog read
    reaches the auth store; without an isolated home the run would touch the real
    ``~/.hermes/auth.json`` (hermes_cli guards against exactly that).
    """
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BASE_HOME", str(home))


def _stub_provider_modules(monkeypatch, detected_provider_ids: list[dict]):
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: detected_provider_ids
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "config_yaml"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(
        config,
        "_get_auth_store_path",
        lambda: config.Path("/tmp/does-not-exist-auth.json"),
    )


def _set_cfg(provider_name: str):
    """Swap in a config with one custom_providers entry, returning a restore fn.

    The mtime is pinned so get_available_models()'s mtime-guard does not reload
    the on-disk config over the patch (the same pattern the other catalog tests
    use — without it the real ~/.hermes config wins and the test is a no-op).
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "DeepSeek-V4.1-Flash",
                "provider": "custom",
                "base_url": "http://127.0.0.1:8317/v1",
                "api_key": "${HERMES_CUSTOM_127_0_0_1_8317_API_KEY}",
            },
            "custom_providers": [
                {
                    "name": provider_name,
                    "base_url": "http://127.0.0.1:8317/v1",
                    "key_env": "HERMES_CUSTOM_127_0_0_1_8317_API_KEY",
                    "model": "DeepSeek-V4.1-Flash",
                }
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    config.invalidate_models_cache()

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return restore


# ---------------------------------------------------------------------------
# The producer itself
# ---------------------------------------------------------------------------


def test_non_ascii_name_mints_a_slug_instead_of_the_empty_string():
    """A name with no ASCII identifier character is not dropped to '' (#8017).

    The empty slug is what the catalog reads as "no provider", so this is the
    root-cause assertion: the identity must survive the name.
    """
    assert (
        config._custom_provider_slug_from_name("晨光鑫遇专用") == "custom:晨光鑫遇专用"
    )


def test_ascii_names_keep_their_existing_identity():
    """The fallback must not disturb names that already slugify (regression guard).

    These already produced slugs before the fix; keeping the same output is what
    stops the change from re-identifying existing ASCII providers.
    """
    assert config._custom_provider_slug_from_name("Proxy Main") == "custom:proxy-main"
    assert config._custom_provider_slug_from_name("Foo (Bar)") == "custom:foo-bar"
    assert config._custom_provider_slug_from_name("foo-bar") == "custom:foo-bar"
    # A mixed name keeps its ASCII identity: the fallback only runs when NOTHING
    # ASCII survived, so this one is slugged exactly as before.
    assert (
        config._custom_provider_slug_from_name("晨光鑫遇专用 (cgxy-cpa)")
        == "custom:cgxy-cpa"
    )


def test_collision_key_matches_the_producer_for_a_non_ascii_name():
    """The slug key derives from the producer, so a non-ASCII name has one identity.

    ``_custom_provider_slug_key`` is the collision/credential boundary: if it and
    the producer disagreed the entry could be catalogued under one id and resolved
    under another.
    """
    assert config._custom_provider_slug_key(
        "晨光鑫遇专用"
    ) == config._custom_provider_slug_key("custom:晨光鑫遇专用")
    assert config._custom_provider_slug_key("晨光鑫遇专用") == "晨光鑫遇专用"


@pytest.mark.parametrize(
    "name",
    [
        "晨光鑫遇专用",  # the reported name: no ASCII at all
        "晨光 鑫遇",  # one space
        "晨光  鑫遇",  # two spaces -> two dashes, neither collapsed
        "晨光-鑫遇",  # an ASCII dash already present
        "晨曦·专用",  # a middle dot
    ],
)
def test_fallback_identity_matches_the_agent_vocabulary(name):
    """The picker's id must be exactly what the Agent mints for the name.

    The picker emits `custom:<slug>` as the id the Agent then has to resolve for
    that entry, so any divergence means selecting the model does not reach its
    endpoint. `_agent_custom_provider_slug` is this module's mirror of
    `hermes_cli.providers.custom_provider_slug()`, so comparing the two is the
    compatibility check — the fallback must reproduce it character for character,
    including a name whose spaces would normalize differently.
    """
    produced = config._custom_provider_slug_from_name(name)
    agent = config._agent_custom_provider_slug(name)
    assert produced == agent


def test_a_colon_in_a_name_takes_the_keyless_pre_fix_identity():
    """A ':' in a non-ASCII name mints no name-derived identity (#8026).

    The qualified-model hint is ``@custom:<name>:<model>``. A name that itself
    carries a colon gives that string a segment the parser cannot attribute:
    ``@custom:晨光:鑫遇:model-a`` splits into provider ``custom:晨光`` and model
    ``鑫遇:model-a``, the endpoint vanishes, and sending fails with
    ``unowned_custom_provider``. The reviewer's fix returns ``""`` from the
    fallback so the name takes the pre-fix route, which for a name with no
    ASCII identifier characters is the empty slug -- the entry is not
    catalogued, exactly as master behaves today; nothing that worked is broken,
    and nothing that cannot route is advertised.
    """
    assert config._custom_provider_slug_from_name("晨光:鑫遇") == ""
    # An ASCII name with a colon still folds as it always has.
    assert config._custom_provider_slug_from_name("Local (127.0.0.1:15721)") == (
        "custom:local-127.0.0.1-15721"
    )


def test_keyless_non_ascii_providers_do_not_share_one_api_key_env(monkeypatch):
    """Two fallback non-ASCII providers must not take the shared variable (#8026).

    The fallback mints an id whose characters all sanitize away (the constant
    ``CUSTOM`` stands in for the empty run), so two distinct non-ASCII providers
    would both read ``CUSTOM_CUSTOM_API_KEY`` and the key meant for the first
    would travel to the second's endpoint as a bearer token.
    ``CUSTOM_API_KEY`` is the wrong variable to set here: master never read it, so
    a test that used it would pass on master too and pin nothing.

    The refusal is a RECORD-level decision, not an id-level one: the id
    ``custom:晨光`` is identical whether the user typed ``custom:晨光`` (which
    reads the convention variable on master) or the fallback minted it, so only
    the record's own ``name`` can tell them apart (round 4).
    """
    monkeypatch.setenv("CUSTOM_CUSTOM_API_KEY", "sk-SHARED")

    # The id-level name follows master's whole-id rule for BOTH shapes ...
    assert config._api_key_env_name("custom:晨光鑫遇专用") == "CUSTOM_CUSTOM_API_KEY"
    assert config._api_key_env_name("custom:晨曦专用") == "CUSTOM_CUSTOM_API_KEY"
    # ... but neither fallback RECORD may read it.
    assert not config._custom_provider_record_may_take_convention_key(
        {"name": "晨光鑫遇专用"}, "custom_providers"
    )
    assert not config._custom_provider_record_may_take_convention_key(
        {"name": "晨曦专用"}, "custom_providers"
    )
    # A prefixed name, an ASCII name, and a providers:/model: record all keep it.
    assert config._custom_provider_record_may_take_convention_key(
        {"name": "custom:晨光"}, "custom_providers"
    )
    assert config._custom_provider_record_may_take_convention_key(
        {"name": "proxy-a"}, "custom_providers"
    )
    assert config._custom_provider_record_may_take_convention_key(
        {"name": "晨光鑫遇专用"}, "providers"
    )

    # The ASCII control still mints DISTINCT per-provider names.
    assert config._api_key_env_name("custom:proxy-a") == "CUSTOM_PROXY_A_API_KEY"
    assert config._api_key_env_name("custom:proxy-b") == "CUSTOM_PROXY_B_API_KEY"


def test_ids_differing_only_by_the_custom_prefix_do_not_share_a_variable():
    """The variable name comes from the WHOLE id, so `custom:` must not be stripped.

    Deriving the name from only the part after `custom:` collapses `custom:foo`
    and `custom:custom_foo` onto one variable, so the second provider would read
    the first's key -- the same leak as the non-ASCII collision, one shape over.
    """
    assert config._api_key_env_name("custom:foo") == "CUSTOM_FOO_API_KEY"
    assert config._api_key_env_name("custom:custom_foo") == "CUSTOM_CUSTOM_FOO_API_KEY"
    assert config._api_key_env_name("custom:bar") != config._api_key_env_name("custom:custom_bar")


def test_two_non_ascii_providers_resolve_no_convention_key(monkeypatch):
    """The maintainer's probe, end to end through the connection resolver (#8026).

    Two keyless non-ASCII records must not both take the shared convention
    variable: with it set, a keyless record that reads it sends the key to an
    endpoint it was never configured for. This drives
    ``resolve_custom_provider_connection`` rather than the name helper alone, so
    it pins the bundle streaming actually hands the Agent.
    """
    monkeypatch.setenv("CUSTOM_CUSTOM_API_KEY", "sk-SHARED")
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "晨光鑫遇专用",
                    "base_url": "http://127.0.0.1:8317/v1",
                },
                {
                    "name": "晨曦专用",
                    "base_url": "http://10.0.0.9:9000/v1",
                },
            ],
        },
    )

    first_key, first_url = config.resolve_custom_provider_connection("custom:晨光鑫遇专用")
    second_key, second_url = config.resolve_custom_provider_connection("custom:晨曦专用")

    assert first_url == "http://127.0.0.1:8317/v1"
    assert second_url == "http://10.0.0.9:9000/v1"
    assert first_key is None, "a fallback provider must not read the shared convention key"
    assert second_key is None, "a fallback provider must not read the shared convention key"


def test_ascii_punctuation_only_id_keeps_its_convention_key(monkeypatch):
    """CONTROL pinning master's behaviour for ASCII punctuation-only ids (#8026).

    The maintainer asked for this guard by name. It asserts PRE-EXISTING
    behaviour, so it does not fail on master and is not the regression proof --
    there is no committed branch here without the guard. Its job is to pin that
    this change does not re-introduce the round-2 regression, where the guard for
    unnameable Unicode ids also caught ASCII ids whose distinctive part has no
    letters or digits. Those names mint their variable from their OWN id, so they
    never shared one; returning ``""`` there sends ``dummy-key`` to a provider
    that authenticates on master. Rare, but a working setup must not break on
    upgrade.
    """
    assert config._api_key_env_name("custom:_") == "CUSTOM_CUSTOM_API_KEY"
    assert config._api_key_env_name("custom:.") == "CUSTOM_CUSTOM_API_KEY"
    assert config._custom_provider_slug_from_name("_") == "custom:_"
    assert config._custom_provider_slug_from_name(".") == "custom:."

    monkeypatch.setenv("CUSTOM_CUSTOM_API_KEY", "sk-ascii")
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {"name": "_", "base_url": "http://127.0.0.1:8400/v1"},
            ],
        },
    )
    api_key, base_url = config.resolve_custom_provider_connection("custom:_")
    assert base_url == "http://127.0.0.1:8400/v1"
    assert api_key == "sk-ascii"


def test_two_whitespace_name_and_double_dash_name_do_not_collapse():
    """The fallback does not collapse dashes or fold characters (#8017).

    ``晨光  鑫遇`` (two spaces) and ``晨光-鑫遇`` (an ASCII dash) are different
    names and must stay different identities; collapsing repeated dashes or
    folding other characters would merge them into one entry's id.
    """
    assert config._custom_provider_slug_from_name("晨光  鑫遇") == "custom:晨光--鑫遇"
    assert config._custom_provider_slug_from_name("晨光-鑫遇") == "custom:晨光-鑫遇"


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def test_catalog_includes_a_non_ascii_only_custom_provider(monkeypatch):
    """The reported symptom: the entry's models appear under its own group (#8017).

    Before the fix the empty slug meant the named group was never created and
    ``/api/models`` reported ``groups: []``. The group must now be present with
    the entry's configured model.
    """
    _stub_provider_modules(
        monkeypatch,
        [{"id": "custom:晨光鑫遇专用", "authenticated": True}],
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _set_cfg(provider_name="晨光鑫遇专用")
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_id = {g["provider_id"]: g for g in result["groups"]}
    assert "custom:晨光鑫遇专用" in groups_by_id, (
        "a non-ASCII-only custom provider must not be dropped from the catalog; "
        f"got groups {sorted(groups_by_id)}"
    )
    model_ids = [m["id"] for m in groups_by_id["custom:晨光鑫遇专用"]["models"]]
    assert "DeepSeek-V4.1-Flash" in model_ids


def test_catalog_keeps_two_non_ascii_providers_with_one_base_url_separate(monkeypatch):
    """Two non-ASCII names sharing a base_url stay two identities (#8017).

    The maintainer asked against an endpoint-only fallback for exactly this: a
    slug derived from the URL would merge these two separately-configured
    providers, so each must reach the catalog under its own identity.
    """
    _stub_provider_modules(
        monkeypatch,
        [
            {"id": "custom:晨光鑫遇专用", "authenticated": True},
            {"id": "custom:晨曦专用", "authenticated": True},
        ],
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "DeepSeek-V4.1-Flash",
                "provider": "custom",
                "base_url": "http://127.0.0.1:8317/v1",
            },
            "custom_providers": [
                {
                    "name": "晨光鑫遇专用",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-a",
                    "model": "DeepSeek-V4.1-Flash",
                },
                {
                    "name": "晨曦专用",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-b",
                    "model": "DeepSeek-V4.1-Flash",
                },
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    config.invalidate_models_cache()
    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    groups_by_id = {g["provider_id"]: g for g in result["groups"]}
    assert "custom:晨光鑫遇专用" in groups_by_id, f"got groups {sorted(groups_by_id)}"
    assert "custom:晨曦专用" in groups_by_id, f"got groups {sorted(groups_by_id)}"


def test_non_ascii_slug_round_trips_through_qualified_selection(monkeypatch):
    """A provider-qualified pick of a non-ASCII provider resolves back to it (#8017).

    The slug is the identity handed to the agent (``custom:<slug>``), so even a
    kept group is useless if selecting a model from it cannot round-trip. This
    drives the ``@provider:model`` hint the picker emits for a non-active
    provider through ``resolve_model_provider``.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "DeepSeek-V4.1-Flash",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
            },
            "custom_providers": [
                {
                    "name": "晨光鑫遇专用",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-xxx",
                    "model": "DeepSeek-V4.1-Flash",
                }
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    try:
        model, provider, base_url = config.resolve_model_provider(
            "@custom:晨光鑫遇专用:DeepSeek-V4.1-Flash", explicitly_picked=True
        )
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path

    assert model == "DeepSeek-V4.1-Flash"
    assert provider == "custom:晨光鑫遇专用"
    assert base_url == "http://127.0.0.1:8317/v1"


def test_catalog_still_includes_an_ascii_custom_provider(monkeypatch):
    """The ASCII path is unchanged: the fix does not cost the ordinary case (#8017).

    A control for the regression above — same fixture, an ASCII name — so a fix
    that "keeps the group" by breaking the ASCII path is caught.
    """
    _stub_provider_modules(
        monkeypatch,
        [{"id": "custom:cgxy-cpa", "authenticated": True}],
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _set_cfg(provider_name="cgxy-cpa")
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_id = {g["provider_id"]: g for g in result["groups"]}
    assert "custom:cgxy-cpa" in groups_by_id
    model_ids = [m["id"] for m in groups_by_id["custom:cgxy-cpa"]["models"]]
    assert "DeepSeek-V4.1-Flash" in model_ids


# ---------------------------------------------------------------------------
# Round four: an EXISTING identity owner comes first (#8026)
# ---------------------------------------------------------------------------


def test_identity_owner_helper_admits_a_free_fallback_and_refuses_a_claimed_one():
    """The ownership rule itself: a claimed slug is refused, a free one admitted.

    ``_custom_provider_entry_identity`` is the cfg-aware view every consumer
    uses. A fallback-derived name whose slug a legacy entry already owns mints
    nothing; on its own, the same name keeps the fallback identity the picker
    needs. The ``providers:`` vocabulary counts too (both the record key and its
    ``name``), so a keyed route is an owner as well.
    """
    # Claimed by a prefixed list entry -> refused.
    cfg_claimed = {
        "custom_providers": [{"name": "custom:晨光"}, {"name": "晨光"}],
    }
    assert (
        config._custom_provider_entry_identity(
            {"name": "晨光"}, cfg_claimed["custom_providers"], None
        )
        == ""
    )
    # Claimed by a keyed providers: record -> refused.
    cfg_keyed = {
        "custom_providers": [{"name": "晨光"}],
        "providers": {"custom:晨光": {"base_url": "http://127.0.0.1:8317/v1"}},
    }
    assert (
        config._custom_provider_entry_identity(
            {"name": "晨光"}, cfg_keyed["custom_providers"], cfg_keyed["providers"]
        )
        == ""
    )
    # Alone -> the fallback identity is kept, unconstrained.
    assert (
        config._custom_provider_entry_identity({"name": "晨光"}, [{"name": "晨光"}], None)
        == "custom:晨光"
    )
    # An ASCII name is never treated as fallback-derived, owner or not.
    assert (
        config._custom_provider_entry_identity(
            {"name": "custom:omni"}, [{"name": "custom:omni"}], None
        )
        == "custom:omni"
    )


def test_resolution_does_not_raise_when_a_legacy_entry_owns_the_identity(monkeypatch):
    """A config holding both `custom:晨光` and `晨光` still resolves the prefixed one.

    The maintainer's first probe: before this round both entries normalized to
    the same slug, so ``resolve_custom_provider_connection`` raised
    ``AmbiguousCustomProviderError`` and sending made zero provider requests,
    where master resolved the prefixed record. The fallback identity belongs to
    the entry that already owned the name, so the other entry mints nothing.
    """
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "custom:晨光",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-keyed",
                },
                {
                    "name": "晨光",
                    "base_url": "http://127.0.0.1:8318/v1",
                    "api_key": "sk-legacy",
                    "model": "chat-model",
                },
            ],
        },
    )

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert base_url == "http://127.0.0.1:8317/v1", "the prefixed entry owns the identity"
    assert api_key == "sk-keyed"


def test_keyed_providers_record_is_not_shadowed_by_a_legacy_list_entry(monkeypatch):
    """An existing `providers: {"custom:晨光": ...}` route keeps its endpoint and key.

    The maintainer's second probe: with a legacy list entry named `晨光` present,
    the fallback gave the list entry the same identity, and resolution returned
    that entry's port and key (8318/sk-legacy) instead of the configured 8317
    record's. Master uses the keyed record.
    """
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "providers": {
                "custom:晨光": {
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-keyed",
                }
            },
            "custom_providers": [
                {
                    "name": "晨光",
                    "base_url": "http://127.0.0.1:8318/v1",
                    "api_key": "sk-legacy",
                    "model": "chat-model",
                }
            ],
        },
    )

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert base_url == "http://127.0.0.1:8317/v1", "the keyed record is the owner"
    assert api_key == "sk-keyed"


def test_ascii_punctuation_only_id_without_a_prefix_keeps_the_convention_name():
    """CONTROL pinning master for an ASCII id with nothing to sanitize (#8026).

    ``"-"`` and ``"()"`` sanitize to the empty string, and master substituted the
    constant ``CUSTOM``, so ``CUSTOM_CUSTOM_API_KEY`` is the variable such a route
    read. Those names must keep the convention variable: their own id is still
    distinct, so none of them shares a variable with another provider, and a
    refusal here would change behaviour for a setup that works today.
    """
    assert config._api_key_env_name("-") == "CUSTOM_CUSTOM_API_KEY"
    assert config._api_key_env_name("()") == "CUSTOM_CUSTOM_API_KEY"
    assert config._api_key_env_name("custom:-") == "CUSTOM_CUSTOM_API_KEY"
    assert config._api_key_env_name("custom:()") == "CUSTOM_CUSTOM_API_KEY"


def test_every_consumer_agrees_on_the_entry_that_owns_the_identity(monkeypatch):
    """The shadowed entry owns nothing, on EVERY consumer of the identity (#8026).

    The first cut fixed the connection resolver but left the model router and the
    name lookup on the unconstrained producer, so a shadowed entry could still
    supply the owner's endpoint and be matched by the owner's slug. The router
    assertion below reproduces that: on the previous head it returned the legacy
    entry's port (8318) for the owner's model, so the owner's provider slug was
    paired with an endpoint it does not own. Every consumer is asserted against
    the entry the connection resolver names as the owner, so a fix that repairs
    one consumer and misses another fails here.
    """
    import api.providers as providers

    entries = [
        {
            "name": "custom:晨光",
            "base_url": "http://127.0.0.1:8317/v1",
            "api_key": "sk-keyed",
        },
        {
            "name": "晨光",
            "base_url": "http://127.0.0.1:8318/v1",
            "api_key": "sk-legacy",
            "model": "chat-model",
        },
    ]
    owner, shadowed = entries[0], entries[1]
    cfg_shape = {
        "model": {"provider": "custom:晨光", "default": "chat-model"},
        "custom_providers": list(entries),
    }
    # resolve_model_provider reads the module-level ``cfg``; the connection
    # resolver reads get_config(). Patch both so the two see one config.
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    # Routing must never adopt the shadowed entry's endpoint for the owner's
    # model. This is the finding the first cut missed.
    _, routed_provider, routed_url = config.resolve_model_provider("chat-model")
    assert routed_url != "http://127.0.0.1:8318/v1", (
        "a shadowed fallback entry must not supply the owner's endpoint"
    )

    # The identity view agrees with itself.
    assert config._custom_provider_entry_identity(owner, entries, None) == "custom:晨光"
    assert config._custom_provider_entry_identity(shadowed, entries, None) == ""

    # The owner's own name maps to the identity; the shadowed entry's identity
    # is empty, so no consumer can match it to the owner's slug.
    assert config._named_custom_provider_slug_for_provider(
        "custom:晨光", {"custom_providers": entries}
    ) == "custom:晨光"

    # Credential attribution does not depend on list order.
    for ordered in (entries, list(reversed(entries))):
        assert providers._custom_provider_entry_matches(
            "custom:晨光", owner, ordered, None
        )
        assert not providers._custom_provider_entry_matches(
            "custom:晨光", shadowed, ordered, None
        )

    # The resolver agrees with the router on which record owns the route.
    api_key, owner_url = config.resolve_custom_provider_connection("custom:晨光")
    assert (api_key, owner_url) == ("sk-keyed", "http://127.0.0.1:8317/v1")


def test_a_legacy_entry_with_no_slug_keeps_its_own_endpoint(monkeypatch):
    """A name the convention cannot slug still routes to its OWN url (#8026 r4).

    ``-`` and ``晨光:鑫遇`` have no identity at all: the fallback refuses the first
    (nothing to slug) and the second splits on its colon. Master routed the models
    such an entry declares to the entry's own ``base_url``; the round-3 head
    skipped it, so the request left on the DEFAULT endpoint instead (the
    maintainer's 200-on-master, 404-here).

    The fix requires the fallback test in the skip, so a non-fallback legacy entry
    keeps returning its configured URL. The assertion is on the URL, because a
    wrong route with the right model id is exactly the failure.
    """
    cfg_shape = {
        "model": {
            "default": "chat-model",
            "provider": "custom",
            "base_url": "http://default.example/v1",
        },
        "custom_providers": [
            {"name": "-", "base_url": "http://dash.example/v2", "models": {"chat-model": {}}},
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    model, _provider, base_url = config.resolve_model_provider("chat-model")
    assert model == "chat-model"
    assert base_url == "http://dash.example/v2", (
        "a legacy entry with no slug must keep routing to its own endpoint"
    )


def test_a_fallback_entry_that_owns_nothing_is_still_skipped(monkeypatch):
    """CONTROL: the widened skip must NOT readmit the fallback case it was for (#8026).

    A fallback-derived name whose slug an existing entry already owns mints
    nothing (it did not before #8026 either), so it must still be skipped and the
    model must fall through to the default endpoint. Run beside the case above, so
    "keep every slugless entry" is distinguished from "keep the ones that existed".
    """
    cfg_shape = {
        "model": {
            "default": "chat-model",
            "provider": "custom",
            "base_url": "http://default.example/v1",
        },
        "custom_providers": [
            {"name": "custom:晨光", "base_url": "http://owner.example/v1"},
            {"name": "晨光", "base_url": "http://fallback.example/v1", "models": {"chat-model": {}}},
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    _model, _provider, base_url = config.resolve_model_provider("chat-model")
    assert base_url == "http://default.example/v1", (
        "a shadowed fallback entry must not claim the model; the default endpoint keeps it"
    )
