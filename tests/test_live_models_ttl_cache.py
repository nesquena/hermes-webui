"""Regression tests for /api/models/live backend TTL caching."""

import ast
import http.client
import io
import json
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

REPO = Path(__file__).resolve().parent.parent


def _install_provider_model_ids(monkeypatch, fn):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _patch_live_models_basics(monkeypatch, routes, profile="default"):
    import api.config as config
    import api.profiles as profiles

    routes._clear_live_models_cache()
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(config, "get_config", lambda: {"model": {"provider": "openai"}})
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: profile)


def test_live_models_cache_hits_within_ttl(monkeypatch):
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return ["openai/gpt-test"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    second = routes._handle_live_models(object(), parsed)

    assert calls == ["openai"]
    assert first == second
    assert first["models"] == [{"id": "openai/gpt-test", "label": "GPT Test", "supports_fast_tier": True}]


def test_live_models_cache_expires(monkeypatch):
    import api.routes as routes

    now = [1000.0]
    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    now[0] += routes._LIVE_MODELS_CACHE_TTL + 1
    second = routes._handle_live_models(object(), parsed)

    assert calls == ["openai", "openai"]
    assert first["models"][0]["id"] == "openai/model-1"
    assert second["models"][0]["id"] == "openai/model-2"


def test_live_models_cache_is_profile_scoped(monkeypatch):
    import api.routes as routes
    import api.profiles as profiles

    active_profile = ["default"]
    calls = []

    def provider_model_ids(provider):
        calls.append((active_profile[0], provider))
        return [f"{provider}/{active_profile[0]}-model"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_profile[0])

    parsed = urlparse("/api/models/live?provider=openai")
    default_payload = routes._handle_live_models(object(), parsed)
    active_profile[0] = "research"
    research_payload = routes._handle_live_models(object(), parsed)
    again_payload = routes._handle_live_models(object(), parsed)

    assert calls == [("default", "openai"), ("research", "openai")]
    assert default_payload["models"][0]["id"] == "openai/default-model"
    assert research_payload["models"][0]["id"] == "openai/research-model"
    assert again_payload == research_payload


def test_live_models_cache_returns_deep_copies(monkeypatch):
    import api.routes as routes

    _install_provider_model_ids(monkeypatch, lambda provider: ["openai/gpt-test"])
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    first["models"].clear()
    first["provider"] = "mutated"

    second = routes._handle_live_models(object(), parsed)

    assert second["provider"] == "openai"
    assert second["models"] == [{"id": "openai/gpt-test", "label": "GPT Test", "supports_fast_tier": True}]


def test_live_models_endpoint_respects_picker_visibility_budget(monkeypatch):
    import api.config as config
    import api.routes as routes

    _install_provider_model_ids(
        monkeypatch,
        lambda provider: [f"{provider}/model-{idx}" for idx in range(40)],
    )
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    payload = routes._handle_live_models(object(), parsed)

    assert payload["count"] == config._MODEL_PICKER_VISIBLE_TARGET
    assert [m["id"] for m in payload["models"]] == [
        f"openai/model-{idx}" for idx in range(config._MODEL_PICKER_VISIBLE_TARGET)
    ]


# ── #6919: POST /api/models/refresh must invalidate the live-models cache ────

def _post_handler(body: dict):
    raw = json.dumps(body).encode()
    handler = SimpleNamespace(
        command="POST",
        rfile=io.BytesIO(raw),
        wfile=io.BytesIO(),
        headers=http.client.HTTPMessage(),
        client_address=("127.0.0.1", 12345),
        status=None,
    )
    handler.headers["Content-Length"] = str(len(raw))
    return handler


def _refresh(provider: str) -> None:
    import api.routes as routes

    routes.handle_post(_post_handler({"provider": provider}), SimpleNamespace(path="/api/models/refresh"))


def test_models_refresh_evicts_live_models_cache(monkeypatch):
    """#6919: after POST /api/models/refresh reports success, the next
    /api/models/live request must refetch — not serve the pre-refresh catalog
    from _LIVE_MODELS_CACHE."""
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    assert first["models"][0]["id"] == "openai/model-1"  # cache primed

    _refresh("openai")

    second = routes._handle_live_models(object(), parsed)
    assert second["models"][0]["id"] == "openai/model-2", (
        "#6919: refresh reported success but /api/models/live still served the "
        "pre-refresh cached catalog"
    )
    assert calls == ["openai", "openai"]  # the second request really refetched


def test_models_refresh_evicts_only_requested_provider(monkeypatch):
    """Provider-scoped invalidation: refreshing one provider must not evict
    another provider's live-model entry."""
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len([c for c in calls if c == provider])}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed_a = urlparse("/api/models/live?provider=openai")
    parsed_b = urlparse("/api/models/live?provider=custom")
    routes._handle_live_models(object(), parsed_a)  # prime openai
    routes._handle_live_models(object(), parsed_b)  # prime custom
    assert calls == ["openai", "custom"]

    _refresh("openai")

    routes._handle_live_models(object(), parsed_a)  # openai refetches
    routes._handle_live_models(object(), parsed_b)  # custom must stay cached
    assert calls == ["openai", "custom", "openai"], (
        "#6919: refreshing openai also evicted custom's live-model entry"
    )


