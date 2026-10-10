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


def test_a_keyless_prefixed_entry_reads_the_convention_key(monkeypatch):
    """A prefixed entry's key comes back from the CONNECTION resolver, end to end.

    The senior review's ask: the only test red on the bounced head for the
    prefixed-key finding was helper-level, so add the full path. A prefixed
    ``custom:晨光`` entry with no literal key and no ``key_env`` must resolve the
    ``CUSTOM_CUSTOM_API_KEY`` convention variable, because that name is its OWN
    (the user typed the id). This goes through
    ``resolve_custom_provider_connection`` rather than the name helper.
    """
    monkeypatch.setenv("CUSTOM_CUSTOM_API_KEY", "sk-convention")
    cfg_shape = {
        "model": {"provider": "custom", "default": "chat-model"},
        "custom_providers": [
            {"name": "custom:晨光", "base_url": "http://prefixed.example/v1"},
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert base_url == "http://prefixed.example/v1"
    assert api_key == "sk-convention", (
        "an existing prefixed entry keeps the convention key it reads on master"
    )


def test_a_model_owned_unicode_route_keeps_its_connection(monkeypatch):
    """An existing ``model:`` authority is not replaced by a newer list entry (#8026 r5).

    With ``model.provider: custom:晨光`` pointing at its own url and key, plus a
    list entry named ``晨光``, the list entry is fallback-derived and would
    otherwise claim ``custom:晨光``: the resolved credential became the dummy and
    the request 401'd, and a literal ``model.api_key`` was discarded too. The
    model block names this slug by its ``provider`` field and owns a real
    connection, so it is an existing owner and the list entry stays shadowed.

    Asserted as a PAIR (key AND url) because a wrong route with the right url is
    exactly the failure: the key is what went missing.
    """
    cfg_shape = {
        "model": {
            "provider": "custom:晨光",
            "default": "chat-model",
            "base_url": "http://model-owned.example/v1",
            "api_key": "sk-model-owned",
        },
        "custom_providers": [
            {"name": "晨光", "base_url": "http://list-entry.example/v1"},
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert (api_key, base_url) == ("sk-model-owned", "http://model-owned.example/v1"), (
        "the model-owned route keeps its own key and url"
    )

    # The list entry owns nothing, so it is not catalogued under the owner's id.
    assert config._custom_provider_entry_identity(
        cfg_shape["custom_providers"][0],
        cfg_shape["custom_providers"],
        None,
        cfg_shape["model"],
    ) == ""


def test_a_disabled_model_block_does_not_own_the_slug(monkeypatch):
    """A switched-off ``model:`` block must not hide a valid same-slug entry (#8026 r7).

    A disabled record is invisible to the Agent's resolver, so it must not own the
    route either. Without the ``enabled`` check the model arm still claimed the
    slug, the list entry stayed shadowed, and the named route ended up with NO
    connection at all (and no catalog group).
    """
    cfg_shape = {
        "model": {
            "provider": "custom:晨光",
            "default": "chat-model",
            "base_url": "http://disabled.example/v1",
            "api_key": "sk-disabled",
            "enabled": False,
        },
        "custom_providers": [
            {"name": "晨光", "base_url": "http://list-entry.example/v1", "api_key": "sk-list"},
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    assert config._custom_provider_identity_owners(
        cfg_shape["custom_providers"], None, cfg_shape["model"]
    ) == set(), "a disabled model block owns nothing"

    # So the previously shadowed list entry is admitted, and its own key resolves.
    assert (
        config._custom_provider_entry_identity(
            cfg_shape["custom_providers"][0],
            cfg_shape["custom_providers"],
            None,
            cfg_shape["model"],
        )
        == "custom:晨光"
    )
    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert (api_key, base_url) == ("sk-list", "http://list-entry.example/v1")


def test_two_non_ascii_providers_do_not_share_one_api_key_env(monkeypatch):
    """Two fallback non-ASCII providers must not take the shared variable (#8026).

    The fallback mints an id whose characters all sanitize away (the constant
    ``CUSTOM`` stands in for the empty run), so two distinct non-ASCII providers
    would both read ``CUSTOM_CUSTOM_API_KEY`` and the key meant for the first
    would travel to the second's endpoint as a bearer token.
    ``CUSTOM_API_KEY`` is the wrong variable to set here: master never read it, so
    a test that used it would pass on master too and pin nothing.

    The two ids map to the SAME variable name (that is the hazard), so the refusal
    cannot live in the name helper; it is a RECORD-level decision, because the id
    ``custom:晨光`` is identical whether the user typed ``custom:晨光`` (which reads
    the convention variable on master) or the fallback minted it (round 4).
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


def test_a_keyless_legacy_entry_cannot_hijack_a_keyed_record_to_the_model_block(monkeypatch):
    """A keyless legacy entry must not hand a keyed record's slug to the model block.

    r9 CORE, ``api/config.py:4213``. The model-block preference used to scan the list
    on its own, before ownership was decided, so a keyless ``晨光`` entry matched the
    slug first and the whole route was redirected to the block's endpoint and
    credential: an existing provider that master completed returned ``auth_mismatch``
    after HTTP 401. Ownership must be settled first, and here the keyed
    ``providers`` record owns ``custom:晨光``.
    """
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {
                "provider": "custom",
                "default": "chat-model",
                "base_url": "http://127.0.0.1:9000/v1",
                "api_key": "sk-model",
            },
            "providers": {
                "custom:晨光": {
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-keyed",
                }
            },
            "custom_providers": [
                {
                    "name": "晨光",
                    "model": "chat-model",
                    # Keyless and endpoint-less: the exact shape the preference loop
                    # matched before ownership filtering ran.
                }
            ],
        },
    )

    assert config.resolve_custom_provider_connection("custom:晨光") == (
        "sk-keyed",
        "http://127.0.0.1:8317/v1",
    ), "the keyed record owns the slug, so the model block must not take it over"


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
    """Each consumer names the right record, and routing keeps master's pair (#8026).

    Two authorities are in play for one slug, and they answer DIFFERENT questions.
    The CREDENTIAL resolver (`custom:晨光`) must follow the prefixed owner, so the
    keyed record's key and URL win: master returns `("sk-keyed", 8317)`.
    ROUTING a model is a separate question: the bare entry declares `chat-model`,
    so master routes that model to the bare entry's OWN url (8318) with an EMPTY
    provider. Keeping the two apart is the point; a fix that makes one consumer
    agree with the other on this config changes master's behaviour either way.
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
    owner, bare = entries[0], entries[1]
    cfg_shape = {
        "model": {"provider": "custom:晨光", "default": "chat-model"},
        "custom_providers": list(entries),
    }
    # resolve_model_provider reads the module-level ``cfg``; the connection
    # resolver reads get_config(). Patch both so the two see one config.
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    # Routing: the bare entry declares this model, so it answers with its own URL
    # and the empty provider. Master's exact pair, asserted whole: a change that
    # sends this to the prefixed owner's port 8317, or to the default endpoint,
    # fails here.
    _, routed_provider, routed_url = config.resolve_model_provider("chat-model")
    assert (routed_provider, routed_url) == ("", "http://127.0.0.1:8318/v1"), (
        "the bare entry's own model routes to its own url with master's empty provider"
    )

    # The identity view agrees with itself.
    assert config._custom_provider_entry_identity(owner, entries, None) == "custom:晨光"
    assert config._custom_provider_entry_identity(bare, entries, None) == ""

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
            "custom:晨光", bare, ordered, None
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

    model, provider, base_url = config.resolve_model_provider("chat-model")
    assert model == "chat-model"
    # The PAIR, not just the URL: master computed the same slug helper (empty for this
    # name) and passed it to the same three-argument _finalize. Asserting only the URL
    # would let a fix that invents the raw name as the provider pass.
    assert provider == "", "a slugless entry resolves to the empty provider, as on master"
    assert base_url == "http://dash.example/v2", (
        "a legacy entry with no slug must keep routing to its own endpoint"
    )


def test_a_legacy_entry_with_no_slug_keeps_its_own_endpoint_for_a_colon_name(monkeypatch):
    """The same skip, for a name that splits on its colon instead of sanitizing away.

    ``晨光:鑫遇`` takes a different branch of the identity helper than ``-`` does, so
    pin both: a fix that widens the skip for one shape and not the other would
    otherwise pass.
    """
    cfg_shape = {
        "model": {
            "default": "chat-model",
            "provider": "custom",
            "base_url": "http://default.example/v1",
        },
        "custom_providers": [
            {
                "name": "晨光:鑫遇",
                "base_url": "http://colon.example/v2",
                "models": {"chat-model": {}},
            },
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    _model, provider, base_url = config.resolve_model_provider("chat-model")
    assert provider == ""
    assert base_url == "http://colon.example/v2"


def test_a_bare_entry_whose_model_is_declared_keeps_routing_even_when_shadowed(monkeypatch):
    """The bare entry still answers for a model it declares (#8026, reviewer finding 1).

    This started life as a control asserting the OPPOSITE (that the shadowed entry
    is skipped and the model falls to the default endpoint). That expectation was
    wrong, and the maintainer reproduced it as a 404 against master's 200: with
    both entries configured, master routes the bare entry's declared model to the
    bare entry's own URL with an empty provider. The skip is gone, so assert
    master's pair.
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

    _model, provider, base_url = config.resolve_model_provider("chat-model")
    assert (provider, base_url) == ("", "http://fallback.example/v1"), (
        "the bare entry's declared model keeps its own endpoint, as on master"
    )


# ---------------------------------------------------------------------------
# Round six: a written model block is not an authority, and a keyless or
# endpoint-less fallback entry keeps the model connection (#8026 r6)
# ---------------------------------------------------------------------------


def test_a_model_block_written_from_a_fallback_entry_does_not_shadow_it(monkeypatch):
    """The picker's own ``model:`` write does not hide the entry it came from (#8026).

    Selecting a model in the picker runs ``set_hermes_default_model``, which copies
    the entry's ``provider`` and ``base_url`` into the ``model:`` block. That block
    then holds the same endpoint as the entry, but it is a COPY of the entry's
    connection, not a separate authority. Counting it as an owner shadowed the
    non-ASCII entry after one click: it minted nothing, lost its credential, and
    the next send went out with the keyless placeholder (401 against an
    authenticated endpoint). An ASCII entry in the identical shape kept its
    identity, so this is the Unicode row failing to match the ASCII one.

    Asserted as a PAIR on the resolver (key AND url) and on the routed slug,
    because a wrong route with the right url is exactly the failure: the key is
    what went missing.
    """
    u = "http://one.example/v1"
    cfg_shape = {
        "model": {"provider": "custom:晨光鑫遇专用", "base_url": u, "default": "chat-model"},
        "custom_providers": [
            {"name": "晨光鑫遇专用", "base_url": u, "api_key": "sk-entry", "model": "chat-model"},
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    assert config._custom_provider_identity_owners(
        cfg_shape["custom_providers"], None, cfg_shape["model"]
    ) == set(), "a model block that mirrors the entry's own endpoint owns nothing"

    assert (
        config._custom_provider_entry_identity(
            cfg_shape["custom_providers"][0],
            cfg_shape["custom_providers"],
            None,
            cfg_shape["model"],
        )
        == "custom:晨光鑫遇专用"
    )

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光鑫遇专用")
    assert (api_key, base_url) == ("sk-entry", u), (
        "the entry the model block was written from keeps its identity and key"
    )

    _model, provider, routed_url = config.resolve_model_provider("chat-model")
    assert (provider, routed_url) == ("custom:晨光鑫遇专用", u)

    # A model block at a DIFFERENT endpoint is a real authority, so the entry
    # stays shadowed (the maintainer's model-owned scenario is unchanged).
    u2 = "http://model-owned.example/v1"
    cfg_own = {
        "model": {"provider": "custom:晨光", "base_url": u2, "default": "chat-model", "api_key": "***"},
        "custom_providers": [{"name": "晨光", "base_url": "http://list-entry.example/v1"}],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_own))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_own))
    assert "晨光" in config._custom_provider_identity_owners(
        cfg_own["custom_providers"], None, cfg_own["model"]
    ), "a model block at its own endpoint still owns the slug"


def test_a_keyless_fallback_entry_sharing_the_model_endpoint_keeps_the_connection(monkeypatch):
    """A keyless fallback entry does not replace the model connection (#8026 r6 CORE).

    ``model: {provider: custom, base_url: U, key_env: MODEL_KEY}`` plus a keyless
    non-ASCII entry at the same U served the model on master. Admitting the entry
    as a fallback identity made the exact-row rule return IT instead, so the
    resolved credential became the keyless placeholder and an authenticated
    endpoint answered 401. The model block supplied the connection, so it is the
    authority here; a same-endpoint entry that declares its OWN key is untouched.
    """
    monkeypatch.setenv("MODEL_KEY", "sk-modelkey")
    u = "http://127.0.0.1:8317/v1"
    cfg_shape = {
        "model": {"provider": "custom", "base_url": u, "default": "chat-model", "key_env": "MODEL_KEY"},
        "custom_providers": [{"name": "晨光鑫遇专用", "base_url": u}],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光鑫遇专用")
    assert (api_key, base_url) == ("sk-modelkey", u), (
        "the model connection's key and endpoint are kept, not the keyless entry's"
    )

    # The constructor-ready bundle: this is what the send path applies, so assert
    # the endpoint AND the credential the request goes out with, not just the URL.
    _m, provider, routed_url = config.resolve_model_provider("chat-model")
    bundle = config.merge_custom_provider_runtime_bundle(
        provider, "dummy-key", routed_url, runtime_provider=None, lookup_provider=provider
    )
    assert bundle["base_url"] == u, "the merged route keeps the model connection's endpoint"
    assert bundle["api_key"] == "sk-modelkey", "and its key, not the placeholder"
    assert bundle.get(config.CUSTOM_ROUTE_ERROR_FIELD) is None


def test_a_fallback_entry_with_no_endpoint_keeps_the_model_connection(monkeypatch):
    """A fallback entry with no endpoint inherits ``model.base_url`` (#8026 r6 CORE).

    Omitting the entry's ``base_url`` previously let the configured model
    connection serve its declared model (master: 200). The new named route instead
    failed with ``custom_provider_endpoint_unresolved`` because the endpoint-less
    entry owned the route. The model block is the connection, so it is the
    authority for a fallback entry that declares no endpoint of its own.
    """
    u = "http://127.0.0.1:8317/v1"
    cfg_shape = {
        "model": {"provider": "custom", "base_url": u, "default": "chat-model", "api_key": "sk-model"},
        "custom_providers": [{"name": "晨光鑫遇专用", "model": "chat-model"}],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光鑫遇专用")
    assert (api_key, base_url) == ("sk-model", u), (
        "an endpoint-less fallback entry inherits the model connection"
    )

    _m, provider, routed_url = config.resolve_model_provider("chat-model")
    bundle = config.merge_custom_provider_runtime_bundle(
        provider, "dummy-key", routed_url, runtime_provider=None, lookup_provider=provider
    )
    assert bundle["base_url"] == u, "the merged route is not left endpoint-unresolved"
    assert bundle.get(config.CUSTOM_ROUTE_ERROR_FIELD) is None


def _write_cfg(tmp_path, body: str) -> "config.Path":
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _load(cfg_path):
    loaded = config._load_yaml_config_file(cfg_path)
    config.cfg.clear()
    config.cfg.update(loaded)
    return loaded


@pytest.fixture(autouse=True)
def _restore_shared_config():
    """Undo the picker tests' in-place mutation of the shared ``config.cfg``.

    ``_load`` clears and refills the module-global config dict, which is the same
    object as ``config._cfg_cache`` (``cfg`` is an alias). Nothing restored it, so a
    later test could inherit this file's fake provider config and its result would
    depend on test order. Snapshot the dict and the mtime/path guards and put them
    back, so the fake config never escapes this module.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    yield
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config._cfg_path = old_path


def test_set_default_model_keeps_the_model_key_after_picker_click(monkeypatch, tmp_path):
    """The picker's own write must not strip the model connection (r6 MUST-FIX a)."""
    monkeypatch.setenv("MODEL_KEY", "sk-modelenv")
    U = "http://127.0.0.1:8317/v1"
    cfg_path = _write_cfg(
        tmp_path,
        "model:\n"
        "  provider: custom\n"
        "  default: chat-model\n"
        f"  base_url: {U}\n"
        "  key_env: MODEL_KEY\n"
        "custom_providers:\n"
        "  - name: 晨光鑫遇专用\n"
        f"    base_url: {U}\n",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    _load(cfg_path)
    assert config.resolve_custom_provider_connection("custom:晨光鑫遇专用") == ("sk-modelenv", U)

    result = config.set_hermes_default_model("chat-model", provider="custom:晨光鑫遇专用")
    assert result["ok"] is True

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光鑫遇专用")
    assert (api_key, base_url) == ("sk-modelenv", U), (
        "after the picker click the route still resolves the model block's key, not the keyless placeholder"
    )
    on_disk = config._load_yaml_config_file(cfg_path)
    assert config._custom_provider_entry_identity(
        on_disk["custom_providers"][0],
        on_disk.get("custom_providers"),
        on_disk.get("providers"),
        on_disk.get("model"),
    ) == "custom:晨光鑫遇专用", "the entry stays catalogued after the click"


def test_set_default_model_keeps_the_base_url_for_an_endpointless_entry(monkeypatch, tmp_path):
    """The picker's write must not drop model.base_url for an endpoint-less entry (r6 MUST-FIX b)."""
    U = "http://127.0.0.1:8317/v1"
    cfg_path = _write_cfg(
        tmp_path,
        "model:\n"
        "  provider: custom\n"
        "  default: chat-model\n"
        f"  base_url: {U}\n"
        "  api_key: sk-model\n"
        "custom_providers:\n"
        "  - name: 晨光鑫遇专用\n"
        "    model: chat-model\n",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    _load(cfg_path)
    result = config.set_hermes_default_model("chat-model", provider="custom:晨光鑫遇专用")
    assert result["ok"] is True

    on_disk = config._load_yaml_config_file(cfg_path)
    assert on_disk["model"].get("base_url") == U, (
        "the endpoint-less entry inherits model.base_url, so the pop must be skipped"
    )
    assert config._custom_provider_entry_identity(
        on_disk["custom_providers"][0],
        on_disk.get("custom_providers"),
        on_disk.get("providers"),
        on_disk.get("model"),
    ) == "custom:晨光鑫遇专用", "the entry stays catalogued"


def test_set_default_model_drops_the_previous_routes_key_for_a_keyless_entry(monkeypatch, tmp_path):
    """Shape K: the model block's key must not follow the pick to another host (r9 MUST-FIX).

    The picker copies the selected entry's URL into the block, so AFTER the click the
    on-disk "same URL" test can no longer separate shape A (the block served that URL,
    correct) from shape K (the block served a different host, a leak). Only the
    pre-click snapshot can. Here the block served ``U0`` under its own key and the
    selected keyless entry lives at ``U``, so the key belongs to the previous route.
    The route must fail closed exactly as an ASCII keyless entry does, instead of
    sending ``sk-previous-route`` to ``U`` (master and ``f1c2afc`` send no key).
    """
    U0 = "http://127.0.0.1:8317/v1"
    U = "http://127.0.0.1:9000/v1"
    cfg_path = _write_cfg(
        tmp_path,
        "model:\n"
        "  provider: custom\n"
        "  default: old-model\n"
        f"  base_url: {U0}\n"
        "  api_key: sk-previous-route\n"
        "custom_providers:\n"
        "  - name: 晨光鑫遇专用\n"
        f"    base_url: {U}\n"
        "    model: chat-model\n",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    _load(cfg_path)
    result = config.set_hermes_default_model("chat-model", provider="custom:晨光鑫遇专用")
    assert result["ok"] is True

    assert config.resolve_custom_provider_connection("custom:晨光鑫遇专用") == (None, U), (
        "the previous route's key must not be sent to the newly selected entry's host"
    )
    on_disk = config._load_yaml_config_file(cfg_path)
    assert not on_disk["model"].get("api_key"), (
        "the stranded previous-route key must be absent from the block on disk"
    )
    assert not on_disk["model"].get("key_env"), "and so must any key_env form of it"


def test_set_default_model_drops_the_previous_routes_key_cmd_for_a_keyless_entry(monkeypatch, tmp_path):
    """The block's ``key_cmd``/``credential_pool`` must not follow the pick either.

    The cleanup that drops the previous route's credential covered only ``api_key``
    and ``key_env``, so a block whose credential was a ``key_cmd`` (a command that
    prints a fresh bearer) or a ``credential_pool`` still handed the old token to the
    newly selected entry's host. Every credential source the block can carry must be
    dropped together, so the route fails closed exactly as an ASCII keyless entry does.
    """
    U0 = "http://127.0.0.1:8317/v1"
    U = "http://127.0.0.1:9000/v1"
    cfg_path = _write_cfg(
        tmp_path,
        "model:\n"
        "  provider: custom\n"
        "  default: old-model\n"
        f"  base_url: {U0}\n"
        "  key_cmd: printf sk-old\n"
        "  credential_pool:\n"
        "    - name: poolA\n"
        "custom_providers:\n"
        "  - name: 晨光鑫遇专用\n"
        f"    base_url: {U}\n"
        "    model: chat-model\n",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    _load(cfg_path)
    result = config.set_hermes_default_model("chat-model", provider="custom:晨光鑫遇专用")
    assert result["ok"] is True

    on_disk = config._load_yaml_config_file(cfg_path)
    assert not on_disk["model"].get("key_cmd"), (
        "the previous route's key_cmd must not stay on the block for the new host"
    )
    assert not on_disk["model"].get("credential_pool"), (
        "and neither must its credential_pool"
    )
    assert config.resolve_custom_provider_connection("custom:晨光鑫遇专用") == (None, U), (
        "the route must fail closed exactly as an ASCII keyless entry does"
    )


def test_endpointless_entry_with_a_key_inherits_the_model_connection(monkeypatch, tmp_path):
    """An endpoint-less entry that declares a key still inherits the model connection.

    r9 CORE, ``api/config.py:4223``. Declaring ``api_key`` made the entry look like a
    real authority, so it skipped the inheritance and the newly named route had no
    endpoint at all: a turn that master completed through the model block failed with
    ``custom_provider_endpoint_unresolved``. Without an endpoint of its own the
    declared key has nowhere of its own to go, so the model block stays the authority.
    """
    U = "http://127.0.0.1:8317/v1"
    cfg_path = _write_cfg(
        tmp_path,
        "model:\n"
        "  provider: custom\n"
        "  default: chat-model\n"
        f"  base_url: {U}\n"
        "  api_key: sk-model\n"
        "custom_providers:\n"
        "  - name: 晨光鑫遇专用\n"
        "    model: chat-model\n"
        "    api_key: sk-entry\n",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    _load(cfg_path)
    _entry, source, _is_exact, _status = config._select_custom_provider_record(
        "custom:晨光鑫遇专用", "晨光鑫遇专用", config.cfg
    )
    assert source == "model", (
        "an endpoint-less entry inherits the model block even when it declares its own key, "
        f"so the route has an endpoint at all (got source={source!r})"
    )

    connection = config.resolve_custom_provider_connection("custom:晨光鑫遇专用")
    assert connection[1] == U, f"the endpoint-less entry must inherit the model endpoint: {connection}"


# ---------------------------------------------------------------------------
# Round eleven: two accounts at one endpoint, and the picker's rewrite must not
# re-open the shared convention variable (#8026 r11)
# ---------------------------------------------------------------------------


def test_a_model_block_with_its_own_key_is_not_demoted_by_a_keyed_same_endpoint_entry(monkeypatch):
    """URL equality is not proof the block was saved from the entry (#8026 r11).

    Greptile's finding: with ``model.provider: custom:晨光`` carrying its OWN key A
    and a list entry named ``晨光`` carrying a different key B at the SAME endpoint,
    the mirror test demoted the block (matching URLs) and the newly admitted entry
    won, so requests went out with key B instead of A and could change accounts.
    The ambiguity only exists when BOTH sides carry a credential; when either side
    is credentialless there is no second account to confuse, so URL equality stays
    sufficient evidence (see the keyless-mirror case).
    """
    u = "http://shared-endpoint.example/v1"
    cfg_shape = {
        "model": {
            "provider": "custom:晨光",
            "default": "chat-model",
            "base_url": u,
            "api_key": "sk-model",
        },
        "custom_providers": [{"name": "晨光", "base_url": u, "api_key": "sk-list"}],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    assert "晨光" in config._custom_provider_identity_owners(
        cfg_shape["custom_providers"], None, cfg_shape["model"]
    ), "a model block with its own key still owns the slug when the entry is keyed too"

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert (api_key, base_url) == ("sk-model", u), (
        "the existing model connection keeps its own key when two accounts share an endpoint"
    )


def test_a_model_block_serving_a_keyless_fallback_entry_does_not_read_the_shared_key(monkeypatch):
    """The picker's rewrite must not re-open the shared variable (#8026 r11).

    Greptile's finding: after selecting a keyless non-ASCII entry the picker writes
    the entry's id and URL into ``model``; ``_select_custom_provider_record`` then
    returns that block with source ``"model"``, and the convention-key gate permitted
    the shared ``CUSTOM_CUSTOM_API_KEY`` because the source was not
    ``custom_providers``. A variable belonging to another provider then travelled to
    the newly selected endpoint. The block is that entry's route, so the fallback
    restriction applies to the model source too and the route fails closed.
    """
    monkeypatch.setenv("CUSTOM_CUSTOM_API_KEY", "sk-SHARED")
    u = "http://new-endpoint.example/v1"
    cfg_shape = {
        "model": {"provider": "custom:晨光", "default": "chat-model", "base_url": u},
        "custom_providers": [{"name": "晨光", "base_url": u, "model": "chat-model"}],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    api_key, base_url = config.resolve_custom_provider_connection("custom:晨光")
    assert base_url == u
    assert api_key is None, (
        "a fallback route served by the model block must not read the shared convention key"
    )


def test_set_default_model_drops_the_previous_routes_key_when_the_endpoint_is_replaced(
    monkeypatch, tmp_path
):
    """Shape K with a CREDENTIALLED entry: the block's key must not follow the pick (r12).

    Greptile's finding on ``68fd668c``: the r11 rule ("two credentialed authorities at
    one URL are two accounts") let the picker-written copy of a credentialed entry pass
    for an independent connection. Select a keyed entry at ``U`` while the block serves
    ``U0`` under its own key, and the block is rewritten to ``U`` with that key still on
    it — so the entry is shadowed and ``U`` receives the key of a host the block just
    left. The endpoint the block carried BEFORE the click is the only fact that separates
    the copy from an independent route at the entry's own URL, and only the save path
    has it.
    """
    U0 = "http://127.0.0.1:8317/v1"
    U = "http://127.0.0.1:9000/v1"
    cfg_path = _write_cfg(
        tmp_path,
        "model:\n"
        "  provider: custom\n"
        "  default: old-model\n"
        f"  base_url: {U0}\n"
        "  api_key: sk-A\n"
        "custom_providers:\n"
        "  - name: 晨光鑫遇专用\n"
        f"    base_url: {U}\n"
        "    api_key: sk-B\n"
        "    model: chat-model\n",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    _load(cfg_path)
    result = config.set_hermes_default_model("chat-model", provider="custom:晨光鑫遇专用")
    assert result["ok"] is True

    on_disk = config._load_yaml_config_file(cfg_path)
    assert not on_disk["model"].get("api_key"), (
        "a key minted for the endpoint this click replaced must not stay on the block"
    )
    assert config._custom_provider_entry_identity(
        on_disk["custom_providers"][0],
        on_disk.get("custom_providers"),
        on_disk.get("providers"),
        on_disk.get("model"),
    ) == "custom:晨光鑫遇专用", "the selected entry must not be shadowed by its own copy"
    assert config.resolve_custom_provider_connection("custom:晨光鑫遇专用") == ("sk-B", U), (
        "the entry's own key must serve the entry's own endpoint"
    )


def test_an_entry_whose_only_credential_is_a_key_cmd_serves_instead_of_the_block(monkeypatch):
    """A declared ``key_cmd`` is a credential: the block must not take the route over.

    Senior review, SHOULD-FIX. The preference test in ``_select_custom_provider_record``
    and the save-path helper both looked only at ``api_key``/``key_env``, so a non-ASCII
    entry whose only credential was a ``key_cmd`` (or a pool) was judged keyless, the
    ``model:`` block was returned as its connection, and the WebUI's keyless bundle
    overrode the Agent-minted token. The ASCII control with the identical shape has always
    been served by the entry's key.
    """
    u = "http://new-endpoint.example/v1"
    cfg_shape = {
        "model": {"provider": "custom", "default": "old-model", "base_url": u, "api_key": "sk-A"},
        "custom_providers": [
            {"name": "晨光鑫遇专用", "base_url": u, "key_cmd": "printf sk-entry-cmd",
             "model": "chat-model"}
        ],
    }
    monkeypatch.setattr(config, "cfg", dict(cfg_shape))
    monkeypatch.setattr(config, "get_config", lambda: dict(cfg_shape))

    record, source, _, _ = config._select_custom_provider_record(
        "custom:晨光鑫遇专用", "晨光鑫遇专用", cfg_shape
    )
    assert source == "custom_providers", (
        "an entry that declares a key_cmd is a real authority, not a keyless placeholder"
    )
    assert record.get("key_cmd") == "printf sk-entry-cmd"


def test_a_key_cmd_entry_is_not_judged_keyless(monkeypatch):
    """The save-path helper must read every credential source, not just the static two.

    Senior review, SHOULD-FIX, second site: ``_selected_fallback_entry_declares_no_credential``
    decided the previous route's credentials could be dropped from the block on a test
    that only saw ``api_key``/``key_env``. A ``key_cmd``/``credential_pool`` entry read as
    keyless there, so the pick was treated as a keyless switch. Both neighbours are
    asserted: a truly keyless entry keeps the keyless path.
    """
    u = "http://new-endpoint.example/v1"
    cfg_shape = {
        "model": {"provider": "custom", "default": "old-model", "base_url": u},
        "custom_providers": [
            {"name": "晨光鑫遇专用", "base_url": u, "key_cmd": "printf sk-entry-cmd"},
            {"name": "晨光", "base_url": u},
        ],
    }
    assert config._selected_fallback_entry_declares_no_credential(
        "custom:晨光鑫遇专用", cfg_shape
    ) is False, "a key_cmd entry declares a credential"
    assert config._selected_fallback_entry_declares_no_credential("custom:晨光", cfg_shape) is True, (
        "a genuinely keyless entry still takes the keyless path"
    )

