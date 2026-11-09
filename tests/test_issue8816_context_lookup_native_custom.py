"""#8816: the context-length lookup must not hand a bare built-in session a native custom twin's key.

With both ``providers.openai`` and ``providers['custom:openai']`` configured, the
lookup for a bare ``openai`` session selected the native custom record by slug.
The metadata lookup then sent the CUSTOM key to the BUILT-IN endpoint, and the
session borrowed the custom record's context limit. v0.52.544 used the built-in
record; exp-v0.52.597 (#8448) introduced the swap, and #8685's fix at
exp-v0.52.606 covered routing but not this lookup.

The rule (as in ``api.config._named_custom_provider_slug_for_provider``): a bare
built-in or plugin id is claimed only by a legacy ``custom_providers`` entry of
that name; a native ``custom:<id>`` record needs the explicit ``custom:<id>``.

#8819 restores v0.52.544's lookup selection (its key and URL choice) and keeps
that routing rule; these tests pin both.
"""

import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from api import config, routes
from tests.conftest import requires_agent


def _config(pid, builtin_url="https://builtin.invalid/v1"):
    return {
        "model": {"provider": pid, "default": "stable-model"},
        "providers": {
            pid: {"base_url": builtin_url, "api_key": "synthetic-builtin"},
            "custom:" + pid: {
                "base_url": "https://named.invalid/v1",
                "api_key": "synthetic-named",
                "models": {"stable-model": {"context_length": 12345}},
            },
        },
    }


def _use(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)


@pytest.mark.parametrize("pid", ["openai", "anthropic", "gemini"])
def test_a_bare_builtin_id_does_not_take_the_native_custom_record(monkeypatch, pid):
    data = _config(pid)
    _use(monkeypatch, data)

    builtin = routes._context_length_lookup_inputs_for_model("stable-model", pid, cfg=data)
    assert builtin.provider == pid
    assert builtin.base_url == "https://builtin.invalid/v1"
    assert builtin.api_key == "synthetic-builtin"
    assert builtin.config_context_length is None

    explicit = routes._context_length_lookup_inputs_for_model("stable-model", "custom:" + pid, cfg=data)
    assert explicit.base_url == "https://named.invalid/v1"
    assert explicit.api_key == "synthetic-named"
    assert explicit.config_context_length == 12345


def _xai_config(active):
    return {
        "model": {"provider": active, "default": "grok-x"},
        "providers": {
            "x-ai": {"base_url": "https://builtin.invalid/v1", "api_key": "synthetic-builtin"},
            "custom:xai": {
                "base_url": "https://named.invalid/v1",
                "api_key": "synthetic-named",
                "models": {"grok-x": {"context_length": 12345}},
            },
        },
    }


@pytest.mark.parametrize("active", ["x-ai", "xai", "grok"])
def test_an_xai_alias_stays_builtin_without_plugin_discovery(monkeypatch, active):
    """With the plugin registry empty, the Agent's canonical ``xai`` is not in WebUI's
    static tables (they say ``x-ai``); it must still count as a built-in, for the
    context lookup and for routing, as on v0.52.544."""
    from api import plugin_providers

    monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    data = _xai_config(active)
    _use(monkeypatch, data)

    bare = routes._context_length_lookup_inputs_for_model("grok-x", active, cfg=data)
    assert (bare.api_key, bare.config_context_length) == ("synthetic-builtin", None)
    assert config._named_custom_provider_slug_for_provider(active, data) == ""
    _model, provider, base_url = config.resolve_model_provider("grok-x")
    assert not str(provider).startswith("custom:"), (provider, base_url)
    assert base_url != "https://named.invalid/v1"

    explicit = routes._context_length_lookup_inputs_for_model("grok-x", "custom:xai", cfg=data)
    assert (explicit.api_key, explicit.config_context_length) == ("synthetic-named", 12345)


def _family_config(active, record, builtin="fireworks"):
    return {
        "model": {"provider": active, "default": "m-x"},
        "providers": {
            builtin: {"base_url": "https://builtin.invalid/v1", "api_key": "synthetic-builtin"},
            "custom:" + record: {
                "base_url": "https://named.invalid/v1",
                "api_key": "synthetic-named",
                "models": {"m-x": {"context_length": 12345}},
            },
        },
    }