def test_models_refresh_evicts_alias_normalized_key(monkeypatch):
    """The refresh route must evict under the same normalized provider key
    /api/models/live uses ('z.ai' -> 'zai')."""
    import api.config as config
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda name: {"z.ai": "zai"}.get(name, name))

    parsed = urlparse("/api/models/live?provider=z.ai")
    first = routes._handle_live_models(object(), parsed)
    assert first["models"][0]["id"] == "zai/model-1"  # primed under 'zai'

    _refresh("z.ai")

    second = routes._handle_live_models(object(), parsed)
    assert second["models"][0]["id"] == "zai/model-2", (
        "#6919: refresh with an alias did not evict the normalized cache key"
    )
    assert calls == ["zai", "zai"]


def _routes_function_source(src: str, signature: str) -> str:
    """Return the exact source of a function in api/routes.py via ast.

    Brace counting cannot delimit Python functions — Python has no braces, so
    the scanner runs past the end of the ``def`` into the next dict literal or
    JS template string and the assertions inspect unrelated code. ast gives the
    real node range, including nested defs.
    """
    name = signature.strip()
    if name.startswith("def "):
        name = name[4:]
    name = name.split("(")[0].strip()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(src, node)
            assert segment, f"could not extract source for {name!r}"
            return segment
    raise AssertionError(f"function {name!r} not found in api/routes.py")


def test_routes_function_extraction_stops_at_the_def_boundary():
    """Greptile follow-up: brace counting ran past the end of a Python def and
    pulled in unrelated functions (plus _ROUTE_SESSION_LIST_CACHE_DYNAMIC_EXPORTS),
    so the shape assertions could pass or fail on code they never claimed to
    check. The ast slice must stop at the real function boundary."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    publish = _routes_function_source(src, "def _publish_live_models(")
    assert publish.rstrip().endswith("return True"), (
        f"_publish_live_models extraction ran past its def:\n{publish[-300:]}"
    )
    for leaked in ("_ROUTE_SESSION_LIST_CACHE_DYNAMIC_EXPORTS", "def _clear_live_models_cache"):
        assert leaked not in publish, (
            f"_publish_live_models extraction leaked {leaked!r} — it inspected "
            "code outside the function it claims to check"
        )
    finish = _routes_function_source(src, "def _finish(payload: dict):")
    assert "_publish_live_models(" in finish, (
        "_finish extraction must cover the real call site"
    )
    assert finish.count("\n") < 20, (
        f"_finish extraction ran past its def:\n{finish}"
    )


def test_live_models_finish_fences_stale_generation():
    """#6919: the expected-generation comparison and the cache publication
    must be ONE critical section, and the refresh's eviction plus
    generation advance must be another. Separate acquisitions are what let
    a refresh land between a worker's check and its write (or between the
    refresh's pop and its bump), resurrecting pre-refresh bytes."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert "def _live_models_generation" in src
    assert "def _bump_live_models_generation" in src

    finish = _routes_function_source(src, "def _finish(payload: dict):")
    assert "_publish_live_models(" in finish, (
        "#6919: _finish must publish through the atomic helper so the "
        "generation comparison and the cache write share one lock hold"
    )

    publish = _routes_function_source(src, "def _publish_live_models(")
    lock_at = publish.find("with _LIVE_MODELS_CACHE_LOCK:")
    assert lock_at != -1, (
        "#6919: _publish_live_models must hold _LIVE_MODELS_CACHE_LOCK"
    )
    locked = publish[lock_at:]
    assert "_live_models_generation()" in locked, (
        "#6919: the expected-generation comparison must run inside the lock"
    )
    assert "_set_cached_live_models(" in locked, (
        "#6919: the cache publication must run inside the same lock hold as "
        "the generation comparison"
    )
    for io_marker in ("urlopen", "provider_model_ids", "j(handler"):
        assert io_marker not in publish, (
            f"#6919: {io_marker} must stay outside the cache critical section"
        )

    route = src.split('if parsed.path == "/api/models/refresh":')[1][:900]
    assert "_invalidate_live_models_cache_and_bump(" in route, (
        "#6919: the refresh route must evict and bump in one critical section"
    )
    fused = _routes_function_source(src, "def _invalidate_live_models_cache_and_bump(")
    lock_at = fused.find("with _LIVE_MODELS_CACHE_LOCK:")
    assert lock_at != -1, (
        "#6919: the refresh must hold _LIVE_MODELS_CACHE_LOCK across eviction "
        "and the generation advance"
    )
    locked = fused[lock_at:]
    assert "_invalidate_live_models_cache(" in locked, (
        "#6919: the live-models eviction must run inside the refresh lock hold"
    )
    assert "_bump_live_models_generation()" in locked, (
        "#6919: the generation advance must run inside the same lock hold as "
        "the eviction"
    )


# ── #6919 review: deterministic barrier tests for both intervals ────────────

def test_refresh_cannot_land_between_generation_check_and_cache_publication(monkeypatch):
    """Barrier for the check→publish interval.

    A real ``_handle_live_models`` worker is parked at its generation
    check; the real ``POST /api/models/refresh`` route then runs on another
    thread; the worker is resumed. If the check and the cache publication
    are separate critical sections, the worker repopulates the just-evicted
    entry with pre-refresh bytes and the next live request serves them from
    cache without a provider lookup.
    """
    import api.routes as routes

    calls = []
    worker_error = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")

    real_generation = routes._live_models_generation
    check_entered = threading.Event()
    release_worker = threading.Event()
    generation_reads = [0]

    def _parked_generation():
        value = real_generation()
        generation_reads[0] += 1
        if generation_reads[0] == 2:
            # The worker has read the generation but has not published yet.
            check_entered.set()
            assert release_worker.wait(10), "parked worker was never released"
        return value

    monkeypatch.setattr(routes, "_live_models_generation", _parked_generation)

    def _run_worker():
        try:
            routes._handle_live_models(object(), parsed)
        except Exception as exc:  # surfaced by the assert below
            worker_error.append(exc)

    worker = threading.Thread(target=_run_worker)
    worker.start()
    assert check_entered.wait(10), "worker never reached its generation check"

    refresh_done = threading.Event()

    def _run_refresh():
        _refresh("openai")
        refresh_done.set()

    refresher = threading.Thread(target=_run_refresh)
    refresher.start()
    # Atomic code: the worker holds the cache lock across its check, so the
    # refresh cannot enter its critical section until the worker publishes.
    # Non-atomic code: the refresh completes while the worker is parked
    # between its two separate acquisitions.
    refresh_done.wait(1.0)
    release_worker.set()
    worker.join(10)
    refresher.join(10)
    assert not worker_error, f"worker raised: {worker_error}"
    assert not worker.is_alive() and not refresher.is_alive()

    after = routes._handle_live_models(object(), parsed)
    assert calls == ["openai", "openai"], (
        "#6919: a refresh that landed between the worker's generation check "
        "and its cache publication left stale bytes in the cache — the next "
        "live request served them instead of refetching"
    )
    assert after["models"][0]["id"] == "openai/model-2"