@pytest.mark.parametrize("discovery", ["as-is", "empty"])
@pytest.mark.parametrize(
    "active,record",
    [("fireworks", "fireworks"), ("Fireworks", "fireworks"), ("fw", "fw"), ("fw", "fireworks"),
     ("fireworks-ai", "fireworks-ai"), ("fireworks_ai", "fireworks_ai")],
)
def test_a_fireworks_session_borrows_no_native_record_in_the_lookup(monkeypatch, discovery, active, record):
    """The lookup half of the catalog test below, with or without an Agent: the bare session takes
    neither the native record's key nor its limit, as on v0.52.544; the explicit id takes both."""
    from api import plugin_providers

    if discovery == "empty":
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    data = _family_config(active, record)
    _use(monkeypatch, data)

    bare = routes._context_length_lookup_inputs_for_model("m-x", active, cfg=data)
    assert bare.api_key != "synthetic-named"
    assert bare.config_context_length != 12345
    explicit = routes._context_length_lookup_inputs_for_model("m-x", "custom:" + record, cfg=data)
    assert (explicit.api_key, explicit.config_context_length) == ("synthetic-named", 12345)


# Needs the Agent: Fireworks is a built-in only through the Agent's catalog (CI installs no Agent).
@requires_agent
@pytest.mark.parametrize("discovery", ["as-is", "empty"])
@pytest.mark.parametrize(
    "active,record",
    [
        ("fireworks", "fireworks"),
        ("Fireworks", "fireworks"),
        ("fw", "fw"),
        ("fw", "fireworks"),
        ("fireworks-ai", "fireworks-ai"),
        ("fireworks_ai", "fireworks_ai"),
    ],
)
def test_an_agent_catalog_builtin_stays_builtin_in_both_discovery_states(monkeypatch, discovery, active, record):
    """Fireworks is outside WebUI's static tables; the Agent's provider catalog and
    alias table still make every bare spelling a built-in, as on v0.52.544."""
    from api import plugin_providers

    if discovery == "empty":
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    data = _family_config(active, record)
    _use(monkeypatch, data)

    assert config._is_bare_builtin_provider_id(active)
    bare = routes._context_length_lookup_inputs_for_model("m-x", active, cfg=data)
    assert bare.api_key != "synthetic-named"
    assert bare.config_context_length != 12345
    assert config._named_custom_provider_slug_for_provider(active, data) == ""
    _model, provider, base_url = config.resolve_model_provider("m-x")
    assert not str(provider).startswith("custom:"), (provider, base_url)
    assert base_url != "https://named.invalid/v1"

    explicit = routes._context_length_lookup_inputs_for_model("m-x", "custom:" + record, cfg=data)
    assert (explicit.api_key, explicit.config_context_length) == ("synthetic-named", 12345)