def test_refresh_eviction_and_generation_bump_are_one_critical_section(monkeypatch):
    """Barrier for the refresh's pop→bump interval.

    A real worker is parked inside its provider lookup (its generation was
    already captured) and the real refresh route is parked after its cache
    eviction but before its generation bump. The worker is then released to
    publish. If eviction and bump are separate critical sections, the worker
    publishes into the gap and the stale entry survives the refresh.
    """
    import api.routes as routes

    calls = []
    worker_error = []
    lookup_entered = threading.Event()
    provider_gate = threading.Event()

    def _parked_provider_model_ids(provider):
        lookup_entered.set()
        assert provider_gate.wait(10), "parked provider lookup was never released"
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, _parked_provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")

    real_bump = routes._bump_live_models_generation
    bump_entered = threading.Event()
    release_bump = threading.Event()

    def _parked_bump():
        # The eviction has run; the generation has not advanced yet.
        bump_entered.set()
        assert release_bump.wait(10), "parked refresh was never released"
        return real_bump()

    monkeypatch.setattr(routes, "_bump_live_models_generation", _parked_bump)

    def _run_worker():
        try:
            routes._handle_live_models(object(), parsed)
        except Exception as exc:  # surfaced by the assert below
            worker_error.append(exc)

    worker = threading.Thread(target=_run_worker)
    worker.start()
    assert lookup_entered.wait(10), "worker never reached its provider lookup"

    refresher = threading.Thread(target=lambda: _refresh("openai"))
    refresher.start()
    assert bump_entered.wait(10), "refresh never reached the generation bump"

    # Release the in-flight worker: it must not be able to publish inside
    # the eviction→bump interval.
    provider_gate.set()
    worker.join(1.0)  # atomic code: blocks on the lock the refresh holds
    release_bump.set()
    refresher.join(10)
    worker.join(10)
    assert not worker_error, f"worker raised: {worker_error}"
    assert not worker.is_alive() and not refresher.is_alive()

    after = routes._handle_live_models(object(), parsed)
    assert calls == ["openai", "openai"], (
        "#6919: the worker published between the refresh's eviction and its "
        "generation bump — the stale entry survived the refresh"
    )
    assert after["models"][0]["id"] == "openai/model-2"


# ---------------------------------------------------------------------------
# #6919 (empty-membership remainder): the endpoint must distinguish an
# authoritative EMPTY membership ("this provider really has no models") from a
# discovery FAILURE ("we could not find out"). Both arrive as an empty list,
# but only the first may reconcile the client's picker to an empty set.
#
# The failure shape is the endpoint's existing one — an ``error`` marker —
# so a client can tell the two apart without a new field.
# ---------------------------------------------------------------------------


def test_live_models_empty_discovery_failure_is_marked_as_a_failure(monkeypatch):
    """No discovery step completed: the empty list means "unknown", and it
    must carry the endpoint's failure marker so the client does not prune
    live choices."""
    import api.routes as routes
    import api.config as config

    _install_provider_model_ids(monkeypatch, lambda provider: [])
    _patch_live_models_basics(monkeypatch, routes)
    # No static catalog for this provider, so the empty list is reached.
    monkeypatch.setattr(config, "_PROVIDER_MODELS", {})

    parsed = urlparse("/api/models/live?provider=openai")
    payload = routes._handle_live_models(object(), parsed)

    assert payload["models"] == [], (
        f"#6919: expected an empty model list, got {payload['models']}"
    )
    assert payload.get("error"), (
        "#6919: a discovery failure must publish the endpoint's failure marker "
        f"so the client can tell it apart from authoritative empty membership: {payload}"
    )