def test_an_agent_without_the_provider_catalog_is_tolerated(monkeypatch):
    """An Agent whose ``hermes_cli.models`` lacks ``CANONICAL_PROVIDERS`` gives no catalog
    ids rather than an error; WebUI's own tables still classify the built-ins they know."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "hermes_cli.models", types.ModuleType("hermes_cli.models"))
    assert config._agent_builtin_provider_ids() == ()
    assert config._is_bare_builtin_provider_id("openai")
    assert not config._is_bare_builtin_provider_id("my-proxy")
    assert not config._is_bare_builtin_provider_id("custom:openai")


class _RaisingSlug:
    @property
    def slug(self):
        raise RuntimeError("catalog entry from a future Agent")


@pytest.mark.parametrize(
    "catalog,expected",
    [
        (5, ()),
        ([_RaisingSlug()], ()),
        (["Fireworks", "", None, ("xai", "xAI", "")], ("fireworks", "xai")),
        ([{"slug": "fireworks"}], ()),
    ],
)
def test_an_unexpected_catalog_shape_never_raises_into_routing(monkeypatch, catalog, expected):
    import sys
    import types

    module = types.ModuleType("hermes_cli.models")
    module.CANONICAL_PROVIDERS = catalog
    monkeypatch.setitem(sys.modules, "hermes_cli.models", module)
    assert config._agent_builtin_provider_ids() == expected
    assert config._is_bare_builtin_provider_id("openai")
    assert not config._is_bare_builtin_provider_id("my-proxy")


@pytest.mark.parametrize("discovery", ["as-is", "empty"])
@pytest.mark.parametrize("active", ["vllm", "local"])
@pytest.mark.parametrize("shape", ["bare-native-key", "custom-key"])
def test_a_generic_custom_alias_keeps_its_own_record(monkeypatch, discovery, active, shape):
    """``vllm``/``local`` alias the generic ``custom`` placeholder in the Agent's table, not
    a built-in: with its own record, the bare id keeps .630's route to that record.
    (``ollama`` is ``custom`` in WebUI's own alias table too, so .630 never routed it to a
    native record; the fold leaves that unchanged.)"""
    from api import plugin_providers

    if discovery == "empty":
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    key = active if shape == "bare-native-key" else "custom:" + active
    data = {
        "model": {"provider": active, "default": "m-x"},
        "providers": {key: {"base_url": "https://named.invalid/v1", "api_key": "synthetic-named", "models": {"m-x": {}}}},
    }
    _use(monkeypatch, data)

    assert not config._is_bare_builtin_provider_id(active)
    assert config._named_custom_provider_slug_for_provider(active, data) == "custom:" + active
    _model, provider, base_url = config.resolve_model_provider("m-x")
    assert (provider, base_url) == ("custom:" + active, "https://named.invalid/v1")


_KEY_OWNER = {"synthetic-A": "https://a.invalid/v1", "synthetic-B": "https://b.invalid/v1"}


@pytest.mark.parametrize("discovery", ["as-is", "empty"])
@pytest.mark.parametrize(
    "pid", ["openai", "anthropic", "fireworks", "fw", "deepseek-chat", "azure", "or", "xai", "x-ai", "my-proxy", "vllm"],
)
def test_a_key_only_goes_to_its_own_records_url(monkeypatch, discovery, pid):
    """The #8816 security property, whatever the id's spelling: with ``providers.<id>`` at
    URL A and ``providers['custom:<id>']`` at URL B, the lookup never pairs one record's key
    with the other record's URL (``deepseek-chat``/``or`` are listed by no WebUI table, Agent
    catalog or alias table)."""
    from api import plugin_providers

    if discovery == "empty":
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    data = {
        "model": {"provider": pid, "default": "m-x"},
        "providers": {
            pid: {"base_url": _KEY_OWNER["synthetic-A"], "api_key": "synthetic-A"},
            "custom:" + pid: {"base_url": _KEY_OWNER["synthetic-B"], "api_key": "synthetic-B",
                              "models": {"m-x": {"context_length": 12345}}},
        },
    }
    _use(monkeypatch, data)
    for provider in (pid, "custom:" + pid):
        got = routes._context_length_lookup_inputs_for_model("m-x", provider, cfg=data)
        if got.api_key in _KEY_OWNER:
            assert got.base_url.rstrip("/") == _KEY_OWNER[got.api_key], (provider, got.base_url, got.api_key)
    explicit = routes._context_length_lookup_inputs_for_model("m-x", "custom:" + pid, cfg=data)
    assert (explicit.base_url, explicit.api_key) == (_KEY_OWNER["synthetic-B"], "synthetic-B")


def test_a_bare_custom_id_with_both_records_pairs_each_key_with_its_own_url(monkeypatch):
    """``providers['my-proxy']`` and ``providers['custom:my-proxy']`` both configured: the bare
    session talks to the bare record's URL with its key; the explicit id to the custom record."""
    data = {
        "model": {"provider": "my-proxy", "default": "m-x"},
        "providers": {
            "my-proxy": {"base_url": _KEY_OWNER["synthetic-A"], "api_key": "synthetic-A"},
            "custom:my-proxy": {"base_url": _KEY_OWNER["synthetic-B"], "api_key": "synthetic-B"},
        },
    }
    _use(monkeypatch, data)
    bare = routes._context_length_lookup_inputs_for_model("m-x", "my-proxy", cfg=data)
    assert (bare.base_url, bare.api_key) == (_KEY_OWNER["synthetic-A"], "synthetic-A")
    explicit = routes._context_length_lookup_inputs_for_model("m-x", "custom:my-proxy", cfg=data)
    assert (explicit.base_url, explicit.api_key) == (_KEY_OWNER["synthetic-B"], "synthetic-B")