def test_live_models_completed_discovery_with_no_models_is_authoritative(monkeypatch):
    """A live endpoint that ANSWERED with an empty list is authoritative: the
    provider really offers no models. It must NOT carry the failure marker.

    Uses the custom-provider probe path: it performs its own /v1/models
    request, so an empty (but successful) answer is observable here.
    """
    import api.routes as routes
    import api.config as config
    import urllib.request

    _install_provider_model_ids(monkeypatch, lambda provider: [])
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(config, "_PROVIDER_MODELS", {})
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {"provider": "custom:test-gateway"},
            "custom_providers": [
                {
                    "name": "test-gateway",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "test-key",
                }
            ],
        },
    )

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    def _fake_urlopen(req, timeout=None):
        # The endpoint answered, with no models at all.
        return _Resp(json.dumps({"data": []}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    parsed = urlparse("/api/models/live?provider=custom:test-gateway")
    payload = routes._handle_live_models(object(), parsed)

    assert payload["models"] == [], (
        f"#6919: expected an empty model list, got {payload['models']}"
    )
    assert not payload.get("error"), (
        "#6919: a completed discovery that found no models is AUTHORITATIVE "
        "membership — it must not carry the failure marker: " f"{payload}"
    )


# ---------------------------------------------------------------------------
# #6919 (discovery-authority remainder): an HTTP 200 is not a model list.
# The upstream probe must be SHAPE-CHECKED before the discovery counts as
# successful, otherwise a wrong-shaped body is published as an authoritative
# EMPTY membership and the client prunes live choices on what is really an
# upstream failure.
#
# The supported model-list shapes are {"data": [...]} and a bare [...]. Every
# other 200 body is a failed discovery.
# ---------------------------------------------------------------------------


def _probe_custom_provider_with_body(monkeypatch, routes, body_bytes):
    """Drive the real custom-provider probe with a canned upstream body."""
    import api.config as config
    import urllib.request

    _install_provider_model_ids(monkeypatch, lambda provider: [])
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(config, "_PROVIDER_MODELS", {})
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {"provider": "custom:test-gateway"},
            "custom_providers": [
                {
                    "name": "test-gateway",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "test-key",
                }
            ],
        },
    )

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    def _fake_urlopen(req, timeout=None):
        return _Resp(body_bytes)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    parsed = urlparse("/api/models/live?provider=custom:test-gateway")
    return routes._handle_live_models(object(), parsed)


@pytest.mark.parametrize(
    "label,body",
    [
        ("error-object", json.dumps({"error": "temporarily unavailable"}).encode()),
        ("empty-object", json.dumps({}).encode()),
        ("data-null", json.dumps({"data": None}).encode()),
        ("data-wrong-type", json.dumps({"data": "unavailable"}).encode()),
        ("json-string", json.dumps("unavailable").encode()),
    ],
)
def test_live_models_wrong_shaped_discovery_is_not_authoritative(monkeypatch, label, body):
    """A 200 whose body is not a model list must NOT count as a successful
    discovery: the envelope has to carry the failure marker, otherwise the
    client reads it as authoritative emptiness and prunes live choices."""
    import api.routes as routes

    payload = _probe_custom_provider_with_body(monkeypatch, routes, body)

    assert payload["models"] == [], (
        f"#6919: expected an empty model list for a {label} body, "
        f"got {payload['models']}"
    )
    assert payload.get("error"), (
        f"#6919: a {label} upstream body is a failed discovery — the envelope "
        "must carry the failure marker so the client does not treat it as "
        f"authoritative empty membership: {payload}"
    )


def test_live_models_valid_empty_model_list_is_still_authoritative(monkeypatch):
    """The supported empty shape stays authoritative: {"data": []} means
    "this provider really has no models" and must NOT carry the marker.

    This is the control for the wrong-shape cases above — the repair must not
    turn a genuine empty membership into a failure."""
    import api.routes as routes

    payload = _probe_custom_provider_with_body(
        monkeypatch, routes, json.dumps({"data": []}).encode()
    )

    assert payload["models"] == [], (
        f"#6919: expected an empty model list, got {payload['models']}"
    )
    assert not payload.get("error"), (
        "#6919: a supported valid-empty model list is AUTHORITATIVE "
        f"membership — it must not carry the failure marker: {payload}"
    )


def test_live_models_bare_list_model_list_is_authoritative(monkeypatch):
    """The other supported shape — a bare [...] — is authoritative too."""
    import api.routes as routes

    payload = _probe_custom_provider_with_body(
        monkeypatch, routes, json.dumps([]).encode()
    )

    assert payload["models"] == [], (
        f"#6919: expected an empty model list, got {payload['models']}"
    )
    assert not payload.get("error"), (
        "#6919: a bare-list model list is AUTHORITATIVE membership — it must "
        f"not carry the failure marker: {payload}"
    )


def test_live_models_non_empty_model_list_is_authoritative(monkeypatch):
    """A normal, non-empty catalog is unaffected by the shape check."""
    import api.routes as routes

    payload = _probe_custom_provider_with_body(
        monkeypatch,
        routes,
        json.dumps({"data": [{"id": "chat-a", "label": "Chat A"}]}).encode(),
    )

    assert [m["id"] for m in payload["models"]] == ["chat-a"], (
        f"#6919: the model list must be published unchanged, got {payload}"
    )
    assert not payload.get("error"), (
        f"#6919: a successful discovery must not carry the failure marker: {payload}"
    )