@pytest.mark.parametrize("discovery", ["as-is", "empty"])
@pytest.mark.parametrize("pid", ["fireworks", "fw", "xai"])
def test_a_builtins_own_record_keeps_its_mirror_url(monkeypatch, discovery, pid):
    """``providers.<built-in>`` with its own base_url (a user mirror) routes there, as on
    v0.52.544 and .630, in both discovery states."""
    from api import plugin_providers

    if discovery == "empty":
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    data = {
        "model": {"provider": pid, "default": "m-x"},
        "providers": {pid: {"base_url": "https://mirror.invalid/v1", "api_key": "synthetic-mirror", "models": ["m-x"]}},
    }
    _use(monkeypatch, data)
    _model, _provider, base_url = config.resolve_model_provider("m-x")
    assert base_url == "https://mirror.invalid/v1"


@contextlib.contextmanager
def _loopback_server(monkeypatch):
    """A loopback endpoint that records ``Authorization`` per request; other hosts are refused."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            body = b'{"data":[{"id":"loopback-model","context_length":65536}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 (BaseHTTPRequestHandler's signature)
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_connect = socket.socket.connect

    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise OSError("this test permits loopback only")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# Needs the Agent: the request is the Agent's own metadata resolver's (CI installs no Agent).
@requires_agent
@pytest.mark.parametrize("pid", ["openai", "deepseek-chat", "azure", "or"])
def test_the_builtin_endpoint_never_receives_the_custom_key(monkeypatch, pid):
    """The real metadata lookup, against a loopback built-in endpoint that records Authorization.
    ``deepseek-chat``/``azure``/``or`` are listed by no WebUI table, Agent catalog or alias table."""
    with _loopback_server(monkeypatch) as (origin, seen):
        builtin_url = f"{origin}/v1/{pid}"
        data = _config(pid, builtin_url)
        data["model"]["default"] = "loopback-model"
        _use(monkeypatch, data)
        routes._resolve_context_length_for_session_model("loopback-model", pid, base_url=builtin_url)

    assert seen, "the lookup made no request to the built-in endpoint"
    assert all(header == "Bearer synthetic-builtin" for header in seen), json.dumps(seen)


@pytest.mark.parametrize("discovery", ["as-is", "empty"])
@pytest.mark.parametrize("pid", ["or", "azure", "deepseek-chat", "my-proxy", "llamacpp"])
def test_a_url_only_record_never_receives_its_key_only_twins_key(monkeypatch, discovery, pid):
    """``providers.<id>`` with only a key and ``providers['custom:<id>']`` with only a URL: the
    selected record owns the URL, so another record's key never travels to it (#8819 senior round 5;
    v0.52.544 never sent it, exp-v0.52.597..630 did)."""
    from api import plugin_providers

    if discovery == "empty":
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {})
    with _loopback_server(monkeypatch) as (origin, seen):
        twin_url = f"{origin}/v1/custom-{pid}"
        data = {
            "model": {"provider": pid, "default": "loopback-model"},
            "providers": {pid: {"api_key": "synthetic-K"}, "custom:" + pid: {"base_url": twin_url}},
        }
        _use(monkeypatch, data)
        got = routes._context_length_lookup_inputs_for_model("loopback-model", pid, cfg=data)
        routes._resolve_context_length_for_session_model("loopback-model", pid)

    if (got.base_url or "").rstrip("/") == twin_url:
        assert got.api_key != "synthetic-K"
    assert "Bearer synthetic-K" not in seen, json.dumps(seen)


def test_resolve_model_provider_cost_does_not_grow_with_unrelated_native_records(monkeypatch):
    """Bound for #8819 senior round 5: the built-in classification runs only for a record that
    lists the model, so native records for other models add no classification calls."""
    calls = []
    real = config._is_bare_builtin_provider_id

    def counting(slug, *args, **kwargs):
        calls.append(slug)
        return real(slug, *args, **kwargs)

    monkeypatch.setattr(config, "_is_bare_builtin_provider_id", counting)
    counts = {}
    for n in (10, 40):
        providers = {
            f"custom:proxy-{i}": {"base_url": f"https://p{i}.invalid/v1", "api_key": f"k{i}", "models": [f"m-{i}"]}
            for i in range(n)
        }
        providers.update({f"proxy-alias-{i}": {"base_url": f"https://q{i}.invalid/v1", "models": [f"q-{i}"]} for i in range(n)})
        data = {"model": {"provider": "openai", "default": "gpt-x"}, "providers": providers}
        _use(monkeypatch, data)
        del calls[:]
        config.resolve_model_provider("unknown-model-xyz")
        counts[n] = len(calls)
    assert counts[10] == counts[40], counts
    assert not any(slug.startswith(("custom:proxy-", "proxy-alias-")) for slug in calls), calls


# --- shared display names and the streaming lookups (#8816 round 7) ---------------


@pytest.mark.parametrize("order", ["native-last", "native-first"])
def test_shared_display_name_lookup_takes_the_url_owners_key(monkeypatch, order):
    """Two records share the display name ``foo``. Given the native record's URL and no
    caller key, the lookup pairs that URL with the native record's own key, whichever
    record is listed first."""
    records = [
        ("myproxy", {"name": "foo", "base_url": "https://b.invalid/v1", "api_key": "synthetic-B"}),
        ("custom:foo", {"name": "foo", "base_url": "https://a.invalid/v1", "api_key": "synthetic-A"}),
    ]
    if order == "native-first":
        records.reverse()
    data = {"model": {"provider": "custom:foo", "default": "loopback-model"}, "providers": dict(records)}
    _use(monkeypatch, data)

    got = routes._context_length_lookup_inputs_for_model(
        "loopback-model", "custom:foo", base_url="https://a.invalid/v1", cfg=data
    )
    assert got.base_url == "https://a.invalid/v1"
    assert got.api_key == "synthetic-A"


@pytest.mark.parametrize(
    "session, model_provider, native, legacy",
    [
        ("custom:foo-bar", "custom:foo_bar", "custom:foo-bar", "foo-bar"),
        ("custom:foo_bar", "custom:foo-bar", "custom:foo_bar", "foo_bar"),
    ],
)
def test_an_underscore_spelling_of_the_session_id_still_owns_the_model_url(
    monkeypatch, session, model_provider, native, legacy
):
    """v0.52.544 compared ``custom:foo_bar`` and ``custom:foo-bar`` as one id when
    deciding whether the model block owns the URL slot. Since 091c26592 the
    canonicaliser keeps underscores, and without a local fold the native record's
    URL filled the slot and the legacy entry's key went to it: a pairing neither
    v0.52.544 nor exp-v0.52.650 makes (#8819 Codex round 10). The model URL is
    544's choice; which key goes with it is #8822's question, not pinned here."""
    model_url, native_url = "https://model.invalid/v1", "https://native.invalid/v1"
    data = {
        "model": {"provider": model_provider, "base_url": model_url},
        "providers": {native: {"base_url": native_url, "api_key": "synthetic-native"}},
        "custom_providers": [
            {"name": legacy, "base_url": "https://legacy.invalid/v1", "api_key": "synthetic-legacy"}
        ],
    }
    _use(monkeypatch, data)

    got = routes._context_length_lookup_inputs_for_model("loopback-model", session, cfg=data)
    assert (got.base_url, got.api_key) != (native_url, "synthetic-legacy")
    assert got.base_url == model_url


def test_streaming_context_lookups_pass_no_caller_key():
    """The two direct streaming calls to the lookup pass the Agent's URL but never its key as the caller's.

    The runtime connection bundle can already pair one record's URL with another
    record's key (a shared-name credential pool). Passed in as the caller's key, it
    would override the lookup's own, correct choice; .644 kept 8,192 there, and
    passing the key fell back to 256,000 (#8816 round 7). Without a caller key the
    lookup takes the key of whichever config source owns the URL.

    Since #8822 the Agent's key reaches these lookups only as ``fallback_api_key``,
    which the lookup uses when no config source owns the URL, and only under the
    same ownership rule (``test_issue8822_context_lookup_key_host``).

    Three more streaming sites reach the lookup under an import alias and pass the
    runtime key as ``api_key=``, as v0.52.544 does; that pairing is #8822's, and this
    test covers only the direct calls.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "_context_length_lookup_inputs_for_model"
    ]
    assert len(calls) >= 2
    for call in calls:
        assert "api_key" not in {kw.arg for kw in call.keywords}, ast.unparse(call)


def test_builtin_forms_are_built_once_and_rebuilt_when_an_alias_table_changes(monkeypatch):
    """``_is_bare_builtin_provider_id`` reuses the cached built-in spellings (round 7:
    rebuilding them per call made resolve_model_provider ~6x slower), and a change to
    an alias table rebuilds them, so the answer never goes stale."""
    calls = []
    real = config._provider_id_forms

    def counting(value):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(config, "_provider_id_forms", counting)
    monkeypatch.setattr(config, "_KNOWN_BUILTIN_FORMS_CACHE", None)
    monkeypatch.setattr(config, "_is_native_custom_provider_key", lambda raw: True)
    monkeypatch.setattr(config, "_is_plugin_model_provider", lambda raw: False)

    assert config._is_bare_builtin_provider_id("proxy-zz") is False
    first = len(calls)
    assert first > 1  # the target's own forms plus every known id's
    assert config._is_bare_builtin_provider_id("proxy-zz") is False
    assert len(calls) - first == 1  # only the target's forms: the known set came from the cache

    monkeypatch.setitem(config._PROVIDER_ALIASES, "proxy-zz", "openai")
    assert config._is_bare_builtin_provider_id("proxy-zz") is True
    assert len(calls) - first > 2  # the alias table changed, so the known set was rebuilt


def test_builtin_forms_are_rebuilt_when_the_agent_catalog_gains_an_entry(monkeypatch):
    """Plugin admission inserts entries into the Agent's ``CANONICAL_PROVIDERS`` at runtime;
    the cached built-in spellings must pick a new entry up on the next call."""
    models = pytest.importorskip("hermes_cli.models")
    if not isinstance(getattr(models, "CANONICAL_PROVIDERS", None), list):
        pytest.skip("this Agent has no list-shaped provider catalog")
    monkeypatch.setattr(config, "_KNOWN_BUILTIN_FORMS_CACHE", None)
    monkeypatch.setattr(config, "_is_native_custom_provider_key", lambda raw: True)
    monkeypatch.setattr(config, "_is_plugin_model_provider", lambda raw: False)

    assert config._is_bare_builtin_provider_id("zz-catalog-provider") is False
    entry = type(models.CANONICAL_PROVIDERS[0])("zz-catalog-provider", "ZZ", "ZZ (direct API)")
    monkeypatch.setattr(models, "CANONICAL_PROVIDERS", [*models.CANONICAL_PROVIDERS, entry])
    assert config._is_bare_builtin_provider_id("zz-catalog-provider") is True


# --- round 8 (senior) ---------------------------------------------------------------

_GW = "https://gateway.invalid/v1"


@pytest.mark.parametrize("caller", [{}, {"base_url": _GW}], ids=["no-caller-url", "caller-url-is-model-url"])
@pytest.mark.parametrize("session", ["openai-codex", "deepseek", "custom:other"])
def test_a_model_block_without_a_provider_lends_its_key_to_no_other_provider(monkeypatch, caller, session):
    """The profile-setup path writes ``model.base_url``/``api_key`` with no provider. A
    session on another provider gets no key from it, as on v0.52.544: the Agent picks
    the metadata host by provider for some ids (v2026.9.24 sends an ``openai-codex``
    lookup's key to chatgpt.com, whatever the URL)."""
    data = {"model": {"base_url": _GW, "api_key": "synthetic-model"}}
    _use(monkeypatch, data)

    got = routes._context_length_lookup_inputs_for_model("probe-model", session, cfg=data, **caller)
    assert got.api_key == ""


@pytest.mark.parametrize("caller", [{}, {"base_url": _GW}], ids=["no-caller-url", "caller-url-is-model-url"])
@pytest.mark.parametrize("pid", ["openai-codex", "custom:my-proxy"])
def test_a_model_block_that_declares_the_provider_keeps_its_key(monkeypatch, caller, pid):
    """When the model block declares the session's provider, its key goes with its own
    ``base_url``, including when the caller passes that URL (the positive twin of
    test_issue3256's runtime-URL case)."""
    data = {"model": {"provider": pid, "base_url": _GW, "api_key": "synthetic-model"}}
    _use(monkeypatch, data)

    got = routes._context_length_lookup_inputs_for_model("probe-model", pid, cfg=data, **caller)
    assert (got.base_url, got.api_key) == (_GW, "synthetic-model")


def test_a_session_without_a_provider_still_takes_the_model_blocks_key(monkeypatch):
    """No session provider: the model block's key goes with its own URL, as on v0.52.544."""
    data = {"model": {"base_url": _GW, "api_key": "synthetic-model"}}
    _use(monkeypatch, data)

    got = routes._context_length_lookup_inputs_for_model("probe-model", "", cfg=data, base_url=_GW)
    assert (got.base_url, got.api_key) == (_GW, "synthetic-model")
